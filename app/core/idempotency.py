"""Idempotency keys for every write that moves money.

A mobile app on a weak connection will retry a request it thinks failed. Without
protection a retried checkout would charge twice. Clients send an `Idempotency-Key`
header; the first request runs and its response is stored, and a retry with the
same key and same body gets the stored response back instead of running again.

Usage in a service::

    return run_idempotent(db, user_id, key, "checkout", payload, lambda: do_checkout(...))

`do_checkout` must return something JSON-serialisable (a dict) and must NOT commit:
`run_idempotent` commits once, so the operation's changes and the stored response
are saved together. If they were saved separately, a crash in between would let a
retry charge a second time.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.db import Base, Timestamps, UUIDPrimaryKey, utcnow
from app.core.errors import BadRequest, Conflict


class IdempotencyRecord(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("user_id", "scope", "key"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(64))  # which operation, e.g. "checkout"
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def _hash_request(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_idempotent(
    db: Session,
    user_id: uuid.UUID,
    key: str | None,
    scope: str,
    payload: Any,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run `operation` once per (user, scope, key); replay the stored response on retries."""
    if not key:
        raise BadRequest("The Idempotency-Key header is required", code="idempotency_key_required")
    request_hash = _hash_request(payload)
    existing = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.key == key,
        )
    )
    if existing:
        if existing.request_hash != request_hash:
            raise Conflict(
                "This Idempotency-Key was already used with a different request",
                code="idempotency_key_reused",
            )
        return existing.response

    response = operation()
    db.add(
        IdempotencyRecord(
            user_id=user_id, scope=scope, key=key, request_hash=request_hash, response=response
        )
    )
    db.commit()
    return response
