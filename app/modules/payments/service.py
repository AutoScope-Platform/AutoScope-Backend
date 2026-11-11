"""Payments business logic: transactions, gateway payments, escrow, wallet, payouts, disputes.

Other modules use only the functions in the "Transactions" section to create and update
their sale's transaction. Everything that moves money lives here, so the rules (commission,
escrow, refunds) are in one place and one test suite.

Money safety rules enforced below:
* Amounts are integers in kobo; commission uses integer arithmetic (rounded down).
* A transaction's escrow can only leave the `held` state once (release or refund), guarded by
  row locks (`SELECT ... FOR UPDATE`) so two requests cannot both pay out the same money.
* Webhook events are recorded by id so a re-sent webhook changes nothing.
* A payment succeeds only if the gateway confirms the amount we asked for.

Section map:
    1. Settings               - commission rates, payout settings
    2. Transactions           - used by orders, booking, insurance
    3. Paying                 - start a payment, confirm it, webhooks
    4. Escrow                 - hold, release, refund
    5. Wallet and payouts     - FR-9.5, FR-13.3
    6. Disputes               - FR-13.4
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.audit import AuditCategory
from app.core.db import utcnow
from app.core.errors import BadRequest, Conflict, Forbidden, NotFound, UnprocessableEntity
from app.core.events import publish
from app.core.pagination import paginate_cursor, paginate_page
from app.integrations import registry
from app.integrations.base import IntegrationError
from app.modules.identity.models import User
from app.modules.payments.models import (
    FINAL_STATUSES,
    REF_PREFIX,
    BankAccount,
    Category,
    CommissionRate,
    Dispute,
    DisputeMessage,
    DisputeOutcome,
    DisputePriority,
    DisputeStatus,
    EscrowStatus,
    LedgerEntry,
    LedgerType,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Payout,
    PayoutFrequency,
    PayoutSettings,
    PayoutStatus,
    Refund,
    Transaction,
    TransactionKind,
    TxStatus,
    Wallet,
    WebhookEvent,
)

# Disputes on sales at or above this amount (N10,000,000) are flagged high priority.
HIGH_VALUE_MINOR = 1_000_000_000

# Starting commission rates, matching the admin dashboard. Admins can change them.
DEFAULT_RATES: dict[Category, tuple[int, int | None]] = {
    Category.VEHICLES: (250, 50_000_000),  # 2.5%, capped at N500,000
    Category.PARTS: (800, None),
    Category.MECHANIC_SERVICES: (1000, None),
    Category.HAULAGE: (1200, None),
    Category.INSURANCE: (500, None),
}


# ======================================================================================
# 1. Settings
# ======================================================================================
def get_commission(db: Session, category: Category) -> CommissionRate:
    """The rate row for a category, created with the default if it does not exist yet."""
    row = db.get(CommissionRate, category)
    if row is None:
        bps, cap = DEFAULT_RATES[category]
        row = CommissionRate(category=category, rate_bps=bps, cap_minor=cap, uses_escrow=True)
        db.add(row)
        db.flush()
    return row


def list_commissions(db: Session) -> list[CommissionRate]:
    return [get_commission(db, c) for c in Category]


def update_commission(
    db: Session,
    category: Category,
    *,
    rate_bps: int,
    cap_minor: int | None,
    uses_escrow: bool,
    admin: User,
    ip: str | None,
) -> CommissionRate:
    """Change a category's commission. Applies to sales paid from now on only."""
    if not 0 <= rate_bps <= 5000:
        raise BadRequest("Commission must be between 0% and 50%", code="invalid_rate")
    if cap_minor is not None and cap_minor < 0:
        raise BadRequest("The cap cannot be negative", code="invalid_cap")
    row = get_commission(db, category)
    row.rate_bps, row.cap_minor, row.uses_escrow = rate_bps, cap_minor, uses_escrow
    row.updated_at = utcnow()
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Changed commission rate",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=category.value,
        ip_address=ip,
    )
    db.commit()
    return row


def get_payout_settings(db: Session) -> PayoutSettings:
    row = db.get(PayoutSettings, 1)
    if row is None:
        row = PayoutSettings(id=1)
        db.add(row)
        db.flush()
    return row


