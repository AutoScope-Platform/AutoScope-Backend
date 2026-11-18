"""Phase 2c: chat about bookings and tow jobs (FR-12.1, FR-5.7), retry-safe sending
(FSD 5.5), live notification events on /ws/events, push/SMS/e-mail through the worker
(FSD 7.2), and the Redis fan-out that makes live events work across API instances."""

from __future__ import annotations

import asyncio
import queue
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.realtime import Hub, user_events
from app.core.security import create_token
from app.integrations import mock
from app.modules.messaging.models import Message
from app.modules.notifications import service as notifications
from tests.factories import auth_headers, make_user
from tests.test_haulage import _offers, _provider, _request
from tests.test_haulage import _pay as _pay_job
from tests.test_money_rules import (  # noqa: F401  (fixture)
    _paid_booking,
    admin,  # noqa: F401  (fixture)
    workshop,
)

API = "/api/v1"


@pytest.fixture(autouse=True)
def local_hub():
    """These tests run without Redis: live events stay in this process."""
    user_events.use_redis(None)
    yield
    user_events.use_redis(None)


def _start(client, user, **body):
    return client.post(
        f"{API}/threads", json={"message": "Hello", **body}, headers=auth_headers(user)
    )


# --- Chat about a booking or a tow job -------------------------------------------------------
def test_chat_about_a_booking(client, db, workshop):  # noqa: F811
    mechanic, service = workshop
    customer = make_user(db)
    booking = _paid_booking(client, customer, mechanic, service, 10)

    started = _start(client, customer, booking_id=booking["id"], message="What time exactly?")
    assert started.status_code == 201, started.text
    thread_id = started.json()["thread_id"]
    # The mechanic sees it, labelled with the service, and can find it by booking.
    mine = client.get(
        f"{API}/threads", params={"booking_id": booking["id"]}, headers=auth_headers(mechanic)
    ).json()
    assert [t["id"] for t in mine["items"]] == [thread_id]
    assert (
        mine["items"][0]["subject_title"] == "Full service"
        and mine["items"][0]["booking_id"] == booking["id"]
    )
    # Replying through the same subject reuses the thread; a general chat is separate.
    again = _start(client, mechanic, booking_id=booking["id"], message="10am")
    assert again.json()["thread_id"] == thread_id
    general = _start(client, customer, recipient_id=str(mechanic.id))
    assert general.json()["thread_id"] != thread_id

    outsider = _start(client, make_user(db), booking_id=booking["id"])
    assert outsider.status_code == 404
    wrong_person = _start(
        client, customer, booking_id=booking["id"], recipient_id=str(make_user(db).id)
    )
    assert wrong_person.status_code == 422 and wrong_person.json()["error"]["code"] == "not_a_party"
    two = _start(client, customer, booking_id=booking["id"], listing_id=service["id"])
    assert two.status_code == 400 and two.json()["error"]["code"] == "one_subject"
    nobody = _start(client, customer)
    assert nobody.status_code == 400 and nobody.json()["error"]["code"] == "recipient_required"


def test_chat_about_a_tow_job(client, db):
    customer = make_user(db)
    driver = _provider(client, db, 6.4300, 3.4200)
    job = _request(client, customer)
    early = _start(client, customer, job_id=job["id"])
    assert early.status_code == 422 and early.json()["error"]["code"] == "no_provider"

    _pay_job(client, customer, job)
    offer = _offers(client, driver)[0]
    client.post(f"{API}/haulage/offers/{offer['offer_id']}/accept", headers=auth_headers(driver))
    started = _start(client, customer, job_id=job["id"], message="I'm by the blue gate")
    assert started.status_code == 201, started.text
    threads = client.get(f"{API}/threads", headers=auth_headers(driver)).json()["items"]
    assert threads[0]["job_id"] == job["id"] and threads[0]["subject_title"].startswith("Tow to")


# --- Retry-safe sending (FSD 5.5) ------------------------------------------------------------
def test_resending_with_the_same_client_id_does_not_duplicate(client, db):
    alice, bob = make_user(db), make_user(db)
    first = _start(client, alice, recipient_id=str(bob.id), client_id="c-1").json()
    retry = _start(client, alice, recipient_id=str(bob.id), client_id="c-1").json()
    assert retry["message"]["id"] == first["message"]["id"]
    assert first["message"]["client_id"] == "c-1"

    thread = first["thread_id"]
    send = lambda cid: client.post(  # noqa: E731
        f"{API}/threads/{thread}/messages", json={"body": "On my way", "client_id": cid},
        headers=auth_headers(alice),
    )  # fmt: skip
    a, b = send("c-2").json(), send("c-2").json()
    assert a["id"] == b["id"]
    # Another person may use the same client id: ids are unique per sender.
    bob_msg = client.post(
        f"{API}/threads/{thread}/messages", json={"body": "OK", "client_id": "c-2"},
        headers=auth_headers(bob),
    ).json()  # fmt: skip
    assert bob_msg["id"] != a["id"]

    photo = b"\xff\xd8\xff\xe0" + b"img" * 20
    upload = lambda: client.post(  # noqa: E731
        f"{API}/threads/{thread}/attachments", files={"file": ("dent.jpg", photo, "image/jpeg")},
        data={"client_id": "c-3"}, headers=auth_headers(alice),
    )  # fmt: skip
    assert upload().json()["id"] == upload().json()["id"]
    total = db.scalar(
        select(func.count()).select_from(Message).where(Message.thread_id == uuid.UUID(thread))
    )
    assert total == 4  # hello, on my way, OK, the photo: no duplicates


