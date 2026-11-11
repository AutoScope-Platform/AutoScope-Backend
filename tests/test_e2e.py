"""Runs the scripted demo (scripts/demo.py) against the in-process app and the seed data."""

from __future__ import annotations

from app import seed
from scripts import demo


def test_four_journeys_end_to_end(client, db):
    seed.run(db)
    lines: list[str] = []
    demo.run(client, log=lines.append)
    assert lines[-1].strip() == "All journeys completed."