def update_payout_settings(
    db: Session,
    *,
    frequency: PayoutFrequency,
    payout_weekday: int,
    hold_days: int,
    min_payout_minor: int,
    admin: User,
    ip: str | None,
) -> PayoutSettings:
    if not 0 <= payout_weekday <= 6 or hold_days < 0 or min_payout_minor < 0:
        raise BadRequest("Invalid payout settings", code="invalid_settings")
    row = get_payout_settings(db)
    row.frequency, row.payout_weekday = frequency, payout_weekday
    row.hold_days, row.min_payout_minor = hold_days, min_payout_minor
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Changed payout schedule",
        actor_id=admin.id,
        actor_name=admin.full_name,
        ip_address=ip,
    )
    db.commit()
    return row


def compute_commission(amount_minor: int, rate_bps: int, cap_minor: int | None) -> int:
    """Commission in kobo: rate applied with integer maths (rounded down), then capped."""
    commission = amount_minor * rate_bps // 10_000
    return min(commission, cap_minor) if cap_minor is not None else commission


# ======================================================================================
# 2. Transactions (the interface other modules use)
# ======================================================================================
def create_transaction(
    db: Session,
    *,
    kind: TransactionKind,
    customer_id: uuid.UUID,
    provider_id: uuid.UUID | None,
    amount_minor: int,
    status: TxStatus = TxStatus.PENDING,
    source_id: uuid.UUID | None = None,
    is_sos: bool = False,
    payment_due: bool = False,
) -> Transaction:
    """Add a transaction to the session (flushed, not committed: the caller commits)."""
    if amount_minor < 0:
        raise BadRequest("Amount cannot be negative", code="invalid_amount")
    tx = Transaction(
        kind=kind,
        customer_id=customer_id,
        provider_id=provider_id,
        amount_minor=amount_minor,
        status=status,
        source_id=source_id,
        is_sos=is_sos,
        payment_due=payment_due,
    )
    db.add(tx)
    db.flush()  # assigns the reference number
    return tx


def get_transaction(db: Session, tx_id: uuid.UUID, *, lock: bool = False) -> Transaction:
    stmt = select(Transaction).where(Transaction.id == tx_id)
    if lock:
        stmt = stmt.with_for_update()
    tx = db.scalar(stmt)
    if tx is None:
        raise NotFound("Transaction not found", code="transaction_not_found")
    return tx


def get_transaction_by_ref(db: Session, ref: str) -> Transaction:
    """Look up by display reference such as ``ORD-80312``."""
    match = re.fullmatch(r"([A-Z]{3})-(\d+)", ref.strip().upper())
    prefix_to_kind = {v: k for k, v in REF_PREFIX.items()}
    if not match or match.group(1) not in prefix_to_kind:
        raise NotFound("Transaction not found", code="transaction_not_found")
    tx = db.scalar(
        select(Transaction).where(
            Transaction.kind == prefix_to_kind[match.group(1)],
            Transaction.ref_no == int(match.group(2)),
        )
    )
    if tx is None:
        raise NotFound("Transaction not found", code="transaction_not_found")
    return tx


def set_status(db: Session, tx: Transaction, status: TxStatus) -> None:
    """Move a transaction to a new status (called by the owning module)."""
    tx.status = status
    db.flush()


def request_payment(db: Session, tx: Transaction, *, amount_minor: int | None = None) -> None:
    """Mark a transaction as ready for the customer to pay (optionally fixing its final amount)."""
    if tx.paid_at is not None:
        raise Conflict("This transaction is already paid", code="already_paid")
    if amount_minor is not None:
        tx.amount_minor = amount_minor
    tx.payment_due = True
    db.flush()


def mark_completed(db: Session, tx: Transaction, status: TxStatus = TxStatus.COMPLETED) -> None:
    """The provider finished the work. Starts the dispute window; escrow releases after it."""
    tx.status = status
    tx.completed_at = tx.completed_at or utcnow()
    db.flush()


