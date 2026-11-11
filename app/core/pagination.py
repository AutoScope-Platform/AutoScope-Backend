"""List pagination helpers.

Two styles, for two kinds of client:

* **Cursor pagination** (`paginate_cursor`) for the mobile apps' infinite-scroll lists.
  Stable while rows are added, fast on big tables. Orders by newest first.
* **Page pagination** (`paginate_page`) for the admin dashboard's tables, which show
  "1-15 of 64" and page numbers, so they need a total.

Both take a SQLAlchemy `select()` over one ORM model and return the rows plus
paging metadata. Schemas for the responses are `CursorPage` and `PageResult`.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest

T = TypeVar("T")

MAX_LIMIT = 100


class CursorPage[T](BaseModel):
    items: list[T]
    next_cursor: str | None = Field(
        None, description="Pass as `cursor` to fetch the next page. Null on the last page."
    )


class PageResult[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


def _encode(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "id": str(row_id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(data["t"]), uuid.UUID(data["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise BadRequest("Invalid cursor", code="invalid_cursor") from exc


def paginate_cursor(
    db: Session, stmt: Select, model: Any, *, limit: int = 20, cursor: str | None = None
) -> tuple[list[Any], str | None]:
    """Return (rows, next_cursor), newest first, ordered by (created_at, id)."""
    limit = max(1, min(limit, MAX_LIMIT))
    if cursor:
        created_at, row_id = _decode(cursor)
        stmt = stmt.where(
            or_(
                model.created_at < created_at,
                and_(model.created_at == created_at, model.id < row_id),
            )
        )
    stmt = stmt.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
    rows = list(db.scalars(stmt))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = _encode(last.created_at, last.id)
    return rows, next_cursor


def paginate_page(
    db: Session, stmt: Select, *, page: int = 1, page_size: int = 15
) -> tuple[list[Any], int]:
    """Return (rows, total) for a numbered page. `stmt` must already have its ordering."""
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_LIMIT))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = list(db.scalars(stmt.limit(page_size).offset((page - 1) * page_size)))
    return rows, total
