"""Admin console endpoints that are not owned by one domain module: overview, verification
queue, users, audit log. (Listing review, disputes, finance, reports, announcements and the
SOS board live in their own modules under `/admin/...`.)

Access: `require_admin` = Super admin or Support; `require_super_admin` = Super admin only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.audit import AuditCategory
from app.core.db import get_db
from app.core.errors import error_responses
from app.core.permissions import Capability, Role
from app.core.ratelimit import client_ip
from app.modules.admin import service
from app.modules.admin.schemas import AnalyticsOut, AuditPage, OverviewOut
from app.modules.identity import service as identity
from app.modules.identity.deps import require_admin, require_capability, require_super_admin
from app.modules.identity.models import AccountStatus, KycSubmissionStatus, User
from app.modules.identity.schemas import (
    KycDecisionIn,
    KycOut,
    KycQueueItem,
    SuspendIn,
    UserListItem,
    UserOut,
)

router = APIRouter(tags=["Admin"])


# --- Overview and analytics --------------------------------------------------------------
@router.get(
    "/admin/overview",
    response_model=OverviewOut,
    summary="Dashboard numbers: GMV, commission, escrow, queues, recent actions",
    dependencies=[Depends(require_admin)],
)
def overview(days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    return service.overview(db, days=days)


@router.get(
    "/me/analytics",
    response_model=AnalyticsOut,
    summary="My sales, earnings and rating (business accounts)",
)
def my_analytics(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(require_capability(Capability.VIEW_OWN_ANALYTICS)),
    db: Session = Depends(get_db),
):
    return service.my_analytics(db, user, days=days)


# --- Verification (KYC) ------------------------------------------------------------------
@router.get(
    "/admin/kyc",
    summary="Verification queue",
    description="Pending first, oldest first. Returns the rows plus the counts for the tabs.",
    dependencies=[Depends(require_admin)],
)
def kyc_queue(
    status: KycSubmissionStatus | None = KycSubmissionStatus.PENDING,
    role: Role | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    rows, total = identity.list_kyc(db, status=status, role=role, page=page, page_size=page_size)
    items = [
        KycQueueItem(
            id=s.id, ref=s.ref, user_id=s.user_id, provider_name=s.user.display_name,
            contact_name=s.user.full_name if s.user.business_name else None,
            roles=s.user.active_roles, city=s.user.city, id_check_status=s.id_check_status,
            id_check_reason=s.id_check_reason, status=s.status, submitted_at=s.submitted_at,
        )
        for s in rows
    ]  # fmt: skip
    return {"items": items, "total": total, "counts": identity.kyc_counts(db)}


@router.get(
    "/admin/kyc/{submission_id}",
    response_model=KycOut,
    summary="One submission with short-lived links to its documents",
    responses=error_responses(404),
    dependencies=[Depends(require_admin)],
)
def kyc_detail(submission_id: uuid.UUID, db: Session = Depends(get_db)) -> KycOut:
    return identity.kyc_to_out(identity.get_kyc(db, submission_id), with_urls=True)


@router.post(
    "/admin/kyc/{submission_id}/decision",
    response_model=KycOut,
    summary="Approve or reject a verification",
    responses=error_responses(404, 422),
)
def kyc_decision(
    submission_id: uuid.UUID,
    body: KycDecisionIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> KycOut:
    submission = identity.decide_kyc(
        db,
        submission_id,
        approve=body.approve,
        reason=body.reason,
        admin=admin,
        ip=client_ip(request),
    )
    return identity.kyc_to_out(submission, with_urls=True)


# --- Users -------------------------------------------------------------------------------
@router.get(
    "/admin/users",
    summary="Users table",
    description="`account` is one of: active, suspended, verification_pending, verification_rejected.",
    dependencies=[Depends(require_admin)],
)
def users(
    q: str | None = Query(None, description="Name, business, e-mail or phone."),
    role: Role | None = None,
    account: str | None = Query(
        None, pattern="^(active|suspended|verification_pending|verification_rejected)$"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    rows, total = identity.search_users(
        db, q=q, role=role, account=account, page=page, page_size=page_size
    )
    return {
        "items": [UserListItem.model_validate(u) for u in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get(
    "/admin/users/{user_id}",
    response_model=UserOut,
    summary="One user",
    responses=error_responses(404),
    dependencies=[Depends(require_admin)],
)
def user_detail(user_id: uuid.UUID, db: Session = Depends(get_db)):
    return identity.get_user(db, user_id)


@router.post(
    "/admin/users/{user_id}/suspend",
    response_model=UserOut,
    summary="Suspend an account (signs the person out everywhere)",
    responses=error_responses(403, 404),
)
def suspend(
    user_id: uuid.UUID, body: SuspendIn, request: Request,
    admin: User = Depends(require_admin), db: Session = Depends(get_db),
):  # fmt: skip
    return identity.set_account_status(
        db, user_id, AccountStatus.SUSPENDED, admin=admin, reason=body.reason, ip=client_ip(request)
    )


@router.post(
    "/admin/users/{user_id}/reactivate",
    response_model=UserOut,
    summary="Reactivate a suspended account",
    responses=error_responses(403, 404),
)
def reactivate(
    user_id: uuid.UUID, body: SuspendIn, request: Request,
    admin: User = Depends(require_admin), db: Session = Depends(get_db),
):  # fmt: skip
    return identity.set_account_status(
        db, user_id, AccountStatus.ACTIVE, admin=admin, reason=body.reason, ip=client_ip(request)
    )


@router.post(
    "/admin/users/{user_id}/staff-role/{role}",
    response_model=UserOut,
    summary="Make someone Support or Super admin (Super admin only)",
    description="Staff accounts must use two-factor sign-in, so it is switched on.",
    responses=error_responses(400, 403, 404, 409),
)
def grant_staff(
    user_id: uuid.UUID, role: Role, request: Request,
    admin: User = Depends(require_super_admin), db: Session = Depends(get_db),
):  # fmt: skip
    return identity.grant_staff_role(db, user_id, role, admin=admin, ip=client_ip(request))


# --- Audit log ---------------------------------------------------------------------------
@router.get(
    "/admin/audit",
    response_model=AuditPage,
    summary="Audit log",
    dependencies=[Depends(require_admin)],
)
def audit_log(
    category: AuditCategory | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AuditPage:
    rows, total = service.search_audit(
        db,
        category=category,
        q=q,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    return AuditPage(items=rows, total=total, page=page, page_size=page_size)


@router.get(
    "/admin/audit/export",
    summary="Download the filtered audit log as CSV (Super admin only; the export is logged)",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
def audit_export(
    request: Request,
    category: AuditCategory | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
) -> Response:
    content = service.export_audit_csv(
        db,
        admin,
        category=category,
        q=q,
        date_from=date_from,
        date_to=date_to,
        ip=client_ip(request),
    )
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )
