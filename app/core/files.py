"""Serves files for the *local* storage driver (development and tests only).

With S3-compatible storage, clients download straight from presigned URLs and this
router is never used. Links are signed and expire, so keys cannot be guessed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import APIRouter, Response

from app.core.config import get_settings
from app.core.errors import Forbidden, NotFound
from app.integrations import registry
from app.integrations.storage import LocalStorage

router = APIRouter(tags=["Files"], include_in_schema=False)


@router.get("/files/{key:path}")
def download(key: str, expires: int, sig: str) -> Response:
    storage = registry.get_storage()
    if not isinstance(storage, LocalStorage):
        raise NotFound("Not found")
    expected = hmac.new(
        get_settings().jwt_secret.encode(), f"{key}:{expires}".encode(), hashlib.sha256
    ).digest()
    token = base64.urlsafe_b64encode(expected).decode().rstrip("=")
    if expires < time.time() or not hmac.compare_digest(token, sig):
        raise Forbidden("This link has expired", code="link_expired")
    try:
        data = storage.get(key)
    except FileNotFoundError:
        raise NotFound("File not found") from None
    return Response(content=data, media_type="application/octet-stream")
