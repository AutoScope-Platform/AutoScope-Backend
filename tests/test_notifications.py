"""Notifications: event-driven delivery, inbox, devices and announcements."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.db import utcnow
from app.core.permissions import Role
from app.integrations import mock
from app.modules.identity.models import KycStatus
from app.modules.notifications import service
from tests.factories import auth_headers, make_user

API = "/api/v1"


def _outbox(kind=None):
    return [m for m in mock.OUTBOX if kind is None or m.channel == kind]


def _register(client, user, token="fcm-token-0123456789"):
    r = client.post(
        f"{API}/me/devices",
        json={"token": token, "platform": "android"},
        headers=auth_headers(user),
    )
    assert r.status_code == 204, r.text


def test_event_creates_inbox_entry_and_push(client, db):
    admin = make_user(db, Role.ADMIN)
    seller = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)
    _register(client, seller)
    listing = client.post(
        f"{API}/listings",
        json={"type": "vehicle", "title": "2018 Camry", "price_minor": 1,
              "attributes": {"make": "Toyota", "model": "Camry", "year": 2018, "mileage_km": 1}},
        headers=auth_headers(seller),
    ).json()  # fmt: skip
    mock.OUTBOX.clear()
    client.post(
        f"{API}/admin/listings/{listing['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    inbox = client.get(f"{API}/me/notifications", headers=auth_headers(seller)).json()
    assert inbox["unread"] == 1 and inbox["items"][0]["title"] == "Your listing is live"
    assert inbox["items"][0]["data"]["listing_id"] == listing["id"]
    assert [m.to for m in _outbox("push")] == ["fcm-token-0123456789"]


def test_mark_read(client, db):
    admin = make_user(db, Role.ADMIN)
    seller = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)
    for _ in range(2):
        listing = client.post(
            f"{API}/listings",
            json={"type": "vehicle", "title": "A car", "price_minor": 1,
                  "attributes": {"make": "Kia", "model": "Rio", "year": 2015, "mileage_km": 1}},
            headers=auth_headers(seller),
        ).json()  # fmt: skip
        client.post(
            f"{API}/admin/listings/{listing['id']}/decision",
            json={"approve": True},
            headers=auth_headers(admin),
        )
    page = client.get(f"{API}/me/notifications", headers=auth_headers(seller)).json()
    assert page["unread"] == 2
    one = client.post(
        f"{API}/me/notifications/read",
        json={"ids": [page["items"][0]["id"]]},
        headers=auth_headers(seller),
    )
    assert one.json() == {"marked": 1}
    assert client.post(
        f"{API}/me/notifications/read", json={}, headers=auth_headers(seller)
    ).json() == {"marked": 1}
    assert client.get(f"{API}/me/notifications", headers=auth_headers(seller)).json()["unread"] == 0


def test_payment_notifies_customer_by_email_and_provider(client, db):
    buyer = make_user(db, Role.VEHICLE_OWNER, email="buyer@example.com")
    seller = make_user(db, Role.PARTS_PROVIDER, kyc=KycStatus.APPROVED)
    from app.modules.payments import service as payments
    from app.modules.payments.models import TransactionKind

    tx = payments.create_transaction(
        db,
        kind=TransactionKind.PARTS_ORDER,
        customer_id=buyer.id,
        provider_id=seller.id,
        amount_minor=10_000_00,
        payment_due=True,
    )
    db.commit()
    mock.OUTBOX.clear()
    paid = client.post(
        f"{API}/payments",
        json={"transaction_id": str(tx.id)},
        headers={**auth_headers(buyer), "Idempotency-Key": "k"},
    ).json()
    client.post(f"{API}/payments/{paid['reference']}/verify", headers=auth_headers(buyer))
    assert [m.to for m in _outbox("email")] == ["buyer@example.com"]
    assert (
        client.get(f"{API}/me/notifications", headers=auth_headers(seller)).json()["items"][0][
            "title"
        ]
        == "Payment held in escrow"
    )


def test_chat_message_is_push_only(client, db):
    a, b = make_user(db, Role.VEHICLE_OWNER, name="Ada"), make_user(db, Role.VEHICLE_OWNER)
    _register(client, b)
    mock.OUTBOX.clear()
    client.post(
        f"{API}/threads",
        json={"recipient_id": str(b.id), "message": "Hello!"},
        headers=auth_headers(a),
    )
    assert _outbox("push")[0].subject == "Ada" and _outbox("push")[0].body == "Hello!"
    assert client.get(f"{API}/me/notifications", headers=auth_headers(b)).json()["items"] == []


def test_device_tokens_follow_the_latest_user(client, db):
    first, second = make_user(db, Role.VEHICLE_OWNER), make_user(db, Role.VEHICLE_OWNER)
    _register(client, first)
    _register(client, second)  # same phone, new owner
    assert (
        client.post(
            f"{API}/me/devices",
            json={"token": "short", "platform": "android"},
            headers=auth_headers(first),
        ).status_code
        == 422
    )
    from app.modules.notifications.models import DeviceToken

    assert [d.user_id for d in db.query(DeviceToken).all()] == [second.id]
    assert (
        client.delete(
            f"{API}/me/devices/fcm-token-0123456789", headers=auth_headers(second)
        ).status_code
        == 204
    )
    assert db.query(DeviceToken).count() == 0


def test_a_dead_provider_does_not_break_the_request(client, db, monkeypatch):
    from app.integrations import registry

    class Broken:
        def send(self, *a, **k):
            raise RuntimeError("provider down")

    monkeypatch.setattr(registry, "get_push", lambda: Broken())
    admin = make_user(db, Role.ADMIN)
    seller = make_user(db, Role.DEALER, kyc=KycStatus.APPROVED)
    _register(client, seller)
    listing = client.post(
        f"{API}/listings",
        json={"type": "vehicle", "title": "A car", "price_minor": 1,
              "attributes": {"make": "Kia", "model": "Rio", "year": 2015, "mileage_km": 1}},
        headers=auth_headers(seller),
    ).json()  # fmt: skip
    ok = client.post(
        f"{API}/admin/listings/{listing['id']}/decision",
        json={"approve": True},
        headers=auth_headers(admin),
    )
    assert ok.status_code == 200
    assert client.get(f"{API}/me/notifications", headers=auth_headers(seller)).json()["unread"] == 1


def test_announcement_audience_channels_and_schedule(client, db):
    admin = make_user(db, Role.ADMIN)
    lagos_mech = make_user(db, Role.MECHANIC, city="Lagos", phone="+2348011111111")
    make_user(db, Role.MECHANIC, city="Abuja", phone="+2348022222222")
    make_user(db, Role.VEHICLE_OWNER, city="Lagos")
    body = {
        "title": "New commission rates",
        "body": "Rates change on Monday.",
        "roles": ["mechanic"],
        "city": "Lagos",
        "channels": ["sms", "email"],
    }
    mock.OUTBOX.clear()
    sent = client.post(f"{API}/admin/announcements", json=body, headers=auth_headers(admin))
    assert (
        sent.status_code == 201
        and sent.json()["status"] == "sent"
        and sent.json()["recipients_count"] == 1
    )
    assert [m.to for m in _outbox("sms")] == ["+2348011111111"]
    assert (
        client.get(f"{API}/me/notifications", headers=auth_headers(lagos_mech)).json()["items"][0][
            "title"
        ]
        == "New commission rates"
    )

    later = (utcnow() + timedelta(days=1)).isoformat()
    scheduled = client.post(
        f"{API}/admin/announcements",
        json={**body, "roles": [], "city": None, "send_at": later},
        headers=auth_headers(admin),
    ).json()
    assert scheduled["status"] == "scheduled"
    assert service.send_due_announcements(db) == 0
    from app.modules.notifications.models import Announcement

    row = db.get(Announcement, scheduled["id"])
    row.send_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert service.send_due_announcements(db) == 1
    assert service.send_due_announcements(db) == 0  # never sent twice
    cancel_me = client.post(
        f"{API}/admin/announcements", json={**body, "send_at": later}, headers=auth_headers(admin)
    ).json()
    assert (
        client.delete(
            f"{API}/admin/announcements/{cancel_me['id']}", headers=auth_headers(admin)
        ).json()["status"]
        == "cancelled"
    )


def test_announcement_validation_and_permissions(client, db):
    admin = make_user(db, Role.ADMIN)
    user = make_user(db, Role.VEHICLE_OWNER)
    ok = {"title": "Hello all", "body": "Test message", "channels": ["push"]}
    assert (
        client.post(f"{API}/admin/announcements", json=ok, headers=auth_headers(user)).status_code
        == 403
    )
    assert (
        client.post(
            f"{API}/admin/announcements",
            json={**ok, "roles": ["wizard"]},
            headers=auth_headers(admin),
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"{API}/admin/announcements", json={**ok, "channels": []}, headers=auth_headers(admin)
        ).status_code
        == 422
    )
    assert client.get(f"{API}/admin/announcements", headers=auth_headers(admin)).status_code == 200


@pytest.mark.parametrize("event", ["kyc.decided"])
def test_kyc_decision_notifies_by_all_channels(client, db, event):
    user = make_user(db, Role.DEALER, phone="+2348033333333", email="dealer@example.com")
    from app.core.events import publish

    mock.OUTBOX.clear()
    publish(event, user_id=str(user.id), submission_id="x", approved=True, reason=None)
    assert {m.channel for m in mock.OUTBOX} == {"sms", "email"}  # no device registered, so no push
