"""Insurance business logic: applications, offers, policies, claims and the renewal job.

Application lifecycle (status on the transaction):

    awaiting_insurer --offer--> quote_sent --customer accepts--> offer_accepted --paid--> active
         |                          |
         +--decline--> declined     +--customer declines / offer expires--> declined

When the payment succeeds a policy is issued automatically and the dispute window starts
(the premium is released to the insurer, minus commission, after the usual hold).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import db as db_module
from app.core.db import utcnow
from app.core.errors import BadRequest, NotFound, UnprocessableEntity
from app.core.events import Event, publish, subscribe
from app.core.legal import insurance_disclosure
from app.core.pagination import paginate_page
from app.core.permissions import ADMIN_ROLES
from app.core.uploads import checked_type
from app.integrations import registry
from app.modules.catalog import service as catalog
from app.modules.catalog.models import Listing, ListingStatus, ListingType
from app.modules.identity.models import User
from app.modules.insurance.models import (
    Application,
    Claim,
    ClaimDocument,
    ClaimStatus,
    Policy,
    PolicyStatus,
)
from app.modules.payments import service as payments
from app.modules.payments.models import Transaction, TransactionKind, TxStatus

REMINDER_DAYS = 30
CLAIM_FILE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}
CLAIM_FILE_MAX = 8 * 1024 * 1024


# ======================================================================================
# Applications
# ======================================================================================
def apply(
    db: Session,
    customer: User,
    *,
    product_id: uuid.UUID,
    vehicle_id: uuid.UUID,
    details: str,
    renewal_of_id: uuid.UUID | None = None,
) -> Application:
    product = db.get(Listing, product_id)
    if (
        product is None
        or product.type != ListingType.INSURANCE_PRODUCT
        or product.status != ListingStatus.LIVE
    ):
        raise NotFound("This insurance product is not available", code="listing_unavailable")
    vehicle = catalog.get_vehicle(db, customer, vehicle_id)  # must be in the customer's garage
    application = _new_application(
        db, customer, product, vehicle, details, renewal_of_id=renewal_of_id
    )
    db.commit()
    _announce(db, application)
    return application


def _announce(db: Session, application: Application) -> None:
    """Tell the insurer (after the commit)."""
    publish(
        "insurance.applied",
        application_id=str(application.id),
        insurer_id=str(application.insurer_id),
        ref=payments.get_transaction(db, application.transaction_id).ref,
    )


def _new_application(
    db: Session,
    customer: User,
    product: Listing,
    vehicle,
    details: str,
    *,
    renewal_of_id: uuid.UUID | None = None,
    quote_request_id: uuid.UUID | None = None,
) -> Application:
    """Create the application and its transaction (flushed, not committed)."""
    tx = payments.create_transaction(
        db,
        kind=TransactionKind.POLICY_PURCHASE,
        customer_id=customer.id,
        provider_id=product.seller_id,
        amount_minor=0,  # set by the insurer's offer
        status=TxStatus.AWAITING_INSURER,
        source_id=product.id,
    )
    application = Application(
        transaction_id=tx.id,
        customer_id=customer.id,
        insurer_id=product.seller_id,
        product_id=product.id,
        product_title=product.title,
        cover_type=str(product.attributes.get("cover_type", "")),
        vehicle_id=vehicle.id,
        vehicle_summary=f"{vehicle.year} {vehicle.make} {vehicle.model}",
        vehicle_vin=vehicle.vin,
        details=details.strip(),
        term_months=int(product.attributes.get("term_months", 12)),
        renewal_of_id=renewal_of_id,
        quote_request_id=quote_request_id,
    )
    db.add(application)
    db.flush()
    return application


MAX_QUOTES = 10


def request_quotes(
    db: Session,
    customer: User,
    *,
    vehicle_id: uuid.UUID,
    details: str,
    cover_type: str | None = None,
    product_ids: list[uuid.UUID] | None = None,
) -> tuple[uuid.UUID, list[Application]]:
    """Send one vehicle and its details to several insurers at once (FR-7.2).

    Picks the given products, or every live product with `cover_type` (up to 10). Each
    becomes a normal application, linked by a shared `quote_request_id`, so insurers answer
    as usual and the customer compares the offers side by side.
    """
    if not cover_type and not product_ids:
        raise BadRequest("Choose a cover type or the products to ask", code="cover_required")
    vehicle = catalog.get_vehicle(db, customer, vehicle_id)
    stmt = select(Listing).where(
        Listing.type == ListingType.INSURANCE_PRODUCT,
        Listing.status == ListingStatus.LIVE,
        Listing.seller_id != customer.id,
    )
    if product_ids:
        stmt = stmt.where(Listing.id.in_(product_ids))
    if cover_type:
        stmt = stmt.where(Listing.attributes["cover_type"].astext.ilike(cover_type.strip()))
    products = list(db.scalars(stmt.order_by(Listing.created_at, Listing.id).limit(MAX_QUOTES)))
    if not products:
        raise NotFound("No insurer offers that cover yet", code="no_products")
    group = uuid.uuid4()
    applications = [
        _new_application(db, customer, p, vehicle, details, quote_request_id=group)
        for p in products
    ]
    db.commit()
    for application in applications:
        _announce(db, application)
    return group, applications


# Order in the comparison: offers to look at first (cheapest first), then the rest.
_RANK = {
    TxStatus.OFFER_ACCEPTED: 0,
    TxStatus.ACTIVE: 0,
    TxStatus.QUOTE_SENT: 1,
    TxStatus.AWAITING_INSURER: 2,
}


def quote_comparison(
    db: Session, customer: User, quote_request_id: uuid.UUID
) -> list[tuple[Application, Transaction]]:
    """The applications of one quote request, best first: an accepted offer, then offers
    by premium (cheapest first), then those still waiting, then declined or withdrawn."""
    rows = [
        (a, payments.get_transaction(db, a.transaction_id))
        for a in db.scalars(
            select(Application).where(
                Application.quote_request_id == quote_request_id,
                Application.customer_id == customer.id,
            )
        )
    ]
    if not rows:
        raise NotFound("Quote request not found", code="quote_request_not_found")
    return sorted(
        rows,
        key=lambda r: (_RANK.get(r[1].status, 3), r[0].premium_minor or 0, r[0].created_at),
    )


def get_application(
    db: Session, application_id: uuid.UUID, viewer: User
) -> tuple[Application, Transaction]:
    app = db.get(Application, application_id)
    if app is None or not (
        viewer.id in (app.customer_id, app.insurer_id) or viewer.has_role(*ADMIN_ROLES)
    ):
        raise NotFound("Application not found", code="application_not_found")
    return app, payments.get_transaction(db, app.transaction_id)


def _locked(
    db: Session,
    application_id: uuid.UUID,
    *,
    insurer: User | None = None,
    customer: User | None = None,
):
    app = db.get(Application, application_id)
    if (
        app is None
        or (insurer and app.insurer_id != insurer.id)
        or (customer and app.customer_id != customer.id)
    ):
        raise NotFound("Application not found", code="application_not_found")
    return app, payments.get_transaction(db, app.transaction_id, lock=True)


def _need(tx: Transaction, *statuses: TxStatus) -> None:
    if tx.status not in statuses:
        raise UnprocessableEntity(
            f"This application is {tx.status.value.replace('_', ' ')}, so that is not possible now",
            code="invalid_status",
        )


def list_applications(
    db: Session, user: User, *, as_insurer: bool, page: int, page_size: int
) -> tuple[list[tuple[Application, Transaction]], int]:
    column = Application.insurer_id if as_insurer else Application.customer_id
    stmt = (
        select(Application, Transaction)
        .join(Transaction, Transaction.id == Application.transaction_id)
        .where(column == user.id)
        .order_by(Application.created_at.desc())
    )
    # Rows are (Application, Transaction) pairs, so `paginate_page` (which returns one entity per
    # row) is not used here.
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.execute(stmt.limit(page_size).offset((max(page, 1) - 1) * page_size)).all()
    return [(a, t) for a, t in rows], total


def make_offer(
    db: Session,
    insurer: User,
    application_id: uuid.UUID,
    *,
    premium_minor: int,
    terms: str,
    valid_days: int,
) -> Application:
    """The insurer prices the cover. The customer then accepts or declines."""
    app, tx = _locked(db, application_id, insurer=insurer)
    _need(tx, TxStatus.AWAITING_INSURER, TxStatus.QUOTE_SENT)  # an offer can be revised
    if premium_minor <= 0:
        raise BadRequest("The premium must be more than zero", code="invalid_premium")
    app.premium_minor, app.terms = premium_minor, terms
    app.offer_valid_until = utcnow() + timedelta(days=valid_days)
    tx.amount_minor, tx.status = premium_minor, TxStatus.QUOTE_SENT
    db.commit()
    publish(
        "insurance.offered",
        application_id=str(app.id),
        customer_id=str(app.customer_id),
        ref=tx.ref,
    )
    return app


def decline_application(
    db: Session, insurer: User, application_id: uuid.UUID, reason: str | None
) -> Application:
    app, tx = _locked(db, application_id, insurer=insurer)
    _need(tx, TxStatus.AWAITING_INSURER, TxStatus.QUOTE_SENT)
    app.decline_reason, tx.status = reason, TxStatus.DECLINED
    db.commit()
    publish(
        "insurance.declined",
        application_id=str(app.id),
        customer_id=str(app.customer_id),
        ref=tx.ref,
    )
    return app


def answer_offer(
    db: Session, customer: User, application_id: uuid.UUID, accept: bool
) -> Application:
    app, tx = _locked(db, application_id, customer=customer)
    _need(tx, TxStatus.QUOTE_SENT)
    if accept and app.offer_valid_until and app.offer_valid_until < utcnow():
        raise UnprocessableEntity(
            "This offer has expired. Ask the insurer to renew it.", code="offer_expired"
        )
    withdrawn: list[tuple[Application, Transaction]] = []
    if accept:
        tx.status = TxStatus.OFFER_ACCEPTED
        payments.request_payment(db, tx)
        withdrawn = _withdraw_other_quotes(db, app)
    else:
        tx.status = TxStatus.DECLINED
    db.commit()
    for other, other_tx in withdrawn:
        publish(
            "insurance.withdrawn",
            application_id=str(other.id),
            insurer_id=str(other.insurer_id),
            ref=other_tx.ref,
        )
    publish(
        "insurance.answered",
        application_id=str(app.id),
        insurer_id=str(app.insurer_id),
        accepted=accept,
        ref=tx.ref,
    )
    return app


def _withdraw_other_quotes(
    db: Session, chosen: Application
) -> list[tuple[Application, Transaction]]:
    """The customer took one insurer's offer: close the rest of that quote request."""
    if chosen.quote_request_id is None:
        return []
    withdrawn = []
    for other in db.scalars(
        select(Application).where(
            Application.quote_request_id == chosen.quote_request_id, Application.id != chosen.id
        )
    ):
        other_tx = payments.get_transaction(db, other.transaction_id, lock=True)
        if other_tx.status in (TxStatus.AWAITING_INSURER, TxStatus.QUOTE_SENT):
            payments.cancel_and_refund(db, other_tx, reason="Customer chose another insurer")
            withdrawn.append((other, other_tx))
    return withdrawn


