"""Mechanic bookings: hours, slots, fixed price and quote flows, payment, history, cancel."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.permissions import Role
from app.modules.booking.service import LAGOS
from app.modules.identity.models import KycStatus
from tests.factories import auth_headers, make_user

API = "/api/v1"


def next_weekday(weekday: int, hour: int, minute: int = 0) -> datetime:
    """The next given weekday at least a week away, at hour:minute Lagos time."""
    day = datetime.now(LAGOS).replace(hour=hour, minute=minute, second=0, microsecond=0)
    day += timedelta(days=7)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day


@pytest.fixture
def setup(client, db):
    admin = make_user(db, Role.ADMIN)
    customer = make_user(db, Role.VEHICLE_OWNER)
    mechanic = make_user(db, Role.MECHANIC, kyc=KycStatus.APPROVED, business_name="Ola Motors")
    hours = {
        "windows": [{"weekday": 2, "start_minute": 8 * 60, "end_minute": 17 * 60}]
    }  # Wednesday
    assert (
        client.put(f"{API}/me/availability", json=hours, headers=auth_headers(mechanic)).status_code
        == 200
    )
    service = client.post(
        f"{API}/listings",
        json={"type": "mechanic_service", "title": "Full service", "price_minor": 25_000_00,
              "attributes": {"category": "servicing", "duration_minutes": 120}},
        headers=auth_headers(mechanic),
    ).json()  # fmt: skip
    client.post(
        f"{API}/admin/listings/{service['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    return customer, mechanic, service


def _book(client, customer, service, when, **extra):
    return client.post(
        f"{API}/bookings",
        json={
            "listing_id": service["id"],
            "scheduled_for": when.isoformat(),
            "description": "Due for service",
            **extra,
        },
        headers=auth_headers(customer),
    )


def _pay(client, customer, booking):
    paid = client.post(
        f"{API}/payments",
        json={"transaction_id": booking["transaction_id"]},
        headers={**auth_headers(customer), "Idempotency-Key": f"pay-{booking['id']}"},
    )
    assert paid.status_code == 200, paid.text
    client.post(f"{API}/payments/{paid.json()['reference']}/verify", headers=auth_headers(customer))


def test_slots_respect_hours_and_existing_bookings(client, db, setup):
    customer, mechanic, service = setup
    when = next_weekday(2, 10)
    day = when.date().isoformat()
    free = client.get(f"{API}/mechanics/{mechanic.id}/slots?date={day}&duration_minutes=120").json()
    assert len(free["slots"]) == 15  # 08:00-15:00 starts every 30 min for a 2-hour job
    assert (
        client.get(
            f"{API}/mechanics/{mechanic.id}/slots?date={(when + timedelta(days=1)).date()}"
        ).json()["slots"]
        == []
    )
    assert _book(client, customer, service, when).status_code == 201
    after = client.get(
        f"{API}/mechanics/{mechanic.id}/slots?date={day}&duration_minutes=120"
    ).json()["slots"]
    assert len(after) == 15 - 7  # 08:30..11:30 overlap the 10:00-12:00 booking


def test_booking_validations(client, db, setup):
    customer, mechanic, service = setup
    when = next_weekday(2, 10)
    assert (
        _book(client, customer, service, next_weekday(3, 10)).status_code == 422
    )  # Thursday: closed
    assert (
        _book(client, customer, service, next_weekday(2, 16)).status_code == 422
    )  # runs past 17:00
    assert (
        _book(client, customer, service, datetime.now(LAGOS) - timedelta(days=1)).status_code == 400
    )
    assert _book(client, customer, service, when).status_code == 201
    assert (
        _book(
            client, make_user(db, Role.VEHICLE_OWNER), service, when + timedelta(hours=1)
        ).status_code
        == 409
    )
    naive = client.post(
        f"{API}/bookings",
        json={"listing_id": service["id"], "scheduled_for": "2030-01-02T10:00:00"},
        headers=auth_headers(customer),
    )
    assert naive.status_code == 422


def test_fixed_price_flow_adds_service_history(client, db, setup):
    customer, mechanic, service = setup
    vehicle = client.post(
        f"{API}/me/vehicles",
        json={"make": "Toyota", "model": "Camry", "year": 2018},
        headers=auth_headers(customer),
    ).json()
    booking = _book(client, customer, service, next_weekday(2, 9), vehicle_id=vehicle["id"]).json()
    assert booking["status"] == "requested" and booking["ref"].startswith("BKG-")
    path = f"{API}/bookings/{booking['id']}"
    assert client.post(f"{path}/start", headers=auth_headers(mechanic)).status_code == 422
    accepted = client.post(f"{path}/accept", headers=auth_headers(mechanic)).json()
    assert accepted["status"] == "confirmed" and accepted["payment_due"] is True
    assert client.post(f"{path}/start", headers=auth_headers(mechanic)).status_code == 422  # unpaid
    _pay(client, customer, accepted)
    assert client.post(f"{path}/start", headers=auth_headers(customer)).status_code == 403
    assert (
        client.post(f"{path}/start", headers=auth_headers(mechanic)).json()["status"]
        == "in_progress"
    )
    done = client.post(f"{path}/complete", headers=auth_headers(mechanic)).json()
    assert done["status"] == "completed" and done["escrow_status"] == "held"
    history = client.get(
        f"{API}/me/vehicles/{vehicle['id']}/service-records", headers=auth_headers(customer)
    ).json()
    assert (
        history[0]["description"] == "Full service" and history[0]["booking_ref"] == booking["ref"]
    )
    invoice = client.get(f"{path}/invoice", headers=auth_headers(customer)).json()
    assert (
        invoice["total_minor"] == 25_000_00
        and invoice["paid"]
        and invoice["issued_by"] == "Ola Motors"
    )
    # The customer can now review the mechanic.
    review = client.post(
        f"{API}/reviews",
        json={"transaction_ref": booking["ref"], "rating": 5},
        headers=auth_headers(customer),
    )
    assert review.status_code == 201


def test_quote_flow(client, db, setup):
    customer, mechanic, _ = setup
    when = next_weekday(2, 11)
    requested = client.post(
        f"{API}/bookings",
        json={"mechanic_id": str(mechanic.id), "scheduled_for": when.isoformat(),
              "description": "Strange noise", "quote_requested": True},
        headers=auth_headers(customer),
    ).json()  # fmt: skip
    assert requested["status"] == "quote_requested" and requested["amount_minor"] == 0
    path = f"{API}/bookings/{requested['id']}"
    lines = {"lines": [{"description": "Diagnosis", "amount_minor": 5_000_00},
                       {"description": "Wheel bearing", "amount_minor": 30_000_00}], "note": "2 hours"}  # fmt: skip
    assert (
        client.put(f"{path}/quote", json=lines, headers=auth_headers(customer)).status_code == 403
    )
    quoted = client.put(f"{path}/quote", json=lines, headers=auth_headers(mechanic)).json()
    assert quoted["status"] == "quote_sent" and quoted["amount_minor"] == 35_000_00
    revised = client.put(
        f"{path}/quote",
        json={"lines": [{"description": "All in", "amount_minor": 32_000_00}]},
        headers=auth_headers(mechanic),
    ).json()
    assert revised["amount_minor"] == 32_000_00 and len(revised["lines"]) == 1
    answer = client.post(
        f"{path}/quote/answer", json={"accept": True}, headers=auth_headers(customer)
    ).json()
    assert answer["status"] == "confirmed" and answer["payment_due"] is True
    _pay(client, customer, answer)
    assert client.get(path, headers=auth_headers(customer)).json()["escrow_status"] == "held"


def test_decline_and_cancel_with_refund(client, db, setup):
    customer, mechanic, service = setup
    first = _book(client, customer, service, next_weekday(2, 9)).json()
    declined = client.post(
        f"{API}/bookings/{first['id']}/decline",
        json={"reason": "Fully booked"},
        headers=auth_headers(mechanic),
    ).json()
    assert declined["status"] == "declined" and declined["decline_reason"] == "Fully booked"
    # A declined booking frees the slot.
    second = _book(client, customer, service, next_weekday(2, 9)).json()
    accepted = client.post(
        f"{API}/bookings/{second['id']}/accept", headers=auth_headers(mechanic)
    ).json()
    _pay(client, customer, accepted)
    cancelled = client.post(
        f"{API}/bookings/{second['id']}/cancel",
        json={"reason": "Plans changed"},
        headers=auth_headers(customer),
    ).json()
    assert cancelled["status"] == "cancelled" and cancelled["escrow_status"] == "refunded"
    assert (
        client.post(
            f"{API}/bookings/{second['id']}/cancel", json={}, headers=auth_headers(customer)
        ).status_code
        == 422
    )


def test_mechanic_must_be_verified_and_strangers_see_nothing(client, db, setup):
    customer, mechanic, service = setup
    booking = _book(client, customer, service, next_weekday(2, 9)).json()
    stranger = make_user(db, Role.VEHICLE_OWNER)
    assert (
        client.get(f"{API}/bookings/{booking['id']}", headers=auth_headers(stranger)).status_code
        == 404
    )
    unverified = make_user(db, Role.MECHANIC)
    body = {
        "mechanic_id": str(unverified.id),
        "scheduled_for": next_weekday(2, 9).isoformat(),
        "quote_requested": True,
    }
    assert (
        client.post(f"{API}/bookings", json=body, headers=auth_headers(customer)).status_code == 404
    )