def list_for_user(
    db: Session,
    user: User,
    *,
    as_provider: bool,
    kind: TransactionKind | None,
    status: TxStatus | None,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Transaction], str | None]:
    """'My Activity' (FR-10.1) for customers and the order/booking/job queue (FR-10.4) for providers."""
    column = Transaction.provider_id if as_provider else Transaction.customer_id
    stmt = select(Transaction).where(column == user.id)
    if kind:
        stmt = stmt.where(Transaction.kind == kind)
    if status:
        stmt = stmt.where(Transaction.status == status)
    if date_from:
        stmt = stmt.where(Transaction.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Transaction.created_at <= date_to)
    return paginate_cursor(db, stmt, Transaction, limit=limit, cursor=cursor)


def can_view(tx: Transaction, user: User) -> bool:
    """A transaction is visible to its two parties and to staff."""
    from app.core.permissions import ADMIN_ROLES  # noqa: PLC0415

    return user.id in (tx.customer_id, tx.provider_id) or user.has_role(*ADMIN_ROLES)


def search_all(
    db: Session,
    *,
    kind: TransactionKind | None,
    status: TxStatus | None,
    q: str | None,
    page: int,
    page_size: int,
) -> tuple[list[Transaction], int]:
    """Admin 'Orders, bookings and jobs' table."""
    stmt = select(Transaction)
    if kind:
        stmt = stmt.where(Transaction.kind == kind)
    if status:
        stmt = stmt.where(Transaction.status == status)
    if q and (m := re.search(r"(\d{3,})", q)):
        stmt = stmt.where(Transaction.ref_no == int(m.group(1)))
    return paginate_page(
        db, stmt.order_by(Transaction.created_at.desc()), page=page, page_size=page_size
    )


# ======================================================================================
# 3. Paying
# ======================================================================================
def start_payment(
    db: Session, user: User, tx_id: uuid.UUID, method: PaymentMethod
) -> tuple[Payment, str, str | None]:
    """Create a gateway payment for a transaction. Returns (payment, checkout URL, access code).

    The app opens the checkout URL (the gateway's hosted page) so card details never touch us.
    Does not commit: `run_idempotent` commits together with the stored response.
    """
    tx = get_transaction(db, tx_id, lock=True)
    if tx.customer_id != user.id:
        raise Forbidden("This is not your transaction", code="forbidden")
    if tx.paid_at is not None:
        raise Conflict("This transaction is already paid", code="already_paid")
    if not tx.payment_due or tx.status in FINAL_STATUSES:
        raise UnprocessableEntity("This transaction is not ready for payment", code="not_payable")
    if tx.amount_minor <= 0:
        raise UnprocessableEntity("There is nothing to pay", code="not_payable")

    gateway = registry.get_payments()
    reference = f"AS-{tx.ref}-{uuid.uuid4().hex[:8]}"
    email = user.email or f"{user.id}@users.autoscope.ng"  # gateways require an email address
    try:
        init = gateway.initialize(
            reference=reference,
            amount_minor=tx.amount_minor,
            email=email,
            currency=tx.currency,
            metadata={"transaction": tx.ref},
        )
    except IntegrationError as exc:
        raise UnprocessableEntity(
            "The payment service is unavailable. Try again shortly.", code="gateway_unavailable"
        ) from exc
    payment = Payment(
        transaction_id=tx.id,
        payer_id=user.id,
        amount_minor=tx.amount_minor,
        currency=tx.currency,
        method=method,
        gateway=gateway.name,
        reference=reference,
    )
    db.add(payment)
    db.flush()
    return payment, init.authorization_url, init.access_code


def confirm_payment(db: Session, reference: str) -> Transaction:
    """Ask the gateway whether a payment succeeded and apply the result (idempotent).

    Called by the client after the hosted checkout closes (`POST /payments/{ref}/verify`)
    and by the webhook handler.
    """
    payment = db.scalar(select(Payment).where(Payment.reference == reference).with_for_update())
    if payment is None:
        raise NotFound("Payment not found", code="payment_not_found")
    tx = get_transaction(db, payment.transaction_id, lock=True)
    if payment.status == PaymentStatus.SUCCEEDED:
        return tx  # already applied
    try:
        result = registry.get_payments().verify(reference)
    except IntegrationError as exc:
        raise UnprocessableEntity(
            "We could not confirm the payment yet. Try again shortly.", code="gateway_unavailable"
        ) from exc
    if result.status == "success":
        if result.amount_minor != payment.amount_minor:
            # Never mark a short payment as paid; flag it for a human.
            payment.status = PaymentStatus.FAILED
            db.commit()
            raise UnprocessableEntity("The amount paid does not match", code="amount_mismatch")
        _apply_successful_payment(db, payment, tx)
        db.commit()
        publish("payment.succeeded", transaction_id=str(tx.id), ref=tx.ref, kind=tx.kind.value)
    elif result.status == "failed":
        payment.status = PaymentStatus.FAILED
        db.commit()
    return tx


