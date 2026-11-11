"""In-process fan-out for live tracking (WebSocket).

Each open tracking connection registers an asyncio queue here. When a provider reports a
position (a normal synchronous request running in a worker thread), `broadcast` hands the
message to every connected queue in a thread-safe way.

Limit: this lives in one process. With several API instances a customer might be connected
to a different instance than the one receiving the provider's update. For that, publish to
Redis and have each instance forward to its own connections (see docs/ARCHITECTURE.md).
The tracking endpoint `GET /haulage/jobs/{id}/tracking` works across instances regardless.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from typing import Any

_subscribers: dict[uuid.UUID, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = defaultdict(
    set
)


def subscribe(job_id: uuid.UUID) -> asyncio.Queue:
    """Call from async code: returns a queue that will receive this job's updates."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers[job_id].add((asyncio.get_running_loop(), queue))
    return queue


def unsubscribe(job_id: uuid.UUID, queue: asyncio.Queue) -> None:
    _subscribers[job_id] = {s for s in _subscribers[job_id] if s[1] is not queue}
    if not _subscribers[job_id]:
        del _subscribers[job_id]


def broadcast(job_id: uuid.UUID, message: dict[str, Any]) -> None:
    """Safe to call from any thread. A full queue (slow client) just drops the update."""
    for loop, queue in list(_subscribers.get(job_id, ())):
        loop.call_soon_threadsafe(_put, queue, message)


def _put(queue: asyncio.Queue, message: dict[str, Any]) -> None:
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        pass
