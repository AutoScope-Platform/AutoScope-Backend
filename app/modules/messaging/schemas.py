"""Request and response bodies for the messaging module."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

CLIENT_ID_HELP = (
    "Optional id your app picks for this message (e.g. a UUID). Sending the same id again "
    "returns the first message instead of a duplicate, so queued messages can be retried."
)


class StartThreadIn(BaseModel):
    recipient_id: uuid.UUID | None = Field(
        None, description="Required unless the chat is about a booking or job."
    )
    listing_id: uuid.UUID | None = Field(None, description="What the conversation is about.")
    booking_id: uuid.UUID | None = Field(None, description="Chat with the other party (FR-5.7).")
    job_id: uuid.UUID | None = Field(None, description="Chat with the driver or the customer.")
    message: str = Field(min_length=1, max_length=2000)
    client_id: str | None = Field(None, min_length=1, max_length=64, description=CLIENT_ID_HELP)


class SendIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    client_id: str | None = Field(None, min_length=1, max_length=64, description=CLIENT_ID_HELP)


class MessageOut(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID
    sender_id: uuid.UUID
    client_id: str | None = None
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
    booking_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    subject_title: str | None = Field(None, description="Listing, service or tow, for a header.")
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
