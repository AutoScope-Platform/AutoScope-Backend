"""Orders: cart, multi-seller checkout, stock, vehicle purchase and the lifecycle."""

from __future__ import annotations

import pytest

from app.core.permissions import Role
from app.modules.catalog.models import Listing, ListingStatus
from app.modules.identity.models import KycStatus
from app.modules.payments import service as payments
from tests.factories import auth_headers, make_user

API = "/api/v1"


def _part(client, db, seller, admin, *, title="Brake pads", price=2_500_00, stock=5):
    body = {
        "type": "part", "title": title, "price_minor": price, "stock": stock,
        "attributes": {"brand": "Bosch", "condition": "new"},
    }  # fmt: skip
    listing = client.post(f"{API}/listings", json=body, headers=auth_headers(seller)).json()
    client.post(
        f"{API}/admin/listings/{listing['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    return listing


@pytest.fixture
def world(client, db):
    admin = make_user(db, Role.ADMIN)
    buyer = make_user(db, Role.VEHICLE_OWNER)
    s1 = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    s2 = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    return admin, buyer, s1, s2


def _checkout(client, buyer, key="co1"):
    return client.post(
        f"{API}/checkout",
        json={"delivery_address": "12 Allen Avenue, Ikeja", "delivery_city": "Lagos"},
        headers={**auth_headers(buyer), "Idempotency-Key": key},
    )


def _pay(client, buyer, order):
    paid = client.post(
        f"{API}/payments",
        json={"transaction_id": order["transaction_id"]},
        headers={**auth_headers(buyer), "Idempotency-Key": f"pay-{order['id']}"},
    )
    assert paid.status_code == 200, paid.text
    verified = client.post(
        f"{API}/payments/{paid.json()['reference']}/verify", headers=auth_headers(buyer)
    )
    assert verified.status_code == 200, verified.text


def test_cart_rules(client, db, world):
    admin, buyer, s1, _ = world
    part = _part(client, db, s1, admin, stock=3)
    headers = auth_headers(buyer)
    put = lambda qty, h=headers: client.put(  # noqa: E731
        f"{API}/cart/items", json={"listing_id": part["id"], "quantity": qty}, headers=h
    )
    assert put(2).json()["subtotal_minor"] == 5_000_00
    assert put(4).status_code == 422  # only 3 in stock
    assert put(3).json()["items"][0]["quantity"] == 3
    assert put(1, auth_headers(s1)).status_code == 422  # own listing
    assert put(0).json()["items"] == []


def test_checkout_splits_by_seller_and_takes_stock(client, db, world):
    admin, buyer, s1, s2 = world
    a = _part(client, db, s1, admin, title="Pads", price=2_000_00, stock=5)
    b = _part(client, db, s1, admin, title="Filter", price=500_00, stock=1)
    c = _part(client, db, s2, admin, title="Plug", price=300_00, stock=None)
    for listing, qty in ((a, 2), (b, 1), (c, 4)):
        client.put(
            f"{API}/cart/items",
            json={"listing_id": listing["id"], "quantity": qty},
            headers=auth_headers(buyer),
        )
    empty = make_user(db, Role.VEHICLE_OWNER)
    assert _checkout(client, empty).status_code == 400
    response = _checkout(client, buyer)
    assert response.status_code == 201, response.text
    orders = response.json()["orders"]
    assert sorted(o["amount_minor"] for o in orders) == [1_200_00, 4_500_00]
    assert all(o["ref"].startswith("ORD-") and o["payment_due"] for o in orders)
    assert _checkout(client, buyer).json() == response.json()  # idempotent replay
    assert client.get(f"{API}/cart", headers=auth_headers(buyer)).json()["items"] == []
    stock = {lst.title: (lst.stock, lst.status) for lst in db.query(Listing).all()}
    assert stock["Pads"] == (3, ListingStatus.LIVE)
    assert stock["Filter"] == (0, ListingStatus.SOLD)  # last unit taken


def test_full_parts_lifecycle_and_payout(client, db, world):
    admin, buyer, s1, _ = world
    part = _part(client, db, s1, admin, price=10_000_00, stock=2)
    client.put(
        f"{API}/cart/items",
        json={"listing_id": part["id"], "quantity": 1},
        headers=auth_headers(buyer),
    )
    order = _checkout(client, buyer).json()["orders"][0]
    path = f"{API}/orders/{order['id']}"
    assert (
        client.post(f"{path}/ship", json={}, headers=auth_headers(s1)).status_code == 422
    )  # unpaid
    _pay(client, buyer, order)
    paid = client.get(path, headers=auth_headers(buyer)).json()
    assert paid["status"] == "confirmed" and paid["escrow_status"] == "held"
    assert client.post(f"{path}/ship", json={}, headers=auth_headers(buyer)).status_code == 404
    shipped = client.post(
        f"{path}/ship", json={"tracking_note": "GIG 4471"}, headers=auth_headers(s1)
    )
    assert shipped.json()["status"] == "shipped"
    assert client.post(f"{path}/cancel", json={}, headers=auth_headers(buyer)).status_code == 422
    assert client.post(f"{path}/confirm-received", headers=auth_headers(s1)).status_code == 404
    done = client.post(f"{path}/confirm-received", headers=auth_headers(buyer)).json()
    assert done["status"] == "delivered"
    # Escrow pays the seller once the dispute window has passed.
    from datetime import timedelta

    from app.core.db import utcnow

    assert payments.release_due_escrow(db, now=utcnow() + timedelta(days=4)) == 1
    wallet = client.get(f"{API}/wallet", headers=auth_headers(s1)).json()
    assert wallet["balance_minor"] == 10_000_00 - 800_00  # 8% parts commission


def test_cancel_unpaid_and_paid_restores_stock_and_refunds(client, db, world):
    admin, buyer, s1, _ = world
    part = _part(client, db, s1, admin, price=1_000_00, stock=1)
    client.put(
        f"{API}/cart/items",
        json={"listing_id": part["id"], "quantity": 1},
        headers=auth_headers(buyer),
    )
    order = _checkout(client, buyer).json()["orders"][0]
    assert db.get(Listing, part["id"]).status == ListingStatus.SOLD
    _pay(client, buyer, order)
    cancelled = client.post(
        f"{API}/orders/{order['id']}/cancel",
        json={"reason": "Changed mind"},
        headers=auth_headers(buyer),
    )
    assert (
        cancelled.json()["status"] == "cancelled"
        and cancelled.json()["escrow_status"] == "refunded"
    )
    listing = db.get(Listing, part["id"])
    db.refresh(listing)
    assert listing.stock == 1 and listing.status == ListingStatus.LIVE


def test_vehicle_purchase_with_accepted_offer(client, db, world):
    admin, buyer, _, _ = world
    dealer = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)
    car = client.post(
        f"{API}/listings",
        json={"type": "vehicle", "title": "2018 Camry", "price_minor": 9_000_000_00,
              "attributes": {"make": "Toyota", "model": "Camry", "year": 2018, "mileage_km": 1}},
        headers=auth_headers(dealer),
    ).json()  # fmt: skip
    client.post(
        f"{API}/admin/listings/{car['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    buy = f"{API}/listings/{car['id']}/purchase"
    inquiry = client.post(
        f"{API}/listings/{car['id']}/inquiries",
        json={"message": "Will you take 8.5m?", "offer_minor": 8_500_000_00},
        headers=auth_headers(buyer),
    ).json()
    bad = client.post(buy, json={"inquiry_id": inquiry["id"]}, headers=auth_headers(buyer))
    assert bad.status_code == 422  # not accepted yet
    client.post(
        f"{API}/inquiries/{inquiry['id']}/answer",
        json={"accept": True},
        headers=auth_headers(dealer),
    )
    order = client.post(buy, json={"inquiry_id": inquiry["id"]}, headers=auth_headers(buyer))
    assert order.status_code == 201 and order.json()["amount_minor"] == 8_500_000_00
    assert order.json()["ref"].startswith("VEH-")
    assert (
        client.post(
            buy, json={}, headers=auth_headers(make_user(db, Role.VEHICLE_OWNER))
        ).status_code
        == 404
    )  # reserved
    _pay(client, buyer, order.json())
    done = client.post(
        f"{API}/orders/{order.json()['id']}/confirm-received", headers=auth_headers(buyer)
    )
    assert done.json()["status"] == "completed"
    assert db.get(Listing, car["id"]).status == ListingStatus.SOLD
    # Vehicles pay 2.5% commission, capped at N500,000: 8.5m x 2.5% = 212,500.
    assert payments.get_transaction(db, order.json()["transaction_id"]).commission_rate_bps == 250


def test_stranger_cannot_see_an_order_but_admin_can(client, db, world):
    admin, buyer, s1, _ = world
    part = _part(client, db, s1, admin)
    client.put(
        f"{API}/cart/items",
        json={"listing_id": part["id"], "quantity": 1},
        headers=auth_headers(buyer),
    )
    order = _checkout(client, buyer).json()["orders"][0]
    path = f"{API}/orders/{order['id']}"
    assert (
        client.get(path, headers=auth_headers(make_user(db, Role.VEHICLE_OWNER))).status_code == 404
    )
    assert client.get(path, headers=auth_headers(admin)).status_code == 200
