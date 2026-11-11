"""Request and response bodies for the notifications module."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.notifications.models import AnnouncementStatus


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    title: str
    body: str
    data: dict
    read: bool = False
    created_at: datetime


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    next_cursor: str | None = None
    unread: int


class MarkReadIn(BaseModel):
    ids: list[uuid.UUID] | None = Field(None, description="Leave out to mark everything as read.")


class MarkedOut(BaseModel):
    marked: int


class DeviceIn(BaseModel):
    token: str = Field(min_length=10, max_length=300, description="The FCM registration token.")
    platform: Literal["ios", "android"]


class AnnouncementIn(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=3, max_length=2000)
    roles: list[str] = Field(
        default_factory=list, description="Empty = all users.", examples=[["mechanic", "dealer"]]
    )
    city: str | None = Field(None, max_length=80)
    channels: list[Literal["push", "sms", "email"]] = Field(min_length=1)
    send_at: datetime | None = Field(None, description="Leave out to send now.")


class AnnouncementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    body: str
    roles: list[str]
    city: str | None
    channels: list[str]
    send_at: datetime | None
    status: AnnouncementStatus
    sent_at: datetime | None
    recipients_count: int
    created_at: datetime


class AnnouncementPage(BaseModel):
    items: list[AnnouncementOut]
    total: int
