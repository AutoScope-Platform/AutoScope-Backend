"""Catalog: garage, listings, fraud signals, search (incl. PostGIS), enquiries, admin review."""

from __future__ import annotations

import pytest

from app.core.permissions import Role
from app.modules.identity.models import KycStatus
from tests.factories import auth_headers, make_user

API = "/api/v1"
VEHICLE_ATTRS = {"make": "Toyota", "model": "Camry", "year": 2018, "mileage_km": 64000}
JPEG = b"\xff\xd8\xff\xe0" + b"photo-bytes-1" * 20


@pytest.fixture
def dealer(db):
    return make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)


@pytest.fixture
def admin(db):
    return make_user(db, Role.ADMIN)


def _listing(client, user, **overrides):
    body = {
        "type": "vehicle",
        "title": "2018 Toyota Camry",
        "description": "Clean car, one owner.",
        "price_minor": 950_000_000,
        "city": "Lagos",
        "latitude": 6.5244,
        "longitude": 3.3792,
        "attributes": VEHICLE_ATTRS,
        **overrides,
    }
    response = client.post(f"{API}/listings", json=body, headers=auth_headers(user))
    assert response.status_code == 201, response.text
    return response.json()


def _approve(client, admin, listing_id):
    response = client.post(
        f"{API}/admin/listings/{listing_id}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- Garage -----------------------------------------------------------------------------
def test_garage_vehicle_and_service_history(client, db):
    owner = make_user(db, Role.VEHICLE_OWNER)
    headers = auth_headers(owner)
    body = {"make": "Honda", "model": "Accord", "year": 2015, "vin": "1hgcm82633a004352"}
    created = client.post(f"{API}/me/vehicles", json=body, headers=headers)
    assert created.status_code == 201 and created.json()["vin"] == "1HGCM82633A004352"
    assert client.post(f"{API}/me/vehicles", json=body, headers=headers).status_code == 409

    vehicle_id = created.json()["id"]
    record = {"done_on": "2026-01-10", "description": "Oil change", "mileage_km": 80000}
    assert (
        client.post(
            f"{API}/me/vehicles/{vehicle_id}/service-records", json=record, headers=headers
        ).status_code
        == 201
    )
    history = client.get(f"{API}/me/vehicles/{vehicle_id}/service-records", headers=headers).json()
    assert [r["description"] for r in history] == ["Oil change"]
    # The odometer follows the latest service reading.
    garage = client.get(f"{API}/me/vehicles", headers=headers).json()
    assert garage[0]["mileage_km"] == 80000

    future = {**record, "done_on": "2999-01-01"}
    assert (
        client.post(
            f"{API}/me/vehicles/{vehicle_id}/service-records", json=future, headers=headers
        ).status_code
        == 400
    )
    other = make_user(db, Role.VEHICLE_OWNER)
    assert (
        client.get(
            f"{API}/me/vehicles/{vehicle_id}/service-records", headers=auth_headers(other)
        ).status_code
        == 404
    )


def test_vin_lookup_and_validation(client, db):
    owner = make_user(db, Role.VEHICLE_OWNER)
    headers = auth_headers(owner)
    ok = client.get(f"{API}/vehicles/vin/1HGCM82633A004352", headers=headers)
    assert ok.status_code == 200 and ok.json()["make"]
    assert client.get(f"{API}/vehicles/vin/TOOSHORT", headers=headers).status_code == 400


# --- Listings ---------------------------------------------------------------------------
def test_listing_starts_pending_and_is_hidden_until_approved(client, db, dealer, admin):
    listing = _listing(client, dealer)
    assert listing["status"] == "pending" and listing["ref"].startswith("LST-")
    assert client.get(f"{API}/listings/{listing['id']}").status_code == 404  # anonymous
    assert (
        client.get(f"{API}/listings/{listing['id']}", headers=auth_headers(dealer)).status_code
        == 200
    )
    assert client.get(f"{API}/listings").json()["total"] == 0
    _approve(client, admin, listing["id"])
    assert client.get(f"{API}/listings/{listing['id']}").status_code == 200
    assert client.get(f"{API}/listings").json()["total"] == 1


def test_roles_limit_listing_types(client, db):
    owner = make_user(db, Role.VEHICLE_OWNER)
    # A private owner may sell a car (FSD section 8) but not list parts.
    part = {
        "type": "part", "title": "Brake pads", "price_minor": 1,
        "attributes": {"brand": "Bosch", "condition": "new"},
    }  # fmt: skip
    assert client.post(f"{API}/listings", json=part, headers=auth_headers(owner)).status_code == 403
    mechanic = make_user(db, Role.MECHANIC)
    car = {"type": "vehicle", "title": "A car", "price_minor": 1, "attributes": VEHICLE_ATTRS}
    assert (
        client.post(f"{API}/listings", json=car, headers=auth_headers(mechanic)).status_code == 403
    )
    assert client.post(f"{API}/listings", json=car, headers=auth_headers(owner)).status_code == 201


def test_type_specific_attributes_are_required(client, db, dealer):
    response = client.post(
        f"{API}/listings",
        json={
            "type": "vehicle",
            "title": "No details",
            "price_minor": 1,
            "attributes": {"make": "Kia"},
        },
        headers=auth_headers(dealer),
    )
    assert response.status_code == 422
    assert "model" in response.json()["error"]["details"]["missing"]


def test_admin_cannot_approve_unverified_business_seller(client, db, admin):
    seller = make_user(db, Role.DEALER)  # KYC not submitted
    listing = _listing(client, seller)
    response = client.post(
        f"{API}/admin/listings/{listing['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "seller_not_verified"


def test_rejection_needs_reason_and_edit_resubmits(client, db, dealer, admin):
    listing = _listing(client, dealer)
    path = f"{API}/admin/listings/{listing['id']}/decision"
    assert (
        client.post(path, json={"approve": False}, headers=auth_headers(admin)).status_code == 400
    )
    rejected = client.post(
        path, json={"approve": False, "reason": "Blurry photos"}, headers=auth_headers(admin)
    ).json()
    assert rejected["status"] == "rejected" and rejected["rejection_reason"] == "Blurry photos"
    edited = client.patch(
        f"{API}/listings/{listing['id']}",
        json={"title": "2018 Toyota Camry SE"},
        headers=auth_headers(dealer),
    ).json()
    assert edited["status"] == "pending" and edited["rejection_reason"] is None


def test_fraud_signals(client, db, dealer):
    vin = "1HGCM82633A004352"
    first = _listing(client, dealer, vin=vin)
    assert first["fraud_signals"] == []
    other_dealer = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)
    dup = _listing(client, other_dealer, vin=vin, description="Call me on 08012345678")
    assert set(dup["fraud_signals"]) == {"duplicate_vin", "contact_details"}
    # Same photo on two sellers' listings.
    for listing, user in ((first, dealer), (dup, other_dealer)):
        response = client.post(
            f"{API}/listings/{listing['id']}/images",
            files={"file": ("car.jpg", JPEG, "image/jpeg")},
            headers=auth_headers(user),
        )
        assert response.status_code == 201, response.text
    refreshed = client.get(f"{API}/listings/{dup['id']}", headers=auth_headers(other_dealer)).json()
    assert "duplicate_photos" in refreshed["fraud_signals"]
    # The public never sees the signals.
    admin_queue = client.get(
        f"{API}/admin/listings?flagged=true", headers=auth_headers(make_user(db, Role.SUPPORT))
    ).json()
    assert admin_queue["counts"]["flagged"] >= 1


def test_image_validation(client, db, dealer):
    listing = _listing(client, dealer)
    bad = client.post(
        f"{API}/listings/{listing['id']}/images",
        files={"file": ("x.pdf", b"%PDF", "application/pdf")},
        headers=auth_headers(dealer),
    )
    assert bad.status_code == 400


def test_seller_status_moves(client, db, dealer, admin):
    listing = _listing(client, dealer)
    path = f"{API}/listings/{listing['id']}/status"
    headers = auth_headers(dealer)
    assert client.put(path, json={"status": "sold"}, headers=headers).status_code == 422  # pending
    _approve(client, admin, listing["id"])
    assert client.put(path, json={"status": "reserved"}, headers=headers).status_code == 200
    assert client.put(path, json={"status": "live"}, headers=headers).status_code == 200
    assert client.put(path, json={"status": "live"}, headers=headers).status_code == 422
    assert client.put(path, json={"status": "rejected"}, headers=headers).status_code == 422
    assert client.delete(f"{API}/listings/{listing['id']}", headers=headers).status_code == 204
    assert client.get(f"{API}/listings/{listing['id']}", headers=headers).status_code == 404


def test_other_sellers_cannot_edit(client, db, dealer):
    listing = _listing(client, dealer)
    thief = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)
    assert (
        client.patch(
            f"{API}/listings/{listing['id']}", json={"title": "Hacked"}, headers=auth_headers(thief)
        ).status_code
        == 404
    )