def _apply_successful_payment(db: Session, payment: Payment, tx: Transaction) -> None:
    """Mark paid, snapshot the commission terms, and hold the money in escrow (or settle it)."""
    now = utcnow()
    payment.status, payment.paid_at = PaymentStatus.SUCCEEDED, now
    rate = get_commission(db, tx.category)
    tx.paid_at, tx.payment_due = now, False
    tx.commission_rate_bps, tx.commission_cap_minor = rate.rate_bps, rate.cap_minor
    tx.uses_escrow = rate.uses_escrow
    if rate.uses_escrow:
        tx.escrow_status = EscrowStatus.HELD
    else:
        # No escrow for this category: the provider is credited straight away.
        _settle_to_provider(db, tx, tx.amount_minor)
    db.flush()


def handle_webhook(db: Session, gateway_name: str, signature: str | None, body: bytes) -> None:
    """Process a gateway callback: check the signature, ignore repeats, confirm the payment."""
    gateway = registry.get_payments()
    if gateway.name != gateway_name or not gateway.verify_webhook(signature, body):
        raise Forbidden("Invalid webhook signature", code="invalid_signature")
    event = gateway.parse_webhook(body)
    if db.get(WebhookEvent, event.event_id):
        return  # already handled
    db.add(WebhookEvent(event_id=event.event_id, gateway=gateway_name, received_at=utcnow()))
    db.commit()
    if event.kind == "charge.success" and event.reference:
        try:
            confirm_payment(db, event.reference)
        except NotFound:
            return  # a reference we do not know: acknowledge so the gateway stops retrying


