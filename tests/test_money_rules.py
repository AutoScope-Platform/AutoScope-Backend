"""Phase 2b: receipts for every sale (FR-9.3), provider cancellation terms with a platform
cap (FR-9.4, FR-10.3), no silent no-refund cancellations, and the insurance disclosure
(FSD 5.7)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.permissions import Role
from app.modules.booking.service import LAGOS
from app.modules.identity.models import KycStatus
from app.modules.payments import service as payments
from app.modules.payments.models import (
    Category,
    EscrowStatus,
    PaymentMethod,
    Transaction,
    TransactionKind,
)
from tests.factories import auth_headers, make_user
from tests.test_insurance import _apply, _buy, world  # noqa: F401  (fixture)

API = "/api/v1"


def _approve(client, admin, listing):
    r = client.post(
        f"{API}/admin/listings/{listing['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _pay(client, user, transaction_id, key):
    paid = client.post(
        f"{API}/payments",
        json={"transaction_id": transaction_id},
        headers={**auth_headers(user), "Idempotency-Key": key},
    )
    assert paid.status_code == 200, paid.text
    r = client.post(f"{API}/payments/{paid.json()['reference']}/verify", headers=auth_headers(user))
    assert r.status_code == 200, r.text


@pytest.fixture
def admin(db):
    return make_user(db, Role.ADMIN)


# --- Parts orders: receipts, cancellation window ---------------------------------------------
@pytest.fixture
def shop(client, db, admin):
    seller = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED, city="Lagos")
    pads = client.post(
        f"{API}/listings",
        headers=auth_headers(seller),
        json={"type": "part", "title": "Brake pads", "price_minor": 2_500_00, "stock": 20,
              "attributes": {"brand": "Bosch", "condition": "new"}},
    ).json()  # fmt: skip
    _approve(client, admin, pads)
    client.put(
        f"{API}/me/delivery-settings",
        headers=auth_headers(seller),
        json={"same_city_fee_minor": 1_000_00, "other_city_fee_minor": 3_000_00},
    )
    return seller, pads


def _order(client, buyer, pads, qty=2, key="co"):
    h = auth_headers(buyer)
    client.put(f"{API}/cart/items", json={"listing_id": pads["id"], "quantity": qty}, headers=h)
    r = client.post(
        f"{API}/checkout",
        json={"delivery_address": "12 Allen Avenue, Ikeja", "delivery_city": "Lagos"},
        headers={**h, "Idempotency-Key": key},
    )
    assert r.status_code == 201, r.text
    return r.json()["orders"][0]


def test_parts_order_receipt(client, db, shop):
    seller, pads = shop
    buyer = make_user(db)
    order = _order(client, buyer, pads)
    early = client.get(f"{API}/transactions/{order['ref']}/receipt", headers=auth_headers(buyer))
    assert early.status_code == 422 and early.json()["error"]["code"] == "not_paid"

    _pay(client, buyer, order["transaction_id"], "pay-1")
    r = client.get(f"{API}/transactions/{order['ref']}/receipt", headers=auth_headers(buyer))
    body = r.json()
    assert r.status_code == 200 and body["receipt_number"].startswith("RCT-")
    assert body["lines"] == [
        {"description": "Brake pads x 2", "amount_minor": 5_000_00},
        {"description": "Delivery", "amount_minor": 1_000_00},
    ]
    assert body["total_minor"] == 6_000_00 and body["net_paid_minor"] == 6_000_00
    assert body["payment_method"] == "card" and body["payment_reference"]
    assert body["provider_name"] == seller.display_name

    seller_view = client.get(body["print_url"], headers=auth_headers(seller))
    assert seller_view.status_code == 200 and "text/html" in seller_view.headers["content-type"]
    assert body["receipt_number"] in seller_view.text and "Delivery" in seller_view.text
    stranger = client.get(
        f"{API}/transactions/{order['ref']}/receipt", headers=auth_headers(make_user(db))
    )
    assert stranger.status_code == 403


def test_seller_cancellation_window_for_parts(client, db, shop):
    seller, pads = shop
    policy = client.put(
        f"{API}/me/cancellation-policy",
        headers=auth_headers(seller),
        json={"order_cancel_window_hours": 2},
    )
    assert policy.status_code == 200 and policy.json()["order_cancel_window_hours"] == 2
    buyer = make_user(db)
    first = _order(client, buyer, pads, qty=1, key="co-a")
    _pay(client, buyer, first["transaction_id"], "pay-a")
    got = client.get(f"{API}/orders/{first['id']}", headers=auth_headers(buyer)).json()
    assert got["cancel_until"] is not None
    ok = client.post(f"{API}/orders/{first['id']}/cancel", json={}, headers=auth_headers(buyer))
    assert ok.status_code == 200 and ok.json()["escrow_status"] == "refunded"

    second = _order(client, buyer, pads, qty=1, key="co-b")
    _pay(client, buyer, second["transaction_id"], "pay-b")
    tx = db.get(Transaction, second["transaction_id"])
    tx.paid_at -= timedelta(hours=3)  # the 2-hour window has passed
    db.commit()
    late = client.post(f"{API}/orders/{second['id']}/cancel", json={}, headers=auth_headers(buyer))
    assert late.status_code == 422 and late.json()["error"]["code"] == "cancellation_window_passed"
    by_seller = client.post(
        f"{API}/orders/{second['id']}/cancel", json={}, headers=auth_headers(seller)
    )
    assert by_seller.status_code == 200 and by_seller.json()["status"] == "cancelled"


def test_paid_sale_outside_escrow_is_not_silently_cancelled(client, db, shop):
    seller, pads = shop
    payments.get_commission(db, Category.PARTS).uses_escrow = False  # admin turned escrow off
    db.commit()
    buyer = make_user(db)
    order = _order(client, buyer, pads, qty=1, key="co-x")
    _pay(client, buyer, order["transaction_id"], "pay-x")
    r = client.post(f"{API}/orders/{order['id']}/cancel", json={}, headers=auth_headers(buyer))
    assert r.status_code == 422 and r.json()["error"]["code"] == "refund_not_possible"
    db.expire_all()
    assert db.get(Transaction, order["transaction_id"]).status != "cancelled"


# --- Bookings: late cancellation fee ---------------------------------------------------------
def next_wednesday(hour: int) -> datetime:
    day = datetime.now(LAGOS).replace(hour=hour, minute=0, second=0, microsecond=0)
    day += timedelta(days=7)
    while day.weekday() != 2:
        day += timedelta(days=1)
    return day


@pytest.fixture
def workshop(client, db, admin):
    mechanic = make_user(db, Role.MECHANIC, kyc=KycStatus.APPROVED, business_name="Ola Motors")
    client.put(
        f"{API}/me/availability",
        json={"windows": [{"weekday": 2, "start_minute": 8 * 60, "end_minute": 17 * 60}]},
        headers=auth_headers(mechanic),
    )
    service = client.post(
        f"{API}/listings",
        headers=auth_headers(mechanic),
        json={"type": "mechanic_service", "title": "Full service", "price_minor": 20_000_00,
              "attributes": {"category": "servicing", "duration_minutes": 60}},
    ).json()  # fmt: skip
    _approve(client, admin, service)
    return mechanic, service


def _paid_booking(client, customer, mechanic, service, hour):
    b = client.post(
        f"{API}/bookings",
        json={"listing_id": service["id"], "scheduled_for": next_wednesday(hour).isoformat()},
        headers=auth_headers(customer),
    ).json()
    client.post(f"{API}/bookings/{b['id']}/accept", headers=auth_headers(mechanic))
    _pay(client, customer, b["transaction_id"], f"pay-{b['id']}")
    return client.get(f"{API}/bookings/{b['id']}", headers=auth_headers(customer)).json()


def test_cancellation_policy_rules_and_platform_cap(client, db, workshop, admin):
    mechanic, _ = workshop
    h = auth_headers(mechanic)
    default = client.get(f"{API}/me/cancellation-policy", headers=h).json()
    assert default["late_cancel_fee_bps"] == 0 and default["max_late_cancel_fee_bps"] == 5000
    too_high = client.put(
        f"{API}/me/cancellation-policy", headers=h, json={"late_cancel_fee_bps": 6000}
    )
    assert too_high.status_code == 422 and too_high.json()["error"]["code"] == "fee_above_cap"

    settings = client.get(
        f"{API}/admin/finance/payout-settings", headers=auth_headers(admin)
    ).json()
    lowered = client.put(
        f"{API}/admin/finance/payout-settings",
        headers=auth_headers(admin),
        json={**settings, "max_cancel_fee_bps": 1000},
    )
    assert lowered.status_code == 200 and lowered.json()["max_cancel_fee_bps"] == 1000
    # An older dashboard that does not send the cap leaves it unchanged.
    old_client = {k: v for k, v in settings.items() if k != "max_cancel_fee_bps"}
    kept = client.put(
        f"{API}/admin/finance/payout-settings", headers=auth_headers(admin), json=old_client
    )
    assert kept.json()["max_cancel_fee_bps"] == 1000
    assert (
        client.put(
            f"{API}/me/cancellation-policy", headers=h, json={"late_cancel_fee_bps": 2000}
        ).status_code
        == 422
    )

    owner = client.put(
        f"{API}/me/cancellation-policy", headers=auth_headers(make_user(db)), json={}
    )
    assert owner.status_code == 403


def test_late_cancellation_keeps_the_fee_for_the_mechanic(client, db, workshop):
    mechanic, service = workshop
    # Free until 30 days before: every booking a week or two away is inside the late window.
    client.put(
        f"{API}/me/cancellation-policy",
        headers=auth_headers(mechanic),
        json={"free_cancel_hours": 720, "late_cancel_fee_bps": 2000},
    )
    customer = make_user(db)
    booking = _paid_booking(client, customer, mechanic, service, 10)
    terms = booking["cancellation"]
    assert terms["late_fee_bps"] == 2000 and terms["fee_if_cancelled_now_minor"] == 4_000_00

    # Changing the policy after payment does not change this booking.
    client.put(f"{API}/me/cancellation-policy", headers=auth_headers(mechanic), json={})

    r = client.post(
        f"{API}/bookings/{booking['id']}/cancel", json={}, headers=auth_headers(customer)
    )
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["escrow_status"] == EscrowStatus.RELEASED
    receipt = client.get(
        f"{API}/transactions/{booking['ref']}/receipt", headers=auth_headers(customer)
    ).json()
    assert receipt["refunded_minor"] == 16_000_00 and receipt["net_paid_minor"] == 4_000_00
    # The mechanic is paid the fee minus the 10% mechanic-services commission.
    wallet = client.get(f"{API}/wallet", headers=auth_headers(mechanic)).json()
    assert wallet["balance_minor"] == 3_600_00


def test_early_or_mechanic_cancellation_is_free(client, db, workshop):
    mechanic, service = workshop
    client.put(
        f"{API}/me/cancellation-policy",
        headers=auth_headers(mechanic),
        json={"free_cancel_hours": 24, "late_cancel_fee_bps": 2000},
    )
    customer = make_user(db)
    early = _paid_booking(client, customer, mechanic, service, 10)  # a week away: still free
    assert early["cancellation"]["fee_if_cancelled_now_minor"] == 0
    r = client.post(f"{API}/bookings/{early['id']}/cancel", json={}, headers=auth_headers(customer))
    assert r.json()["escrow_status"] == EscrowStatus.REFUNDED

    client.put(
        f"{API}/me/cancellation-policy",
        headers=auth_headers(mechanic),
        json={"free_cancel_hours": 720, "late_cancel_fee_bps": 2000},
    )
    late = _paid_booking(client, customer, mechanic, service, 13)
    by_mechanic = client.post(
        f"{API}/bookings/{late['id']}/cancel", json={}, headers=auth_headers(mechanic)
    )
    assert by_mechanic.json()["escrow_status"] == EscrowStatus.REFUNDED  # never charged


def test_booking_receipt_uses_quote_lines(client, db, workshop):
    mechanic, _ = workshop
    customer = make_user(db)
    b = client.post(
        f"{API}/bookings",
        json={"mechanic_id": str(mechanic.id), "quote_requested": True,
              "scheduled_for": next_wednesday(11).isoformat()},
        headers=auth_headers(customer),
    ).json()  # fmt: skip
    client.put(
        f"{API}/bookings/{b['id']}/quote",
        json={"lines": [{"description": "Labour", "amount_minor": 10_000_00},
                        {"description": "Brake pads (front)", "amount_minor": 15_000_00}]},
        headers=auth_headers(mechanic),
    )  # fmt: skip
    client.post(
        f"{API}/bookings/{b['id']}/quote/answer",
        json={"accept": True},
        headers=auth_headers(customer),
    )
    _pay(client, customer, b["transaction_id"], "pay-q")
    lines = client.get(
        f"{API}/transactions/{b['ref']}/receipt", headers=auth_headers(mechanic)
    ).json()["lines"]
    assert [line["description"] for line in lines] == ["Labour", "Brake pads (front)"]


def test_receipt_falls_back_to_one_line(db):
    customer, provider = make_user(db), make_user(db, Role.HAULAGE_PROVIDER)
    tx = payments.create_transaction(
        db, kind=TransactionKind.HAULAGE_JOB, customer_id=customer.id, provider_id=provider.id,
        amount_minor=12_000_00, payment_due=True,
    )  # fmt: skip
    db.commit()
    reference = payments.start_payment(db, customer, tx.id, PaymentMethod.BANK_TRANSFER)[
        0
    ].reference
    payments.confirm_payment(db, reference)
    receipt = payments.build_receipt(db, tx)
    assert receipt["lines"] == [{"description": "Towing / haulage", "amount_minor": 12_000_00}]
    assert receipt["payment_method"] == PaymentMethod.BANK_TRANSFER


# --- Insurance disclosure (FSD 5.7) ----------------------------------------------------------
def test_insurance_disclosure_everywhere(client, db, world):  # noqa: F811
    insurer, customer, product, vehicle = world
    listing = client.get(f"{API}/listings/{product['id']}").json()
    assert "underwritten by SafeCover" in listing["disclosure"]
    application = _apply(client, customer, product, vehicle)
    assert "does not underwrite" in application["disclosure"]
    _buy(client, insurer, customer, application)
    policy = client.get(f"{API}/me/policies", headers=auth_headers(customer)).json()[0]
    assert "does not underwrite" in policy["disclosure"]
    certificate = client.get(
        f"{API}/insurance/policies/{policy['id']}/certificate", headers=auth_headers(customer)
    )
    assert "underwritten by SafeCover" in certificate.text
    receipt = client.get(
        f"{API}/transactions/{application['ref']}/receipt", headers=auth_headers(customer)
    ).json()
    assert receipt["lines"][0]["description"].startswith("Comprehensive Motor (comprehensive)")
    cars = client.get(f"{API}/listings", params={"type": "vehicle"}).json()["items"]
    assert all(c["disclosure"] is None for c in cars)
