"""Notifications business logic: delivery, inbox, device tokens and announcements.

Delivery is triggered by domain events (see `rules.py`). Each notice becomes an in-app
inbox entry plus, depending on the rule, a push, SMS and/or e-mail. A failure to deliver one
channel is logged and never breaks the others or the request that caused the event.

Order of work (FSD 7.2: notifications must not slow down the action that caused them):
1. the inbox rows are written and committed;
2. a live `{"type": "notification"}` event goes to the person's open apps (`/ws/events`),
   which is how provider queues refresh without polling;
3. push/SMS/e-mail are handed to the background worker (`TASK_QUEUE=celery`) or, in local
   development and tests, sent straight away.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core import audit
from app.core import db as db_module
from app.core.audit import AuditCategory
from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import BadRequest, NotFound, UnprocessableEntity
from app.core.events import Event, subscribe
from app.core.pagination import paginate_cursor, paginate_page
from app.core.permissions import Role
from app.core.realtime import user_events
from app.integrations import registry
from app.modules.identity.models import AccountStatus, User, UserRole
from app.modules.notifications.models import (
    Announcement,
    AnnouncementStatus,
    DeviceToken,
    Notification,
)
from app.modules.notifications.rules import RULES, Notice

logger = logging.getLogger(__name__)

CHANNELS = ("push", "sms", "email")


# ======================================================================================
# Delivery
# ======================================================================================
def deliver(db: Session, notice: Notice, *, user: User | None = None) -> tuple[User, Notice] | None:
    """Write one notice's inbox entry. Returns what still has to go out once the caller
    has committed (pass the results to `dispatch`), or None if nothing should be sent."""
    user = user or db.get(User, notice.user_id)
    if user is None or user.erased_at is not None:
        return None
    # A suspended person is only told about it (by e-mail); nothing else is sent.
    if user.status == AccountStatus.SUSPENDED and notice.channels != ("email",):
        return None
    if notice.inbox:
        db.add(
            Notification(
                user_id=user.id,
                kind=notice.data.get("kind", ""),
                title=notice.title[:120],
                body=notice.body[:400],
                data=notice.data,
            )
        )
    return user, notice


def dispatch(pending: list[tuple[User, Notice] | None]) -> None:
    """After the commit: live event to the person's open apps, then push/SMS/e-mail."""
    for item in pending:
        if item is None:
            continue
        user, notice = item
        if notice.inbox:
            user_events.broadcast(
                user.id,
                {
                    "type": "notification",
                    "kind": notice.data.get("kind", ""),
                    "title": notice.title,
                    "body": notice.body,
                    "data": notice.data,
                },
            )
        channels = [c for c in notice.channels if c in CHANNELS]
        if channels:
            _queue_send(
                {
                    "user_id": str(user.id),
                    "title": notice.title,
                    "body": notice.body,
                    "data": notice.data,
                    "channels": channels,
                }
            )


def _queue_send(payload: dict) -> None:
    """Hand push/SMS/e-mail to the worker; send now if there is no queue (or it is down)."""
    if get_settings().task_queue == "celery":
        try:
            from app.worker import send_notification  # noqa: PLC0415

            send_notification.delay(payload)
            return
        except Exception:  # noqa: BLE001 - never lose a notice because the queue is down
            logger.exception("Could not queue a notification; sending it now")
    send_now(payload)


def send_now(payload: dict) -> None:
    """Send one notice's push/SMS/e-mail. Runs in the worker (or inline without one)."""
    db = db_module.new_session()  # looked up at call time so tests can swap it
    try:
        user = db.get(User, uuid.UUID(payload["user_id"]))
        if user is None or user.erased_at is not None:
            return
        channels = tuple(payload["channels"])
        if user.status == AccountStatus.SUSPENDED and channels != ("email",):
            return
        notice = Notice(user.id, payload["title"], payload["body"], payload["data"], channels)
        _send_channels(db, user, notice)
    finally:
        db.close()


def _send_channels(db: Session, user: User, notice: Notice) -> None:
    if "push" in notice.channels:
        tokens = list(db.scalars(select(DeviceToken.token).where(DeviceToken.user_id == user.id)))
        if tokens:
            _try(
                lambda: registry.get_push().send(tokens, notice.title, notice.body, notice.data),
                "push",
                user,
            )
    if "sms" in notice.channels and user.phone:
        _try(
            lambda: registry.get_sms().send(
                user.phone, f"AutoScope: {notice.title}. {notice.body}"
            ),
            "sms",
            user,
        )
    if "email" in notice.channels and user.email:
        _try(
            lambda: registry.get_email().send(user.email, notice.title, notice.body), "email", user
        )


def _try(action, channel: str, user: User) -> None:
    try:
        action()
    except Exception:  # noqa: BLE001 - a dead provider must not break the others
        logger.exception("Could not send %s notification to user %s", channel, user.id)


@subscribe("*")
def on_event(event: Event) -> None:
    """Turn a domain event into notifications using the rules table."""
    rule = RULES.get(event.name)
    if rule is None:
        return
    db = db_module.new_session()  # looked up at call time so tests can swap it
    try:
        pending = []
        for notice in rule(db, event.payload):
            notice.data = {**notice.data, "kind": event.name}
            pending.append(deliver(db, notice))
        db.commit()
    finally:
        db.close()
    dispatch(pending)


