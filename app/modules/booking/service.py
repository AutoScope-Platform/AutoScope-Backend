"""Booking business logic: availability, requesting, quoting, doing the work, cancelling.

Lifecycle (status lives on the transaction):

    fixed price :  requested --accept--> confirmed --paid--> in_progress --complete--> completed
    with a quote:  quote_requested --send quote--> quote_sent --customer accepts--> confirmed
                   (then the same as above)
    Either side can end it early: declined (before payment) or cancelled (refund if paid).

Before the work starts the mechanic can propose a different time instead (FR-5.4). If the
customer agrees, the booking moves; agreeing on a new *request* also confirms it, because
proposing a time is the mechanic's way of accepting (FSD flow 9.1).

The customer pays after the mechanic accepts (or the customer accepts the quote); the
money is held in escrow until the work is completed and the dispute window has passed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import BadRequest, Conflict, NotFound, UnprocessableEntity
from app.core.events import publish
from app.core.permissions import ADMIN_ROLES, Role
from app.modules.booking.models import Booking, BookingLine, MechanicAvailability
from app.modules.catalog import service as catalog
from app.modules.catalog.models import Listing, ListingStatus, ListingType
from app.modules.identity.models import KycStatus, User
from app.modules.payments import service as payments
from app.modules.payments.models import Transaction, TransactionKind, TxStatus

# Nigeria has no daylight saving, so a fixed UTC+1 offset is always right.
LAGOS = timezone(timedelta(hours=1))
SLOT_STEP_MINUTES = 30
# Statuses in which a booking occupies the mechanic's time.
BLOCKING = (TxStatus.REQUESTED, TxStatus.CONFIRMED, TxStatus.IN_PROGRESS)
# Statuses in which the mechanic may still propose a different time (work not started).
RESCHEDULABLE = (
    TxStatus.REQUESTED,
    TxStatus.QUOTE_REQUESTED,
    TxStatus.QUOTE_SENT,
    TxStatus.CONFIRMED,
)


# ======================================================================================
# Availability
# ======================================================================================
def set_availability(
    db: Session, mechanic: User, windows: list[dict]
) -> list[MechanicAvailability]:
    """Replace the mechanic's weekly working hours."""
    for w in windows:
        if not 0 <= w["weekday"] <= 6 or not 0 <= w["start_minute"] < w["end_minute"] <= 1440:
            raise BadRequest("Invalid working hours", code="invalid_hours")
    for row in db.scalars(
        select(MechanicAvailability).where(MechanicAvailability.mechanic_id == mechanic.id)
    ):
        db.delete(row)
    rows = [MechanicAvailability(mechanic_id=mechanic.id, **w) for w in windows]
    db.add_all(rows)
    db.commit()
    return rows


def get_availability(db: Session, mechanic_id: uuid.UUID) -> list[MechanicAvailability]:
    return list(
        db.scalars(
            select(MechanicAvailability)
            .where(MechanicAvailability.mechanic_id == mechanic_id)
            .order_by(MechanicAvailability.weekday, MechanicAvailability.start_minute)
        )
    )


def _overlaps(
    db: Session,
    mechanic_id: uuid.UUID,
    start: datetime,
    minutes: int,
    *,
    ignore: uuid.UUID | None = None,
) -> bool:
    """Is the mechanic already booked at any time in [start, start + minutes)?"""
    end = start + timedelta(minutes=minutes)
    stmt = (
        select(Booking)
        .join(Transaction, Transaction.id == Booking.transaction_id)
        .where(Booking.mechanic_id == mechanic_id, Transaction.status.in_(BLOCKING))
    )
    for other in db.scalars(stmt):
        if other.id == ignore:
            continue
        if other.scheduled_for < end and start < other.scheduled_for + timedelta(
            minutes=other.duration_minutes
        ):
            return True
    return False


def _inside_hours(db: Session, mechanic_id: uuid.UUID, start: datetime, minutes: int) -> bool:
    local = start.astimezone(LAGOS)
    start_min = local.hour * 60 + local.minute
    end_min = start_min + minutes
    return any(
        w.weekday == local.weekday() and w.start_minute <= start_min and end_min <= w.end_minute
        for w in get_availability(db, mechanic_id)
    )