# ======================================================================================
# 4. Escrow
# ======================================================================================
def _wallet(db: Session, user_id: uuid.UUID) -> Wallet:
    wallet = db.scalar(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
    if wallet is None:
        wallet = Wallet(user_id=user_id, balance_minor=0)
        db.add(wallet)
        db.flush()
    return wallet


def _ledger(
    db: Session,
    user_id: uuid.UUID,
    entry_type: LedgerType,
    amount: int,
    description: str,
    *,
    transaction_id: uuid.UUID | None = None,
    payout_id: uuid.UUID | None = None,
) -> None:
    wallet = _wallet(db, user_id)
    wallet.balance_minor += amount
    if wallet.balance_minor < 0:
        raise UnprocessableEntity("Insufficient wallet balance", code="insufficient_balance")
    db.add(
        LedgerEntry(
            user_id=user_id,
            type=entry_type,
            amount_minor=amount,
            balance_after_minor=wallet.balance_minor,
            transaction_id=transaction_id,
            payout_id=payout_id,
            description=description,
        )
    )


def _settle_to_provider(db: Session, tx: Transaction, amount_minor: int) -> int:
    """Credit the provider `amount_minor` minus commission. Returns the commission taken."""
    commission = compute_commission(
        amount_minor, tx.commission_rate_bps or 0, tx.commission_cap_minor
    )
    net = amount_minor - commission
    tx.commission_minor = (tx.commission_minor or 0) + commission
    tx.provider_net_minor = (tx.provider_net_minor or 0) + net
    tx.released_at = utcnow()
    if tx.provider_id and net > 0:
        _ledger(
            db, tx.provider_id, LedgerType.SALE, net, f"Earnings for {tx.ref}", transaction_id=tx.id
        )
    return commission


def release_escrow(db: Session, tx_id: uuid.UUID, *, actor: User | None = None) -> Transaction:
    """Pay the provider for a held transaction (minus commission)."""
    tx = get_transaction(db, tx_id, lock=True)
    if tx.escrow_status != EscrowStatus.HELD:
        raise UnprocessableEntity("Nothing is held in escrow for this transaction", code="not_held")
    _settle_to_provider(db, tx, tx.amount_minor)
    tx.escrow_status = EscrowStatus.RELEASED
    db.commit()
    publish("escrow.released", transaction_id=str(tx.id), ref=tx.ref)
    return tx


def _refund(db: Session, tx: Transaction, amount_minor: int, reason: str) -> None:
    """Send `amount_minor` back to the customer through the gateway."""
    payment = db.scalar(
        select(Payment).where(
            Payment.transaction_id == tx.id, Payment.status == PaymentStatus.SUCCEEDED
        )
    )
    if payment is None:
        raise UnprocessableEntity("There is no payment to refund", code="no_payment")
    try:
        ref = registry.get_payments().refund(payment.reference, amount_minor)
    except IntegrationError as exc:
        raise UnprocessableEntity(
            "The refund could not be sent. Nothing was changed.", code="gateway_unavailable"
        ) from exc
    db.add(
        Refund(
            transaction_id=tx.id,
            payment_id=payment.id,
            amount_minor=amount_minor,
            gateway_reference=ref,
            reason=reason[:200],
        )
    )


def refund_escrow(
    db: Session, tx_id: uuid.UUID, *, reason: str, commit: bool = True
) -> Transaction:
    """Return a held transaction's money to the customer in full (cancellation or dispute)."""
    tx = get_transaction(db, tx_id, lock=True)
    if tx.escrow_status not in (EscrowStatus.HELD, EscrowStatus.FROZEN):
        raise UnprocessableEntity("Nothing is held in escrow for this transaction", code="not_held")
    _refund(db, tx, tx.amount_minor, reason)
    tx.escrow_status = EscrowStatus.REFUNDED
    if commit:
        db.commit()
        publish("escrow.refunded", transaction_id=str(tx.id), ref=tx.ref)
    return tx


def cancel_and_refund(db: Session, tx: Transaction, *, reason: str) -> None:
    """Used by owning modules when a customer cancels. Refunds if money was paid; no commit."""
    tx.payment_due = False
    tx.status = TxStatus.CANCELLED
    if tx.paid_at is not None and tx.escrow_status in (EscrowStatus.HELD, EscrowStatus.FROZEN):
        refund_escrow(db, tx.id, reason=reason, commit=False)
    db.flush()


def release_due_escrow(db: Session, *, now: datetime | None = None) -> int:
    """Release every held transaction whose dispute window has passed. Returns how many.

    Run by the scheduled job (`app/worker.py`) and by the admin 'run now' button.
    """
    now = now or utcnow()
    hold = timedelta(days=get_payout_settings(db).hold_days)
    open_dispute = select(Dispute.transaction_id).where(Dispute.status != DisputeStatus.RESOLVED)
    due_ids = db.scalars(
        select(Transaction.id).where(
            Transaction.escrow_status == EscrowStatus.HELD,
            Transaction.completed_at.is_not(None),
            Transaction.completed_at <= now - hold,
            Transaction.id.not_in(open_dispute),
        )
    ).all()
    released = 0
    for tx_id in due_ids:
        try:
            release_escrow(db, tx_id)
            released += 1
        except UnprocessableEntity:
            db.rollback()
    return released


# ======================================================================================
# 5. Wallet and payouts
# ======================================================================================
def wallet_summary(db: Session, user: User) -> tuple[Wallet, int]:
    """(wallet, amount currently queued for payout)."""
    wallet = _wallet(db, user.id)
    queued = db.scalar(
        select(func.coalesce(func.sum(Payout.amount_minor), 0)).where(
            Payout.provider_id == user.id,
            Payout.status.in_([PayoutStatus.SCHEDULED, PayoutStatus.ON_HOLD]),
        )
    )
    return wallet, int(queued or 0)


def list_ledger(
    db: Session, user: User, *, limit: int, cursor: str | None
) -> tuple[list[LedgerEntry], str | None]:
    return paginate_cursor(
        db,
        select(LedgerEntry).where(LedgerEntry.user_id == user.id),
        LedgerEntry,
        limit=limit,
        cursor=cursor,
    )


def set_bank_account(
    db: Session,
    user: User,
    *,
    bank_name: str,
    bank_code: str,
    account_number: str,
    account_name: str,
) -> BankAccount:
    if not re.fullmatch(r"\d{10}", account_number):
        raise BadRequest("Account numbers have 10 digits", code="invalid_account_number")
    account = db.scalar(select(BankAccount).where(BankAccount.user_id == user.id))
    if account is None:
        account = BankAccount(user_id=user.id)
        db.add(account)
    account.bank_name, account.bank_code = bank_name, bank_code
    account.account_number, account.account_name = account_number, account_name
    db.commit()
    return account


def get_bank_account(db: Session, user_id: uuid.UUID) -> BankAccount | None:
    return db.scalar(select(BankAccount).where(BankAccount.user_id == user_id))


def next_payout_date(settings: PayoutSettings, today: date) -> date:
    """The next date a payout run happens, given the weekday setting."""
    days_ahead = (settings.payout_weekday - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


def schedule_payouts(db: Session, *, today: date | None = None) -> int:
    """Turn wallet balances above the minimum into scheduled payouts. Returns how many.

    The balance moves out of the wallet into the payout at once, so it cannot be scheduled
    twice. Providers without a bank account are skipped until they add one.
    """
    today = today or datetime.now(UTC).date()
    settings = get_payout_settings(db)
    due = next_payout_date(settings, today)
    created = 0
    for wallet in db.scalars(
        select(Wallet).where(Wallet.balance_minor >= max(settings.min_payout_minor, 1))
    ).all():
        bank = get_bank_account(db, wallet.user_id)
        if bank is None:
            continue
        count = (
            db.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.user_id == wallet.user_id, LedgerEntry.type == LedgerType.SALE)
            )
            or 0
        )
        payout = Payout(
            provider_id=wallet.user_id,
            amount_minor=wallet.balance_minor,
            transactions_count=count,
            due_date=due,
            bank_name=bank.bank_name,
        )
        db.add(payout)
        db.flush()
        _ledger(
            db,
            wallet.user_id,
            LedgerType.PAYOUT,
            -payout.amount_minor,
            f"Payout {payout.id.hex[:8]}",
            payout_id=payout.id,
        )
        created += 1
    db.commit()
    return created


