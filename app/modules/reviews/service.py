"""Reviews business logic: ratings after a completed sale, replies, reports and moderation.

Rules (FSD 3.11):
* Only the customer of a *completed* transaction can review its provider, once.
* The provider can reply once, publicly.
* Anyone can report a listing, review or account; admins dismiss or act on the report.
* Average ratings are kept in `RatingSummary` so lists never need to aggregate reviews.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import audit
from app.core.audit import AuditCategory
from app.core.db import utcnow
from app.core.errors import Conflict, Forbidden, NotFound, UnprocessableEntity
from app.core.events import publish
from app.core.pagination import paginate_page
from app.modules.identity import service as identity
from app.modules.identity.models import AccountStatus, User
from app.modules.payments import service as payments
from app.modules.reviews.models import (
    RatingSummary,
    Report,
    ReportReason,
    ReportStatus,
    ReportTarget,
    Review,
    ReviewStatus,
)


# ======================================================================================
# Ratings
# ======================================================================================
def ratings_for(db: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[float, int]]:
    """{user id: (average rating, number of reviews)}. Used by catalog for seller badges."""
    if not user_ids:
        return {}
    rows = db.execute(select(RatingSummary).where(RatingSummary.user_id.in_(user_ids))).scalars()
    return {r.user_id: (round(r.average, 1), r.count) for r in rows}


def rating_summary_subquery():
    """(user_id, average, count) per reviewed provider, for other modules that sort or
    filter by rating in SQL (read-only; providers with no reviews have no row)."""
    return select(RatingSummary.user_id, RatingSummary.average, RatingSummary.count).subquery()


def _recompute_summary(db: Session, user_id: uuid.UUID) -> None:
    """Rebuild a provider's average from their visible reviews."""
    db.flush()
    avg, count = db.execute(
        select(func.coalesce(func.avg(Review.rating), 0.0), func.count()).where(
            Review.target_id == user_id, Review.status == ReviewStatus.VISIBLE
        )
    ).one()
    summary = db.get(RatingSummary, user_id) or RatingSummary(user_id=user_id)
    summary.average, summary.count = float(avg), int(count)
    db.add(summary)


# ======================================================================================
# Reviews
# ======================================================================================
def create_review(
    db: Session, author: User, transaction_ref: str, rating: int, comment: str | None
) -> Review:
    tx = payments.get_transaction_by_ref(db, transaction_ref)
    if tx.customer_id != author.id or tx.provider_id is None:
        raise Forbidden("You can only review something you bought", code="forbidden")
    if tx.completed_at is None:
        raise UnprocessableEntity(
            "You can review once the job or order is completed", code="not_completed"
        )
    review = Review(
        author_id=author.id,
        target_id=tx.provider_id,
        transaction_ref=tx.ref,
        rating=rating,
        comment=(comment or "").strip() or None,
    )
    db.add(review)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("You already reviewed this", code="already_reviewed") from None
    _recompute_summary(db, tx.provider_id)
    db.commit()
    publish("review.created", review_id=str(review.id), provider_id=str(tx.provider_id))
    return review


def reply_to_review(db: Session, provider: User, review_id: uuid.UUID, reply: str) -> Review:
    review = db.get(Review, review_id)
    if review is None or review.status != ReviewStatus.VISIBLE or review.target_id != provider.id:
        raise NotFound("Review not found", code="review_not_found")
    if review.reply:
        raise Conflict("You already replied to this review", code="already_replied")
    review.reply, review.replied_at = reply.strip(), utcnow()
    db.commit()
    return review


def list_reviews(
    db: Session, user_id: uuid.UUID, *, page: int, page_size: int
) -> tuple[list[Review], int]:
    """Visible reviews about a provider, newest first (public)."""
    stmt = select(Review).where(Review.target_id == user_id, Review.status == ReviewStatus.VISIBLE)
    return paginate_page(
        db, stmt.order_by(Review.created_at.desc()), page=page, page_size=page_size
    )


def author_names(db: Session, reviews: list[Review]) -> dict[uuid.UUID, str]:
    ids = {r.author_id for r in reviews}
    if not ids:
        return {}
    return {u.id: u.display_name for u in db.scalars(select(User).where(User.id.in_(ids)))}


# ======================================================================================
# Reports
# ======================================================================================
def _target_exists(db: Session, target_type: ReportTarget, target_id: uuid.UUID) -> bool:
    # Imported here to keep reviews independent of catalog at import time.
    from app.modules.catalog.models import Listing  # noqa: PLC0415

    model = {ReportTarget.LISTING: Listing, ReportTarget.REVIEW: Review, ReportTarget.ACCOUNT: User}
    return db.get(model[target_type], target_id) is not None


def create_report(
    db: Session,
    reporter: User,
    target_type: ReportTarget,
    target_id: uuid.UUID,
    reason: ReportReason,
    details: str | None,
) -> Report:
    if not _target_exists(db, target_type, target_id):
        raise NotFound("The reported item was not found", code="target_not_found")
    if target_type == ReportTarget.ACCOUNT and target_id == reporter.id:
        raise UnprocessableEntity("You cannot report yourself", code="self_report")
    report = Report(
        reporter_id=reporter.id,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        details=(details or "").strip() or None,
    )
    db.add(report)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict("You already reported this", code="already_reported") from None
    return report


def list_reports(
    db: Session, *, status: ReportStatus | None, page: int, page_size: int
) -> tuple[list[Report], int]:
    stmt = select(Report).where(Report.status == (status or ReportStatus.OPEN))
    return paginate_page(db, stmt.order_by(Report.created_at), page=page, page_size=page_size)


def report_counts(db: Session) -> dict[str, int]:
    return {
        s.value: db.scalar(select(func.count()).select_from(Report).where(Report.status == s)) or 0
        for s in ReportStatus
    }


def resolve_report(
    db: Session, report_id: uuid.UUID, *, action: bool, admin: User, ip: str | None
) -> Report:
    """Dismiss the report, or act on it: suspend the listing, remove the review, or suspend
    the account. Every other open report on the same item is closed with it."""
    report = db.get(Report, report_id)
    if report is None:
        raise NotFound("Report not found", code="report_not_found")
    if report.status != ReportStatus.OPEN:
        raise UnprocessableEntity("This report is already resolved", code="already_resolved")

    if action:
        _take_action(db, report, admin=admin, ip=ip)
    new_status = ReportStatus.ACTIONED if action else ReportStatus.DISMISSED
    siblings = db.scalars(
        select(Report).where(
            Report.target_type == report.target_type,
            Report.target_id == report.target_id,
            Report.status == ReportStatus.OPEN,
        )
    ).all()
    for r in siblings:
        r.status, r.resolved_by_id = new_status, admin.id
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Actioned report" if action else "Dismissed report",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=f"RPT-{report.ref_no}",
        ip_address=ip,
    )
    db.commit()
    return report


def _take_action(db: Session, report: Report, *, admin: User, ip: str | None) -> None:
    from app.modules.catalog import service as catalog  # noqa: PLC0415

    reason = f"Report: {report.reason.value.replace('_', ' ')}"
    if report.target_type == ReportTarget.LISTING:
        catalog.suspend_listing(db, report.target_id, reason=reason, admin=admin, ip=ip)
    elif report.target_type == ReportTarget.REVIEW:
        review = db.get(Review, report.target_id)
        if review:
            review.status = ReviewStatus.REMOVED
            _recompute_summary(db, review.target_id)
    else:
        identity.set_account_status(
            db, report.target_id, AccountStatus.SUSPENDED, admin=admin, reason=reason, ip=ip
        )