def free_slots(db: Session, mechanic_id: uuid.UUID, day: date, minutes: int = 60) -> list[datetime]:
    """Start times (every 30 minutes) on `day` when the mechanic is working and free."""
    slots: list[datetime] = []
    now = utcnow()
    for w in get_availability(db, mechanic_id):
        if w.weekday != day.weekday():
            continue
        minute = w.start_minute
        while minute + minutes <= w.end_minute:
            start = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=LAGOS)
            if start > now and not _overlaps(db, mechanic_id, start, minutes):
                slots.append(start.astimezone(UTC))
            minute += SLOT_STEP_MINUTES
    return sorted(slots)


# ======================================================================================
# Requesting
# ======================================================================================
def _mechanic(db: Session, mechanic_id: uuid.UUID) -> User:
    user = db.get(User, mechanic_id)
    if user is None or not user.has_role(Role.MECHANIC) or user.kyc_status != KycStatus.APPROVED:
        raise NotFound("Mechanic not found", code="mechanic_not_found")
    return user


def _check_time(
    db: Session,
    mechanic_id: uuid.UUID,
    start: datetime,
    minutes: int,
    *,
    ignore: uuid.UUID | None = None,
) -> None:
    """The time must be in the future, inside working hours, and free."""
    if start <= utcnow():
        raise BadRequest("Pick a time in the future", code="invalid_time")
    if not _inside_hours(db, mechanic_id, start, minutes):
        raise UnprocessableEntity("The mechanic is not working at that time", code="outside_hours")
    if _overlaps(db, mechanic_id, start, minutes, ignore=ignore):
        raise Conflict("That time is already booked", code="slot_taken")


def request_booking(
    db: Session,
    customer: User,
    *,
    mechanic_id: uuid.UUID | None,
    listing_id: uuid.UUID | None,
    vehicle_id: uuid.UUID | None,
    scheduled_for: datetime,
    description: str,
    quote_requested: bool,
) -> Booking:
    """Ask a mechanic for a slot. Give a `listing_id` (a service with a price) or a
    `mechanic_id` plus `quote_requested` to ask for a quote."""
    listing = None
    if listing_id:
        listing = db.get(Listing, listing_id)
        if (
            listing is None
            or listing.type != ListingType.MECHANIC_SERVICE
            or listing.status != ListingStatus.LIVE
        ):
            raise NotFound("This service is not available", code="listing_unavailable")
        mechanic_id = listing.seller_id
    if mechanic_id is None:
        raise BadRequest("Choose a mechanic or a service", code="mechanic_required")
    mechanic = _mechanic(db, mechanic_id)
    if mechanic.id == customer.id:
        raise UnprocessableEntity("You cannot book yourself", code="own_service")
    if vehicle_id:
        catalog.get_vehicle(db, customer, vehicle_id)  # must be in the customer's garage
    quote = quote_requested or listing is None
    minutes = int((listing.attributes or {}).get("duration_minutes", 60)) if listing else 60

    _check_time(db, mechanic.id, scheduled_for, minutes)

    tx = payments.create_transaction(
        db,
        kind=TransactionKind.SERVICE_BOOKING,
        customer_id=customer.id,
        provider_id=mechanic.id,
        amount_minor=0 if quote else listing.price_minor,
        status=TxStatus.QUOTE_REQUESTED if quote else TxStatus.REQUESTED,
        source_id=listing.id if listing else None,
    )
    booking = Booking(
        transaction_id=tx.id,
        customer_id=customer.id,
        mechanic_id=mechanic.id,
        listing_id=listing.id if listing else None,
        vehicle_id=vehicle_id,
        service_title=listing.title if listing else "Quote request",
        description=description.strip(),
        scheduled_for=scheduled_for,
        duration_minutes=minutes,
        quote_requested=quote,
    )
    db.add(booking)
    db.commit()
    publish(
        "booking.requested", booking_id=str(booking.id), mechanic_id=str(mechanic.id), ref=tx.ref
    )
    return booking


# ======================================================================================
# Reading
# ======================================================================================
def get_booking(db: Session, booking_id: uuid.UUID, viewer: User) -> tuple[Booking, Transaction]:
    booking = db.get(Booking, booking_id)
    if booking is None or not (
        viewer.id in (booking.customer_id, booking.mechanic_id) or viewer.has_role(*ADMIN_ROLES)
    ):
        raise NotFound("Booking not found", code="booking_not_found")
    return booking, payments.get_transaction(db, booking.transaction_id)


def _locked(db: Session, booking_id: uuid.UUID) -> tuple[Booking, Transaction]:
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise NotFound("Booking not found", code="booking_not_found")
    return booking, payments.get_transaction(db, booking.transaction_id, lock=True)