def _get_payout(db: Session, payout_id: uuid.UUID) -> Payout:
    payout = db.scalar(select(Payout).where(Payout.id == payout_id).with_for_update())
    if payout is None:
        raise NotFound("Payout not found", code="payout_not_found")
    return payout


def hold_payout(db: Session, payout_id: uuid.UUID, *, admin: User, ip: str | None) -> Payout:
    payout = _get_payout(db, payout_id)
    if payout.status != PayoutStatus.SCHEDULED:
        raise UnprocessableEntity("Only scheduled payouts can be put on hold", code="invalid_state")
    payout.status = PayoutStatus.ON_HOLD
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Held payout",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=str(payout.id)[:8],
        ip_address=ip,
    )
    db.commit()
    return payout


def release_payout(db: Session, payout_id: uuid.UUID, *, admin: User, ip: str | None) -> Payout:
    """'Release now': send the money immediately, even if on hold or not yet due."""
    payout = _get_payout(db, payout_id)
    if payout.status not in (PayoutStatus.SCHEDULED, PayoutStatus.ON_HOLD):
        raise UnprocessableEntity("This payout was already sent", code="invalid_state")
    _send_payout(db, payout)
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Released payout",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=str(payout.id)[:8],
        ip_address=ip,
    )
    db.commit()
    return payout


def _send_payout(db: Session, payout: Payout) -> None:
    """Pay out through the gateway. On failure the money goes back to the provider's wallet."""
    bank = get_bank_account(db, payout.provider_id)
    try:
        if bank is None:
            raise IntegrationError("No bank account on file")
        payout.gateway_reference = registry.get_payments().payout(
            reference=f"PO-{payout.id.hex[:16]}",
            amount_minor=payout.amount_minor,
            bank_code=bank.bank_code,
            account_number=bank.account_number,
            account_name=bank.account_name,
        )
        payout.status, payout.paid_at = PayoutStatus.PAID, utcnow()
    except IntegrationError as exc:
        payout.status, payout.failure_reason = PayoutStatus.FAILED, str(exc)[:200]
        _ledger(
            db,
            payout.provider_id,
            LedgerType.PAYOUT_REVERSAL,
            payout.amount_minor,
            "Payout failed, returned to wallet",
            payout_id=payout.id,
        )


