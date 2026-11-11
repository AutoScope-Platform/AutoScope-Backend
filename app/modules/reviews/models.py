"""Reviews, rating summaries and abuse reports (FSD 3.11)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamps, UUIDPrimaryKey, enum_type, ref_column


class ReviewStatus(StrEnum):
    VISIBLE = "visible"
    REMOVED = "removed"  # taken down by an admin after a report


class Review(UUIDPrimaryKey, Timestamps, Base):
    """A 1-5 star rating and comment about a provider after a completed transaction."""

    __tablename__ = "reviews"
    # One review per author per transaction.
    __table_args__ = (UniqueConstraint("author_id", "transaction_ref"),)

    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    transaction_ref: Mapped[str] = mapped_column(String(32))  # e.g. "BKG-1042"
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)  # the provider's public response (FR-11.3)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReviewStatus] = mapped_column(
        enum_type(ReviewStatus), default=ReviewStatus.VISIBLE
    )


class RatingSummary(Base):
    """Denormalised average rating per provider so lists and search stay fast (FR-11.2)."""

    __tablename__ = "rating_summaries"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    average: Mapped[float] = mapped_column(Float, default=0.0)
    count: Mapped[int] = mapped_column(Integer, default=0)


class ReportTarget(StrEnum):
    LISTING = "listing"
    REVIEW = "review"
    ACCOUNT = "account"


class ReportReason(StrEnum):
    FRAUD = "fraud_or_scam"
    COUNTERFEIT = "counterfeit_goods"
    SPAM = "spam"
    OFF_PLATFORM_PAYMENT = "off_platform_payment_request"
    ABUSIVE = "abusive_content"
    OTHER = "other"


class ReportStatus(StrEnum):
    OPEN = "open"
    DISMISSED = "dismissed"
    ACTIONED = "actioned"  # the reported item was removed or suspended


class Report(UUIDPrimaryKey, Timestamps, Base):
    """A user's flag on a listing, review or account (FR-11.4). Many reports can point at
    the same target; the admin screen groups them."""

    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("reporter_id", "target_type", "target_id"),)

    ref_no: Mapped[int] = ref_column(start=3000)
    reporter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    target_type: Mapped[ReportTarget] = mapped_column(enum_type(ReportTarget), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(index=True)
    reason: Mapped[ReportReason] = mapped_column(enum_type(ReportReason))
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReportStatus] = mapped_column(
        enum_type(ReportStatus), default=ReportStatus.OPEN, index=True
    )
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
