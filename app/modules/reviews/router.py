"""Reviews and report endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import error_responses
from app.core.permissions import Capability
from app.core.ratelimit import client_ip
from app.modules.identity.deps import CurrentUser, require_admin, require_capability
from app.modules.identity.models import User
from app.modules.reviews import service
from app.modules.reviews.models import ReportStatus
from app.modules.reviews.schemas import (
    ReplyIn,
    ReportIn,
    ReportOut,
    ReportPage,
    ResolveReportIn,
    ReviewIn,
    ReviewOut,
    ReviewPage,
)

router = APIRouter(tags=["Reviews"])


def _review_out(r, names: dict) -> ReviewOut:
    out = ReviewOut.model_validate(r)
    out.author_name = names.get(r.author_id, "")
    return out


def _report_out(r) -> ReportOut:
    return ReportOut.model_validate(r)


@router.post(
    "/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="Review a provider after a completed order, booking or job",
    responses=error_responses(403, 404, 409, 422),
)
def create_review(
    body: ReviewIn,
    user: User = Depends(require_capability(Capability.REVIEW)),
    db: Session = Depends(get_db),
):
    review = service.create_review(db, user, body.transaction_ref, body.rating, body.comment)
    return _review_out(review, {user.id: user.display_name})


@router.get(
    "/users/{user_id}/reviews",
    response_model=ReviewPage,
    summary="Reviews and average rating of a provider (public)",
)
def provider_reviews(
    user_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ReviewPage:
    rows, total = service.list_reviews(db, user_id, page=page, page_size=page_size)
    names = service.author_names(db, rows)
    average, count = service.ratings_for(db, [user_id]).get(user_id, (0.0, 0))
    return ReviewPage(
        items=[_review_out(r, names) for r in rows], total=total, average=average, count=count
    )


@router.post(
    "/reviews/{review_id}/reply",
    response_model=ReviewOut,
    summary="Reply publicly to a review about me",
    responses=error_responses(404, 409),
)
def reply(review_id: uuid.UUID, body: ReplyIn, user: CurrentUser, db: Session = Depends(get_db)):
    review = service.reply_to_review(db, user, review_id, body.reply)
    return _review_out(review, service.author_names(db, [review]))


@router.post(
    "/reports",
    response_model=ReportOut,
    status_code=status.HTTP_201_CREATED,
    summary="Report a listing, review or account",
    responses=error_responses(404, 409, 422),
)
def report(body: ReportIn, user: CurrentUser, db: Session = Depends(get_db)):
    return _report_out(
        service.create_report(db, user, body.target_type, body.target_id, body.reason, body.details)
    )


@router.get(
    "/admin/reports",
    response_model=ReportPage,
    summary="Abuse reports (admin)",
    dependencies=[Depends(require_admin)],
)
def admin_reports(
    status: ReportStatus | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ReportPage:
    rows, total = service.list_reports(db, status=status, page=page, page_size=page_size)
    return ReportPage(
        items=[_report_out(r) for r in rows], total=total, counts=service.report_counts(db)
    )


@router.post(
    "/admin/reports/{report_id}/resolve",
    response_model=ReportOut,
    summary="Dismiss a report, or act on the reported item",
    responses=error_responses(404, 422),
)
def admin_resolve(
    report_id: uuid.UUID,
    body: ResolveReportIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _report_out(
        service.resolve_report(
            db, report_id, action=body.action, admin=admin, ip=client_ip(request)
        )
    )
