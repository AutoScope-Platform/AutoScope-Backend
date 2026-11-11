"""Insurance (FSD 3.7): applications and offers, policies, and claims.

Journey: the customer applies for a listed product for one of their vehicles -> the insurer
sends an offer (premium and terms) -> the customer accepts and pays -> a policy is issued.
Money and status live on the `Transaction`, as everywhere else.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamps, UUIDPrimaryKey, enum_type, ref_column


class Application(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "insurance_applications"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    insurer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL")
    )
    product_title: Mapped[str] = mapped_column(String(160))
    cover_type: Mapped[str] = mapped_column(String(60))
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL")
    )
    # Copied from the garage at application time so the policy stays accurate if it changes.
    vehicle_summary: Mapped[str] = mapped_column(String(200))
    vehicle_vin: Mapped[str | None] = mapped_column(String(17))
    details: Mapped[str] = mapped_column(Text, default="")
    term_months: Mapped[int] = mapped_column(default=12)
    # Filled in when the insurer makes an offer.
    premium_minor: Mapped[int | None] = mapped_column(BigInteger)
    terms: Mapped[str | None] = mapped_column(Text)
    offer_valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decline_reason: Mapped[str | None] = mapped_column(String(300))
    # use_alter: applications and policies point at each other, so this constraint is
    # added after both tables exist (otherwise table creation order has no valid answer).
    renewal_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "policies.id", ondelete="SET NULL", use_alter=True, name="fk_applications_renewal_of"
        )
    )


class PolicyStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


class Policy(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "policies"

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("insurance_applications.id", ondelete="RESTRICT"), unique=True
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT")
    )
    holder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    insurer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    policy_number: Mapped[str] = mapped_column(String(40), unique=True)
    product_title: Mapped[str] = mapped_column(String(160))
    cover_type: Mapped[str] = mapped_column(String(60))
    vehicle_summary: Mapped[str] = mapped_column(String(200))
    vehicle_vin: Mapped[str | None] = mapped_column(String(17))
    premium_minor: Mapped[int] = mapped_column(BigInteger)
    terms: Mapped[str | None] = mapped_column(Text)
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[PolicyStatus] = mapped_column(
        enum_type(PolicyStatus), default=PolicyStatus.ACTIVE, index=True
    )
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClaimStatus(StrEnum):
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class Claim(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "claims"

    ref_no: Mapped[int] = ref_column(start=600)
    policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policies.id", ondelete="RESTRICT"), index=True
    )
    claimant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    insurer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    incident_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    amount_claimed_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[ClaimStatus] = mapped_column(
        enum_type(ClaimStatus), default=ClaimStatus.SUBMITTED, index=True
    )
    approved_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    insurer_note: Mapped[str | None] = mapped_column(String(500))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def ref(self) -> str:
        return f"CLM-{self.ref_no}"


class ClaimDocument(UUIDPrimaryKey, Timestamps, Base):
    """A photo or PDF supporting a claim (damage photos, police report)."""

    __tablename__ = "claim_documents"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claims.id", ondelete="CASCADE"), index=True
    )
    storage_key: Mapped[str] = mapped_column(String(300))
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(80))
