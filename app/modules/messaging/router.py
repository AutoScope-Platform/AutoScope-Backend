"""Messaging endpoints (REST) and the chat WebSocket."""

from __future__ import annotations

import uuid

import jwt
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, WebSocket, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core import db as db_module
from app.core.db import get_db
from app.core.errors import error_responses
from app.core.ratelimit import rate_limit
from app.core.realtime import pump
from app.core.security import decode_token
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import AccountStatus, User
from app.modules.messaging import service
from app.modules.messaging.models import Message
from app.modules.messaging.schemas import (
    MessageOut,
    MessagePage,
    PersonOut,
    ReadOut,
    SendIn,
    StartThreadIn,
    StartThreadOut,
    ThreadOut,
    ThreadPage,
    UnreadOut,
)

router = APIRouter(tags=["Messaging"])

# Stops a script flooding someone with messages.
SEND_LIMIT = [Depends(rate_limit("chat-send", 60, 60))]


def _msg(m: Message, me: User) -> MessageOut:
    return MessageOut(
        id=m.id, thread_id=m.thread_id, sender_id=m.sender_id, body=m.body,
        attachment_name=m.attachment_name, attachment_url=service.attachment_url(m),
        read=m.read_at is not None, created_at=m.created_at,
    )  # fmt: skip


@router.post(
    "/threads",
    response_model=StartThreadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Message someone (opens the conversation if it does not exist)",
    responses=error_responses(404, 422, 429),
    dependencies=SEND_LIMIT,
)
def start_thread(body: StartThreadIn, user: CurrentUser, db: Session = Depends(get_db)):
    thread, message = service.start_thread(
        db, user, body.recipient_id, body.listing_id, body.message
    )
    return StartThreadOut(thread_id=thread.id, message=_msg(message, user))


@router.get("/threads", response_model=ThreadPage, summary="My conversations")
def list_threads(
    user: CurrentUser,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
    db: Session = Depends(get_db),
) -> ThreadPage:
    rows, next_cursor = service.list_threads(db, user, limit=limit, cursor=cursor)
    return ThreadPage(
        items=[
            ThreadOut(
                id=r["thread"].id,
                other=PersonOut(id=r["other"].id, name=r["other"].display_name),
                listing_id=r["thread"].listing_id,
                listing_title=r["listing_title"],
                last_message_at=r["thread"].last_message_at,
                last_message_preview=r["thread"].last_message_preview,
                unread=r["unread"],
            )
            for r in rows
        ],
        next_cursor=next_cursor,
    )


@router.get(
    "/threads/unread-count",
    response_model=UnreadOut,
    summary="Unread messages in total (for a badge)",
)
def unread(user: CurrentUser, db: Session = Depends(get_db)) -> UnreadOut:
    return UnreadOut(unread=service.unread_total(db, user))


@router.get(
    "/threads/{thread_id}/messages",
    response_model=MessagePage,
    summary="Messages in a conversation (newest first)",
    responses=error_responses(404),
)
def list_messages(
    thread_id: uuid.UUID,
    user: CurrentUser,
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = None,
    db: Session = Depends(get_db),
) -> MessagePage:
    rows, next_cursor = service.list_messages(db, user, thread_id, limit=limit, cursor=cursor)
    return MessagePage(items=[_msg(m, user) for m in rows], next_cursor=next_cursor)


@router.post(
    "/threads/{thread_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message",
    responses=error_responses(404, 429),
    dependencies=SEND_LIMIT,
)
def send(thread_id: uuid.UUID, body: SendIn, user: CurrentUser, db: Session = Depends(get_db)):
    return _msg(service.send_message(db, user, thread_id, body.body), user)


@router.post(
    "/threads/{thread_id}/attachments",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send a photo or PDF (up to 8 MB)",
    responses=error_responses(400, 404, 429),
    dependencies=SEND_LIMIT,
)
async def send_attachment(
    thread_id: uuid.UUID,
    user: CurrentUser,
    file: UploadFile = File(...),
    caption: str = Form(""),
    db: Session = Depends(get_db),
):
    message = service.send_attachment(
        db,
        user,
        thread_id,
        file.filename or "file",
        file.content_type or "",
        await file.read(),
        caption,
    )
    return _msg(message, user)


@router.post(
    "/threads/{thread_id}/read",
    response_model=ReadOut,
    summary="Mark the other person's messages as read",
    responses=error_responses(404),
)
def mark_read(thread_id: uuid.UUID, user: CurrentUser, db: Session = Depends(get_db)) -> ReadOut:
    return ReadOut(marked=service.mark_read(db, user, thread_id))


# --- WebSocket ---------------------------------------------------------------------------
def _user_for_token(token: str) -> uuid.UUID | None:
    """Blocking helper (runs in a thread): the user id if the token is valid and active."""
    try:
        user_id = uuid.UUID(decode_token(token, expected_type="access")["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        return None
    db = db_module.new_session()
    try:
        user = db.get(User, user_id)
        ok = user is not None and user.erased_at is None and user.status != AccountStatus.SUSPENDED
        return user_id if ok else None
    finally:
        db.close()


@router.websocket("/ws/chat")
async def chat(websocket: WebSocket, token: str = Query("")) -> None:
    """Live chat. Connect with `?token=<access token>`. After the first message the server
    pushes `{"type": "message", ...}` for every new message in any of your conversations and
    `{"type": "read", "thread_id": ...}` when the other person reads yours. Send messages
    with `POST /threads/{id}/messages` (the WebSocket is receive-only)."""
    user_id = await run_in_threadpool(_user_for_token, token)
    if user_id is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    await pump(websocket, service.hub, user_id, first={"type": "ready"})