def test_duplicate_send_does_not_push_twice(client, db):
    alice, bob = make_user(db), make_user(db)
    thread = _start(client, alice, recipient_id=str(bob.id)).json()["thread_id"]
    with client.websocket_connect(f"{API}/ws/events?token={create_token(bob.id)}") as ws:
        assert ws.receive_json() == {"type": "ready"}
        for _ in range(2):
            client.post(
                f"{API}/threads/{thread}/messages", json={"body": "Once", "client_id": "same"},
                headers=auth_headers(alice),
            )  # fmt: skip
        client.post(
            f"{API}/threads/{thread}/messages", json={"body": "Next"}, headers=auth_headers(alice)
        )
        assert ws.receive_json()["body"] == "Once"
        assert ws.receive_json()["body"] == "Next"  # the retry was not pushed


# --- Live notification events (provider queues refresh without polling) ----------------------
def test_booking_request_reaches_the_mechanic_live(client, db, workshop):  # noqa: F811
    from tests.test_money_rules import next_wednesday

    mechanic, service = workshop
    customer = make_user(db)
    with client.websocket_connect(f"{API}/ws/events?token={create_token(mechanic.id)}") as ws:
        assert ws.receive_json() == {"type": "ready"}
        r = client.post(
            f"{API}/bookings",
            json={"listing_id": service["id"], "scheduled_for": next_wednesday(10).isoformat()},
            headers=auth_headers(customer),
        )
        assert r.status_code == 201, r.text
        event = ws.receive_json()
    assert event["type"] == "notification" and event["kind"] == "booking.requested"
    assert event["data"]["booking_id"] == r.json()["id"] and event["title"] == "New booking request"
    inbox = client.get(f"{API}/me/notifications", headers=auth_headers(mechanic)).json()
    assert inbox["items"][0]["kind"] == "booking.requested"  # committed before the push


# --- Push/SMS/e-mail through the background worker (FSD 7.2) ---------------------------------
def test_channels_are_queued_for_the_worker(client, db, monkeypatch):
    import app.worker as worker

    queued = []
    monkeypatch.setattr(worker.send_notification, "delay", queued.append)
    monkeypatch.setattr(
        notifications, "get_settings",
        lambda: Settings(task_queue="celery", redis_url="redis://localhost:6399/0"),
    )  # fmt: skip
    user = make_user(db, phone="+2348031110000")
    mock.OUTBOX.clear()
    notice = notifications.Notice(
        user.id, "Paid", "Your order was paid", {"kind": "x"}, ("push", "sms")
    )
    notifications.dispatch([notifications.deliver(db, notice)])
    db.commit()
    assert mock.OUTBOX == []  # nothing sent during the request
    assert (
        queued and queued[0]["channels"] == ["push", "sms"] and queued[0]["user_id"] == str(user.id)
    )

    notifications.send_now(queued[0])  # what the worker does
    assert [m.channel for m in mock.OUTBOX] == ["sms"]  # no device registered, so no push


def test_queue_down_falls_back_to_sending_now(client, db, monkeypatch):
    import app.worker as worker

    def broken(_payload):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(worker.send_notification, "delay", broken)
    monkeypatch.setattr(
        notifications, "get_settings",
        lambda: Settings(task_queue="celery", redis_url="redis://localhost:6399/0"),
    )  # fmt: skip
    user = make_user(db, phone="+2348031110001")
    mock.OUTBOX.clear()
    notice = notifications.Notice(user.id, "Hi", "Body", {}, ("sms",))
    notifications.dispatch([notifications.deliver(db, notice)])
    assert [m.to for m in mock.OUTBOX] == ["+2348031110001"]


def test_celery_queue_needs_redis():
    with pytest.raises(ValueError, match="REDIS_URL"):
        Settings(task_queue="celery", redis_url=None)


# --- Redis fan-out across API instances ------------------------------------------------------
class FakeRedis:
    """Just enough of redis-py: publish() and pubsub().psubscribe()/listen()."""

    def __init__(self) -> None:
        self.listeners: list[queue.Queue] = []
        self.fail = False

    def publish(self, channel: str, data: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        for q in self.listeners:
            q.put({"type": "pmessage", "channel": channel.encode(), "data": data.encode()})
        return len(self.listeners)

    def pubsub(self, ignore_subscribe_messages: bool = True):
        redis = self

        class PubSub:
            def psubscribe(self, pattern):
                self.q = queue.Queue()
                redis.listeners.append(self.q)

            def listen(self):
                while True:
                    yield self.q.get()

        return PubSub()


def test_redis_fans_out_to_every_instance():
    redis = FakeRedis()
    instance_a, instance_b = Hub("t1"), Hub("t1")  # the same hub on two API servers
    instance_a.use_redis(redis)
    instance_b.use_redis(redis)
    key = uuid.uuid4()

    async def scenario():
        on_a, on_b = instance_a.subscribe(key), instance_b.subscribe(key)
        for _ in range(50):  # wait for both listener threads to subscribe
            if len(redis.listeners) == 2:
                break
            await asyncio.sleep(0.02)
        # A worker thread on instance A broadcasts; a socket on instance B gets it too.
        await asyncio.get_running_loop().run_in_executor(
            None, instance_a.broadcast, key, {"type": "notification", "n": 1}
        )
        got = await asyncio.wait_for(asyncio.gather(on_a.get(), on_b.get()), timeout=3)
        assert got == [{"type": "notification", "n": 1}] * 2

        redis.fail = True  # Redis down: this instance's sockets still get it
        instance_a.broadcast(key, {"n": 2})
        assert await asyncio.wait_for(on_a.get(), timeout=3) == {"n": 2}

    asyncio.run(scenario())


def test_malformed_redis_messages_are_ignored():
    hub = Hub("t2")
    hub.on_redis_message(b"rt:t2:not-a-uuid", b"{}")
    hub.on_redis_message(f"rt:t2:{uuid.uuid4()}", b"not json")  # logged, not raised