def run_due_payouts(db: Session, *, today: date | None = None) -> int:
    """Send every scheduled (not held) payout that is due. Returns how many were paid."""
    today = today or datetime.now(UTC).date()
    paid = 0
    for payout in db.scalars(
        select(Payout).where(Payout.status == PayoutStatus.SCHEDULED, Payout.due_date <= today)
    ).all():
        _send_payout(db, payout)
        paid += payout.status == PayoutStatus.PAID
    db.commit()
    return paid


def list_payouts(
    db: Session, *, status: PayoutStatus | None, page: int, page_size: int
) -> tuple[list[Payout], int]:
    stmt = select(Payout)
    if status:
        stmt = stmt.where(Payout.status == status)
    return paginate_page(
        db, stmt.order_by(Payout.due_date, Payout.created_at), page=page, page_size=page_size
    )


# ======================================================================================
# 6. Disputes (FR-13.4)
# ======================================================================================
def open_dispute(db: Session, user: User, tx_id: uuid.UUID, reason: str) -> Dispute:
    """Either party can dispute a paid transaction while its money is still in escrow.

    Opening a dispute freezes the escrow so the automatic release cannot pay it out.
    """
    tx = get_transaction(db, tx_id, lock=True)
    if user.id not in (tx.customer_id, tx.provider_id):
        raise Forbidden("You are not part of this transaction", code="forbidden")
    if tx.escrow_status != EscrowStatus.HELD:
        raise UnprocessableEntity("This transaction cannot be disputed now", code="not_disputable")
    if tx.provider_id is None:
        raise UnprocessableEntity("This transaction has no provider", code="not_disputable")
    dispute = Dispute(
        transaction_id=tx.id,
        customer_id=tx.customer_id,
        provider_id=tx.provider_id,
        opened_by_id=user.id,
        reason=reason.strip()[:200],
        priority=DisputePriority.HIGH
        if tx.amount_minor >= HIGH_VALUE_MINOR or tx.is_sos
        else DisputePriority.NORMAL,
        escrow_amount_minor=tx.amount_minor,
    )
    db.add(dispute)
    tx.escrow_status = EscrowStatus.FROZEN
    db.flush()
    db.add(
        DisputeMessage(
            dispute_id=dispute.id,
            author_id=user.id,
            author_name=user.display_name,
            body=f"Opened dispute: {dispute.reason.lower()}.",
        )
    )
    db.commit()
    publish(
        "dispute.opened", dispute_id=str(dispute.id), transaction_id=str(tx.id), ref=dispute.ref
    )
    return dispute


def get_dispute(db: Session, dispute_id: uuid.UUID) -> Dispute:
    dispute = db.get(Dispute, dispute_id)
    if dispute is None:
        raise NotFound("Dispute not found", code="dispute_not_found")
    return dispute


def dispute_messages(
    db: Session, dispute: Dispute, *, include_internal: bool
) -> list[DisputeMessage]:
    stmt = select(DisputeMessage).where(DisputeMessage.dispute_id == dispute.id)
    if not include_internal:
        stmt = stmt.where(DisputeMessage.internal.is_(False))
    return list(db.scalars(stmt.order_by(DisputeMessage.created_at)))


