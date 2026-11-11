"""The demo seed must keep working as the models evolve (it is what the demo runs on)."""

from __future__ import annotations

from sqlalchemy import func, select

from app import seed
from app.modules.catalog.models import Listing
from app.modules.payments.models import Transaction


def test_seed_builds_demo_data_and_is_repeatable(db):
    seed.run(db)
    seed.run(db)  # second run is a no-op
    assert db.scalar(select(func.count()).select_from(Listing)) == 10
    assert db.scalar(select(func.count()).select_from(Transaction)) == 4