def _mechanic_booking(
    db: Session, mechanic: User, booking_id: uuid.UUID
) -> tuple[Booking, Transaction]:
    booking, tx = _locked(db, booking_id)
    if booking.mechanic_id != mechanic.id:
        raise NotFound("Booking not found", code="booking_not_found")
    return booking, tx


def _customer_booking(
    db: Session, customer: User, booking_id: uuid.UUID
) -> tuple[Booking, Transaction]:
    booking, tx = _locked(db, booking_id)
    if booking.customer_id != customer.id:
        raise NotFound("Booking not found", code="booking_not_found")
    return booking, tx


def _need(tx: Transaction, *statuses: TxStatus) -> None:
    if tx.status not in statuses:
        raise UnprocessableEntity(
            f"This booking is {tx.status.value.replace('_', ' ')}, so that is not possible now",
            code="invalid_status",
        )


# ======================================================================================
# Mechanic actions
# ======================================================================================
def accept_booking(db: Session, mechanic: User, booking_id: uuid.UUID) -> Booking:
    """Accept a fixed-price request. The customer is then asked to pay."""
    booking, tx = _mechanic_booking(db, mechanic, booking_id)
    _need(tx, TxStatus.REQUESTED)
    if _overlaps(
        db, mechanic.id, booking.scheduled_for, booking.duration_minutes, ignore=booking.id
    ):
        raise Conflict("You have another booking at that time", code="slot_taken")
    tx.status = TxStatus.CONFIRMED
    booking.proposed_for = booking.proposal_note = None  # the original time stands
    payments.request_payment(db, tx)
    db.commit()
    publish(
        "booking.accepted",
        booking_id=str(booking.id),
        customer_id=str(booking.customer_id),
        ref=tx.ref,
    )
    return booking


def decline_booking(
    db: Session, mechanic: User, booking_id: uuid.UUID, reason: str | None
) -> Booking:
    booking, tx = _mechanic_booking(db, mechanic, booking_id)
    _need(tx, TxStatus.REQUESTED, TxStatus.QUOTE_REQUESTED)
    booking.decline_reason = reason
    tx.status = TxStatus.DECLINED
    db.commit()
    publish(
        "booking.declined",
        booking_id=str(booking.id),
        customer_id=str(booking.customer_id),
        ref=tx.ref,
    )
    return booking


def send_quote(
    db: Session, mechanic: User, booking_id: uuid.UUID, lines: list[dict], note: str | None
) -> Booking:
    """Itemise the job. The total becomes the amount the customer is asked to accept."""
    booking, tx = _mechanic_booking(db, mechanic, booking_id)
    _need(tx, TxStatus.QUOTE_REQUESTED, TxStatus.QUOTE_SENT)  # a quote can be revised
    if not lines or any(line["amount_minor"] < 0 for line in lines):
        raise BadRequest("Add at least one line with a valid amount", code="invalid_quote")
    booking.lines.clear()
    db.flush()
    for i, line in enumerate(lines):
        booking.lines.append(BookingLine(position=i, **line))
    booking.quote_note = note
    tx.amount_minor = sum(line["amount_minor"] for line in lines)
    tx.status = TxStatus.QUOTE_SENT
    db.commit()
    publish(
        "booking.quoted",
        booking_id=str(booking.id),
        customer_id=str(booking.customer_id),
        ref=tx.ref,
    )
    return booking


def propose_time(
    db: Session,
    mechanic: User,
    booking_id: uuid.UUID,
    scheduled_for: datetime,
    note: str | None,
) -> Booking:
    """Suggest a different time (FR-5.4). Replaces any earlier suggestion."""
    booking, tx = _mechanic_booking(db, mechanic, booking_id)
    _need(tx, *RESCHEDULABLE)
    if scheduled_for == booking.scheduled_for:
        raise BadRequest("That is already the booked time", code="same_time")
    _check_time(db, mechanic.id, scheduled_for, booking.duration_minutes, ignore=booking.id)
    booking.proposed_for, booking.proposal_note = scheduled_for, note
    db.commit()
    publish(
        "booking.time_proposed",
        booking_id=str(booking.id),
        customer_id=str(booking.customer_id),
        ref=tx.ref,
    )
    return booking


