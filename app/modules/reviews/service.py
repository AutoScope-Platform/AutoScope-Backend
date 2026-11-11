"""Reviews business logic. (Written in full in the reviews step; `ratings_for` is used by catalog.)"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.reviews.models import RatingSummary


def ratings_for(db: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[float, int]]:
    """{user id: (average rating, number of reviews)} for the users that have any reviews."""
    if not user_ids:
        return {}
    rows = db.execute(select(RatingSummary).where(RatingSummary.user_id.in_(user_ids))).scalars()
    return {r.user_id: (round(r.average, 1), r.count) for r in rows}
