"""In-app chat (FSD 3.9): one thread per pair of people (and per listing), with messages."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Timestamps, UUIDPrimaryKey


class Thread(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "threads"

    # The two participants, stored in sorted order (low < high) so a pair always maps to
    # the same row no matter who started the conversation.
    user_low_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    user_high_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # What the conversation is about (optional): shown as a header in the app.
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL")
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(120))


class Message(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "messages"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    body: Mapped[str] = mapped_column(Text, default="")
    attachment_key: Mapped[str | None] = mapped_column(String(300))
    attachment_name: Mapped[str | None] = mapped_column(String(200))
    attachment_type: Mapped[str | None] = mapped_column(String(80))
    # Set when the *other* participant has seen it.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
