"""FastAPI dependencies other modules use to know who is calling.

    from app.modules.identity.deps import CurrentUser, require_capability

    @router.post("/listings")
    def create_listing(user: User = Depends(require_capability(Capability.LIST_PARTS))): ...

These are the identity module's public interface: other modules import from here,
never from `identity.models` or `identity.service` directly for auth decisions.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.db import get_db, utcnow
from app.core.errors import Forbidden, Unauthorized
from app.core.permissions import Capability, has_capability
from app.core.security import decode_token
from app.modules.identity.models import AccountStatus, User

# `auto_error=False` so we can return our own error shape instead of FastAPI's default.
_bearer = HTTPBearer(auto_error=False, description="Access token from `POST /auth/login`.")

# `last_active_at` is refreshed at most this often to avoid a write on every request.
_ACTIVITY_THROTTLE = timedelta(minutes=5)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the bearer token to an active user, or raise 401/403."""
    if credentials is None:
        raise Unauthorized("Sign in to continue", code="not_authenticated")
    try:
        claims = decode_token(credentials.credentials, expected_type="access")
        user_id = uuid.UUID(claims["sub"])
    except jwt.ExpiredSignatureError:
        raise Unauthorized("Your session expired", code="token_expired") from None
    except (jwt.PyJWTError, ValueError, KeyError):
        raise Unauthorized("Invalid access token", code="invalid_token") from None

    user = db.get(User, user_id)
    if user is None or user.erased_at is not None:
        raise Unauthorized("Invalid access token", code="invalid_token")
    if user.status == AccountStatus.SUSPENDED:
        raise Forbidden("This account has been suspended", code="account_suspended")

    now = utcnow()
    if user.last_active_at is None or now - user.last_active_at > _ACTIVITY_THROTTLE:
        user.last_active_at = now
        db.commit()
    request.state.user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_capability(capability: Capability) -> Callable[..., User]:
    """Dependency factory: the caller must hold `capability` (see `core/permissions.py`)."""

    def dependency(user: User = Depends(get_current_user)) -> User:
        if not has_capability(user.active_roles, capability):
            raise Forbidden("You do not have permission to do that", code="forbidden")
        return user

    return dependency


# Ready-made dependencies for the two staff levels.
require_admin = require_capability(Capability.ADMIN_CONSOLE)
require_super_admin = require_capability(Capability.ADMIN_FINANCE)
