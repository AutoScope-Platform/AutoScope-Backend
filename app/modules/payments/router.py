"""Payments endpoints: paying, activity, wallet, bank account, disputes and the finance console.

Routes are grouped as: customer/provider endpoints first, then `/admin/...` ones
(staff only; finance actions need the Super admin role, FSD 8).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import Forbidden, error_responses
from app.core.idempotency import run_idempotent
from app.core.permissions import ADMIN_ROLES, Capability
from app.core.ratelimit import client_ip
from app.modules.identity.deps import (
    CurrentUser,
    require_admin,
    require_capability,
    require_super_admin,
    user_rate_limit,
)
from app.modules.identity.models import User
from app.modules.payments import service
from app.modules.payments.models import (
    Category,
    DisputeStatus,
    PayoutStatus,
    TransactionKind,
    TxStatus,
)
from app.modules.payments.schemas import (
    AdminTransactionPage,
    BankAccountIn,
    BankAccountOut,
    CancellationPolicyIn,
    CancellationPolicyOut,
    CommissionIn,
    CommissionOut,
    DisputeDetailOut,
    DisputeIn,
    DisputeMessageIn,
    DisputeMessageOut,
    DisputeOut,
    DisputePage,
    JobRunOut,
    LedgerPage,
    PayIn,
    PayOut,
    PayoutOut,
    PayoutPage,
    PayoutSettingsIn,
    PayoutSettingsOut,
    ReceiptOut,
    ResolveIn,
    TransactionOut,
    TransactionPage,
    VerifyOut,
    WalletOut,
)

router = APIRouter(tags=["Payments"])


# --- Paying ------------------------------------------------------------------------------
@router.post(
    "/payments",
    response_model=PayOut,
    summary="Start paying for a transaction",
    description="Returns the gateway's hosted checkout URL. Requires an `Idempotency-Key` "
    "header so a retry never creates a second payment.",
    responses=error_responses(400, 403, 409, 422, 429),
    dependencies=[Depends(user_rate_limit("pay", 10, 60))],
)
def pay(
    body: PayIn,
    user: CurrentUser,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    def operation() -> dict:
        payment, url, code = service.start_payment(db, user, body.transaction_id, body.method)
        return {"reference": payment.reference, "authorization_url": url, "access_code": code}

    return run_idempotent(
        db, user.id, idempotency_key, "pay", body.model_dump(mode="json"), operation
    )


@router.post(
    "/payments/{reference}/verify",
    response_model=VerifyOut,
    summary="Confirm a payment after checkout",
    description="Call when the hosted checkout closes. Safe to call repeatedly.",
    responses=error_responses(404, 422, 429),
    dependencies=[Depends(user_rate_limit("pay-verify", 30, 60))],
)
def verify(reference: str, user: CurrentUser, db: Session = Depends(get_db)) -> VerifyOut:
    tx = service.confirm_payment(db, reference)
    if not service.can_view(tx, user):
        raise Forbidden("This is not your payment", code="forbidden")
    return VerifyOut(transaction=TransactionOut.model_validate(tx))


@router.post(
    "/payments/webhooks/{gateway}",
    status_code=200,
    summary="Gateway webhook",
    description="Called by the payment gateway, not by apps. The signature header is checked.",
    responses=error_responses(403),
)
async def webhook(gateway: str, request: Request, db: Session = Depends(get_db)) -> dict:
    body = await request.body()
    signature = request.headers.get("x-paystack-signature") or request.headers.get("x-signature")
    service.handle_webhook(db, gateway, signature, body)
    return {"received": True}


# --- Activity ----------------------------------------------------------------------------
@router.get(
    "/activity",
    response_model=TransactionPage,
    summary="My activity (orders, bookings, jobs, policies)",
    description="Customers see what they bought; with `as_provider=true` providers see "
    "the orders, bookings and jobs they received.",
)
def activity(
    user: CurrentUser,
    as_provider: bool = False,
    kind: TransactionKind | None = None,
    status: TxStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
    db: Session = Depends(get_db),
) -> TransactionPage:
    rows, next_cursor = service.list_for_user(
        db, user, as_provider=as_provider, kind=kind, status=status,
        date_from=date_from, date_to=date_to, limit=limit, cursor=cursor,
    )  # fmt: skip
    return TransactionPage(
        items=[TransactionOut.model_validate(r) for r in rows], next_cursor=next_cursor
    )


@router.get(
    "/transactions/{reference}",
    response_model=TransactionOut,
    summary="One transaction by reference (e.g. ORD-80312)",
    responses=error_responses(403, 404),
)
def get_transaction(reference: str, user: CurrentUser, db: Session = Depends(get_db)):
    tx = service.get_transaction_by_ref(db, reference)
    if not service.can_view(tx, user):
        raise Forbidden("This is not your transaction", code="forbidden")
    return tx


def _visible_tx(db: Session, reference: str, user: User):
    tx = service.get_transaction_by_ref(db, reference)
    if not service.can_view(tx, user):
        raise Forbidden("This is not your transaction", code="forbidden")
    return tx


@router.get(
    "/transactions/{reference}/receipt",
    response_model=ReceiptOut,
    summary="Receipt for a paid order, booking, job or policy (FR-9.3)",
    description="For the customer, the provider and staff. `print_url` is a printable version.",
    responses=error_responses(403, 404, 422),
)
def receipt(reference: str, user: CurrentUser, db: Session = Depends(get_db)) -> ReceiptOut:
    tx = _visible_tx(db, reference, user)
    return ReceiptOut(
        **service.build_receipt(db, tx), print_url=f"/api/v1/transactions/{tx.ref}/receipt/print"
    )


@router.get(
    "/transactions/{reference}/receipt/print",
    response_class=HTMLResponse,
    summary="Printable receipt (open it and Print / Save as PDF)",
    responses=error_responses(403, 404, 422),
)
def receipt_print(reference: str, user: CurrentUser, db: Session = Depends(get_db)):
    return HTMLResponse(
        service.receipt_html(service.build_receipt(db, _visible_tx(db, reference, user)))
    )


# --- Cancellation policy -----------------------------------------------------------------
BusinessUser = Depends(require_capability(Capability.VIEW_OWN_ANALYTICS))  # any business role


def _policy_out(db: Session, user: User) -> CancellationPolicyOut:
    policy = service.get_cancellation_policy(db, user.id)
    cap = service.get_payout_settings(db).max_cancel_fee_bps
    if policy is None:
        return CancellationPolicyOut(max_late_cancel_fee_bps=cap)
    return CancellationPolicyOut(
        free_cancel_hours=policy.free_cancel_hours,
        late_cancel_fee_bps=policy.late_cancel_fee_bps,
        order_cancel_window_hours=policy.order_cancel_window_hours,
        max_late_cancel_fee_bps=cap,
    )


@router.get(
    "/me/cancellation-policy",
    response_model=CancellationPolicyOut,
    summary="Provider: my cancellation terms",
)
def my_cancellation_policy(user: User = BusinessUser, db: Session = Depends(get_db)):
    return _policy_out(db, user)


@router.put(
    "/me/cancellation-policy",
    response_model=CancellationPolicyOut,
    summary="Provider: set my cancellation terms (FR-9.4, FR-10.3)",
    description="Applies to sales paid from now on. Bookings: free until `free_cancel_hours` "
    "before the appointment, then `late_cancel_fee_bps` of the price is kept (capped by the "
    "platform). Parts orders: buyers may cancel within `order_cancel_window_hours` of paying. "
    "Your own cancellations are always refunded in full.",
    responses=error_responses(403, 422),
)
def set_cancellation_policy(
    body: CancellationPolicyIn, user: User = BusinessUser, db: Session = Depends(get_db)
):
    service.set_cancellation_policy(db, user, **body.model_dump())
    return _policy_out(db, user)


# --- Wallet and bank account -------------------------------------------------------------
@router.get("/wallet", response_model=WalletOut, summary="My earnings wallet")
def wallet(user: CurrentUser, db: Session = Depends(get_db)) -> WalletOut:
    w, queued = service.wallet_summary(db, user)
    db.commit()  # the wallet row may have just been created
    return WalletOut(balance_minor=w.balance_minor, queued_payout_minor=queued)


@router.get("/wallet/ledger", response_model=LedgerPage, summary="My wallet history")
def ledger(
    user: CurrentUser,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
    db: Session = Depends(get_db),
) -> LedgerPage:
    rows, next_cursor = service.list_ledger(db, user, limit=limit, cursor=cursor)
    return LedgerPage(items=rows, next_cursor=next_cursor)


@router.put(
    "/me/bank-account",
    response_model=BankAccountOut,
    summary="Set the bank account payouts go to",
    responses=error_responses(400),
)
def put_bank_account(
    body: BankAccountIn, user: CurrentUser, db: Session = Depends(get_db)
) -> BankAccountOut:
    account = service.set_bank_account(db, user, **body.model_dump())
    return BankAccountOut.from_model(account)


@router.get(
    "/me/bank-account",
    response_model=BankAccountOut | None,
    summary="My payout bank account (account number masked)",
)
def get_bank_account(user: CurrentUser, db: Session = Depends(get_db)):
    account = service.get_bank_account(db, user.id)
    return BankAccountOut.from_model(account) if account else None


# --- Disputes (customers and providers) --------------------------------------------------
@router.post(
    "/transactions/{transaction_id}/dispute",
    response_model=DisputeOut,
    status_code=201,
    summary="Open a dispute (freezes the escrow)",
    responses=error_responses(403, 404, 422),
)
def open_dispute(
    transaction_id: uuid.UUID, body: DisputeIn, user: CurrentUser, db: Session = Depends(get_db)
):
    return service.open_dispute(db, user, transaction_id, body.reason)


def _detail(db: Session, dispute, user: User) -> DisputeDetailOut:
    staff = user.has_role(*ADMIN_ROLES)
    messages = service.dispute_messages(db, dispute, include_internal=staff)
    base = DisputeOut.model_validate(dispute).model_dump()
    return DisputeDetailOut(
        **base, messages=[DisputeMessageOut.model_validate(m) for m in messages]
    )


@router.get(
    "/disputes/{dispute_id}",
    response_model=DisputeDetailOut,
    summary="A dispute and its conversation",
    responses=error_responses(403, 404),
)
def get_dispute(dispute_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    dispute = service.get_dispute(db, dispute_id)
    if not user.has_role(*ADMIN_ROLES) and user.id not in (
        dispute.customer_id,
        dispute.provider_id,
    ):
        raise Forbidden("You are not part of this dispute", code="forbidden")
    return _detail(db, dispute, user)


@router.post(
    "/disputes/{dispute_id}/messages",
    response_model=DisputeMessageOut,
    status_code=201,
    summary="Reply in a dispute",
    responses=error_responses(403, 404, 422),
)
def post_dispute_message(
    dispute_id: uuid.UUID, body: DisputeMessageIn, user: CurrentUser, db: Session = Depends(get_db)
):
    dispute = service.get_dispute(db, dispute_id)
    return service.add_dispute_message(db, dispute, user, body.body, internal=body.internal)


# --- Admin: activity, disputes -----------------------------------------------------------
@router.get(
    "/admin/transactions",
    response_model=AdminTransactionPage,
    summary="All orders, bookings and jobs (admin)",
    dependencies=[Depends(require_admin)],
)
def admin_transactions(
    kind: TransactionKind | None = None,
    status: TxStatus | None = None,
    q: str | None = Query(None, description="Reference number, e.g. ORD-80312 or 80312."),
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AdminTransactionPage:
    rows, total = service.search_all(
        db, kind=kind, status=status, q=q, page=page, page_size=page_size
    )
    return AdminTransactionPage(
        items=[TransactionOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )  # fmt: skip


@router.get(
    "/admin/disputes",
    response_model=DisputePage,
    summary="Disputes queue (admin)",
    dependencies=[Depends(require_admin)],
)
def admin_disputes(
    status: DisputeStatus | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
) -> DisputePage:
    rows, total = service.list_disputes(db, status=status, page=page, page_size=page_size)
    return DisputePage(items=rows, total=total, counts=service.dispute_counts(db))


@router.post(
    "/admin/disputes/{dispute_id}/resolve",
    response_model=DisputeOut,
    summary="Resolve a dispute: refund buyer, release to provider, or split 50/50",
    responses=error_responses(404, 422),
)
def admin_resolve(
    dispute_id: uuid.UUID,
    body: ResolveIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return service.resolve_dispute(
        db, dispute_id, body.outcome, admin=admin, note=body.note, ip=client_ip(request)
    )


# --- Admin: finance (Super admin) --------------------------------------------------------
@router.get(
    "/admin/finance/commissions",
    response_model=list[CommissionOut],
    summary="Commission rate per category",
    dependencies=[Depends(require_admin)],
)
def commissions(db: Session = Depends(get_db)):
    rows = service.list_commissions(db)
    db.commit()
    return rows


@router.put(
    "/admin/finance/commissions/{category}",
    response_model=CommissionOut,
    summary="Change a category's commission (Super admin)",
    responses=error_responses(400, 403),
)
def set_commission(
    category: Category,
    body: CommissionIn,
    request: Request,
    admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return service.update_commission(
        db, category, **body.model_dump(), admin=admin, ip=client_ip(request)
    )


@router.get(
    "/admin/finance/payout-settings",
    response_model=PayoutSettingsOut,
    summary="Payout schedule, hold days and minimum",
    dependencies=[Depends(require_admin)],
)
def payout_settings(db: Session = Depends(get_db)):
    row = service.get_payout_settings(db)
    db.commit()
    return row


@router.put(
    "/admin/finance/payout-settings",
    response_model=PayoutSettingsOut,
    summary="Change payout settings (Super admin)",
    responses=error_responses(400, 403),
)
def set_payout_settings(
    body: PayoutSettingsIn,
    request: Request,
    admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return service.update_payout_settings(
        db, **body.model_dump(), admin=admin, ip=client_ip(request)
    )


@router.get(
    "/admin/finance/payouts",
    response_model=PayoutPage,
    summary="Payout queue",
    dependencies=[Depends(require_admin)],
)
def payouts(
    status: PayoutStatus | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PayoutPage:
    rows, total = service.list_payouts(db, status=status, page=page, page_size=page_size)
    return PayoutPage(items=rows, total=total)


@router.post(
    "/admin/finance/payouts/{payout_id}/hold",
    response_model=PayoutOut,
    summary="Put a scheduled payout on hold (Super admin)",
    responses=error_responses(403, 404, 422),
)
def hold(
    payout_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return service.hold_payout(db, payout_id, admin=admin, ip=client_ip(request))


@router.post(
    "/admin/finance/payouts/{payout_id}/release",
    response_model=PayoutOut,
    summary="Send a payout now (Super admin)",
    responses=error_responses(403, 404, 422),
)
def release(
    payout_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return service.release_payout(db, payout_id, admin=admin, ip=client_ip(request))


@router.post(
    "/admin/finance/jobs/release-escrow",
    response_model=JobRunOut,
    summary="Run the escrow auto-release now (Super admin)",
    dependencies=[Depends(require_super_admin)],
)
def run_release_job(db: Session = Depends(get_db)) -> JobRunOut:
    return JobRunOut(processed=service.release_due_escrow(db))


@router.post(
    "/admin/finance/jobs/schedule-payouts",
    response_model=JobRunOut,
    summary="Create payouts from wallet balances now (Super admin)",
    dependencies=[Depends(require_super_admin)],
)
def run_schedule_job(db: Session = Depends(get_db)) -> JobRunOut:
    return JobRunOut(processed=service.schedule_payouts(db))
