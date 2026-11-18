"""Fan-out of messages to open WebSocket connections, across API instances.

Each connection registers an asyncio queue under a key (a job id, a user id...). Normal
synchronous endpoints run in worker threads; `Hub.broadcast` is safe to call from them.

Two modes, chosen by `REDIS_URL`:

* **No Redis** (local development, tests, a single instance): a broadcast goes straight to
  the queues in this process.
* **Redis** (staging/production, several API instances, the Celery worker): a broadcast is
  published on the channel ``rt:<hub>:<key>``. Every process that has sockets open listens
  on ``rt:<hub>:*`` in a background thread and hands each message to its own queues, so a
  message reaches the person whichever instance they are connected to, and the worker can
  push live events too.

Hubs in use: `user_events` (one per signed-in person: chat messages, read receipts and
notifications, served at `/ws/events` and `/ws/chat`) and the haulage tracking hub (one per
job, `/ws/jobs/{id}/track`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class Hub:
    def __init__(self, name: str = "default") -> None:
        self.name = name
        self._subscribers: dict[uuid.UUID, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = (
            defaultdict(set)
        )
        self._lock = threading.Lock()
        self._redis: Any | None = None
        self._redis_checked = False
        self._listener: threading.Thread | None = None

    # --- transport -----------------------------------------------------------------------
    def use_redis(self, client: Any | None) -> None:
        """Set the Redis client explicitly (tests pass a fake; None = local only)."""
        self._redis, self._redis_checked = client, True

    def _redis_client(self) -> Any | None:
        if not self._redis_checked:
            url = get_settings().redis_url
            if url:
                import redis  # noqa: PLC0415

                self._redis = redis.Redis.from_url(url)
            self._redis_checked = True
        return self._redis

    def _channel(self, key: uuid.UUID) -> str:
        return f"rt:{self.name}:{key}"

    # --- subscribing (async side) -----------------------------------------------------
    def subscribe(self, key: uuid.UUID) -> asyncio.Queue:
        """Call from async code: returns a queue that receives this key's messages."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subscribers[key].add((asyncio.get_running_loop(), queue))
        self._ensure_listener()
        return queue

    def unsubscribe(self, key: uuid.UUID, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers[key] = {s for s in self._subscribers[key] if s[1] is not queue}
            if not self._subscribers[key]:
                del self._subscribers[key]

    # --- publishing (any thread) --------------------------------------------------------
    def broadcast(self, key: uuid.UUID, message: dict[str, Any]) -> None:
        """Safe from any thread. A full queue (slow client) just drops the message."""
        client = self._redis_client()
        if client is None:
            self._deliver_local(key, message)
            return
        try:
            client.publish(self._channel(key), json.dumps(message, default=str))
        except Exception:  # noqa: BLE001 - Redis down: at least reach this instance's sockets
            logger.exception("Could not publish to Redis; delivering locally only")
            self._deliver_local(key, message)

    def _deliver_local(self, key: uuid.UUID, message: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers.get(key, ()))
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(self._put, queue, message)
            except RuntimeError:  # the loop closed while we were looking
                pass

    @staticmethod
    def _put(queue: asyncio.Queue, message: dict[str, Any]) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass

    # --- Redis listener -------------------------------------------------------------------
    def _ensure_listener(self) -> None:
        """Start the background thread that forwards Redis messages to local sockets."""
        client = self._redis_client()
        if client is None:
            return
        with self._lock:
            if self._listener is not None and self._listener.is_alive():
                return
            self._listener = threading.Thread(
                target=self._listen, args=(client,), name=f"hub-{self.name}", daemon=True
            )
            self._listener.start()

    def _listen(self, client: Any) -> None:
        pattern = f"rt:{self.name}:*"
        while True:
            try:
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.psubscribe(pattern)
                for item in pubsub.listen():
                    if item and item.get("type") == "pmessage":
                        self.on_redis_message(item["channel"], item["data"])
            except Exception:  # noqa: BLE001 - reconnect after a Redis blip
                logger.exception("Redis listener for hub %s stopped; reconnecting", self.name)
                time.sleep(1)

    def on_redis_message(self, channel: bytes | str, data: bytes | str) -> None:
        """Decode one Redis message and hand it to this process's sockets."""
        try:
            channel = channel.decode() if isinstance(channel, bytes) else channel
            key = uuid.UUID(channel.rsplit(":", 1)[1])
            message = json.loads(data)
        except (ValueError, IndexError):
            logger.warning("Ignoring malformed realtime message on %s", channel)
            return
        self._deliver_local(key, message)


# One channel per signed-in person: chat, read receipts and live notifications.
user_events = Hub("user")


async def pump(websocket, hub: Hub, key: uuid.UUID, first: dict[str, Any] | None = None) -> None:
    """Run one WebSocket connection: send `first`, then every broadcast for `key`, until the
    client disconnects. Shared by tracking and chat so the loop is written (and fixed) once."""
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    queue = hub.subscribe(key)
    try:
        if first is not None:
            await websocket.send_json(first)
        receiver = asyncio.create_task(websocket.receive_text())  # notices the client leaving
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                getter.cancel()
                receiver.result()  # raises WebSocketDisconnect when the client closed
                receiver = asyncio.create_task(websocket.receive_text())  # ignore chatter
            if getter in done:
                await websocket.send_json(getter.result())
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(key, queue)
