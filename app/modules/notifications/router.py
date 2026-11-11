"""Notification endpoints: inbox, devices and admin announcements."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import error_responses
from app.core.ratelimit import client_ip
from app.modules.identity.deps import CurrentUser, require_admin
from app.modules.identity.models import User
from app.modules.notifications import service
from app.modules.notifications.schemas import (
    AnnouncementIn,
    AnnouncementOut,
    AnnouncementPage,
    DeviceIn,
    MarkedOut,
    MarkReadIn,
    NotificationOut,
    NotificationPage,
)

router = APIRouter(tags=["Notifications"])


@router.get(
    "/me/notifications", response_model=NotificationPage, summary="My notifications (newest first)"
)
def my_notifications(
    user: CurrentUser,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
    db: Session = Depends(get_db),
) -> NotificationPage:
    rows, next_cursor = service.list_notifications(db, user, limit=limit, cursor=cursor)
    items = [
        NotificationOut(
            id=n.id, kind=n.kind, title=n.title, body=n.body, data=n.data,
            read=n.read_at is not None, created_at=n.created_at,
        )
        for n in rows
    ]  # fmt: skip
    return NotificationPage(
        items=items, next_cursor=next_cursor, unread=service.unread_count(db, user)
    )


@router.post(
    "/me/notifications/read", response_model=MarkedOut, summary="Mark notifications as read"
)
def mark_read(body: MarkReadIn, user: CurrentUser, db: Session = Depends(get_db)) -> MarkedOut:
    return MarkedOut(marked=service.mark_read(db, user, body.ids))


@router.post(
    "/me/devices",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Register this phone for push notifications",
    description="Call after every sign-in and whenever the FCM token changes.",
)
def register_device(body: DeviceIn, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    service.register_device(db, user, body.token, body.platform)


@router.delete(
    "/me/devices/{token}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop push notifications to this phone (call on sign-out)",
)
def remove_device(token: str, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    service.remove_device(db, user, token)


# --- Admin: announcements ----------------------------------------------------------------
@router.get(
    "/admin/announcements",
    response_model=AnnouncementPage,
    summary="Announcements (admin)",
    dependencies=[Depends(require_admin)],
)
def list_announcements(
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AnnouncementPage:
    rows, total = service.list_announcements(db, page=page, page_size=page_size)
    return AnnouncementPage(items=rows, total=total)


@router.post(
    "/admin/announcements",
    response_model=AnnouncementOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send or schedule an announcement",
    description="Audience = users with any of `roles` (empty = everyone), optionally in one "
    "`city`. Without `send_at` it is sent immediately.",
    responses=error_responses(400),
)
def create_announcement(
    body: AnnouncementIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return service.create_announcement(db, admin, ip=client_ip(request), **body.model_dump())


@router.delete(
    "/admin/announcements/{announcement_id}",
    response_model=AnnouncementOut,
    summary="Cancel a scheduled announcement",
    responses=error_responses(404, 422),
)
def cancel_announcement(
    announcement_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return service.cancel_announcement(db, admin, announcement_id, client_ip(request))
