"""Admin business logic that does not belong to a single domain: dashboards and the audit log.

Documented exception to the module-boundary rule: reporting reads other modules' tables
(transactions, listings, users) directly. It only reads, so no domain rule can be bypassed.
The admin *actions* (approve a listing, resolve a dispute...) stay in their own modules.

Section map:
    1. Overview     - the dashboard's KPI cards, GMV chart and recent actions
    2. Analytics    - a business user's own numbers (FR-12)
    3. Audit log    - search and CSV export
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.audit import AuditCategory, AuditLog
from app.core.db import utcnow
from app.core.pagination import paginate_page
from app.modules.catalog import service as catalog
from app.modules.catalog.models import Listing, ListingStatus
from app.modules.identity import service as identity
from app.modules.identity.models import AccountStatus, User
from app.modules.payments import service as payments
from app.modules.payments.models import (
    CATEGORY_OF,
    Category,
    EscrowStatus,
    Transaction,
    TransactionKind,
    TxStatus,
)
from app.modules.reviews import service as reviews

# Money is "real" once paid and not sent back to the customer.
_COUNTED = (Transaction.paid_at.is_not(None), Transaction.escrow_status != EscrowStatus.REFUNDED)


# ======================================================================================
# 1. Overview
# ======================================================================================
def overview(db: Session, *, days: int) -> dict:
    since = utcnow() - timedelta(days=days)

    def money(*conditions) -> int:
        return int(
            db.scalar(
                select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
                    *_COUNTED, *conditions
                )
            )
            or 0
        )

    def count(model, *conditions) -> int:
        return int(db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)

    by_category = {c.value: 0 for c in Category}
    for kind, total in db.execute(
        select(Transaction.kind, func.sum(Transaction.amount_minor))
        .where(*_COUNTED, Transaction.paid_at >= since)
        .group_by(Transaction.kind)
    ):
        by_category[CATEGORY_OF[kind].value] += int(total)

    series = [
        {"date": day.isoformat(), "gmv_minor": int(total)}
        for day, total in db.execute(
            select(cast(Transaction.paid_at, Date).label("d"), func.sum(Transaction.amount_minor))
            .where(*_COUNTED, Transaction.paid_at >= since)
            .group_by("d")
            .order_by("d")
        )
    ]
    open_sos = db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.is_sos.is_(True),
            Transaction.paid_at.is_not(None),
            Transaction.status.not_in([TxStatus.COMPLETED, TxStatus.CANCELLED, TxStatus.DECLINED]),
        )
    )
    return {
        "period_days": days,
        "users": {
            "total": count(User, User.erased_at.is_(None)),
            "new": count(User, User.created_at >= since),
            "suspended": count(User, User.status == AccountStatus.SUSPENDED),
        },
        "listings_live": count(Listing, Listing.status == ListingStatus.LIVE),
        "gmv_minor": money(Transaction.paid_at >= since),
        "gmv_all_time_minor": money(),
        "orders_paid": count(Transaction, *_COUNTED, Transaction.paid_at >= since),
        "commission_minor": int(
            db.scalar(
                select(func.coalesce(func.sum(Transaction.commission_minor), 0)).where(
                    Transaction.escrow_status == EscrowStatus.RELEASED, Transaction.released_at >= since
                )
            )
            or 0
        ),
        "escrow_held_minor": money(Transaction.escrow_status.in_([EscrowStatus.HELD, EscrowStatus.FROZEN])),
        "gmv_by_category": by_category,
        "gmv_series": series,
        "open_sos": int(open_sos or 0),
        "queues": {
            "kyc_pending": identity.kyc_counts(db).get("pending", 0),
            "listings_pending": catalog.review_counts(db)["pending"],
            "disputes_open": payments.dispute_counts(db)["open"],
            "reports_open": reviews.report_counts(db)["open"],
        },
        "recent_actions": [
            {"at": a.created_at, "actor": a.actor_name, "action": a.action, "target": a.target}
            for a in db.scalars(select(AuditLog).where(AuditLog.category == AuditCategory.ADMIN).order_by(AuditLog.created_at.desc()).limit(8))
        ],
    }  # fmt: skip


# ======================================================================================
# 2. Analytics for a business user
# ======================================================================================
def my_analytics(db: Session, user: User, *, days: int) -> dict:
    """A provider's sales, earnings and what is still held, for the last `days` days."""
    since = utcnow() - timedelta(days=days)
    mine = (Transaction.provider_id == user.id, *_COUNTED)
    gross = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
            *mine, Transaction.paid_at >= since
        )
    )
    earned = db.scalar(
        select(func.coalesce(func.sum(Transaction.provider_net_minor), 0)).where(
            Transaction.provider_id == user.id, Transaction.released_at >= since
        )
    )
    held = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
            Transaction.provider_id == user.id,
            Transaction.escrow_status.in_([EscrowStatus.HELD, EscrowStatus.FROZEN]),
        )
    )
    by_kind = {k.value: 0 for k in TransactionKind}
    for kind, n in db.execute(
        select(Transaction.kind, func.count())
        .where(*mine, Transaction.paid_at >= since)
        .group_by(Transaction.kind)
    ):
        by_kind[kind.value] = n
    series = [
        {"date": d.isoformat(), "sales_minor": int(total)}
        for d, total in db.execute(
            select(cast(Transaction.paid_at, Date).label("d"), func.sum(Transaction.amount_minor))
            .where(*mine, Transaction.paid_at >= since)
            .group_by("d")
            .order_by("d")
        )
    ]
    rating, review_count = reviews.ratings_for(db, [user.id]).get(user.id, (0.0, 0))
    live = db.scalar(
        select(func.count())
        .select_from(Listing)
        .where(Listing.seller_id == user.id, Listing.status == ListingStatus.LIVE)
    )
    return {
        "period_days": days,
        "sales_minor": int(gross or 0),
        "earned_minor": int(earned or 0),
        "held_in_escrow_minor": int(held or 0),
        "paid_transactions_by_kind": by_kind,
        "sales_series": series,
        "rating": rating,
        "review_count": review_count,
        "live_listings": int(live or 0),
    }