def add_dispute_message(
    db: Session, dispute: Dispute, author: User, body: str, *, internal: bool
) -> DisputeMessage:
    from app.core.permissions import ADMIN_ROLES  # noqa: PLC0415

    is_staff = author.has_role(*ADMIN_ROLES)
    if not is_staff and author.id not in (dispute.customer_id, dispute.provider_id):
        raise Forbidden("You are not part of this dispute", code="forbidden")
    if internal and not is_staff:
        raise Forbidden("Only staff can add internal notes", code="forbidden")
    if dispute.status == DisputeStatus.RESOLVED:
        raise UnprocessableEntity("This dispute is closed", code="dispute_closed")
    message = DisputeMessage(
        dispute_id=dispute.id,
        author_id=author.id,
        author_name=f"{author.full_name} (Support)" if is_staff else author.display_name,
        body=body.strip(),
        internal=internal,
    )
    db.add(message)
    if is_staff and dispute.status == DisputeStatus.OPEN:
        dispute.status = DisputeStatus.INVESTIGATING
    db.commit()
    if not internal:
        publish("dispute.message", dispute_id=str(dispute.id), ref=dispute.ref)
    return message


def list_disputes(
    db: Session, *, status: DisputeStatus | None, page: int, page_size: int
) -> tuple[list[Dispute], int]:
    stmt = select(Dispute)
    if status == DisputeStatus.RESOLVED:
        stmt = stmt.where(Dispute.status == DisputeStatus.RESOLVED)
    elif status:
        stmt = stmt.where(Dispute.status == status)
    # Priority is stored as text ('high' sorts before 'normal' only alphabetically by luck),
    # so rank it explicitly: high first, then oldest first.
    high_first = case((Dispute.priority == DisputePriority.HIGH, 0), else_=1)
    order = high_first, Dispute.created_at.asc()
    return paginate_page(db, stmt.order_by(*order), page=page, page_size=page_size)


def dispute_counts(db: Session) -> dict[str, int]:
    open_count = db.scalar(
        select(func.count()).select_from(Dispute).where(Dispute.status != DisputeStatus.RESOLVED)
    )
    resolved = db.scalar(
        select(func.count()).select_from(Dispute).where(Dispute.status == DisputeStatus.RESOLVED)
    )
    return {"open": int(open_count or 0), "resolved": int(resolved or 0)}


def resolve_dispute(
    db: Session,
    dispute_id: uuid.UUID,
    outcome: DisputeOutcome,
    *,
    admin: User,
    note: str | None,
    ip: str | None,
) -> Dispute:
    """Settle the escrow: refund the buyer, pay the provider, or split 50/50."""
    dispute = get_dispute(db, dispute_id)
    if dispute.status == DisputeStatus.RESOLVED:
        raise UnprocessableEntity("This dispute is already resolved", code="dispute_closed")
    tx = get_transaction(db, dispute.transaction_id, lock=True)
    if tx.escrow_status != EscrowStatus.FROZEN:
        raise UnprocessableEntity("The escrow is not frozen", code="not_frozen")

    if outcome == DisputeOutcome.REFUND_BUYER:
        _refund(db, tx, tx.amount_minor, f"Dispute {dispute.ref}: refunded")
        tx.escrow_status = EscrowStatus.REFUNDED
    elif outcome == DisputeOutcome.RELEASE_PROVIDER:
        _settle_to_provider(db, tx, tx.amount_minor)
        tx.escrow_status = EscrowStatus.RELEASED
    else:  # split 50/50: the buyer gets the rounded-down half, the provider the rest
        refund_part = tx.amount_minor // 2
        _refund(db, tx, refund_part, f"Dispute {dispute.ref}: split")
        _settle_to_provider(db, tx, tx.amount_minor - refund_part)
        tx.escrow_status = EscrowStatus.RELEASED

    dispute.status, dispute.outcome = DisputeStatus.RESOLVED, outcome
    dispute.resolved_by_id, dispute.resolved_at = admin.id, utcnow()
    labels = {
        DisputeOutcome.REFUND_BUYER: "Refunded buyer in full",
        DisputeOutcome.RELEASE_PROVIDER: "Released funds to provider",
        DisputeOutcome.SPLIT: "Split 50/50",
    }
    db.add(
        DisputeMessage(
            dispute_id=dispute.id,
            author_id=admin.id,
            author_name=f"{admin.full_name} (Support)",
            body=f"{labels[outcome]}." + (f" {note.strip()}" if note else ""),
        )
    )
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Resolved dispute",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=dispute.ref,
        ip_address=ip,
    )
    db.commit()
    publish("dispute.resolved", dispute_id=str(dispute.id), ref=dispute.ref, outcome=outcome.value)
    return dispute