# --- Search -----------------------------------------------------------------------------
def test_search_filters_and_counts(client, db, dealer, admin):
    parts = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    car = _listing(client, dealer)
    cheap = _listing(
        client, dealer, title="2012 Honda Civic", price_minor=300_000_000, city="Abuja",
        attributes={"make": "Honda", "model": "Civic", "year": 2012, "mileage_km": 150000},
    )  # fmt: skip
    pad = _listing(
        client, parts, type="part", title="Brake pads", price_minor=2_500_000, stock=10,
        attributes={"brand": "Bosch", "condition": "new"},
    )  # fmt: skip
    for item in (car, cheap, pad):
        _approve(client, admin, item["id"])

    everything = client.get(f"{API}/listings").json()
    assert everything["total"] == 3
    assert (
        everything["counts_by_type"]["vehicle"] == 2 and everything["counts_by_type"]["part"] == 1
    )
    only_parts = client.get(f"{API}/listings?type=part").json()
    assert only_parts["total"] == 1 and only_parts["counts_by_type"]["vehicle"] == 2
    assert client.get(f"{API}/listings?q=honda").json()["total"] == 1
    assert client.get(f"{API}/listings?city=abuja").json()["total"] == 1
    assert client.get(f"{API}/listings?make=toyota").json()["total"] == 1
    assert client.get(f"{API}/listings?year_min=2015&type=vehicle").json()["total"] == 1
    assert client.get(f"{API}/listings?max_price=500000000").json()["total"] == 2
    asc = client.get(f"{API}/listings?sort=price_asc").json()["items"]
    assert [i["price_minor"] for i in asc] == sorted(i["price_minor"] for i in asc)


