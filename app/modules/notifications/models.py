"""Notifications (FSD 3.10): the in-app inbox, push device tokens, and admin announcements."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamps, UUIDPrimaryKey, enum_type


class DeviceToken(UUIDPrimaryKey, Timestamps, Base):
    """A phone registered for push. One user can have several; a token belongs to one user."""

    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token: Mapped[str] = mapped_column(String(300), unique=True)
    platform: Mapped[str] = mapped_column(String(10))  # "ios" | "android"


class Notification(UUIDPrimaryKey, Timestamps, Base):
    """One entry in a user's in-app notification list."""

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(60))  # the event name, e.g. "order.paid"
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(400))
    # Where the app should go when tapped, e.g. {"order_id": "..."}.
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnnouncementStatus(StrEnum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    CANCELLED = "cancelled"


class Announcement(UUIDPrimaryKey, Timestamps, Base):
    """A message an admin sends to a group of users (FR-13.6)."""

    __tablename__ = "announcements"

    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    # Who gets it: users holding any of these roles (empty = everyone), optionally in one city.
    roles: Mapped[list[str]] = mapped_column(JSONB, default=list)
    city: Mapped[str | None] = mapped_column(String(80))
    channels: Mapped[list[str]] = mapped_column(JSONB, default=list)  # push | sms | email
    send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[AnnouncementStatus] = mapped_column(
        enum_type(AnnouncementStatus), default=AnnouncementStatus.SCHEDULED, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipients_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
