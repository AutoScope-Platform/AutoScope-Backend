"""Payments tables: the transaction ledger, payments, escrow, wallets, payouts and disputes.

The central idea: every sale on the platform (parts order, vehicle sale, service booking,
haulage job, insurance policy) has exactly one `Transaction` row. The business module that
owns the sale (orders, booking, insurance) keeps its own details and points at its
transaction. Money, escrow, commission and the admin "Orders, bookings and jobs" screen all
work from `Transaction`, so they behave the same for every kind of sale.

Money is stored as integer minor units (kobo). `rate_bps` means basis points: 250 = 2.5%.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,  # noqa: F401
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamps, UUIDPrimaryKey, enum_type, ref_column


class TransactionKind(StrEnum):
    PARTS_ORDER = "parts_order"
    VEHICLE_SALE = "vehicle_sale"
    SERVICE_BOOKING = "service_booking"
    HAULAGE_JOB = "haulage_job"
    POLICY_PURCHASE = "policy_purchase"


# Reference prefix per kind: the human-readable id shown in the apps and the dashboard.
REF_PREFIX = {
    TransactionKind.PARTS_ORDER: "ORD",
    TransactionKind.VEHICLE_SALE: "VEH",
    TransactionKind.SERVICE_BOOKING: "BKG",
    TransactionKind.HAULAGE_JOB: "JOB",
    TransactionKind.POLICY_PURCHASE: "POL",
}


class Category(StrEnum):
    """Commission categories (FR-13.3). Each transaction kind belongs to exactly one."""

    VEHICLES = "vehicles"
    PARTS = "parts"
    MECHANIC_SERVICES = "mechanic_services"
    HAULAGE = "haulage"
    INSURANCE = "insurance"


CATEGORY_OF = {
    TransactionKind.VEHICLE_SALE: Category.VEHICLES,
    TransactionKind.PARTS_ORDER: Category.PARTS,
    TransactionKind.SERVICE_BOOKING: Category.MECHANIC_SERVICES,
    TransactionKind.HAULAGE_JOB: Category.HAULAGE,
    TransactionKind.POLICY_PURCHASE: Category.INSURANCE,
}


class TxStatus(StrEnum):
    """Every status a transaction can show in 'My Activity' and the admin Activity screen."""

    PENDING = "pending"  # created, waiting for payment
    REQUESTED = "requested"
    QUOTE_REQUESTED = "quote_requested"
    QUOTE_SENT = "quote_sent"
    AWAITING_INSURER = "awaiting_insurer"
    OFFER_ACCEPTED = "offer_accepted"
    CONFIRMED = "confirmed"
    SEARCHING = "searching"
    PROCESSING = "processing"
    EN_ROUTE = "en_route"
    IN_PROGRESS = "in_progress"
    SHIPPED = "shipped"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    IN_ESCROW = "in_escrow"
    ACTIVE = "active"
    COMPLETED = "completed"
    DECLINED = "declined"
    CANCELLED = "cancelled"


# Statuses after which a sale is over: no further movement except escrow release / refund.
FINAL_STATUSES = frozenset({TxStatus.COMPLETED, TxStatus.CANCELLED, TxStatus.DECLINED})


class EscrowStatus(StrEnum):
    NONE = "none"  # not paid yet, or the category does not use escrow ("n/a" in the dashboard)
    HELD = "held"
    FROZEN = "frozen"  # a dispute is open; nothing moves until an admin decides
    RELEASED = "released"
    REFUNDED = "refunded"


class Transaction(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "transactions"

    ref_no: Mapped[int] = ref_column(start=80000)
    kind: Mapped[TransactionKind] = mapped_column(enum_type(TransactionKind), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[TxStatus] = mapped_column(
        enum_type(TxStatus), default=TxStatus.PENDING, index=True
    )
    escrow_status: Mapped[EscrowStatus] = mapped_column(
        enum_type(EscrowStatus), default=EscrowStatus.NONE, index=True
    )
    is_sos: Mapped[bool] = mapped_column(Boolean, default=False)
    # Commission is fixed when the payment succeeds, so later rate changes only affect new sales.
    commission_rate_bps: Mapped[int | None] = mapped_column(Integer)
    commission_cap_minor: Mapped[int | None] = mapped_column(BigInteger)
    commission_minor: Mapped[int | None] = mapped_column(BigInteger)
    provider_net_minor: Mapped[int | None] = mapped_column(BigInteger)
    uses_escrow: Mapped[bool] = mapped_column(Boolean, default=True)
    # True while the customer is expected to pay (set by the owning module when the sale is
    # ready for payment, e.g. at checkout or after a mechanic accepts a booking).
    payment_due: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the provider finished the work; the dispute window (hold days) counts from here.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The row in the owning module (order, booking, job, policy) this transaction belongs to.
    source_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    # The provider's cancellation terms, copied when the payment succeeds (FR-9.4, FR-10.3)
    # so a later change to their policy never changes a sale that is already paid.
    cancel_free_hours: Mapped[int | None] = mapped_column(Integer)
    cancel_fee_bps: Mapped[int | None] = mapped_column(Integer)
    cancel_window_hours: Mapped[int | None] = mapped_column(Integer)

    @property
    def ref(self) -> str:
        return f"{REF_PREFIX[self.kind]}-{self.ref_no}"

    @property
    def category(self) -> Category:
        return CATEGORY_OF[self.kind]


class PaymentMethod(StrEnum):
    CARD = "card"
    BANK_TRANSFER = "bank_transfer"
    MOBILE_MONEY = "mobile_money"


class PaymentStatus(StrEnum):
    INITIATED = "initiated"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Payment(UUIDPrimaryKey, Timestamps, Base):
    """One attempt to pay for a transaction through the gateway. No card data is stored."""

    __tablename__ = "payments"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    payer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    method: Mapped[PaymentMethod] = mapped_column(enum_type(PaymentMethod))
    gateway: Mapped[str] = mapped_column(String(20))
    reference: Mapped[str] = mapped_column(String(64), unique=True)  # our id, sent to the gateway
    status: Mapped[PaymentStatus] = mapped_column(
        enum_type(PaymentStatus), default=PaymentStatus.INITIATED
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Refund(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "refunds"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    gateway_reference: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(String(200))


class WebhookEvent(Base):
    """Gateways re-send webhooks; recording each event id makes handling idempotent."""

    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    gateway: Mapped[str] = mapped_column(String(20))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- Wallet and payouts ---------------------------------------------------------------
class Wallet(Base):
    """A provider's spendable balance: released earnings not yet paid out (FR-9.5)."""

    __tablename__ = "wallets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    balance_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")