def test_search_by_distance_with_postgis(client, db, dealer, admin):
    lagos = _listing(client, dealer)  # Lagos
    abuja = _listing(client, dealer, title="Abuja car", latitude=9.0765, longitude=7.3986)
    nowhere = _listing(client, dealer, title="No location", latitude=None, longitude=None)
    for item in (lagos, abuja, nowhere):
        _approve(client, admin, item["id"])
    near_lagos = client.get(f"{API}/listings?lat=6.45&lng=3.40&radius_km=30&sort=distance").json()
    assert [i["id"] for i in near_lagos["items"]] == [lagos["id"]]
    wide = client.get(f"{API}/listings?lat=6.45&lng=3.40&radius_km=1000&sort=distance").json()
    assert [i["id"] for i in wide["items"]] == [lagos["id"], abuja["id"]]  # nearest first


def test_mechanic_directory_needs_verified_mechanic_with_live_service(client, db, admin):
    verified = make_user(db, Role.MECHANIC, kyc=KycStatus.APPROVED, business_name="Ola Motors")
    unverified = make_user(db, Role.MECHANIC)
    for user in (verified, unverified):
        service = _listing(
            client, user, type="mechanic_service", title="Full service", price_minor=5_000_000,
            attributes={"category": "servicing"}, vin=None,
        )  # fmt: skip
        if user is verified:
            _approve(client, admin, service["id"])
    listed = client.get(f"{API}/mechanics").json()
    assert [m["name"] for m in listed["items"]] == ["Ola Motors"]
    assert listed["items"][0]["from_price_minor"] == 5_000_000


# --- Enquiries --------------------------------------------------------------------------
def test_enquiry_and_offer_flow(client, db, dealer, admin):
    listing = _listing(client, dealer)
    buyer = make_user(db, Role.VEHICLE_OWNER)
    body = {"message": "Is it negotiable?", "offer_minor": 900_000_000}
    path = f"{API}/listings/{listing['id']}/inquiries"
    assert (
        client.post(path, json=body, headers=auth_headers(buyer)).status_code == 404
    )  # hidden until live
    _approve(client, admin, listing["id"])
    assert client.post(path, json=body, headers=auth_headers(dealer)).status_code == 422  # own
    created = client.post(path, json=body, headers=auth_headers(buyer))
    assert created.status_code == 201
    inbox = client.get(path, headers=auth_headers(dealer)).json()
    assert inbox[0]["offer_minor"] == 900_000_000
    assert client.get(path, headers=auth_headers(buyer)).status_code == 404
    answer = f"{API}/inquiries/{created.json()['id']}/answer"
    assert (
        client.post(answer, json={"accept": True}, headers=auth_headers(dealer)).json()["status"]
        == "accepted"
    )
    assert (
        client.post(answer, json={"accept": False}, headers=auth_headers(dealer)).status_code == 422
    )


def test_support_can_review_and_suspend(client, db, dealer):
    support = make_user(db, Role.SUPPORT)
    listing = _listing(client, dealer)
    _approve(client, support, listing["id"])
    suspended = client.post(
        f"{API}/admin/listings/{listing['id']}/suspend",
        json={"reason": "Reported as a scam"},
        headers=auth_headers(support),
    )
    assert suspended.json()["status"] == "suspended"
    assert client.get(f"{API}/listings/{listing['id']}").status_code == 404
    assert client.get(f"{API}/admin/listings", headers=auth_headers(dealer)).status_code == 403
