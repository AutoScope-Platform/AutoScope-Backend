"""Request and response bodies for the booking module."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.modules.payments.models import EscrowStatus, TxStatus


class WindowIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday ... 6 = Sunday.")
    start_minute: int = Field(
        ge=0, le=1439, description="Minutes after midnight, e.g. 480 = 08:00."
    )
    end_minute: int = Field(ge=1, le=1440)


class AvailabilityIn(BaseModel):
    windows: list[WindowIn] = Field(max_length=40)


class WindowOut(WindowIn):
    pass


class SlotsOut(BaseModel):
    date: date
    duration_minutes: int
    slots: list[datetime] = Field(description="Free start times (UTC).")


class BookingIn(BaseModel):
    listing_id: uuid.UUID | None = Field(None, description="A mechanic's priced service.")
    mechanic_id: uuid.UUID | None = Field(None, description="Used with `quote_requested`.")
    vehicle_id: uuid.UUID | None = Field(None, description="A vehicle in your garage.")
    scheduled_for: datetime = Field(description="Start time, with a timezone offset.")
    description: str = Field("", max_length=2000, examples=["Brakes squeal when stopping"])
    quote_requested: bool = False

    @model_validator(mode="after")
    def _needs_target(self) -> BookingIn:
        if not self.listing_id and not self.mechanic_id:
            raise ValueError("Give a listing_id or a mechanic_id")
        if self.scheduled_for.tzinfo is None:
            raise ValueError(
                "scheduled_for needs a timezone offset, e.g. 2026-12-01T10:00:00+01:00"
            )
        return self


class LineIn(BaseModel):
    description: str = Field(min_length=2, max_length=200, examples=["Brake pads (front)"])
    amount_minor: int = Field(ge=0)


class QuoteIn(BaseModel):
    lines: list[LineIn] = Field(min_length=1, max_length=30)
    note: str | None = Field(None, max_length=500)


class ReasonIn(BaseModel):
    reason: str | None = Field(None, max_length=300)


class QuoteAnswerIn(BaseModel):
    accept: bool


class LineOut(BaseModel):
    description: str
    amount_minor: int


class BookingOut(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    ref: str = Field(examples=["BKG-80420"])
    status: TxStatus
    escrow_status: EscrowStatus
    amount_minor: int
    payment_due: bool
    customer_id: uuid.UUID
    mechanic_id: uuid.UUID
    vehicle_id: uuid.UUID | None
    service_title: str
    description: str
    scheduled_for: datetime
    duration_minutes: int
    quote_requested: bool
    quote_note: str | None
    decline_reason: str | None
    lines: list[LineOut]
    created_at: datetime


class InvoiceOut(BaseModel):
    ref: str
    issued_to: str
    issued_by: str
    service_title: str
    lines: list[LineOut]
    total_minor: int
    paid: bool
    paid_at: datetime | None
