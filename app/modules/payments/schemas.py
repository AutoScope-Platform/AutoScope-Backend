"""Request and response bodies for the payments module (these also drive the Swagger docs)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.modules.payments.models import (
    Category,
    DisputeOutcome,
    DisputePriority,
    DisputeStatus,
    EscrowStatus,
    LedgerType,
    PaymentMethod,
    PayoutFrequency,
    PayoutStatus,
    TransactionKind,
    TxStatus,
)


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TransactionOut(_Orm):
    id: uuid.UUID
    ref: str = Field(examples=["ORD-80312"])
    kind: TransactionKind
    status: TxStatus
    escrow_status: EscrowStatus
    amount_minor: int = Field(description="Amount in kobo (1 naira = 100 kobo).")
    currency: str
    customer_id: uuid.UUID
    provider_id: uuid.UUID | None
    is_sos: bool
    payment_due: bool
    commission_minor: int | None
    created_at: datetime
    paid_at: datetime | None
    completed_at: datetime | None


class TransactionPage(BaseModel):
    items: list[TransactionOut]
    next_cursor: str | None = None


class AdminTransactionPage(BaseModel):
    items: list[TransactionOut]
    total: int
    page: int
    page_size: int


# --- Paying ------------------------------------------------------------------------------
class PayIn(BaseModel):
    transaction_id: uuid.UUID
    method: PaymentMethod = PaymentMethod.CARD


class PayOut(BaseModel):
    reference: str
    authorization_url: str = Field(description="Open this hosted checkout page to pay.")
    access_code: str | None = None


class VerifyOut(BaseModel):
    transaction: TransactionOut


# --- Wallet ------------------------------------------------------------------------------
class WalletOut(BaseModel):
    balance_minor: int
    queued_payout_minor: int
    currency: str = "NGN"


class LedgerEntryOut(_Orm):
    id: uuid.UUID
    type: LedgerType
    amount_minor: int = Field(description="Positive = credit, negative = debit.")
    balance_after_minor: int
    description: str
    created_at: datetime


class LedgerPage(BaseModel):
    items: list[LedgerEntryOut]
    next_cursor: str | None = None


class BankAccountIn(BaseModel):
    bank_name: str = Field(min_length=2, max_length=80, examples=["GTBank"])
    bank_code: str = Field(min_length=2, max_length=20, examples=["058"])
    account_number: str = Field(pattern=r"^\d{10}$", examples=["0123456789"])
    account_name: str = Field(min_length=2, max_length=120)


class BankAccountOut(_Orm):
    bank_name: str
    bank_code: str
    account_name: str
    account_last4: str = ""

    @classmethod
    def from_model(cls, account) -> BankAccountOut:
        """Never return the full account number."""
        return cls(
            bank_name=account.bank_name,
            bank_code=account.bank_code,
            account_name=account.account_name,
            account_last4=account.account_number[-4:],
        )


# --- Disputes ----------------------------------------------------------------------------
class DisputeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200, examples=["Part not as described"])


class DisputeMessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    internal: bool = Field(False, description="Staff-only note, hidden from the parties.")


class DisputeMessageOut(_Orm):
    id: uuid.UUID
    author_name: str
    body: str
    internal: bool
    created_at: datetime


class DisputeOut(_Orm):
    id: uuid.UUID
    ref: str = Field(examples=["DSP-0142"])
    transaction_id: uuid.UUID
    reason: str
    priority: DisputePriority
    status: DisputeStatus
    outcome: DisputeOutcome | None
    escrow_amount_minor: int
    created_at: datetime
    resolved_at: datetime | None


class DisputeDetailOut(DisputeOut):
    messages: list[DisputeMessageOut]


class ResolveIn(BaseModel):
    outcome: DisputeOutcome
    note: str | None = Field(None, max_length=500)


class DisputePage(BaseModel):
    items: list[DisputeOut]
    total: int
    counts: dict[str, int]


# --- Finance (admin) ---------------------------------------------------------------------
class CommissionOut(_Orm):
    category: Category
    rate_bps: int = Field(description="Basis points: 250 = 2.5%.")
    cap_minor: int | None = Field(description="Maximum commission per sale, in kobo.")
    uses_escrow: bool

    @computed_field
    @property
    def rate_percent(self) -> float:
        return self.rate_bps / 100


class CommissionIn(BaseModel):
    rate_bps: int = Field(ge=0, le=5000)
    cap_minor: int | None = Field(None, ge=0)
    uses_escrow: bool = True


class PayoutSettingsOut(_Orm):
    frequency: PayoutFrequency
    payout_weekday: int = Field(description="0 = Monday ... 6 = Sunday.")
    hold_days: int
    min_payout_minor: int
    max_cancel_fee_bps: int = Field(
        description="Highest late-cancellation fee a provider may set (5000 = 50%)."
    )


class PayoutSettingsIn(BaseModel):
    frequency: PayoutFrequency
    payout_weekday: int = Field(ge=0, le=6)
    hold_days: int = Field(ge=0, le=60)
    min_payout_minor: int = Field(ge=0)
    max_cancel_fee_bps: int | None = Field(
        None, ge=0, le=10_000, description="Leave out to keep the current cap."
    )


class CancellationPolicyIn(BaseModel):
    free_cancel_hours: int = Field(
        0, ge=0, le=720, description="Bookings: customers cancel free until this long before."
    )
    late_cancel_fee_bps: int = Field(
        0, ge=0, le=10_000, description="Bookings: share kept after that (2000 = 20%)."
    )
    order_cancel_window_hours: int | None = Field(
        None,
        ge=1,
        le=720,
        description="Parts: buyers may cancel this long after paying. Null = until it ships.",
    )


class CancellationPolicyOut(CancellationPolicyIn):
    max_late_cancel_fee_bps: int = Field(description="The platform's cap, for the form.")


class ReceiptLineOut(BaseModel):
    description: str
    amount_minor: int


class ReceiptOut(BaseModel):
    receipt_number: str = Field(examples=["RCT-80312"])
    ref: str
    kind: TransactionKind
    description: str
    issued_at: datetime
    customer_name: str
    provider_name: str | None
    lines: list[ReceiptLineOut]
    total_minor: int
    refunded_minor: int
    net_paid_minor: int
    currency: str
    payment_method: PaymentMethod | None
    payment_reference: str | None
    status: TxStatus
    escrow_status: EscrowStatus
    print_url: str = Field(description="Printable HTML version (Print / Save as PDF).")


class PayoutOut(_Orm):
    id: uuid.UUID
    provider_id: uuid.UUID
    amount_minor: int
    transactions_count: int
    due_date: date
    status: PayoutStatus
    bank_name: str | None
    paid_at: datetime | None
    failure_reason: str | None


class PayoutPage(BaseModel):
    items: list[PayoutOut]
    total: int


class JobRunOut(BaseModel):
    processed: int