def cancel_application(db: Session, customer: User, application_id: uuid.UUID) -> Application:
    """Withdraw before paying. (Once a policy is issued it cannot be cancelled here.)"""
    app, tx = _locked(db, application_id, customer=customer)
    _need(tx, TxStatus.AWAITING_INSURER, TxStatus.QUOTE_SENT, TxStatus.OFFER_ACCEPTED)
    if tx.paid_at is not None:
        raise UnprocessableEntity("A paid policy cannot be cancelled here", code="already_paid")
    payments.cancel_and_refund(db, tx, reason="Application withdrawn")
    db.commit()
    return app


# ======================================================================================
# Policies
# ======================================================================================
@subscribe("payment.succeeded")
def _issue_policy_on_payment(event: Event) -> None:
    if event.payload.get("kind") != TransactionKind.POLICY_PURCHASE.value:
        return
    db = db_module.new_session()  # looked up at call time so tests can swap it
    try:
        tx = db.get(Transaction, uuid.UUID(event.payload["transaction_id"]))
        app = (
            db.scalar(select(Application).where(Application.transaction_id == tx.id))
            if tx
            else None
        )
        if app is None or db.scalar(select(Policy.id).where(Policy.application_id == app.id)):
            return  # unknown or already issued: handlers must be idempotent
        policy = issue_policy(db, app, tx)
        db.commit()
        publish(
            "policy.issued", policy_id=str(policy.id), holder_id=str(policy.holder_id), ref=tx.ref
        )
    finally:
        db.close()


