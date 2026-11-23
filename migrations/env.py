"""Alembic environment: wires migrations to the app's settings and models.

Neon note: run migrations against the DIRECT (non-pooled) connection string. Set
`MIGRATION_DATABASE_URL` for that; otherwise DATABASE_URL is used.

Only one migration run at a time: a Postgres advisory lock is held while migrating, so two
instances starting together (or a deploy job and an instance) cannot apply the same
migration twice. The second waits, then finds nothing to do.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.core.config import get_settings
from app.core.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def _url() -> str:
    override = os.environ.get("MIGRATION_DATABASE_URL")
    if override:
        # Same driver rewrite the app does (postgresql:// -> postgresql+psycopg://).
        return override.replace("postgresql://", "postgresql+psycopg://", 1)
    return get_settings().database_url_sync


def include_object(obj, name, type_, reflected, compare_to):
    """PostGIS and tiger-geocoder own some tables; never try to manage them."""
    return not (type_ == "table" and name in {"spatial_ref_sys"})


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


MIGRATION_LOCK_ID = 726_501_001  # any fixed number; shared by every migration run


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        connection.commit()  # the lock is held by the session, not the transaction
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_object=include_object,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
