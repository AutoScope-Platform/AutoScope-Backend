"""The per-process request limit that keeps the API from locking up under load."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI

from app.core.concurrency import ConcurrencyLimitMiddleware, concurrency_limit
from app.core.headers import SecurityHeadersMiddleware


def make_app(limit: int, queue_seconds: float) -> tuple[FastAPI, dict[str, int]]:
    app = FastAPI()
    app.add_middleware(ConcurrencyLimitMiddleware, limit=limit, queue_seconds=queue_seconds)
    app.add_middleware(SecurityHeadersMiddleware)
    seen = {"running": 0, "peak": 0}

    @app.get("/work")
    async def work() -> dict[str, bool]:
        seen["running"] += 1
        seen["peak"] = max(seen["peak"], seen["running"])
        await asyncio.sleep(0.2)
        seen["running"] -= 1
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app, seen


def run(coro):
    return asyncio.run(coro)


def test_limit_is_half_the_connections_unless_set():
    assert concurrency_limit(5, 5, None) == 5
    assert concurrency_limit(10, 20, None) == 15
    assert concurrency_limit(1, 0, None) == 1
    assert concurrency_limit(10, 20, 4) == 4


def test_extra_requests_wait_their_turn_instead_of_running_at_once():
    app, seen = make_app(limit=2, queue_seconds=5)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await asyncio.gather(*(c.get("/work") for _ in range(6)))

    responses = run(go())
    assert [r.status_code for r in responses] == [200] * 6
    assert seen["peak"] == 2


def test_a_request_that_waits_too_long_gets_503_busy_with_headers():
    app, _ = make_app(limit=1, queue_seconds=0.05)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await asyncio.gather(c.get("/work"), c.get("/work"))

    responses = run(go())
    assert sorted(r.status_code for r in responses) == [200, 503]
    busy = next(r for r in responses if r.status_code == 503)
    assert busy.json()["error"]["code"] == "service_busy"
    assert busy.headers["retry-after"] == "2"
    assert busy.headers["x-frame-options"] == "DENY"  # still gets the security headers


def test_health_answers_even_when_the_api_is_full():
    app, _ = make_app(limit=1, queue_seconds=5)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            slow = asyncio.create_task(c.get("/work"))
            await asyncio.sleep(0.05)  # /work now holds the only slot
            health = await asyncio.wait_for(c.get("/health"), timeout=0.1)
            await slow
            return health

    assert run(go()).status_code == 200