def start_work(db: Session, mechanic: User, booking_id: uuid.UUID) -> Booking:
    booking, tx = _mechanic_booking(db, mechanic, booking_id)
    _need(tx, TxStatus.CONFIRMED)
    if tx.paid_at is None:
        raise UnprocessableEntity("The customer has not paid yet", code="not_paid")
    booking.started_at = utcnow()
    tx.status = TxStatus.IN_PROGRESS
    db.commit()
    publish(
        "booking.started",
        booking_id=str(booking.id),
        customer_id=str(booking.customer_id),
        ref=tx.ref,
    )
    return booking


def complete_work(db: Session, mechanic: User, booking_id: uuid.UUID) -> Booking:
    """Finish the job: starts the dispute window and adds the work to the vehicle's history."""
    booking, tx = _mechanic_booking(db, mechanic, booking_id)
    _need(tx, TxStatus.IN_PROGRESS)
    payments.mark_completed(db, tx, TxStatus.COMPLETED)
    if booking.vehicle_id:
        customer = db.get(User, booking.customer_id)
        vehicle = catalog.get_vehicle(db, customer, booking.vehicle_id)
        catalog.add_service_record(
            db,
            vehicle,
            {
                "done_on": datetime.now(LAGOS).date(),
                "description": booking.service_title,
                "cost_minor": tx.amount_minor,
                "mechanic_name": mechanic.display_name,
                "booking_ref": tx.ref,
            },
            commit=False,
        )
    db.commit()
    publish(
        "booking.completed",
        booking_id=str(booking.id),
        customer_id=str(booking.customer_id),
        ref=tx.ref,
    )
    return booking


# ======================================================================================
# Customer actions
# ======================================================================================
def answer_quote(db: Session, customer: User, booking_id: uuid.UUID, accept: bool) -> Booking:
    booking, tx = _customer_booking(db, customer, booking_id)
    _need(tx, TxStatus.QUOTE_SENT)
    if accept:
        if _overlaps(
            db,
            booking.mechanic_id,
            booking.scheduled_for,
            booking.duration_minutes,
            ignore=booking.id,
        ):
            raise Conflict("That time was taken in the meantime", code="slot_taken")
        tx.status = TxStatus.CONFIRMED
        payments.request_payment(db, tx)
    else:
        tx.status = TxStatus.DECLINED
    db.commit()
    publish(
        "booking.quote_answered", booking_id=str(booking.id), mechanic_id=str(booking.mechanic_id),
        accepted=accept, ref=tx.ref,
    )  # fmt: skip
    return booking


def answer_proposed_time(
    db: Session, customer: User, booking_id: uuid.UUID, accept: bool
) -> Booking:
    """Agree to (or turn down) the mechanic's suggested time.

    Turning it down keeps the original time and status; either side can then cancel, or
    the mechanic can accept the original time or suggest another.
    """
    booking, tx = _customer_booking(db, customer, booking_id)
    if booking.proposed_for is None:
        raise UnprocessableEntity("There is no new time to answer", code="no_proposal")
    _need(tx, *RESCHEDULABLE)
    if accept:
        _check_time(
            db, booking.mechanic_id, booking.proposed_for, booking.duration_minutes,
            ignore=booking.id,
        )  # fmt: skip
        booking.scheduled_for = booking.proposed_for
        if tx.status == TxStatus.REQUESTED:
            tx.status = TxStatus.CONFIRMED
            payments.request_payment(db, tx)
    booking.proposed_for = booking.proposal_note = None
    db.commit()
    publish(
        "booking.time_answered", booking_id=str(booking.id), mechanic_id=str(booking.mechanic_id),
        accepted=accept, ref=tx.ref,
    )  # fmt: skip
    return booking


def cancel_booking(db: Session, user: User, booking_id: uuid.UUID, reason: str | None) -> Booking:
    """Either side can cancel before the work starts. A paid booking is refunded in full."""
    booking, tx = _locked(db, booking_id)
    if user.id not in (booking.customer_id, booking.mechanic_id):
        raise NotFound("Booking not found", code="booking_not_found")
    _need(tx, TxStatus.REQUESTED, TxStatus.QUOTE_REQUESTED, TxStatus.QUOTE_SENT, TxStatus.CONFIRMED)
    payments.cancel_and_refund(db, tx, reason=reason or "Booking cancelled")
    db.commit()
    other = booking.mechanic_id if user.id == booking.customer_id else booking.customer_id
    publish("booking.cancelled", booking_id=str(booking.id), notify_id=str(other), ref=tx.ref)
    return booking
