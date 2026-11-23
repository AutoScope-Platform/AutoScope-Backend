"""Phase 2a: parts SKU and vehicle fitment (FR-4.1/4.2), vehicle model/condition filters
(FR-3.2), per-seller delivery fees (FR-4.3), mechanic search (FR-5.2) and a mechanic
proposing a different booking time (FR-5.4)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.permissions import Role
from app.modules.booking.service import LAGOS
from app.modules.identity.models import KycStatus
from app.modules.reviews.models import RatingSummary
from tests.factories import auth_headers, make_user

API = "/api/v1"


def _approve(client, admin, listing):
    r = client.post(
        f"{API}/admin/listings/{listing['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create(client, user, **body):
    r = client.post(f"{API}/listings", json=body, headers=auth_headers(user))
    assert r.status_code == 201, r.text
    return r.json()


def _part(client, seller, **extra):
    body = {
        "type": "part",
        "title": "Brake pads",
        "price_minor": 2_500_00,
        "stock": 10,
        "attributes": {"brand": "Bosch", "condition": "new"},
        **extra,
    }
    return _create(client, seller, **body)


def _search(client, headers=None, **params):
    r = client.get(f"{API}/listings", params=params, headers=headers or {})
    return r


def _titles(r):
    assert r.status_code == 200, r.text
    return sorted(i["title"] for i in r.json()["items"])


@pytest.fixture
def admin(db):
    return make_user(db, Role.ADMIN)


@pytest.fixture
def seller(db):
    return make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED, city="Lagos")


# --- Parts: SKU and fitment ----------------------------------------------------------------
def test_part_sku_and_fitment_search(client, db, admin, seller):
    fits = [
        {"make": "Toyota", "model": "Camry", "year_from": 2012, "year_to": 2017},
        {"make": "Lexus"},  # every Lexus model and year
    ]
    pads = _part(client, seller, title="Camry brake pads", sku="BP-1042", fits=fits)
    assert pads["sku"] == "BP-1042"
    assert {f["make"] for f in pads["fits"]} == {"Toyota", "Lexus"}
    _approve(client, admin, pads)
    _approve(client, admin, _part(client, seller, title="Universal wipers", sku="WP-1"))

    assert _titles(_search(client, fits_make="toyota", fits_model="camry", fits_year=2015)) == [
        "Camry brake pads"
    ]
    assert _titles(_search(client, fits_make="Toyota", fits_model="Camry", fits_year=2019)) == []
    assert _titles(_search(client, fits_make="Toyota", fits_model="Corolla")) == []
    assert _titles(_search(client, fits_make="Lexus", fits_model="RX350", fits_year=2010)) == [
        "Camry brake pads"
    ]
    assert _titles(_search(client, fits_make="Honda")) == []
    assert _titles(_search(client, q="bp-1042")) == ["Camry brake pads"]

    bad = _search(client, fits_model="Camry")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "fits_make_required"


def test_search_parts_for_a_garage_vehicle(client, db, admin, seller):
    _approve(
        client,
        admin,
        _part(client, seller, title="Camry pads", fits=[{"make": "Toyota", "model": "Camry"}]),
    )
    owner = make_user(db)
    car = client.post(
        f"{API}/me/vehicles",
        json={"make": "Toyota", "model": "Camry", "year": 2014},
        headers=auth_headers(owner),
    ).json()
    mine = _search(client, auth_headers(owner), vehicle_id=car["id"])
    assert _titles(mine) == ["Camry pads"]

    anonymous = _search(client, vehicle_id=car["id"])
    assert anonymous.status_code == 401
    someone_else = _search(client, auth_headers(make_user(db)), vehicle_id=car["id"])
    assert someone_else.status_code == 404


def test_sku_rules(client, db, admin, seller):
    first = _part(client, seller, sku="ABC-1")
    clash = client.post(
        f"{API}/listings",
        headers=auth_headers(seller),
        json={"type": "part", "title": "Other", "price_minor": 1, "sku": "abc-1",
              "attributes": {"brand": "X", "condition": "new"}},
    )  # fmt: skip
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "duplicate_sku"
    # Another seller may use the same code.
    other_seller = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    _part(client, other_seller, sku="ABC-1")
    # Removing a listing frees its SKU.
    client.delete(f"{API}/listings/{first['id']}", headers=auth_headers(seller))
    _part(client, seller, sku="ABC-1")

    dealer = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)
    car = client.post(
        f"{API}/listings",
        headers=auth_headers(dealer),
        json={"type": "vehicle", "title": "Camry", "price_minor": 1, "sku": "X",
              "attributes": {"make": "Toyota", "model": "Camry", "year": 2018, "mileage_km": 1}},
    )  # fmt: skip
    assert car.status_code == 422 and car.json()["error"]["code"] == "parts_only"

    bad_years = client.post(
        f"{API}/listings",
        headers=auth_headers(seller),
        json={"type": "part", "title": "Pads", "price_minor": 1,
              "fits": [{"make": "Toyota", "year_from": 2018, "year_to": 2012}],
              "attributes": {"brand": "X", "condition": "new"}},
    )  # fmt: skip
    assert bad_years.status_code == 422


def test_editing_fitment_replaces_it(client, db, seller):
    part = _part(client, seller, fits=[{"make": "Toyota"}, {"make": "Honda"}])
    r = client.patch(
        f"{API}/listings/{part['id']}",
        json={"fits": [{"make": "Kia", "model": "Rio"}], "sku": "  K-9  "},
        headers=auth_headers(seller),
    )
    assert r.status_code == 200
    assert r.json()["fits"] == [{"make": "Kia", "model": "Rio", "year_from": None, "year_to": None}]
    assert r.json()["sku"] == "K-9"


# --- Vehicles: model and condition filters (FR-3.2) -------------------------------------------
def test_vehicle_model_and_condition_filters(client, db, admin):
    dealer = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)
    for model, condition in (("Camry", "used"), ("Corolla", "new")):
        _approve(
            client,
            admin,
            _create(
                client, dealer, type="vehicle", title=f"Toyota {model}", price_minor=5_000_000_00,
                attributes={"make": "Toyota", "model": model, "year": 2020, "mileage_km": 100,
                            "condition": condition},
            ),
        )  # fmt: skip
    assert _titles(_search(client, make="toyota", model="camry")) == ["Toyota Camry"]
    assert _titles(_search(client, type="vehicle", condition="new")) == ["Toyota Corolla"]


# --- Delivery fees (FR-4.3) ----------------------------------------------------------------
def test_delivery_fees_per_seller(client, db, admin, seller):
    free_seller = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED, city="Kano")
    settings = client.put(
        f"{API}/me/delivery-settings",
        headers=auth_headers(seller),
        json={"same_city_fee_minor": 1_000_00, "other_city_fee_minor": 3_000_00,
              "free_over_minor": 10_000_00},
    )  # fmt: skip
    assert settings.status_code == 200 and settings.json()["free_over_minor"] == 10_000_00
    unset = client.get(f"{API}/me/delivery-settings", headers=auth_headers(free_seller)).json()
    assert unset == {"same_city_fee_minor": 0, "other_city_fee_minor": 0, "free_over_minor": None}

    pads = _approve(client, admin, _part(client, seller))
    filter_ = _approve(
        client, admin, _part(client, free_seller, title="Oil filter", price_minor=900_00)
    )
    buyer = make_user(db)
    h = auth_headers(buyer)
    for listing in (pads, filter_):
        client.put(
            f"{API}/cart/items", json={"listing_id": listing["id"], "quantity": 1}, headers=h
        )

    def cart(city=None):
        params = {"delivery_city": city} if city else {}
        body = client.get(f"{API}/cart", params=params, headers=h).json()
        return body, {s["seller_id"]: s["delivery_fee_minor"] for s in body["sellers"]}

    body, fees = cart("lagos ")
    assert fees == {str(seller.id): 1_000_00, str(free_seller.id): 0}
    assert body["subtotal_minor"] == 3_400_00 and body["total_minor"] == 4_400_00
    assert cart("Abuja")[1][str(seller.id)] == 3_000_00
    assert cart()[1][str(seller.id)] == 3_000_00  # no city: the other-city fee

    # Spending over the threshold with that seller makes their delivery free.
    client.put(f"{API}/cart/items", json={"listing_id": pads["id"], "quantity": 4}, headers=h)
    assert cart("Abuja")[1][str(seller.id)] == 0
    client.put(f"{API}/cart/items", json={"listing_id": pads["id"], "quantity": 1}, headers=h)

    checkout = client.post(
        f"{API}/checkout",
        json={"delivery_address": "12 Allen Avenue, Ikeja", "delivery_city": "Lagos"},
        headers={**h, "Idempotency-Key": "co-fees"},
    )
    assert checkout.status_code == 201, checkout.text
    orders = {o["seller_id"]: o for o in checkout.json()["orders"]}
    paid = orders[str(seller.id)]
    assert (paid["subtotal_minor"], paid["delivery_fee_minor"], paid["amount_minor"]) == (
        2_500_00, 1_000_00, 3_500_00,
    )  # fmt: skip
    assert orders[str(free_seller.id)]["amount_minor"] == 900_00

    owner_only = client.put(
        f"{API}/me/delivery-settings",
        headers=h,
        json={"same_city_fee_minor": 1, "other_city_fee_minor": 1},
    )
    assert owner_only.status_code == 403


# --- Mechanic search (FR-5.2) ----------------------------------------------------------------
def _mechanic(client, db, admin, name, title, category, price, rating, lat, lng):
    mech = make_user(db, Role.MECHANIC, kyc=KycStatus.APPROVED, business_name=name)
    _approve(
        client,
        admin,
        _create(
            client, mech, type="mechanic_service", title=title, price_minor=price,
            latitude=lat, longitude=lng, attributes={"category": category},
        ),
    )  # fmt: skip
    db.add(RatingSummary(user_id=mech.id, average=rating, count=10))
    db.commit()
    return mech


@pytest.fixture
def garages(client, db, admin):
    return {
        "A": _mechanic(client, db, admin, "Ace Brakes", "Brake repair", "brakes", 15_000_00, 5.0,
                       6.60, 3.35),
        "B": _mechanic(client, db, admin, "Budget Pads", "Brake pads replacement", "brakes",
                       8_000_00, 3.0, 6.45, 3.40),
        "C": _mechanic(client, db, admin, "Care Garage", "Full service", "servicing", 25_000_00,
                       4.0, 6.52, 3.37),
    }  # fmt: skip


def _names(r):
    assert r.status_code == 200, r.text
    return [m["name"] for m in r.json()["items"]]


def test_mechanic_search_filters_and_sorts(client, garages):
    get = lambda **p: client.get(f"{API}/mechanics", params=p)  # noqa: E731
    assert _names(get(service="brake")) == ["Ace Brakes", "Budget Pads"]  # best rated first
    assert _names(get(service="brake", sort="price")) == ["Budget Pads", "Ace Brakes"]
    assert _names(get(max_price=10_000_00)) == ["Budget Pads"]
    assert _names(get(min_rating=4)) == ["Ace Brakes", "Care Garage"]
    assert _names(get(service="servicing")) == ["Care Garage"]  # matches the category

    # Ordering is done in the database, so page 2 continues the rating order.
    assert _names(get(page_size=1, page=1)) == ["Ace Brakes"]
    assert _names(get(page_size=1, page=2)) == ["Care Garage"]
    assert get(page_size=1).json()["total"] == 3

    near = get(lat=6.45, lng=3.40, radius_km=50, sort="distance")
    assert _names(near) == ["Budget Pads", "Care Garage", "Ace Brakes"]
    assert near.json()["items"][0]["distance_km"] == 0.0
    assert get().json()["items"][0]["distance_km"] is None


# --- Booking: mechanic proposes another time (FR-5.4) -----------------------------------------
def next_wednesday(hour: int) -> datetime:
    day = datetime.now(LAGOS).replace(hour=hour, minute=0, second=0, microsecond=0)
    day += timedelta(days=7)
    while day.weekday() != 2:
        day += timedelta(days=1)
    return day


@pytest.fixture
def workshop(client, db, admin):
    customer = make_user(db)
    mechanic = make_user(db, Role.MECHANIC, kyc=KycStatus.APPROVED, business_name="Ola Motors")
    client.put(
        f"{API}/me/availability",
        json={"windows": [{"weekday": 2, "start_minute": 8 * 60, "end_minute": 17 * 60}]},
        headers=auth_headers(mechanic),
    )
    service = _approve(
        client,
        admin,
        _create(
            client, mechanic, type="mechanic_service", title="Full service", price_minor=25_000_00,
            attributes={"category": "servicing", "duration_minutes": 60},
        ),
    )  # fmt: skip
    return customer, mechanic, service


def _book(client, customer, service, when):
    r = client.post(
        f"{API}/bookings",
        json={"listing_id": service["id"], "scheduled_for": when.isoformat()},
        headers=auth_headers(customer),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _propose(client, mechanic, booking, when, note=None):
    return client.put(
        f"{API}/bookings/{booking['id']}/proposed-time",
        json={"scheduled_for": when.isoformat(), "note": note},
        headers=auth_headers(mechanic),
    )


def _answer(client, customer, booking, accept):
    return client.post(
        f"{API}/bookings/{booking['id']}/proposed-time/answer",
        json={"accept": accept},
        headers=auth_headers(customer),
    )


def test_customer_accepts_proposed_time_and_booking_is_confirmed(client, db, workshop):
    customer, mechanic, service = workshop
    booking = _book(client, customer, service, next_wednesday(10))
    proposed = _propose(client, mechanic, booking, next_wednesday(14), "Morning is full")
    body = proposed.json()
    assert proposed.status_code == 200 and body["status"] == "requested"
    assert body["proposal_note"] == "Morning is full"
    assert datetime.fromisoformat(body["proposed_for"]) == next_wednesday(14)
    inbox = client.get(f"{API}/me/notifications", headers=auth_headers(customer)).json()
    assert any(n["title"] == "New time suggested" for n in inbox["items"])

    accepted = _answer(client, customer, booking, True).json()
    assert accepted["status"] == "confirmed" and accepted["payment_due"] is True
    assert datetime.fromisoformat(accepted["scheduled_for"]) == next_wednesday(14)
    assert accepted["proposed_for"] is None
    # The old 10:00 slot is free again, the new 14:00 slot is taken.
    day = next_wednesday(10).date().isoformat()
    slots = client.get(f"{API}/mechanics/{mechanic.id}/slots?date={day}").json()["slots"]
    times = {datetime.fromisoformat(s.replace("Z", "+00:00")) for s in slots}
    assert next_wednesday(10) in times and next_wednesday(14) not in times


def test_customer_declines_proposed_time(client, db, workshop):
    customer, mechanic, service = workshop
    booking = _book(client, customer, service, next_wednesday(10))
    _propose(client, mechanic, booking, next_wednesday(15))
    declined = _answer(client, customer, booking, False).json()
    assert declined["status"] == "requested" and declined["proposed_for"] is None
    assert datetime.fromisoformat(declined["scheduled_for"]) == next_wednesday(10)
    again = _answer(client, customer, booking, True)
    assert again.status_code == 422 and again.json()["error"]["code"] == "no_proposal"
    # The mechanic can still accept the original time.
    ok = client.post(f"{API}/bookings/{booking['id']}/accept", headers=auth_headers(mechanic))
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"


def test_proposed_time_rules(client, db, workshop):
    customer, mechanic, service = workshop
    booking = _book(client, customer, service, next_wednesday(10))
    other = _book(client, make_user(db), service, next_wednesday(12))

    outside = _propose(client, mechanic, booking, next_wednesday(18))
    assert outside.status_code == 422 and outside.json()["error"]["code"] == "outside_hours"
    taken = _propose(client, mechanic, booking, next_wednesday(12))
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "slot_taken"
    same = _propose(client, mechanic, booking, next_wednesday(10))
    assert same.status_code == 400 and same.json()["error"]["code"] == "same_time"
    assert _propose(client, customer, booking, next_wednesday(13)).status_code == 403

    _propose(client, mechanic, booking, next_wednesday(13))
    stranger = _answer(client, make_user(db), booking, True)
    assert stranger.status_code == 404

    # The slot is not held while the customer decides: if it fills up, accepting fails.
    client.post(f"{API}/bookings/{other['id']}/decline", json={}, headers=auth_headers(mechanic))
    _book(client, make_user(db), service, next_wednesday(13))
    late = _answer(client, customer, booking, True)
    assert late.status_code == 409 and late.json()["error"]["code"] == "slot_taken"

    client.post(f"{API}/bookings/{booking['id']}/decline", json={}, headers=auth_headers(mechanic))
    after = _propose(client, mechanic, booking, next_wednesday(16))
    assert after.status_code == 422 and after.json()["error"]["code"] == "invalid_status"