def issue_policy(db: Session, app: Application, tx: Transaction) -> Policy:
    start = date.today()
    # A renewal continues from the day the old policy ends, so cover has no gap.
    if app.renewal_of_id:
        previous = db.get(Policy, app.renewal_of_id)
        if previous and previous.ends_on > start:
            start = previous.ends_on
    end = start + timedelta(days=round(app.term_months * 365 / 12))
    policy = Policy(
        application_id=app.id,
        transaction_id=tx.id,
        holder_id=app.customer_id,
        insurer_id=app.insurer_id,
        policy_number=f"AS-POL-{tx.ref_no}",
        product_title=app.product_title,
        cover_type=app.cover_type,
        vehicle_summary=app.vehicle_summary,
        vehicle_vin=app.vehicle_vin,
        premium_minor=app.premium_minor or tx.amount_minor,
        terms=app.terms,
        starts_on=start,
        ends_on=end,
    )
    db.add(policy)
    payments.mark_completed(db, tx, TxStatus.ACTIVE)  # the sale is done; dispute window starts
    db.flush()
    return policy


def get_policy(db: Session, policy_id: uuid.UUID, viewer: User) -> Policy:
    policy = db.get(Policy, policy_id)
    if policy is None or not (
        viewer.id in (policy.holder_id, policy.insurer_id) or viewer.has_role(*ADMIN_ROLES)
    ):
        raise NotFound("Policy not found", code="policy_not_found")
    return policy


