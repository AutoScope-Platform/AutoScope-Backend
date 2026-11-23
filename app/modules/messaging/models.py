"""In-app chat (FSD 3.12): one thread per pair of people and subject, with messages.

A thread can be about a listing, a mechanic booking or a tow job (FR-12.1, FR-5.7), or
nothing in particular. The same two people have a separate thread per subject.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
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
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("haulage_jobs.id", ondelete="SET NULL"), index=True
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(120))


class Message(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "messages"
    # A retried send with the same client id is the same message (offline queue, FSD 5.5).
    __table_args__ = (UniqueConstraint("sender_id", "client_id"),)

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    body: Mapped[str] = mapped_column(Text, default="")
    attachment_key: Mapped[str | None] = mapped_column(String(300))
    attachment_name: Mapped[str | None] = mapped_column(String(200))
    attachment_type: Mapped[str | None] = mapped_column(String(80))
    # Chosen by the sender's app (e.g. a UUID) before sending; unique per sender.
    client_id: Mapped[str | None] = mapped_column(String(64))
    # Set when the *other* participant has seen it.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