class LedgerType(StrEnum):
    SALE = "sale"  # earnings credited when escrow is released
    PAYOUT = "payout"  # money sent to the bank account
    PAYOUT_REVERSAL = "payout_reversal"  # a failed or cancelled payout returned to the wallet


class LedgerEntry(UUIDPrimaryKey, Timestamps, Base):
    """Append-only history behind the wallet balance."""

    __tablename__ = "ledger_entries"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[LedgerType] = mapped_column(enum_type(LedgerType))
    amount_minor: Mapped[int] = mapped_column(BigInteger)  # positive = credit, negative = debit
    balance_after_minor: Mapped[int] = mapped_column(BigInteger)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    payout_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payouts.id", ondelete="SET NULL")
    )
    description: Mapped[str] = mapped_column(String(200))


class BankAccount(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "bank_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    bank_name: Mapped[str] = mapped_column(String(80))
    bank_code: Mapped[str] = mapped_column(String(20))
    account_number: Mapped[str] = mapped_column(String(20))
    account_name: Mapped[str] = mapped_column(String(120))


class PayoutStatus(StrEnum):
    SCHEDULED = "scheduled"
    ON_HOLD = "on_hold"
    PAID = "paid"
    FAILED = "failed"


class Payout(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "payouts"

    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    transactions_count: Mapped[int] = mapped_column(Integer, default=0)
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[PayoutStatus] = mapped_column(
        enum_type(PayoutStatus), default=PayoutStatus.SCHEDULED, index=True
    )
    # Snapshot of the destination so the payout is explainable even if the account changes later.
    bank_name: Mapped[str | None] = mapped_column(String(80))
    gateway_reference: Mapped[str | None] = mapped_column(String(80))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(200))


# --- Platform settings (editable in the admin dashboard) --------------------------------
class CommissionRate(Base):
    __tablename__ = "commission_rates"

    category: Mapped[Category] = mapped_column(enum_type(Category), primary_key=True)
    rate_bps: Mapped[int] = mapped_column(Integer)  # 250 = 2.5%
    cap_minor: Mapped[int | None] = mapped_column(BigInteger)  # max commission per transaction
    uses_escrow: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PayoutFrequency(StrEnum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


class PayoutSettings(Base):
    """Single row (id = 1): when payouts run and how long funds stay in escrow."""

    __tablename__ = "payout_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    frequency: Mapped[PayoutFrequency] = mapped_column(
        enum_type(PayoutFrequency), default=PayoutFrequency.WEEKLY
    )
    payout_weekday: Mapped[int] = mapped_column(Integer, default=4)  # 0 = Monday ... 4 = Friday
    hold_days: Mapped[int] = mapped_column(Integer, default=3)
    min_payout_minor: Mapped[int] = mapped_column(BigInteger, default=500_000)  # N5,000
    # Platform cap on the late-cancellation fee any provider may charge (FR-9.4).
    max_cancel_fee_bps: Mapped[int] = mapped_column(Integer, default=5000, server_default="5000")


class CancellationPolicy(UUIDPrimaryKey, Timestamps, Base):
    """A provider's cancellation terms (FR-9.4, FR-10.3). No row means the platform default:
    customers cancel free until the work starts or the order ships.

    * Bookings: cancelling less than `free_cancel_hours` before the appointment keeps
      `late_cancel_fee_bps` of the price for the provider (capped by the platform).
    * Parts orders: the buyer may cancel only within `order_cancel_window_hours` of paying
      (and before shipping). None = until shipping.
    Cancellations by the provider are always refunded in full.
    """

    __tablename__ = "cancellation_policies"

    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    free_cancel_hours: Mapped[int] = mapped_column(Integer, default=0)
    late_cancel_fee_bps: Mapped[int] = mapped_column(Integer, default=0)
    order_cancel_window_hours: Mapped[int | None] = mapped_column(Integer)


# --- Disputes ---------------------------------------------------------------------------
class DisputeStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"


class DisputePriority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"


class DisputeOutcome(StrEnum):
    REFUND_BUYER = "refund_buyer"
    RELEASE_PROVIDER = "release_provider"
    SPLIT = "split"


class Dispute(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "disputes"

    ref_no: Mapped[int] = ref_column(start=140)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    opened_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason: Mapped[str] = mapped_column(String(200))
    priority: Mapped[DisputePriority] = mapped_column(
        enum_type(DisputePriority), default=DisputePriority.NORMAL
    )
    status: Mapped[DisputeStatus] = mapped_column(
        enum_type(DisputeStatus), default=DisputeStatus.OPEN, index=True
    )
    escrow_amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    outcome: Mapped[DisputeOutcome | None] = mapped_column(enum_type(DisputeOutcome))
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def ref(self) -> str:
        return f"DSP-{self.ref_no:04d}"


class DisputeMessage(UUIDPrimaryKey, Timestamps, Base):
    """A comment in the dispute thread. `internal` notes are visible to staff only."""

    __tablename__ = "dispute_messages"

    dispute_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("disputes.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    author_name: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    internal: Mapped[bool] = mapped_column(Boolean, default=False)