def list_policies(db: Session, user: User, *, as_insurer: bool) -> list[Policy]:
    column = Policy.insurer_id if as_insurer else Policy.holder_id
    return list(db.scalars(select(Policy).where(column == user.id).order_by(Policy.ends_on.desc())))


def renew(db: Session, customer: User, policy_id: uuid.UUID) -> Application:
    """Start a renewal: a new application for the same product and vehicle."""
    policy = get_policy(db, policy_id, customer)
    if policy.holder_id != customer.id:
        raise NotFound("Policy not found", code="policy_not_found")
    original = db.get(Application, policy.application_id)
    if original is None or original.product_id is None or original.vehicle_id is None:
        raise UnprocessableEntity("This product can no longer be renewed", code="not_renewable")
    return apply(
        db, customer, product_id=original.product_id, vehicle_id=original.vehicle_id,
        details="Renewal", renewal_of_id=policy.id,
    )  # fmt: skip


def certificate_html(db: Session, policy: Policy) -> str:
    """A printable certificate (the apps open it and 'Print / Save as PDF')."""
    from html import escape  # noqa: PLC0415

    holder = db.get(User, policy.holder_id)
    insurer = db.get(User, policy.insurer_id)
    rows = [
        ("Policy number", policy.policy_number), ("Policy holder", holder.display_name),
        ("Insurer", insurer.display_name), ("Product", policy.product_title),
        ("Cover", policy.cover_type), ("Vehicle", policy.vehicle_summary),
        ("VIN", policy.vehicle_vin or "-"), ("Premium", f"NGN {policy.premium_minor / 100:,.2f}"),
        ("Valid", f"{policy.starts_on:%d %b %Y} to {policy.ends_on:%d %b %Y}"),
    ]  # fmt: skip
    body = "".join(f"<tr><th>{escape(k)}</th><td>{escape(str(v))}</td></tr>" for k, v in rows)
    return (
        "<!doctype html><meta charset=utf-8><title>Certificate of Insurance</title>"
        "<style>body{font-family:sans-serif;max-width:640px;margin:40px auto}"
        "th{text-align:left;padding:6px 16px 6px 0;color:#555}</style>"
        f"<h1>Certificate of Insurance</h1><table>{body}</table>"
        f"<p><small>{escape(insurance_disclosure(insurer.display_name))}</small></p>"
    )


def run_policy_lifecycle(db: Session, *, today: date | None = None) -> dict[str, int]:
    """Scheduled job (daily): mark ended policies expired and remind holders 30 days ahead."""
    today = today or date.today()
    expired = reminded = 0
    for policy in db.scalars(select(Policy).where(Policy.status == PolicyStatus.ACTIVE)):
        if policy.ends_on < today:
            policy.status = PolicyStatus.EXPIRED
            expired += 1
        elif policy.reminder_sent_at is None and policy.ends_on <= today + timedelta(
            days=REMINDER_DAYS
        ):
            policy.reminder_sent_at = utcnow()
            reminded += 1
            publish(
                "policy.expiring",
                policy_id=str(policy.id),
                holder_id=str(policy.holder_id),
                ends_on=policy.ends_on.isoformat(),
            )
    db.commit()
    return {"expired": expired, "reminded": reminded}


# ======================================================================================
# Claims
# ======================================================================================
def submit_claim(
    db: Session,
    holder: User,
    policy_id: uuid.UUID,
    *,
    incident_date: date,
    description: str,
    amount_minor: int,
) -> Claim:
    policy = get_policy(db, policy_id, holder)
    if policy.holder_id != holder.id:
        raise NotFound("Policy not found", code="policy_not_found")
    if policy.status != PolicyStatus.ACTIVE or policy.ends_on < date.today():
        raise UnprocessableEntity("This policy is not active", code="policy_inactive")
    if not policy.starts_on <= incident_date <= date.today():
        raise UnprocessableEntity(
            "The incident must be within the policy period", code="invalid_incident_date"
        )
    claim = Claim(
        policy_id=policy.id, claimant_id=holder.id, insurer_id=policy.insurer_id,
        incident_date=incident_date, description=description.strip(), amount_claimed_minor=amount_minor,
    )  # fmt: skip
    db.add(claim)
    db.commit()
    publish(
        "claim.submitted", claim_id=str(claim.id), insurer_id=str(policy.insurer_id), ref=claim.ref
    )
    return claim


