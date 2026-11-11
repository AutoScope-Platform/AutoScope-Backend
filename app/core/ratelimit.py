"""Simple fixed-window rate limiting (FSD 5.2).

Used as a route dependency on the endpoints attackers target: sign-up, OTP
requests, login, listing creation and payments::

    @router.post("/register", dependencies=[Depends(rate_limit("register", 5, 60))])

Counters live in Redis when `REDIS_URL` is set (shared by every API instance) and
in process memory otherwise (fine for local development and a single instance).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import lru_cache

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import TooManyRequests


def client_ip(request: Request) -> str:
    """Best-effort client address.

    Behind Render's proxy the real client is the *last* entry of X-Forwarded-For (the
    proxy appends it; earlier entries can be forged by the client).
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class _MemoryCounter:
    def __init__(self) -> None:
        self._windows: dict[str, tuple[int, float]] = {}

    def hit(self, key: str, window_seconds: int) -> int:
        now = time.monotonic()
        count, expires = self._windows.get(key, (0, 0.0))
        if now >= expires:
            count, expires = 0, now + window_seconds
        count += 1
        self._windows[key] = (count, expires)
        return count

    def clear(self) -> None:
        self._windows.clear()


class _RedisCounter:
    def __init__(self, url: str) -> None:
        import redis  # noqa: PLC0415

        self._redis = redis.Redis.from_url(url)

    def hit(self, key: str, window_seconds: int) -> int:
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count, _ = pipe.execute()
        return int(count)

    def clear(self) -> None:  # pragma: no cover - only meaningful for the memory counter
        pass


@lru_cache
def _counter() -> _MemoryCounter | _RedisCounter:
    url = get_settings().redis_url
    return _RedisCounter(url) if url else _MemoryCounter()


def reset_counters() -> None:
    """Clear in-memory counters (tests)."""
    _counter().clear()


def rate_limit(name: str, limit: int, window_seconds: int) -> Callable[[Request], None]:
    """Build a dependency allowing `limit` requests per `window_seconds` per client IP."""

    def dependency(request: Request) -> None:
        if not get_settings().rate_limit_enabled:
            return
        key = f"rl:{name}:{client_ip(request)}"
        if _counter().hit(key, window_seconds) > limit:
            raise TooManyRequests("Too many requests. Please wait a moment and try again.")

    return dependency
