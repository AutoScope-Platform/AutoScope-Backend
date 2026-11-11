"""Request and response bodies for the messaging module."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class StartThreadIn(BaseModel):
    recipient_id: uuid.UUID
    listing_id: uuid.UUID | None = Field(None, description="What the conversation is about.")
    message: str = Field(min_length=1, max_length=2000)


class SendIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class MessageOut(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID
    sender_id: uuid.UUID
    body: str
    attachment_name: str | None
    attachment_url: str | None = Field(None, description="Time-limited link.")
    read: bool
    created_at: datetime


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None = None


class PersonOut(BaseModel):
    id: uuid.UUID
    name: str


class ThreadOut(BaseModel):
    id: uuid.UUID
    other: PersonOut
    listing_id: uuid.UUID | None
    listing_title: str | None
    last_message_at: datetime | None
    last_message_preview: str | None
    unread: int


class ThreadPage(BaseModel):
    items: list[ThreadOut]
    next_cursor: str | None = None


class StartThreadOut(BaseModel):
    thread_id: uuid.UUID
    message: MessageOut


class UnreadOut(BaseModel):
    unread: int


class ReadOut(BaseModel):
    marked: int
