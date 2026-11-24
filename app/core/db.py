"""Database engine, session handling and shared model base classes.

Conventions used by every module:

* Primary keys are UUIDs. Entities the admin dashboard shows to humans (orders,
  disputes, listings...) also get a short reference such as `ORD-80312` built from
  a Postgres sequence (see `ref_column`).
* All timestamps are timezone-aware UTC.
* Money is stored as integer minor units (kobo for NGN) in a `*_minor` column,
  never as a float.
* Services call `db.commit()` once at the end of each public write operation.
  Private helpers (names starting with `_`) only `flush()`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import DateTime, Identity, Integer, MetaData, create_engine, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import get_settings

# Stable constraint names keep Alembic migrations predictable across environments.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Parent of every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def enum_type(enum_cls: type[Enum]) -> SAEnum:
    """Store a Python `StrEnum` as its plain string value in a VARCHAR column.

    No native Postgres enum and no CHECK constraint: adding a new status later is
    just a code change with no migration. Invalid values are still rejected by the
    Python enum when rows are written or loaded.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=32,
        create_constraint=False,
        values_callable=lambda e: [member.value for member in e],
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


class UUIDPrimaryKey:
    """Mixin: `id` UUID primary key generated in Python."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class Timestamps:
    """Mixin: `created_at` / `updated_at`, maintained automatically."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, onupdate=utcnow
    )


def ref_column(start: int = 1000) -> Mapped[int]:
    """A human-friendly counter column (Postgres identity, not the primary key).

    Models expose it as `ref_no`; the display form (e.g. ``ORD-1042``) is built by
    the model's `ref` property so the prefix lives next to the model. Postgres
    assigns the number on insert, so it is unique and gap-tolerant.
    """
    return mapped_column(Integer, Identity(start=start), unique=True)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Create the engine lazily so tests can point it at a different database first."""
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url_sync,
            pool_pre_ping=True,  # survives Neon scale-to-zero and dropped connections
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
        )
        _session_factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Dispose the engine (used by tests after changing DATABASE_URL)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def new_session() -> Session:
    """A standalone session for scripts, workers and WebSocket handlers."""
    get_engine()
    assert _session_factory is not None
    return _session_factory()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed afterwards.

    Rolls back automatically if the request fails before the service committed.
    """
    db = new_session()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
