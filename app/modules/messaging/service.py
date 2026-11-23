"""Messaging business logic: threads, messages, attachments and read receipts.

Threads can be about a listing, a booking or a tow job; for a booking or job the other
person is worked out from it, and only its two parties can talk about it.

Sending is retry-safe: a message carries an optional `client_id` chosen by the app, and
sending the same id again returns the first message instead of a duplicate. That lets the
apps queue messages offline and resend them when the connection is back (FSD 5.5).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import BadRequest, NotFound, UnprocessableEntity
from app.core.events import publish
from app.core.pagination import paginate_cursor
from app.core.realtime import user_events
from app.core.uploads import checked_type
from app.integrations import registry
from app.modules.booking import service as booking_service
from app.modules.booking.models import Booking
from app.modules.catalog.models import Listing
from app.modules.haulage import service as haulage_service
from app.modules.haulage.models import HaulageJob
from app.modules.identity.models import AccountStatus, User
from app.modules.messaging.models import Message, Thread

# Each person's open apps, keyed by user id; shared with notifications (core/realtime.py).
hub = user_events

ATTACHMENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
ATTACHMENT_MAX = 8 * 1024 * 1024


def _pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) < str(b) else (b, a)


def other_party(thread: Thread, me: uuid.UUID) -> uuid.UUID:
    return thread.user_high_id if thread.user_low_id == me else thread.user_low_id


def _get_thread(db: Session, thread_id: uuid.UUID, user: User) -> Thread:
    thread = db.get(Thread, thread_id)
    if thread is None or user.id not in (thread.user_low_id, thread.user_high_id):
        raise NotFound("Conversation not found", code="thread_not_found")
    return thread


def _parties(
    db: Session, sender: User, booking_id: uuid.UUID | None, job_id: uuid.UUID | None
) -> set[uuid.UUID] | None:
    """The two people a booking or job thread is between (None for other threads)."""
    if booking_id:
        booking, _ = booking_service.get_booking(db, booking_id, sender)
        return {booking.customer_id, booking.mechanic_id}
    if job_id:
        job, _ = haulage_service.get_job(db, job_id, sender)
        if job.provider_id is None:
            raise UnprocessableEntity("No driver has accepted this job yet", code="no_provider")
        return {job.customer_id, job.provider_id}
    return None


def start_thread(
    db: Session,
    sender: User,
    recipient_id: uuid.UUID | None,
    listing_id: uuid.UUID | None,
    body: str,
    *,
    booking_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    client_id: str | None = None,
) -> tuple[Thread, Message]:
    """Open (or reuse) the conversation with someone and send the first message.

    About a booking or job, the recipient may be left out: it is the other party.
    """
    if sum(x is not None for x in (listing_id, booking_id, job_id)) > 1:
        raise BadRequest("Link a conversation to one listing, booking or job", code="one_subject")
    parties = _parties(db, sender, booking_id, job_id)
    if parties is not None:
        if sender.id not in parties:
            raise UnprocessableEntity("Only its two parties can chat about it", code="not_a_party")
        recipient_id = recipient_id or next(iter(parties - {sender.id}), None)
        if recipient_id not in parties:
            raise UnprocessableEntity("Only its two parties can chat about it", code="not_a_party")
    if recipient_id is None:
        raise BadRequest("Choose who to message", code="recipient_required")
    if recipient_id == sender.id:
        raise UnprocessableEntity("You cannot message yourself", code="self_message")
    recipient = db.get(User, recipient_id)
    if recipient is None or recipient.erased_at or recipient.status == AccountStatus.SUSPENDED:
        raise NotFound("That person was not found", code="user_not_found")
    if listing_id and db.get(Listing, listing_id) is None:
        raise NotFound("Listing not found", code="listing_not_found")
    low, high = _pair(sender.id, recipient_id)
    thread = db.scalar(
        select(Thread).where(
            Thread.user_low_id == low,
            Thread.user_high_id == high,
            *_subject_filter(listing_id, booking_id, job_id),
        )
    )
    if thread is None:
        thread = Thread(
            user_low_id=low, user_high_id=high, listing_id=listing_id, booking_id=booking_id,
            job_id=job_id,
        )  # fmt: skip
        db.add(thread)
        db.flush()
    message = _add_message(db, thread, sender, body=body, client_id=client_id)
    return thread, message


def _subject_filter(
    listing_id: uuid.UUID | None, booking_id: uuid.UUID | None, job_id: uuid.UUID | None
) -> list:
    def same(column, value):
        return column.is_(None) if value is None else column == value

    return [
        same(Thread.listing_id, listing_id),
        same(Thread.booking_id, booking_id),
        same(Thread.job_id, job_id),
    ]


def _add_message(
    db: Session,
    thread: Thread,
    sender: User,
    *,
    body: str,
    attachment: tuple[str, str, str] | None = None,
    client_id: str | None = None,
) -> Message:
    """Store the message, update the thread preview, commit, then notify. (key, name, type)

    With a `client_id` already used by this sender, the earlier message is returned and
    nothing is stored, pushed or notified again.
    """
    if client_id and (earlier := _by_client_id(db, sender, client_id)):
        return earlier
    body = body.strip()
    if not body and attachment is None:
        raise BadRequest("A message cannot be empty", code="empty_message")
    message = Message(thread_id=thread.id, sender_id=sender.id, body=body, client_id=client_id)
    if attachment:
        message.attachment_key, message.attachment_name, message.attachment_type = attachment
    db.add(message)
    thread.last_message_at = utcnow()
    thread.last_message_preview = (body or f"📎 {attachment[1]}")[:120]
    try:
        db.commit()
    except IntegrityError:
        # The same retry arrived twice at once and the other request stored it first.
        db.rollback()
        if client_id and (earlier := _by_client_id(db, sender, client_id)):
            return earlier
        raise
    recipient_id = other_party(thread, sender.id)
    payload = message_payload(message)
    hub.broadcast(recipient_id, payload)
    hub.broadcast(sender.id, payload)  # the sender's other devices
    publish(
        "message.sent", message_id=str(message.id), thread_id=str(thread.id),
        sender_id=str(sender.id), recipient_id=str(recipient_id), sender_name=sender.display_name,
        preview=thread.last_message_preview,
    )  # fmt: skip
    return message


def _by_client_id(db: Session, sender: User, client_id: str) -> Message | None:
    return db.scalar(
        select(Message).where(Message.sender_id == sender.id, Message.client_id == client_id)
    )


def send_message(
    db: Session, sender: User, thread_id: uuid.UUID, body: str, client_id: str | None = None
) -> Message:
    return _add_message(
        db, _get_thread(db, thread_id, sender), sender, body=body, client_id=client_id
    )


def send_attachment(
    db: Session,
    sender: User,
    thread_id: uuid.UUID,
    filename: str,
    content_type: str,
    data: bytes,
    caption: str,
    client_id: str | None = None,
) -> Message:
    thread = _get_thread(db, thread_id, sender)
    if client_id and (earlier := _by_client_id(db, sender, client_id)):
        return earlier  # do not store the file twice
    content_type = checked_type(
        data, ATTACHMENT_TYPES, message="Send a JPEG, PNG, WebP or PDF", code="invalid_file_type"
    )
    if not data or len(data) > ATTACHMENT_MAX:
        raise BadRequest("Files must be under 8 MB", code="file_too_large")
    key = f"chat/{thread.id}/{uuid.uuid4().hex}{ATTACHMENT_TYPES[content_type]}"
    registry.get_storage().put(key, data, content_type)
    return _add_message(
        db, thread, sender, body=caption, attachment=(key, filename[:200], content_type),
        client_id=client_id,
    )  # fmt: skip


def message_payload(message: Message) -> dict:
    """The shape pushed over the WebSocket (matches `MessageOut` in the REST API)."""
    return {
        "type": "message",
        "id": str(message.id),
        "thread_id": str(message.thread_id),
        "sender_id": str(message.sender_id),
        "client_id": message.client_id,
        "body": message.body,
        "attachment_name": message.attachment_name,
        "attachment_url": attachment_url(message),
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def attachment_url(message: Message) -> str | None:
    return registry.get_storage().url(message.attachment_key) if message.attachment_key else None


def list_messages(
    db: Session, user: User, thread_id: uuid.UUID, *, limit: int, cursor: str | None
) -> tuple[list[Message], str | None]:
    """Newest first (the app reverses for display); pass `next_cursor` for older messages."""
    thread = _get_thread(db, thread_id, user)
    return paginate_cursor(
        db,
        select(Message).where(Message.thread_id == thread.id),
        Message,
        limit=limit,
        cursor=cursor,
    )


def mark_read(db: Session, user: User, thread_id: uuid.UUID) -> int:
    """Mark everything the other person sent as read. Returns how many changed."""
    thread = _get_thread(db, thread_id, user)
    result = db.execute(
        update(Message)
        .where(
            Message.thread_id == thread.id, Message.sender_id != user.id, Message.read_at.is_(None)
        )
        .values(read_at=utcnow())
    )
    db.commit()
    count = result.rowcount or 0
    if count:
        hub.broadcast(other_party(thread, user.id), {"type": "read", "thread_id": str(thread.id)})
    return count


def list_threads(
    db: Session,
    user: User,
    *,
    limit: int,
    cursor: str | None,
    listing_id: uuid.UUID | None = None,
    booking_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> tuple[list[dict], str | None]:
    """My conversations, most recent first, with the other person and unread count.

    Filter by subject to find the existing chat for a listing, booking or job."""
    stmt = select(Thread).where(or_(Thread.user_low_id == user.id, Thread.user_high_id == user.id))
    for column, value in (
        (Thread.listing_id, listing_id), (Thread.booking_id, booking_id), (Thread.job_id, job_id)
    ):  # fmt: skip
        if value is not None:
            stmt = stmt.where(column == value)
    threads, next_cursor = paginate_cursor(db, stmt, Thread, limit=limit, cursor=cursor)
    if not threads:
        return [], next_cursor
    unread = dict(
        db.execute(
            select(Message.thread_id, func.count())
            .where(
                Message.thread_id.in_([t.id for t in threads]),
                Message.sender_id != user.id,
                Message.read_at.is_(None),
            )
            .group_by(Message.thread_id)
        ).all()
    )
    others = {t.id: other_party(t, user.id) for t in threads}
    names = {u.id: u for u in db.scalars(select(User).where(User.id.in_(set(others.values()))))}
    titles = {
        li.id: li.title
        for li in db.scalars(
            select(Listing).where(Listing.id.in_([t.listing_id for t in threads if t.listing_id]))
        )
    }
    bookings = {
        b.id: b.service_title
        for b in db.scalars(
            select(Booking).where(Booking.id.in_([t.booking_id for t in threads if t.booking_id]))
        )
    }
    jobs = {
        j.id: f"Tow to {j.drop_address}"
        for j in db.scalars(
            select(HaulageJob).where(HaulageJob.id.in_([t.job_id for t in threads if t.job_id]))
        )
    }
    return [
        {
            "thread": t,
            "other": names.get(others[t.id]),
            "unread": unread.get(t.id, 0),
            "listing_title": titles.get(t.listing_id),
            "subject_title": titles.get(t.listing_id)
            or bookings.get(t.booking_id)
            or jobs.get(t.job_id),
        }
        for t in threads
    ], next_cursor


def unread_total(db: Session, user: User) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Message)
            .join(Thread, Thread.id == Message.thread_id)
            .where(
                or_(Thread.user_low_id == user.id, Thread.user_high_id == user.id),
                Message.sender_id != user.id,
                Message.read_at.is_(None),
            )
        )
        or 0
    )
