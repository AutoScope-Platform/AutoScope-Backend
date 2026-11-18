"""Booking endpoints: mechanic hours, slots, requests, quotes and the job lifecycle."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import error_responses
from app.core.permissions import Capability
from app.modules.booking import service
from app.modules.booking.models import Booking
from app.modules.booking.schemas import (
    AvailabilityIn,
    BookingIn,
    BookingOut,
    InvoiceOut,
    LineOut,
    ProposeTimeIn,
    QuoteAnswerIn,
    QuoteIn,
    ReasonIn,
    SlotsOut,
    TimeAnswerIn,
    WindowOut,
)
from app.modules.identity.deps import CurrentUser, require_capability
from app.modules.identity.models import User
from app.modules.payments import service as payments

router = APIRouter(tags=["Bookings"])

Customer = Depends(require_capability(Capability.PURCHASE))
Mechanic = Depends(require_capability(Capability.OFFER_SERVICES))


def _out(db: Session, booking: Booking, tx=None) -> BookingOut:
    tx = tx or payments.get_transaction(db, booking.transaction_id)
    return BookingOut(
        id=booking.id, transaction_id=tx.id, ref=tx.ref, status=tx.status,
        escrow_status=tx.escrow_status, amount_minor=tx.amount_minor, payment_due=tx.payment_due,
        customer_id=booking.customer_id, mechanic_id=booking.mechanic_id,
        vehicle_id=booking.vehicle_id, service_title=booking.service_title,
        description=booking.description, scheduled_for=booking.scheduled_for,
        duration_minutes=booking.duration_minutes, quote_requested=booking.quote_requested,
        quote_note=booking.quote_note, decline_reason=booking.decline_reason,
        proposed_for=booking.proposed_for, proposal_note=booking.proposal_note,
        lines=[LineOut(description=li.description, amount_minor=li.amount_minor) for li in booking.lines],
        created_at=booking.created_at,
    )  # fmt: skip


# --- Working hours and free slots --------------------------------------------------------
@router.put(
    "/me/availability",
    response_model=list[WindowOut],
    summary="Mechanic: set my weekly working hours",
    description="Replaces all existing hours. Times are Nigerian local time.",
    responses=error_responses(400),
)
def set_availability(body: AvailabilityIn, user: User = Mechanic, db: Session = Depends(get_db)):
    return service.set_availability(db, user, [w.model_dump() for w in body.windows])


@router.get(
    "/me/availability", response_model=list[WindowOut], summary="Mechanic: my working hours"
)
def my_availability(user: User = Mechanic, db: Session = Depends(get_db)):
    return service.get_availability(db, user.id)


@router.get(
    "/mechanics/{mechanic_id}/slots",
    response_model=SlotsOut,
    summary="Free appointment times for a mechanic on a day",
)
def slots(
    mechanic_id: uuid.UUID,
    day: date = Query(alias="date"),
    duration_minutes: int = Query(60, ge=15, le=480),
    db: Session = Depends(get_db),
) -> SlotsOut:
    return SlotsOut(
        date=day,
        duration_minutes=duration_minutes,
        slots=service.free_slots(db, mechanic_id, day, duration_minutes),
    )


# --- Customer ----------------------------------------------------------------------------
@router.post(
    "/bookings",
    response_model=BookingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request a booking (fixed price) or a quote",
    responses=error_responses(400, 404, 409, 422),
)
def request_booking(body: BookingIn, user: User = Customer, db: Session = Depends(get_db)):
    booking = service.request_booking(db, user, **body.model_dump())
    return _out(db, booking)


@router.get(
    "/bookings/{booking_id}",
    response_model=BookingOut,
    summary="One booking (customer, mechanic or staff)",
    responses=error_responses(404),
)
def get_booking(booking_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)):
    booking, tx = service.get_booking(db, booking_id, user)
    return _out(db, booking, tx)


@router.get(
    "/bookings/{booking_id}/invoice",
    response_model=InvoiceOut,
    summary="Invoice for a booking",
    responses=error_responses(404),
)
def invoice(booking_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)) -> InvoiceOut:
    booking, tx = service.get_booking(db, booking_id, user)
    lines = [
        LineOut(description=li.description, amount_minor=li.amount_minor) for li in booking.lines
    ]
    if not lines:  # fixed-price service: a single line for the service itself
        lines = [LineOut(description=booking.service_title, amount_minor=tx.amount_minor)]
    return InvoiceOut(
        ref=tx.ref,
        issued_to=db.get(User, booking.customer_id).display_name,
        issued_by=db.get(User, booking.mechanic_id).display_name,
        service_title=booking.service_title,
        lines=lines,
        total_minor=tx.amount_minor,
        paid=tx.paid_at is not None,
        paid_at=tx.paid_at,
    )


@router.post(
    "/bookings/{booking_id}/quote/answer",
    response_model=BookingOut,
    summary="Customer: accept or decline a quote",
    responses=error_responses(404, 409, 422),
)
def answer_quote(
    booking_id: uuid.UUID, body: QuoteAnswerIn, user: CurrentUser, db: Session = Depends(get_db)
):
    return _out(db, service.answer_quote(db, user, booking_id, body.accept))


@router.post(
    "/bookings/{booking_id}/proposed-time/answer",
    response_model=BookingOut,
    summary="Customer: accept or turn down the mechanic's suggested time",
    description="Accepting a suggestion on a new request also confirms the booking (the "
    "customer is then asked to pay). Turning it down keeps the original time.",
    responses=error_responses(404, 409, 422),
)
def answer_time(
    booking_id: uuid.UUID, body: TimeAnswerIn, user: CurrentUser, db: Session = Depends(get_db)
):
    return _out(db, service.answer_proposed_time(db, user, booking_id, body.accept))


@router.post(
    "/bookings/{booking_id}/cancel",
    response_model=BookingOut,
    summary="Cancel before the work starts (paid bookings are refunded)",
    responses=error_responses(404, 422),
)
def cancel(booking_id: uuid.UUID, body: ReasonIn, user: CurrentUser, db: Session = Depends(get_db)):
    return _out(db, service.cancel_booking(db, user, booking_id, body.reason))


# --- Mechanic ----------------------------------------------------------------------------
@router.post(
    "/bookings/{booking_id}/accept",
    response_model=BookingOut,
    summary="Mechanic: accept a fixed-price booking (customer is then asked to pay)",
    responses=error_responses(404, 409, 422),
)
def accept(booking_id: uuid.UUID, user: User = Mechanic, db: Session = Depends(get_db)):
    return _out(db, service.accept_booking(db, user, booking_id))


@router.post(
    "/bookings/{booking_id}/decline",
    response_model=BookingOut,
    summary="Mechanic: decline a request",
    responses=error_responses(404, 422),
)
def decline(
    booking_id: uuid.UUID, body: ReasonIn, user: User = Mechanic, db: Session = Depends(get_db)
):
    return _out(db, service.decline_booking(db, user, booking_id, body.reason))


@router.put(
    "/bookings/{booking_id}/proposed-time",
    response_model=BookingOut,
    summary="Mechanic: suggest a different time (FR-5.4)",
    description="Possible until the work starts. The new time must be inside your working "
    "hours and free. The customer accepts or turns it down.",
    responses=error_responses(400, 404, 409, 422),
)
def propose_time(
    booking_id: uuid.UUID, body: ProposeTimeIn, user: User = Mechanic, db: Session = Depends(get_db)
):
    return _out(db, service.propose_time(db, user, booking_id, body.scheduled_for, body.note))


@router.put(
    "/bookings/{booking_id}/quote",
    response_model=BookingOut,
    summary="Mechanic: send or revise an itemised quote",
    responses=error_responses(400, 404, 422),
)
def send_quote(
    booking_id: uuid.UUID, body: QuoteIn, user: User = Mechanic, db: Session = Depends(get_db)
):
    return _out(
        db,
        service.send_quote(db, user, booking_id, [x.model_dump() for x in body.lines], body.note),
    )


@router.post(
    "/bookings/{booking_id}/start",
    response_model=BookingOut,
    summary="Mechanic: start the work (needs payment)",
    responses=error_responses(404, 422),
)
def start(booking_id: uuid.UUID, user: User = Mechanic, db: Session = Depends(get_db)):
    return _out(db, service.start_work(db, user, booking_id))


@router.post(
    "/bookings/{booking_id}/complete",
    response_model=BookingOut,
    summary="Mechanic: finish the work (adds it to the vehicle's service history)",
    responses=error_responses(404, 422),
)
def complete(booking_id: uuid.UUID, user: User = Mechanic, db: Session = Depends(get_db)):
    return _out(db, service.complete_work(db, user, booking_id))
