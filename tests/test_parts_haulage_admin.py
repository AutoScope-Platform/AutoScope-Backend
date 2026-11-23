"""Phase 2e: low-stock alerts (FR-4.5), part requests (FR-4.6), tow fare by vehicle class
(FR-6.5), admins removing an account (FR-13.2) and the repeat-offender list (TSD)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core import events
from app.core.audit import AuditLog
from app.core.permissions import Role
from app.modules.catalog.models import Listing, ListingStatus
from app.modules.identity.models import KycStatus, User
from tests.factories import auth_headers, make_user

API = "/api/v1"
VI = {"latitude": 6.4281, "longitude": 3.4219, "address": "Victoria Island"}
IKEJA = {"latitude": 6.6018, "longitude": 3.3515, "address": "Ikeja"}


@pytest.fixture
def admin(db):
    return make_user(db, Role.ADMIN)


def _live(client, admin, user, **body):
    r = client.post(f"{API}/listings", json=body, headers=auth_headers(user))
    assert r.status_code == 201, r.text
    client.post(
        f"{API}/admin/listings/{r.json()['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    return r.json()


def _part(client, admin, seller, title="Brake pads", **extra):
    body = {
        "type": "part",
        "title": title,
        "price_minor": 1_000_00,
        "attributes": {"brand": "X", "condition": "new"},
    }
    return _live(client, admin, seller, **{**body, **extra})


def _inbox_titles(client, user):
    return [
        n["title"]
        for n in client.get(f"{API}/me/notifications", headers=auth_headers(user)).json()["items"]
    ]


# --- Events after commit ---------------------------------------------------------------------
def test_publish_after_commit_waits_for_the_commit(db):
    seen = []
    events.subscribe("test.after_commit")(lambda e: seen.append(e.payload["n"]))
    try:
        events.publish_after_commit(db, "test.after_commit", n=1)
        db.rollback()  # rolled back: never published
        events.publish_after_commit(db, "test.after_commit", n=2)
        assert seen == []
        db.commit()
        assert seen == [2]
    finally:
        events._handlers.pop("test.after_commit", None)


# --- Low-stock alerts (FR-4.5) ---------------------------------------------------------------
def _buy(client, buyer, listing, qty, key):
    h = auth_headers(buyer)
    client.put(f"{API}/cart/items", json={"listing_id": listing["id"], "quantity": qty}, headers=h)
    r = client.post(
        f"{API}/checkout",
        json={"delivery_address": "12 Allen Avenue, Ikeja"},
        headers={**h, "Idempotency-Key": key},
    )
    assert r.status_code == 201, r.text


def test_low_stock_alert_once_per_dip(client, db, admin):
    seller = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    pads = _part(client, admin, seller, stock=5, low_stock_threshold=2)
    assert pads["low_stock_threshold"] == 2
    buyer = make_user(db)
    _buy(client, buyer, pads, 3, "k1")  # 5 -> 2: alert
    _buy(client, buyer, pads, 1, "k2")  # 2 -> 1: already alerted
    assert _inbox_titles(client, seller).count("Running low") == 1

    client.patch(
        f"{API}/listings/{pads['id']}", json={"stock": 10}, headers=auth_headers(seller)
    )  # restock re-arms
    _buy(client, buyer, pads, 8, "k3")  # 10 -> 2: alert again
    assert _inbox_titles(client, seller).count("Running low") == 2


# --- Part requests (FR-4.6) ------------------------------------------------------------------
def test_part_request_quotes_and_close(client, db, admin):
    toyota_seller = make_user(
        db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED, business_name="Toyota Parts"
    )
    honda_seller = make_user(
        db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED, business_name="Honda Parts"
    )
    lamp = _part(client, admin, toyota_seller, title="Camry headlamp", fits=[{"make": "Toyota"}])
    _part(client, admin, honda_seller, title="Civic filter", fits=[{"make": "Honda"}])

    owner = make_user(db)
    car = client.post(
        f"{API}/me/vehicles",
        json={"make": "Toyota", "model": "Camry", "year": 2015},
        headers=auth_headers(owner),
    ).json()
    missing = client.post(
        f"{API}/part-requests", json={"description": "Left headlamp"}, headers=auth_headers(owner)
    )
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "vehicle_required"
    created = client.post(
        f"{API}/part-requests",
        json={"description": "Left headlamp", "vehicle_id": car["id"]},
        headers=auth_headers(owner),
    )
    assert created.status_code == 201
    req = created.json()
    assert (req["make"], req["model"], req["year"]) == ("Toyota", "Camry", 2015) and req[
        "ref"
    ].startswith("PRQ-")

    # Only the seller who stocks Toyota parts is told.
    assert "New part request" in _inbox_titles(client, toyota_seller)
    assert "New part request" not in _inbox_titles(client, honda_seller)
    feed = client.get(
        f"{API}/part-requests", params={"make": "toyota"}, headers=auth_headers(honda_seller)
    ).json()
    assert [r["id"] for r in feed["items"]] == [req["id"]]  # but any parts seller can browse
    assert client.get(f"{API}/part-requests", headers=auth_headers(owner)).status_code == 403

    quote = lambda seller, price, **x: client.put(  # noqa: E731
        f"{API}/part-requests/{req['id']}/quote", json={"price_minor": price, "message": "In stock", **x},
        headers=auth_headers(seller),
    )  # fmt: skip
    assert quote(toyota_seller, 45_000_00, listing_id=lamp["id"]).status_code == 200
    assert quote(honda_seller, 39_000_00).status_code == 200
    assert quote(honda_seller, 38_000_00).status_code == 200  # revised, still one quote
    not_mine = quote(honda_seller, 1, listing_id=lamp["id"])
    assert not_mine.status_code == 404
    assert "New quote for your part request" in _inbox_titles(client, owner)

    seen = client.get(f"{API}/part-requests/{req['id']}", headers=auth_headers(owner)).json()[
        "quotes"
    ]
    assert [(q["provider_name"], q["price_minor"]) for q in seen] == [
        ("Honda Parts", 38_000_00),
        ("Toyota Parts", 45_000_00),
    ]
    theirs = client.get(
        f"{API}/part-requests/{req['id']}", headers=auth_headers(toyota_seller)
    ).json()["quotes"]
    assert [q["price_minor"] for q in theirs] == [
        45_000_00
    ]  # sellers do not see each other's prices

    closed = client.post(f"{API}/part-requests/{req['id']}/close", headers=auth_headers(owner))
    assert closed.json()["status"] == "closed"
    assert quote(toyota_seller, 1).json()["error"]["code"] == "request_closed"
    assert (
        client.get(f"{API}/part-requests", headers=auth_headers(toyota_seller)).json()["total"] == 0
    )
    assert [
        r["id"] for r in client.get(f"{API}/me/part-requests", headers=auth_headers(owner)).json()
    ] == [req["id"]]


# --- Tow fare by vehicle class (FR-6.5) ------------------------------------------------------
def test_tow_fare_depends_on_vehicle_class(client, db):
    q = {"pickup_lat": 6.4281, "pickup_lng": 3.4219, "drop_lat": 6.6018, "drop_lng": 3.3515}
    fare = {c: client.get(f"{API}/haulage/estimate", params={**q, "vehicle_class": c}).json()["fare_minor"]
            for c in ("motorcycle", "car", "truck")}  # fmt: skip
    assert fare["motorcycle"] < fare["car"] < fare["truck"]
    assert abs(fare["truck"] - 2 * fare["car"]) <= 100  # rounded to the nearest naira
    assert (
        client.get(f"{API}/haulage/estimate", params={**q, "vehicle_class": "boat"}).status_code
        == 422
    )

    customer = make_user(db)
    job = client.post(
        f"{API}/haulage/jobs", json={"pickup": VI, "drop": IKEJA, "vehicle_class": "truck"},
        headers=auth_headers(customer),
    ).json()  # fmt: skip
    assert job["vehicle_class"] == "truck" and job["fare_minor"] == fare["truck"]


# --- Admins remove an account (FR-13.2) ------------------------------------------------------
def test_super_admin_removes_an_account(client, db, admin):
    dealer = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED, email="crook@example.com")
    car = _live(client, admin, dealer, type="vehicle", title="Too good to be true", price_minor=1,
                attributes={"make": "Toyota", "model": "Camry", "year": 2020, "mileage_km": 1})  # fmt: skip
    remove = lambda who, target, reason="Repeated fake listings": client.post(  # noqa: E731
        f"{API}/admin/users/{target.id}/remove", json={"reason": reason}, headers=auth_headers(who)
    )
    assert remove(make_user(db, Role.SUPPORT), dealer).status_code == 403  # Super admin only
    assert remove(admin, admin).status_code == 403
    assert remove(admin, make_user(db, Role.SUPPORT)).json()["error"]["code"] == "staff_account"

    assert remove(admin, dealer).status_code == 204
    db.expire_all()
    gone = db.get(User, dealer.id)
    assert gone.erased_at is not None and gone.email is None and gone.full_name == "Erased user"
    assert db.get(Listing, car["id"]).status == ListingStatus.REMOVED
    assert client.get(f"{API}/listings/{car['id']}").status_code == 404
    entry = db.scalar(select(AuditLog).where(AuditLog.action.like("Removed user%")))
    assert entry.target == gone.ref and "Repeated fake listings" in entry.action
    assert remove(admin, dealer).status_code == 409


# --- Repeat offenders (TSD) ------------------------------------------------------------------
def test_repeat_offenders_are_flagged(client, db, admin):
    bad = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED, business_name="Shady Spares")
    meh = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED, business_name="Careless Co")
    for i in range(2):  # two listings taken down: score 4
        listing = _part(client, admin, bad, title=f"Fake part {i}")
        client.post(
            f"{API}/admin/listings/{listing['id']}/suspend",
            json={"reason": "Counterfeit"},
            headers=auth_headers(admin),
        )
    pending = client.post(  # one rejection: score 1, not flagged
        f"{API}/listings", headers=auth_headers(meh),
        json={"type": "part", "title": "Blurry", "price_minor": 1, "attributes": {"brand": "X", "condition": "new"}},
    ).json()  # fmt: skip
    client.post(
        f"{API}/admin/listings/{pending['id']}/decision",
        json={"approve": False, "reason": "Bad photos"},
        headers=auth_headers(admin),
    )

    support = make_user(db, Role.SUPPORT)
    flags = client.get(f"{API}/admin/risk-flags", headers=auth_headers(support)).json()["items"]
    assert [f["name"] for f in flags] == ["Shady Spares"]
    assert flags[0]["score"] == 4 and flags[0]["signals"]["suspended_listings"] == 2
    assert flags[0]["reasons"] == ["2 listing(s) taken down"]
    overview = client.get(f"{API}/admin/overview", headers=auth_headers(admin)).json()
    assert overview["queues"]["flagged_users"] == 1
    assert client.get(f"{API}/admin/risk-flags", headers=auth_headers(bad)).status_code == 403
