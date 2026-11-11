"""Haulage: estimate, dispatch by distance (PostGIS), first-to-accept, trip, tracking, SOS."""

from __future__ import annotations

import pytest

from app.core.permissions import Role
from app.modules.haulage import service
from app.modules.identity.models import KycStatus
from tests.factories import auth_headers, make_user

API = "/api/v1"
VI = {"latitude": 6.4281, "longitude": 3.4219, "address": "Victoria Island"}  # pickup
IKEJA = {"latitude": 6.6018, "longitude": 3.3515, "address": "Ikeja"}  # drop


def _provider(client, db, lat, lng, *, available=True, kyc=KycStatus.APPROVED):
    user = make_user(db, Role.HAULAGE_PROVIDER, kyc=kyc)
    client.put(
        f"{API}/me/haulage/presence",
        json={"available": available, "latitude": lat, "longitude": lng},
        headers=auth_headers(user),
    )
    return user


def _request(client, customer, sos=False):
    response = client.post(
        f"{API}/haulage/jobs",
        json={"pickup": VI, "drop": IKEJA, "vehicle_description": "Corolla", "sos": sos},
        headers=auth_headers(customer),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pay(client, customer, job):
    paid = client.post(
        f"{API}/payments",
        json={"transaction_id": job["transaction_id"]},
        headers={**auth_headers(customer), "Idempotency-Key": f"p-{job['id']}"},
    )
    assert paid.status_code == 200, paid.text
    client.post(f"{API}/payments/{paid.json()['reference']}/verify", headers=auth_headers(customer))


def _offers(client, provider):
    return client.get(f"{API}/me/haulage/offers", headers=auth_headers(provider)).json()


@pytest.fixture
def customer(db):
    return make_user(db, Role.VEHICLE_OWNER)


def test_fare_estimate_and_sos_surcharge(client):
    q = "pickup_lat=6.4281&pickup_lng=3.4219&drop_lat=6.6018&drop_lng=3.3515"
    normal = client.get(f"{API}/haulage/estimate?{q}").json()
    sos = client.get(f"{API}/haulage/estimate?{q}&sos=true").json()
    assert 20 < normal["distance_km"] < 35  # about 22 km straight line x 1.3
    assert sos["fare_minor"] == pytest.approx(normal["fare_minor"] * 1.5, abs=200)
    assert normal["fare_minor"] >= service.MIN_FARE_MINOR


def test_dispatch_offers_nearest_available_verified_providers(client, db, customer):
    near = _provider(client, db, 6.4300, 3.4200)  # ~300 m from the pickup
    mid = _provider(client, db, 6.4700, 3.4000)  # ~5 km
    _provider(client, db, 9.0765, 7.3986)  # Abuja: far outside the radius
    _provider(client, db, 6.4290, 3.4210, available=False)  # offline
    _provider(client, db, 6.4290, 3.4210, kyc=KycStatus.PENDING)  # not verified
    job = _request(client, customer)
    assert job["status"] == "pending" and job["payment_due"] is True
    assert _offers(client, near) == []  # nothing is offered before payment
    _pay(client, customer, job)
    assert (
        client.get(f"{API}/haulage/jobs/{job['id']}", headers=auth_headers(customer)).json()[
            "status"
        ]
        == "searching"
    )
    near_offer, mid_offer = _offers(client, near), _offers(client, mid)
    assert len(near_offer) == 1 and len(mid_offer) == 1
    assert near_offer[0]["distance_to_pickup_m"] < mid_offer[0]["distance_to_pickup_m"]


def test_first_to_accept_wins_and_trip_lifecycle(client, db, customer):
    a = _provider(client, db, 6.4300, 3.4200)
    b = _provider(client, db, 6.4400, 3.4100)
    job = _request(client, customer)
    _pay(client, customer, job)
    offer_a, offer_b = _offers(client, a)[0], _offers(client, b)[0]
    won = client.post(f"{API}/haulage/offers/{offer_a['offer_id']}/accept", headers=auth_headers(a))
    assert won.status_code == 200 and won.json()["status"] == "confirmed"
    lost = client.post(
        f"{API}/haulage/offers/{offer_b['offer_id']}/accept", headers=auth_headers(b)
    )
    assert lost.status_code == 422  # b's offer was expired when a won
    assert (
        client.post(
            f"{API}/haulage/offers/{offer_a['offer_id']}/accept", headers=auth_headers(b)
        ).status_code
        == 404
    )

    path = f"{API}/haulage/jobs/{job['id']}"
    assert client.post(f"{path}/pickup", headers=auth_headers(a)).status_code == 422  # not started
    assert client.post(f"{path}/start", headers=auth_headers(b)).status_code == 404
    assert client.post(f"{path}/start", headers=auth_headers(a)).json()["status"] == "en_route"
    track = client.get(f"{path}/tracking", headers=auth_headers(customer)).json()
    assert track["provider_location"] and track["eta_minutes"] >= 1
    assert client.post(f"{path}/pickup", headers=auth_headers(a)).json()["status"] == "in_transit"
    assert client.post(f"{path}/cancel", json={}, headers=auth_headers(customer)).status_code == 422
    done = client.post(f"{path}/complete", headers=auth_headers(a)).json()
    assert done["status"] == "completed" and done["escrow_status"] == "held"
    assert (
        client.get(f"{path}", headers=auth_headers(make_user(db, Role.VEHICLE_OWNER))).status_code
        == 404
    )


def test_busy_provider_gets_no_new_offers(client, db, customer):
    provider = _provider(client, db, 6.4300, 3.4200)
    first = _request(client, customer)
    _pay(client, customer, first)
    offer = _offers(client, provider)[0]
    client.post(f"{API}/haulage/offers/{offer['offer_id']}/accept", headers=auth_headers(provider))
    second = _request(client, make_user(db, Role.VEHICLE_OWNER))
    _pay(client, db.get(type(customer), second["customer_id"]), second)
    assert _offers(client, provider) == []


def test_cancel_refunds_and_withdraws_offers(client, db, customer):
    provider = _provider(client, db, 6.4300, 3.4200)
    job = _request(client, customer)
    _pay(client, customer, job)
    cancelled = client.post(
        f"{API}/haulage/jobs/{job['id']}/cancel",
        json={"reason": "Fixed it"},
        headers=auth_headers(customer),
    )
    assert (
        cancelled.json()["status"] == "cancelled"
        and cancelled.json()["escrow_status"] == "refunded"
    )
    assert _offers(client, provider) == []


def test_redispatch_widens_the_search(client, db, customer):
    far = _provider(client, db, 6.60, 3.35)  # ~22 km from VI: outside the 10 km first round
    job = _request(client, customer)
    _pay(client, customer, job)
    assert _offers(client, far) == []
    from datetime import timedelta

    from app.core.db import utcnow
    from app.modules.haulage.models import JobOffer

    for offer in db.query(JobOffer).all():  # none exist, but age any that do
        offer.expires_at = utcnow() - timedelta(minutes=1)
    assert service.redispatch_searching(db) == 0  # round 2 = 20 km, still too far
    assert service.redispatch_searching(db) == 1  # round 3 = 30 km
    assert len(_offers(client, far)) == 1


def test_sos_board_priority_and_permissions(client, db, customer):
    provider = _provider(client, db, 6.4300, 3.4200)
    sos = _request(client, customer, sos=True)
    normal = _request(client, customer)
    _pay(client, customer, sos)
    _pay(client, customer, normal)
    assert _offers(client, provider)[0]["is_sos"] is True  # SOS listed first
    support = make_user(db, Role.SUPPORT)
    board = client.get(f"{API}/admin/haulage/sos", headers=auth_headers(support)).json()
    assert [b["ref"] for b in board] == [sos["ref"]] and board[0]["provider"] is None
    assert client.get(f"{API}/admin/haulage/sos", headers=auth_headers(customer)).status_code == 403


def test_websocket_tracking(client, db, customer):
    from app.core.security import create_token

    provider = _provider(client, db, 6.4300, 3.4200)
    job = _request(client, customer)
    _pay(client, customer, job)
    offer = _offers(client, provider)[0]
    client.post(f"{API}/haulage/offers/{offer['offer_id']}/accept", headers=auth_headers(provider))
    url = f"{API}/ws/jobs/{job['id']}/track"
    with client.websocket_connect(f"{url}?token={create_token(customer.id)}") as ws:
        first = ws.receive_json()
        assert first["status"] == "confirmed"
        client.post(f"{API}/haulage/jobs/{job['id']}/start", headers=auth_headers(provider))
        assert ws.receive_json()["status"] == "en_route"
        client.put(
            f"{API}/me/haulage/presence",
            json={"available": True, "latitude": 6.4290, "longitude": 3.4210},
            headers=auth_headers(provider),
        )
        moved = ws.receive_json()
        assert moved["provider_location"]["latitude"] == pytest.approx(6.4290)
    stranger = make_user(db, Role.VEHICLE_OWNER)
    with pytest.raises(Exception):  # noqa: B017,PT011 - connection is refused
        with client.websocket_connect(f"{url}?token={create_token(stranger.id)}"):
            pass
