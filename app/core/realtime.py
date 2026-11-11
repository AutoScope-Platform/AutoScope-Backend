"""Thread-safe fan-out of messages to open WebSocket connections.

Each connection registers an asyncio queue under a key (a job id, a user id...). Normal
synchronous endpoints run in worker threads; `Hub.broadcast` is safe to call from them and
hands the message to every queue on its own event loop.

Limit: one process only. With several API instances, publish to Redis and have each
instance forward to its own connections (see docs/ARCHITECTURE.md). The REST endpoints
that return the same data keep working across instances.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from typing import Any


class Hub:
    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = (
            defaultdict(set)
        )

    def subscribe(self, key: uuid.UUID) -> asyncio.Queue:
        """Call from async code: returns a queue that receives this key's messages."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers[key].add((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, key: uuid.UUID, queue: asyncio.Queue) -> None:
        self._subscribers[key] = {s for s in self._subscribers[key] if s[1] is not queue}
        if not self._subscribers[key]:
            del self._subscribers[key]

    def broadcast(self, key: uuid.UUID, message: dict[str, Any]) -> None:
        """Safe from any thread. A full queue (slow client) just drops the message."""
        for loop, queue in list(self._subscribers.get(key, ())):
            loop.call_soon_threadsafe(self._put, queue, message)

    @staticmethod
    def _put(queue: asyncio.Queue, message: dict[str, Any]) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass


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
