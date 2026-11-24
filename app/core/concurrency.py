"""Caps how many HTTP requests one API process works on at once.

Why: FastAPI runs our (synchronous) endpoints and dependencies on a pool of 40 threads, but
each process has far fewer database connections. Under a burst of traffic, requests that
already held a connection waited for a free thread, while every thread was busy with a new
request waiting for a connection. Nothing could move, and the API stayed stuck, even
`/health`, after the burst had ended, until it was restarted (load test, Nov 2026).

So at most `max_concurrent_requests` requests run at once, half the connections, because a
request can hold two (its own session plus an event handler's). A request that is let in
always gets its connections. The others wait here, holding neither a thread nor a connection,
and answer 503 "busy" (with Retry-After) if they wait longer than `request_queue_seconds`.
`/health` skips the queue, so a busy API is not mistaken for a dead one.
"""

from __future__ import annotations

import asyncio
import json

from starlette.types import ASGIApp, Receive, Scope, Send

SKIP_PATHS = ("/health",)  # exact: /health/ready still needs a connection, so it queues

BUSY_BODY = json.dumps(
    {
        "error": {
            "code": "service_busy",
            "message": "The service is busy. Please try again in a moment.",
            "details": None,
        }
    }
).encode()


def concurrency_limit(pool_size: int, max_overflow: int, configured: int | None) -> int:
    if configured:
        return max(1, configured)
    return max(1, (pool_size + max_overflow) // 2)


class ConcurrencyLimitMiddleware:
    def __init__(self, app: ASGIApp, limit: int, queue_seconds: float) -> None:
        self.app = app
        self.limit = limit
        self.queue_seconds = queue_seconds
        self._semaphore: asyncio.Semaphore | None = None  # made in the server's event loop

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in SKIP_PATHS:
            await self.app(scope, receive, send)
            return
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.limit)
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.queue_seconds)
        except TimeoutError:
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", b"2"),
                        (b"content-length", str(len(BUSY_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": BUSY_BODY})
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self._semaphore.release()