def get_claim(db: Session, claim_id: uuid.UUID, viewer: User) -> Claim:
    claim = db.get(Claim, claim_id)
    if claim is None or not (
        viewer.id in (claim.claimant_id, claim.insurer_id) or viewer.has_role(*ADMIN_ROLES)
    ):
        raise NotFound("Claim not found", code="claim_not_found")
    return claim


def list_claims(
    db: Session, user: User, *, as_insurer: bool, page: int, page_size: int
) -> tuple[list[Claim], int]:
    column = Claim.insurer_id if as_insurer else Claim.claimant_id
    stmt = select(Claim).where(column == user.id).order_by(Claim.created_at.desc())
    return paginate_page(db, stmt, page=page, page_size=page_size)


def add_claim_document(
    db: Session, holder: User, claim_id: uuid.UUID, filename: str, content_type: str, data: bytes
) -> ClaimDocument:
    claim = get_claim(db, claim_id, holder)
    if claim.claimant_id != holder.id:
        raise NotFound("Claim not found", code="claim_not_found")
    content_type = checked_type(
        data, CLAIM_FILE_TYPES, message="Upload a JPEG, PNG or PDF", code="invalid_file_type"
    )
    if not data or len(data) > CLAIM_FILE_MAX:
        raise BadRequest("Files must be under 8 MB", code="file_too_large")
    key = f"claims/{claim.id}/{uuid.uuid4().hex}{CLAIM_FILE_TYPES[content_type]}"
    registry.get_storage().put(key, data, content_type)
    doc = ClaimDocument(
        claim_id=claim.id, storage_key=key, filename=filename[:200], content_type=content_type
    )
    db.add(doc)
    db.commit()
    return doc


def claim_documents(db: Session, claim: Claim) -> list[tuple[ClaimDocument, str]]:
    docs = db.scalars(
        select(ClaimDocument)
        .where(ClaimDocument.claim_id == claim.id)
        .order_by(ClaimDocument.created_at)
    )
    storage = registry.get_storage()
    return [(d, storage.url(d.storage_key)) for d in docs]


def decide_claim(
    db: Session,
    insurer: User,
    claim_id: uuid.UUID,
    *,
    status: ClaimStatus,
    approved_amount_minor: int | None,
    note: str | None,
) -> Claim:
    """Insurer moves a claim through review to approved/rejected. (Settlement of the money
    happens between insurer and policy holder; AutoScope records the decision.)"""
    claim = db.get(Claim, claim_id)
    if claim is None or claim.insurer_id != insurer.id:
        raise NotFound("Claim not found", code="claim_not_found")
    if claim.status in (ClaimStatus.APPROVED, ClaimStatus.REJECTED):
        raise UnprocessableEntity("This claim was already decided", code="already_decided")
    if status == ClaimStatus.SUBMITTED:
        raise BadRequest("Choose under_review, approved or rejected", code="invalid_status")
    if status == ClaimStatus.APPROVED:
        if (
            approved_amount_minor is None
            or not 0 < approved_amount_minor <= claim.amount_claimed_minor
        ):
            raise BadRequest(
                "Give an approved amount up to the amount claimed", code="invalid_amount"
            )
        claim.approved_amount_minor = approved_amount_minor
    claim.status, claim.insurer_note = status, note
    if status != ClaimStatus.UNDER_REVIEW:
        claim.decided_at = utcnow()
    db.commit()
    publish(
        "claim.updated",
        claim_id=str(claim.id),
        claimant_id=str(claim.claimant_id),
        status=status.value,
        ref=claim.ref,
    )
    return claim


@payments.receipt_lines(TransactionKind.POLICY_PURCHASE)
def _receipt_lines(db: Session, tx: Transaction) -> list[tuple[str, int]]:
    """Receipt line (FR-9.3): the product and vehicle the premium is for."""
    app = db.scalar(select(Application).where(Application.transaction_id == tx.id))
    if app is None:
        return []
    cover = f" ({app.cover_type})" if app.cover_type else ""
    return [(f"{app.product_title}{cover} - {app.vehicle_summary}", tx.amount_minor)]
