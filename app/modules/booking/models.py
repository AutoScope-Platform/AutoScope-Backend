"""Mechanic bookings (FSD 3.4): working hours, bookings, and itemised quotes/invoices.

As with orders, a booking's money and status live on its `Transaction`. The booking row
holds the details: when, which vehicle, what is wrong, and the quote lines.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamps, UUIDPrimaryKey


class MechanicAvailability(UUIDPrimaryKey, Base):
    """One working window on one weekday, e.g. Monday 08:00-17:00. A mechanic can have
    several windows per day (for a lunch break). Times are Nigerian local time, stored
    as minutes after midnight."""

    __tablename__ = "mechanic_availability"

    mechanic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday ... 6 = Sunday
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)


class Booking(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "bookings"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    mechanic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL")
    )
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL")
    )
    service_title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    # True when the customer asked for a quote instead of a fixed-price service.
    quote_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    quote_note: Mapped[str | None] = mapped_column(String(500))
    decline_reason: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list[BookingLine]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", order_by="BookingLine.position"
    )


class BookingLine(UUIDPrimaryKey, Base):
    """One item on the quote (and later the invoice): labour, a part, a call-out fee."""

    __tablename__ = "booking_lines"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    description: Mapped[str] = mapped_column(String(200))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer, default=0)

    booking: Mapped[Booking] = relationship(back_populates="lines")
