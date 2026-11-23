"""Insurance endpoints: applications, offers, policies, certificates and claims."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import error_responses
from app.core.permissions import Capability
from app.modules.identity.deps import CurrentUser, require_capability
from app.modules.identity.models import User
from app.modules.insurance import service
from app.modules.insurance.models import Application, Claim
from app.modules.insurance.schemas import (
    AnswerIn,
    ApplicationOut,
    ApplicationPage,
    ApplyIn,
    ClaimDecisionIn,
    ClaimDocumentOut,
    ClaimIn,
    ClaimOut,
    ClaimPage,
    OfferIn,
    PolicyOut,
    QuoteComparisonOut,
    QuoteRequestIn,
    ReasonIn,
)
from app.modules.payments.models import Transaction

router = APIRouter(tags=["Insurance"])

Customer = Depends(require_capability(Capability.PURCHASE))
Insurer = Depends(require_capability(Capability.OFFER_INSURANCE))


def _app_out(a: Application, tx: Transaction) -> ApplicationOut:
    return ApplicationOut(
        id=a.id, transaction_id=tx.id, ref=tx.ref, status=tx.status, escrow_status=tx.escrow_status,
        payment_due=tx.payment_due, customer_id=a.customer_id, insurer_id=a.insurer_id,
        product_title=a.product_title, cover_type=a.cover_type, vehicle_summary=a.vehicle_summary,
        details=a.details, premium_minor=a.premium_minor, terms=a.terms,
        offer_valid_until=a.offer_valid_until, decline_reason=a.decline_reason,
        is_renewal=a.renewal_of_id is not None, quote_request_id=a.quote_request_id,
        created_at=a.created_at,
    )  # fmt: skip


def _claim_out(db: Session, claim: Claim) -> ClaimOut:
    out = ClaimOut.model_validate(claim, from_attributes=True)
    out.ref = claim.ref
    out.documents = [
        ClaimDocumentOut(id=d.id, filename=d.filename, content_type=d.content_type, url=url)
        for d, url in service.claim_documents(db, claim)
    ]
    return out


# --- Applications ------------------------------------------------------------------------
@router.post(
    "/insurance/applications",
    response_model=ApplicationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Apply for an insurance product for one of my vehicles",
    responses=error_responses(404),
)
def apply(body: ApplyIn, user: User = Customer, db: Session = Depends(get_db)):
    application = service.apply(db, user, **body.model_dump())
    return _app_out(*service.get_application(db, application.id, user))


@router.get(
    "/insurance/applications",
    response_model=ApplicationPage,
    summary="My applications (or, with as_insurer, the ones sent to me)",
)
def list_applications(
    user: CurrentUser,
    as_insurer: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ApplicationPage:
    rows, total = service.list_applications(
        db, user, as_insurer=as_insurer, page=page, page_size=page_size
    )
    return ApplicationPage(items=[_app_out(a, t) for a, t in rows], total=total)


@router.get(
    "/insurance/applications/{application_id}",
    response_model=ApplicationOut,
    summary="One application",
    responses=error_responses(404),
)
def get_application(application_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    return _app_out(*service.get_application(db, application_id, user))


@router.put(
    "/insurance/applications/{application_id}/offer",
    response_model=ApplicationOut,
    summary="Insurer: send or revise an offer (premium and terms)",
    responses=error_responses(400, 404, 422),
)
def make_offer(
    application_id: uuid.UUID, body: OfferIn, user: User = Insurer, db: Session = Depends(get_db)
):
    service.make_offer(
        db,
        user,
        application_id,
        premium_minor=body.premium_minor,
        terms=body.terms,
        valid_days=body.valid_days,
    )
    return _app_out(*service.get_application(db, application_id, user))


@router.post(
    "/insurance/applications/{application_id}/decline",
    response_model=ApplicationOut,
    summary="Insurer: decline an application",
    responses=error_responses(404, 422),
)
def decline(
    application_id: uuid.UUID, body: ReasonIn, user: User = Insurer, db: Session = Depends(get_db)
):
    service.decline_application(db, user, application_id, body.reason)
    return _app_out(*service.get_application(db, application_id, user))


@router.post(
    "/insurance/applications/{application_id}/answer",
    response_model=ApplicationOut,
    summary="Customer: accept or decline the offer (accepting asks you to pay)",
    responses=error_responses(404, 422),
)
def answer(
    application_id: uuid.UUID, body: AnswerIn, user: User = Customer, db: Session = Depends(get_db)
):
    service.answer_offer(db, user, application_id, body.accept)
    return _app_out(*service.get_application(db, application_id, user))


@router.post(
    "/insurance/applications/{application_id}/cancel",
    response_model=ApplicationOut,
    summary="Customer: withdraw an application before paying",
    responses=error_responses(404, 422),
)
def cancel(application_id: uuid.UUID, user: User = Customer, db: Session = Depends(get_db)):
    service.cancel_application(db, user, application_id)
    return _app_out(*service.get_application(db, application_id, user))


# --- Policies ----------------------------------------------------------------------------
@router.get(
    "/me/policies",
    response_model=list[PolicyOut],
    summary="My policies (or issued by me, as_insurer)",
)
def my_policies(user: CurrentUser, as_insurer: bool = False, db: Session = Depends(get_db)):
    return service.list_policies(db, user, as_insurer=as_insurer)


@router.get(
    "/insurance/policies/{policy_id}",
    response_model=PolicyOut,
    summary="One policy",
    responses=error_responses(404),
)
def get_policy(policy_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    return service.get_policy(db, policy_id, user)


@router.get(
    "/insurance/policies/{policy_id}/certificate",
    response_class=HTMLResponse,
    summary="Printable certificate of insurance (open it and Print / Save as PDF)",
    responses=error_responses(404),
)
def certificate(
    policy_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)
) -> HTMLResponse:
    return HTMLResponse(service.certificate_html(db, service.get_policy(db, policy_id, user)))


@router.post(
    "/insurance/policies/{policy_id}/renew",
    response_model=ApplicationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a renewal (a new application for the same product and vehicle)",
    responses=error_responses(404, 422),
)
def renew(policy_id: uuid.UUID, user: User = Customer, db: Session = Depends(get_db)):
    application = service.renew(db, user, policy_id)
    return _app_out(*service.get_application(db, application.id, user))


# --- Claims ------------------------------------------------------------------------------
@router.post(
    "/insurance/policies/{policy_id}/claims",
    response_model=ClaimOut,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a claim on an active policy",
    responses=error_responses(404, 422),
)
def submit_claim(
    policy_id: uuid.UUID, body: ClaimIn, user: CurrentUser, db: Session = Depends(get_db)
):
    claim = service.submit_claim(
        db, user, policy_id, incident_date=body.incident_date,
        description=body.description, amount_minor=body.amount_claimed_minor,
    )  # fmt: skip
    return _claim_out(db, claim)


@router.get(
    "/insurance/claims",
    response_model=ClaimPage,
    summary="My claims (or, with as_insurer, the ones against my policies)",
)
def list_claims(
    user: CurrentUser,
    as_insurer: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ClaimPage:
    rows, total = service.list_claims(
        db, user, as_insurer=as_insurer, page=page, page_size=page_size
    )
    return ClaimPage(items=[_claim_out(db, c) for c in rows], total=total)


@router.get(
    "/insurance/claims/{claim_id}",
    response_model=ClaimOut,
    summary="One claim",
    responses=error_responses(404),
)
def get_claim(claim_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    return _claim_out(db, service.get_claim(db, claim_id, user))


@router.post(
    "/insurance/claims/{claim_id}/documents",
    response_model=ClaimDocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a photo or PDF to my claim (up to 8 MB)",
    responses=error_responses(400, 404),
)
async def upload_claim_document(
    claim_id: uuid.UUID,
    user: CurrentUser,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> ClaimDocumentOut:
    doc = service.add_claim_document(
        db, user, claim_id, file.filename or "document", file.content_type or "", await file.read()
    )
    url = dict(
        (d.id, u) for d, u in service.claim_documents(db, service.get_claim(db, claim_id, user))
    )[doc.id]
    return ClaimDocumentOut(
        id=doc.id, filename=doc.filename, content_type=doc.content_type, url=url
    )


@router.post(
    "/insurance/claims/{claim_id}/decision",
    response_model=ClaimOut,
    summary="Insurer: move a claim to under_review, approved or rejected",
    responses=error_responses(400, 404, 422),
)
def decide_claim(
    claim_id: uuid.UUID, body: ClaimDecisionIn, user: User = Insurer, db: Session = Depends(get_db)
):
    claim = service.decide_claim(
        db, user, claim_id, status=body.status,
        approved_amount_minor=body.approved_amount_minor, note=body.note,
    )  # fmt: skip
    return _claim_out(db, claim)


# --- Comparing insurers (FR-7.2) ---------------------------------------------------------
@router.post(
    "/insurance/quote-requests",
    response_model=QuoteComparisonOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ask several insurers for a quote at once",
    description="Sends the vehicle and details to every insurer with a live product of "
    "`cover_type` (or to `product_ids`), up to 10. Each insurer answers as usual; compare "
    "with `GET /insurance/quote-requests/{id}`. Accepting one offer withdraws the others.",
    responses=error_responses(400, 404),
)
def request_quotes(body: QuoteRequestIn, user: User = Customer, db: Session = Depends(get_db)):
    group, applications = service.request_quotes(db, user, **body.model_dump())
    return QuoteComparisonOut(
        id=group,
        items=[_app_out(a, t) for a, t in service.quote_comparison(db, user, group)],
    )


@router.get(
    "/insurance/quote-requests/{quote_request_id}",
    response_model=QuoteComparisonOut,
    summary="Compare the insurers' answers",
    responses=error_responses(404),
)
def compare_quotes(quote_request_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    rows = service.quote_comparison(db, user, quote_request_id)
    return QuoteComparisonOut(id=quote_request_id, items=[_app_out(a, t) for a, t in rows])