# ======================================================================================
# Inbox
# ======================================================================================
def list_notifications(
    db: Session, user: User, *, limit: int, cursor: str | None
) -> tuple[list[Notification], str | None]:
    return paginate_cursor(
        db,
        select(Notification).where(Notification.user_id == user.id),
        Notification,
        limit=limit,
        cursor=cursor,
    )


def unread_count(db: Session, user: User) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        )
        or 0
    )


def mark_read(db: Session, user: User, ids: list[uuid.UUID] | None) -> int:
    """Mark the given notifications (or all, when `ids` is None) as read."""
    stmt = update(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)
    )
    if ids is not None:
        stmt = stmt.where(Notification.id.in_(ids))
    result = db.execute(stmt.values(read_at=utcnow()))
    db.commit()
    return result.rowcount or 0


# ======================================================================================
# Devices
# ======================================================================================
def register_device(db: Session, user: User, token: str, platform: str) -> DeviceToken:
    """Register (or move) a push token. A token always belongs to the most recent user to
    register it, so a phone passed to someone else stops notifying the old owner."""
    device = db.scalar(select(DeviceToken).where(DeviceToken.token == token))
    if device is None:
        device = DeviceToken(token=token, user_id=user.id, platform=platform)
        db.add(device)
    device.user_id, device.platform = user.id, platform
    db.commit()
    return device


def remove_device(db: Session, user: User, token: str) -> None:
    device = db.scalar(
        select(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user.id)
    )
    if device:
        db.delete(device)
        db.commit()


# ======================================================================================
# Announcements (admin)
# ======================================================================================
def create_announcement(
    db: Session,
    admin: User,
    *,
    title: str,
    body: str,
    roles: list[str],
    city: str | None,
    channels: list[str],
    send_at: datetime | None,
    ip: str | None,
) -> Announcement:
    """Create an announcement. With no `send_at` (or a time in the past) it goes out now."""
    if not channels or any(c not in CHANNELS for c in channels):
        raise BadRequest(f"Choose channels from {', '.join(CHANNELS)}", code="invalid_channels")
    if any(r not in {role.value for role in Role} for r in roles):
        raise BadRequest("Unknown role in the audience", code="invalid_roles")
    announcement = Announcement(
        title=title.strip(), body=body.strip(), roles=roles, city=city, channels=channels,
        send_at=send_at, created_by_id=admin.id,
    )  # fmt: skip
    db.add(announcement)
    db.flush()
    audit.record(
        db, category=AuditCategory.ADMIN, action="Created announcement", actor_id=admin.id,
        actor_name=admin.full_name, target=announcement.title[:60], ip_address=ip,
    )  # fmt: skip
    db.commit()
    if send_at is None or send_at <= utcnow():
        send_announcement(db, announcement)
    return announcement


def audience(db: Session, announcement: Announcement) -> list[User]:
    """Active users matching the announcement's roles and city."""
    stmt = select(User).where(User.status == AccountStatus.ACTIVE, User.erased_at.is_(None))
    if announcement.roles:
        stmt = stmt.where(
            User.id.in_(
                select(UserRole.user_id).where(
                    UserRole.role.in_(announcement.roles), UserRole.active.is_(True)
                )
            )
        )
    if announcement.city:
        stmt = stmt.where(User.city.ilike(announcement.city))
    return list(db.scalars(stmt))


def send_announcement(db: Session, announcement: Announcement) -> int:
    """Deliver to every recipient now. Safe to call once: it is skipped if already sent."""
    locked = db.scalar(
        select(Announcement).where(Announcement.id == announcement.id).with_for_update()
    )
    if locked.status != AnnouncementStatus.SCHEDULED:
        return locked.recipients_count
    users = audience(db, locked)
    channels = tuple(c for c in locked.channels if c in CHANNELS)
    data = {"announcement_id": str(locked.id), "kind": "announcement"}
    pending = [
        deliver(db, Notice(user.id, locked.title, locked.body, data, channels), user=user)
        for user in users
    ]
    locked.status, locked.sent_at, locked.recipients_count = (
        AnnouncementStatus.SENT,
        utcnow(),
        len(users),
    )
    db.commit()
    dispatch(pending)
    return len(users)


def send_due_announcements(db: Session) -> int:
    """Scheduled job (every minute): send announcements whose time has come."""
    due = db.scalars(
        select(Announcement).where(
            Announcement.status == AnnouncementStatus.SCHEDULED, Announcement.send_at <= utcnow()
        )
    )
    return sum(1 for a in list(due) if send_announcement(db, a) >= 0)


def cancel_announcement(
    db: Session, admin: User, announcement_id: uuid.UUID, ip: str | None
) -> Announcement:
    a = db.get(Announcement, announcement_id)
    if a is None:
        raise NotFound("Announcement not found", code="announcement_not_found")
    if a.status != AnnouncementStatus.SCHEDULED:
        raise UnprocessableEntity(
            "Only scheduled announcements can be cancelled", code="invalid_status"
        )
    a.status = AnnouncementStatus.CANCELLED
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Cancelled announcement",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=a.title[:60],
        ip_address=ip,
    )
    db.commit()
    return a


def list_announcements(db: Session, *, page: int, page_size: int) -> tuple[list[Announcement], int]:
    return paginate_page(
        db,
        select(Announcement).order_by(Announcement.created_at.desc()),
        page=page,
        page_size=page_size,
    )
