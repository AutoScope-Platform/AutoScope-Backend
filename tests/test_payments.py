"""Payments: paying, escrow, commission, wallet, payouts and disputes."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.core.db import utcnow
from app.core.permissions import Role
from app.integrations.mock import sign_mock_webhook
from app.modules.payments import service
from app.modules.payments.models import (
    DisputeOutcome,
    EscrowStatus,
    PayoutStatus,
    TransactionKind,
    TxStatus,
)
from tests.factories import auth_headers, make_user


@pytest.fixture
def parties(db):
    buyer = make_user(db, Role.VEHICLE_OWNER)
    seller = make_user(db, Role.PARTS_PROVIDER)
    admin = make_user(db, Role.ADMIN)
    return buyer, seller, admin


def _payable_tx(db, buyer, seller, amount=100_000_00, kind=TransactionKind.PARTS_ORDER):
    tx = service.create_transaction(
        db, kind=kind, customer_id=buyer.id, provider_id=seller.id,
        amount_minor=amount, payment_due=True,
    )  # fmt: skip
    db.commit()
    return tx


def _pay(client, db, buyer, tx, key="k1"):
    response = client.post(
        "/api/v1/payments",
        json={"transaction_id": str(tx.id)},
        headers={**auth_headers(buyer), "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    ref = response.json()["reference"]
    verified = client.post(f"/api/v1/payments/{ref}/verify", headers=auth_headers(buyer))
    assert verified.status_code == 200, verified.text
    return ref


def test_commission_is_capped_and_rounded_down():
    assert service.compute_commission(100_000, 800, None) == 8_000
    assert service.compute_commission(1_001, 250, None) == 25  # 25.025 rounds down
    assert service.compute_commission(30_000_000_00, 250, 50_000_000) == 50_000_000


def test_pay_holds_money_in_escrow_and_snapshots_commission(client, db, parties):
    buyer, seller, _ = parties
    tx = _payable_tx(db, buyer, seller)
    _pay(client, db, buyer, tx)
    db.refresh(tx)
    assert tx.escrow_status == EscrowStatus.HELD
    assert tx.paid_at is not None and not tx.payment_due
    assert tx.commission_rate_bps == 800  # orders are parts: 8%


def test_pay_is_idempotent(client, db, parties):
    buyer, seller, _ = parties
    tx = _payable_tx(db, buyer, seller)
    body = {"transaction_id": str(tx.id)}
    headers = {**auth_headers(buyer), "Idempotency-Key": "same"}
    first = client.post("/api/v1/payments", json=body, headers=headers).json()
    second = client.post("/api/v1/payments", json=body, headers=headers).json()
    assert first == second


def test_pay_requires_idempotency_key(client, db, parties):
    buyer, seller, _ = parties
    tx = _payable_tx(db, buyer, seller)
    response = client.post(
        "/api/v1/payments", json={"transaction_id": str(tx.id)}, headers=auth_headers(buyer)
    )
    assert response.status_code == 400


def test_only_the_customer_can_pay(client, db, parties):
    buyer, seller, _ = parties
    tx = _payable_tx(db, buyer, seller)
    response = client.post(
        "/api/v1/payments",
        json={"transaction_id": str(tx.id)},
        headers={**auth_headers(seller), "Idempotency-Key": "x"},
    )
    assert response.status_code == 403


def test_cannot_pay_twice(client, db, parties):
    buyer, seller, _ = parties
    tx = _payable_tx(db, buyer, seller)
    _pay(client, db, buyer, tx)
    response = client.post(
        "/api/v1/payments",
        json={"transaction_id": str(tx.id)},
        headers={**auth_headers(buyer), "Idempotency-Key": "again"},
    )
    assert response.status_code == 409


def test_release_credits_provider_net_of_commission(client, db, parties):
    buyer, seller, _ = parties
    tx = _payable_tx(db, buyer, seller, amount=100_000_00)
    _pay(client, db, buyer, tx)
    service.release_escrow(db, tx.id)
    db.refresh(tx)
    assert tx.escrow_status == EscrowStatus.RELEASED
    assert tx.commission_minor == 8_000_00
    wallet = client.get("/api/v1/wallet", headers=auth_headers(seller)).json()
    assert wallet["balance_minor"] == 92_000_00
    with pytest.raises(Exception):  # noqa: B017,PT011 - cannot release twice
        service.release_escrow(db, tx.id)


def test_auto_release_waits_for_hold_days_and_skips_disputes(client, db, parties):
    buyer, seller, _ = parties
    done = _payable_tx(db, buyer, seller)
    disputed = _payable_tx(db, buyer, seller)
    for tx in (done, disputed):
        _pay(client, db, buyer, tx, key=str(tx.id))
        service.mark_completed(db, tx)
    db.commit()
    assert service.release_due_escrow(db) == 0  # still inside the dispute window
    service.open_dispute(db, buyer, disputed.id, "Not as described")
    later = utcnow() + timedelta(days=4)
    assert service.release_due_escrow(db, now=later) == 1
    db.refresh(done), db.refresh(disputed)
    assert done.escrow_status == EscrowStatus.RELEASED
    assert disputed.escrow_status == EscrowStatus.FROZEN


@pytest.mark.parametrize(
    ("outcome", "buyer_gets", "seller_net"),
    [
        (DisputeOutcome.REFUND_BUYER, 100_000_00, 0),
        (DisputeOutcome.RELEASE_PROVIDER, 0, 92_000_00),
        (DisputeOutcome.SPLIT, 50_000_00, 46_000_00),  # commission on the provider's half
    ],
)
def test_dispute_resolution(client, db, parties, outcome, buyer_gets, seller_net):
    buyer, seller, admin = parties
    tx = _payable_tx(db, buyer, seller)
    _pay(client, db, buyer, tx)
    opened = client.post(
        f"/api/v1/transactions/{tx.id}/dispute",
        json={"reason": "Part not as described"},
        headers=auth_headers(buyer),
    )
    assert opened.status_code == 201
    dispute_id = opened.json()["id"]
    db.refresh(tx)
    assert tx.escrow_status == EscrowStatus.FROZEN

    resolved = client.post(
        f"/api/v1/admin/disputes/{dispute_id}/resolve",
        json={"outcome": outcome.value},
        headers=auth_headers(admin),
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    wallet = client.get("/api/v1/wallet", headers=auth_headers(seller)).json()
    assert wallet["balance_minor"] == seller_net
    again = client.post(
        f"/api/v1/admin/disputes/{dispute_id}/resolve",
        json={"outcome": outcome.value},
        headers=auth_headers(admin),
    )
    assert again.status_code == 422


def test_dispute_only_while_held(db, parties):
    buyer, seller, _ = parties
    tx = _payable_tx(db, buyer, seller)  # not paid yet
    with pytest.raises(Exception):  # noqa: B017,PT011
        service.open_dispute(db, buyer, tx.id, "Too early")


def test_internal_notes_hidden_from_parties(client, db, parties):
    buyer, seller, admin = parties
    tx = _payable_tx(db, buyer, seller)
    _pay(client, db, buyer, tx)
    dispute_id = client.post(
        f"/api/v1/transactions/{tx.id}/dispute",
        json={"reason": "Damaged"},
        headers=auth_headers(buyer),
    ).json()["id"]
    client.post(
        f"/api/v1/disputes/{dispute_id}/messages",
        json={"body": "Checking with seller", "internal": True},
        headers=auth_headers(admin),
    )
    as_buyer = client.get(f"/api/v1/disputes/{dispute_id}", headers=auth_headers(buyer)).json()
    as_admin = client.get(f"/api/v1/disputes/{dispute_id}", headers=auth_headers(admin)).json()
    assert all(not m["internal"] for m in as_buyer["messages"])
    assert any(m["internal"] for m in as_admin["messages"])
    stranger = make_user(db, Role.VEHICLE_OWNER)
    assert (
        client.get(f"/api/v1/disputes/{dispute_id}", headers=auth_headers(stranger)).status_code
        == 403
    )


def test_webhook_signature_and_replay(client, db, parties):
    buyer, seller, _ = parties
    tx = _payable_tx(db, buyer, seller)
    ref = client.post(
        "/api/v1/payments",
        json={"transaction_id": str(tx.id)},
        headers={**auth_headers(buyer), "Idempotency-Key": "w"},
    ).json()["reference"]
    body = json.dumps({"id": "evt_1", "event": "charge.success", "reference": ref}).encode()
    bad = client.post(
        "/api/v1/payments/webhooks/mock", content=body, headers={"x-signature": "nope"}
    )
    assert bad.status_code == 403
    good = {"x-signature": sign_mock_webhook(body)}
    assert (
        client.post("/api/v1/payments/webhooks/mock", content=body, headers=good).status_code == 200
    )
    assert (
        client.post("/api/v1/payments/webhooks/mock", content=body, headers=good).status_code == 200
    )
    db.refresh(tx)
    assert tx.escrow_status == EscrowStatus.HELD


def test_payouts_schedule_hold_release(client, db, parties):
    buyer, seller, admin = parties
    tx = _payable_tx(db, buyer, seller)
    _pay(client, db, buyer, tx)
    service.release_escrow(db, tx.id)
    assert service.schedule_payouts(db) == 0  # no bank account yet
    client.put(
        "/api/v1/me/bank-account",
        json={
            "bank_name": "GTBank",
            "bank_code": "058",
            "account_number": "0123456789",
            "account_name": "Seller Ltd",
        },
        headers=auth_headers(seller),
    )
    shown = client.get("/api/v1/me/bank-account", headers=auth_headers(seller)).json()
    assert shown["account_last4"] == "6789" and "account_number" not in shown
    assert service.schedule_payouts(db) == 1
    wallet = client.get("/api/v1/wallet", headers=auth_headers(seller)).json()
    assert wallet["balance_minor"] == 0 and wallet["queued_payout_minor"] == 92_000_00

    payout = client.get("/api/v1/admin/finance/payouts", headers=auth_headers(admin)).json()[
        "items"
    ][0]
    held = client.post(
        f"/api/v1/admin/finance/payouts/{payout['id']}/hold", headers=auth_headers(admin)
    )
    assert held.json()["status"] == PayoutStatus.ON_HOLD
    sent = client.post(
        f"/api/v1/admin/finance/payouts/{payout['id']}/release", headers=auth_headers(admin)
    )
    assert sent.json()["status"] == PayoutStatus.PAID


def test_support_cannot_change_finance_settings(client, db):
    support = make_user(db, Role.SUPPORT)
    response = client.put(
        "/api/v1/admin/finance/commissions/parts",
        json={"rate_bps": 100, "uses_escrow": True},
        headers=auth_headers(support),
    )
    assert response.status_code == 403
    assert (
        client.get("/api/v1/admin/finance/commissions", headers=auth_headers(support)).status_code
        == 200
    )


def test_super_admin_changes_commission_only_for_future_sales(client, db, parties):
    buyer, seller, admin = parties
    tx = _payable_tx(db, buyer, seller)
    _pay(client, db, buyer, tx)
    changed = client.put(
        "/api/v1/admin/finance/commissions/parts",
        json={"rate_bps": 500, "uses_escrow": True},
        headers=auth_headers(admin),
    )
    assert changed.status_code == 200
    service.release_escrow(db, tx.id)
    db.refresh(tx)
    assert tx.commission_minor == 8_000_00  # the 8% in force when it was paid


def test_activity_lists_by_side(client, db, parties):
    buyer, seller, _ = parties
    tx = _payable_tx(db, buyer, seller)
    mine = client.get("/api/v1/activity", headers=auth_headers(buyer)).json()["items"]
    received = client.get("/api/v1/activity?as_provider=true", headers=auth_headers(seller)).json()[
        "items"
    ]
    assert [t["ref"] for t in mine] == [tx.ref] == [t["ref"] for t in received]
    assert (
        client.get(f"/api/v1/transactions/{tx.ref}", headers=auth_headers(buyer)).status_code == 200
    )
    stranger = make_user(db, Role.VEHICLE_OWNER)
    assert (
        client.get(f"/api/v1/transactions/{tx.ref}", headers=auth_headers(stranger)).status_code
        == 403
    )
    assert TxStatus.PENDING == tx.status
