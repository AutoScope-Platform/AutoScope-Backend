"""Password hashing, JWTs and one-time codes.

Nothing here touches the database; the identity module wires these helpers to
its tables. Secrets (passwords, OTP codes, refresh tokens) are never stored in
plain text: passwords use Argon2, OTP codes use a keyed HMAC, refresh tokens use
SHA-256 of a random value.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings
from app.core.db import utcnow

_hasher = PasswordHasher()
TokenType = Literal["access", "challenge"]


# --- Passwords ---------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check; returns False (never raises) for a wrong or malformed hash."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# --- JWT access tokens ---------------------------------------------------------------
def create_token(
    subject: uuid.UUID,
    *,
    token_type: TokenType = "access",
    minutes: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Sign a short-lived JWT.

    `access` tokens authorise API calls. `challenge` tokens are handed out after a
    correct password when a second factor (OTP) is still required; they cannot be
    used as access tokens because `decode_token` checks the `typ` claim.
    """
    settings = get_settings()
    now = utcnow()
    lifetime = minutes if minutes is not None else settings.access_token_minutes
    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        "iat": now,
        "exp": now + timedelta(minutes=lifetime),
        "jti": uuid.uuid4().hex,
        **(extra or {}),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: TokenType = "access") -> dict[str, Any]:
    """Validate signature, expiry and token type. Raises `jwt.PyJWTError` on any problem."""
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


# --- Refresh tokens and OTP codes ----------------------------------------------------
def new_refresh_token() -> str:
    """Opaque random refresh token (the plain value is only ever sent to the client)."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_otp_code() -> str:
    """Six random digits. The mock SMS driver overrides this in non-production."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(code: str, salt: str) -> str:
    """Keyed hash so a leaked table does not reveal valid 6-digit codes."""
    key = get_settings().jwt_secret.encode()
    return hmac.new(key, f"{salt}:{code}".encode(), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
