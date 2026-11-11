"""Audit log shared by every module (FR-1.7, FSD 5.2).

Two kinds of entries, both shown on the admin dashboard's Audit log screen:

* `auth`  - sign-ins, failures, password resets, 2FA and role changes.
* `admin` - every decision an admin takes (KYC approval, listing rejection, dispute
  resolution, payout release...).

Rows are append-only: nothing in the codebase updates or deletes them.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.db import Base, Timestamps, UUIDPrimaryKey, enum_type


class AuditCategory(StrEnum):
    AUTH = "auth"
    ADMIN = "admin"


class AuditLog(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "audit_logs"

    category: Mapped[AuditCategory] = mapped_column(enum_type(AuditCategory), index=True)
    # Nullable because a failed sign-in may not match any account.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # Name copied at write time so the log stays readable if the account is erased.
    actor_name: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(160))
    # What was acted on: a reference like "KYC-2423", or the client for auth events.
    target: Mapped[str | None] = mapped_column(String(160))
    ip_address: Mapped[str | None] = mapped_column(String(64))


def record(
    db: Session,
    *,
    category: AuditCategory,
    action: str,
    actor_id: uuid.UUID | None = None,
    actor_name: str = "Unknown",
    target: str | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Add an audit row to the current session (the caller commits)."""
    entry = AuditLog(
        category=category,
        action=action,
        actor_id=actor_id,
        actor_name=actor_name,
        target=target,
        ip_address=ip_address,
    )
    db.add(entry)
    return entry