# ======================================================================================
# 3. Audit log
# ======================================================================================
def _audit_filter(
    category: AuditCategory | None,
    q: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
):
    stmt = select(AuditLog)
    if category:
        stmt = stmt.where(AuditLog.category == category)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                AuditLog.actor_name.ilike(like),
                AuditLog.action.ilike(like),
                AuditLog.target.ilike(like),
            )
        )
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= date_from)
    if date_to:
        stmt = stmt.where(AuditLog.created_at <= date_to)
    return stmt.order_by(AuditLog.created_at.desc())


def search_audit(
    db: Session, *, category: AuditCategory | None, q: str | None, date_from: datetime | None,
    date_to: datetime | None, page: int, page_size: int,
) -> tuple[list[AuditLog], int]:  # fmt: skip
    return paginate_page(
        db, _audit_filter(category, q, date_from, date_to), page=page, page_size=page_size
    )


EXPORT_LIMIT = 10_000


def _csv_safe(value: str | None) -> str:
    """Stop spreadsheet formula injection: a cell starting with = + - @ would be run as a
    formula when the file is opened in Excel, so it gets a leading apostrophe."""
    text = value or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_audit_csv(
    db: Session, admin: User, *, category: AuditCategory | None, q: str | None,
    date_from: datetime | None, date_to: datetime | None, ip: str | None,
) -> str:  # fmt: skip
    """The filtered log as CSV (newest first, at most 10,000 rows). The export itself is logged."""
    rows = db.scalars(_audit_filter(category, q, date_from, date_to).limit(EXPORT_LIMIT)).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["time_utc", "category", "actor", "action", "target", "ip_address"])
    for r in rows:
        writer.writerow(
            [
                r.created_at.isoformat(),
                r.category.value,
                _csv_safe(r.actor_name),
                _csv_safe(r.action),
                _csv_safe(r.target),
                r.ip_address or "",
            ]
        )
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Exported audit log",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=f"{len(rows)} rows",
        ip_address=ip,
    )
    db.commit()
    return out.getvalue()
