"""Messaging: threads, messages, attachments, read receipts and the chat WebSocket."""

from __future__ import annotations

import pytest

from app.core.permissions import Role
from app.core.security import create_token
from tests.factories import auth_headers, make_user

API = "/api/v1"


@pytest.fixture
def pair(db):
    return make_user(db, Role.VEHICLE_OWNER, name="Buyer Bola"), make_user(
        db, Role.DEALER, business_name="Car Mart"
    )


def _start(client, sender, recipient, text="Is it still available?", **extra):
    return client.post(
        f"{API}/threads",
        json={"recipient_id": str(recipient.id), "message": text, **extra},
        headers=auth_headers(sender),
    )


def test_start_reuses_thread_per_pair_and_listing(client, db, pair):
    buyer, seller = pair
    first = _start(client, buyer, seller)
    assert first.status_code == 201
    again = _start(client, seller, buyer, "Yes, it is")  # the seller replies by "starting" it too
    assert again.json()["thread_id"] == first.json()["thread_id"]
    assert _start(client, buyer, buyer).status_code == 422
    assert (
        _start(client, buyer, make_user(db, Role.DEALER)).json()["thread_id"]
        != first.json()["thread_id"]
    )
    messages = client.get(
        f"{API}/threads/{first.json()['thread_id']}/messages", headers=auth_headers(buyer)
    ).json()
    assert [m["body"] for m in messages["items"]] == [
        "Yes, it is",
        "Is it still available?",
    ]  # newest first


def test_inbox_unread_and_read_receipts(client, db, pair):
    buyer, seller = pair
    thread = _start(client, buyer, seller).json()["thread_id"]
    client.post(
        f"{API}/threads/{thread}/messages",
        json={"body": "Can I see it Saturday?"},
        headers=auth_headers(buyer),
    )
    inbox = client.get(f"{API}/threads", headers=auth_headers(seller)).json()["items"]
    assert inbox[0]["other"]["name"] == "Buyer Bola" and inbox[0]["unread"] == 2
    assert client.get(f"{API}/threads/unread-count", headers=auth_headers(seller)).json() == {
        "unread": 2
    }
    assert (
        client.get(f"{API}/threads", headers=auth_headers(buyer)).json()["items"][0]["unread"] == 0
    )
    assert client.post(f"{API}/threads/{thread}/read", headers=auth_headers(seller)).json() == {
        "marked": 2
    }
    assert client.get(f"{API}/threads/unread-count", headers=auth_headers(seller)).json() == {
        "unread": 0
    }
    seen = client.get(f"{API}/threads/{thread}/messages", headers=auth_headers(buyer)).json()[
        "items"
    ]
    assert all(m["read"] for m in seen)


def test_only_participants_can_read_or_write(client, db, pair):
    buyer, seller = pair
    thread = _start(client, buyer, seller).json()["thread_id"]
    outsider = make_user(db, Role.VEHICLE_OWNER)
    for method, path in (("get", "messages"), ("post", "read")):
        assert (
            getattr(client, method)(
                f"{API}/threads/{thread}/{path}", headers=auth_headers(outsider)
            ).status_code
            == 404
        )
    assert (
        client.post(
            f"{API}/threads/{thread}/messages", json={"body": "hi"}, headers=auth_headers(outsider)
        ).status_code
        == 404
    )
    assert client.get(f"{API}/threads").status_code == 401


def test_attachments(client, db, pair):
    buyer, seller = pair
    thread = _start(client, buyer, seller).json()["thread_id"]
    ok = client.post(
        f"{API}/threads/{thread}/attachments",
        data={"caption": "Engine bay"},
        files={"file": ("engine.jpg", b"\xff\xd8\xff" + b"x" * 40, "image/jpeg")},
        headers=auth_headers(seller),
    )
    assert (
        ok.status_code == 201
        and ok.json()["attachment_name"] == "engine.jpg"
        and ok.json()["attachment_url"]
    )
    bad = client.post(
        f"{API}/threads/{thread}/attachments",
        files={"file": ("run.exe", b"MZ", "application/octet-stream")},
        headers=auth_headers(seller),
    )
    assert bad.status_code == 400
    # The file itself is served through the signed link.
    assert client.get(ok.json()["attachment_url"]).content.startswith(b"\xff\xd8\xff")


def test_listing_context_in_inbox(client, db, pair):
    buyer, seller = pair
    from app.modules.identity.models import KycStatus

    seller.kyc_status = KycStatus.APPROVED
    db.commit()
    listing = client.post(
        f"{API}/listings",
        json={"type": "vehicle", "title": "2018 Camry", "price_minor": 1,
              "attributes": {"make": "Toyota", "model": "Camry", "year": 2018, "mileage_km": 1}},
        headers=auth_headers(seller),
    ).json()  # fmt: skip
    _start(client, buyer, seller, listing_id=listing["id"])
    inbox = client.get(f"{API}/threads", headers=auth_headers(buyer)).json()["items"]
    assert inbox[0]["listing_title"] == "2018 Camry"
    assert (
        _start(client, buyer, seller, listing_id="00000000-0000-0000-0000-000000000000").status_code
        == 404
    )


def test_chat_websocket_pushes_messages_and_read_receipts(client, db, pair):
    buyer, seller = pair
    with client.websocket_connect(f"{API}/ws/chat?token={create_token(seller.id)}") as ws:
        assert ws.receive_json() == {"type": "ready"}
        thread = _start(client, buyer, seller, "Hello there").json()["thread_id"]
        pushed = ws.receive_json()
        assert pushed["type"] == "message" and pushed["body"] == "Hello there"
        client.post(f"{API}/threads/{thread}/read", headers=auth_headers(seller))
    with client.websocket_connect(f"{API}/ws/chat?token={create_token(buyer.id)}") as ws:
        ws.receive_json()
        client.post(
            f"{API}/threads/{thread}/messages", json={"body": "Reply"}, headers=auth_headers(seller)
        )
        assert ws.receive_json()["body"] == "Reply"
        client.post(f"{API}/threads/{thread}/read", headers=auth_headers(seller))
    with pytest.raises(Exception):  # noqa: B017,PT011 - refused without a valid token
        with client.websocket_connect(f"{API}/ws/chat?token=nope"):
            pass
