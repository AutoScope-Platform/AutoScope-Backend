"""Shared test setup.

Tests run against a real PostgreSQL database with PostGIS (never SQLite), because the
code relies on Postgres features. Each test runs inside a transaction that is rolled
back afterwards, so tests cannot affect each other.

Set `TEST_DATABASE_URL` to point at another database (CI uses a PostGIS service).
"""

from __future__ import annotations

import os
import tempfile

# Settings must be in the environment before the app is imported.
os.environ["ENV"] = "test"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://autoscope:autoscope@127.0.0.1:5432/autoscope_test"
)
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["LOCAL_STORAGE_DIR"] = tempfile.mkdtemp(prefix="autoscope-test-storage-")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import app.core.db as db_module  # noqa: E402
from app import models  # noqa: E402,F401
from app.core.db import Base, get_db, get_engine  # noqa: E402
from app.integrations import mock, registry  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    engine = get_engine()
    with engine.begin() as conn:
        for ext in ("postgis", "pg_trgm", "pgcrypto"):
            conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine, monkeypatch):
    """A session whose changes are rolled back at the end of the test.

    The service layer calls `db.commit()`; `create_savepoint` turns each commit into a
    savepoint release, so nothing is really committed. Handlers that open their own
    session (event subscribers) are pointed at the same connection.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    monkeypatch.setattr(
        db_module,
        "new_session",
        lambda: Session(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        ),
    )
    registry.reset()
    mock.OUTBOX.clear()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
