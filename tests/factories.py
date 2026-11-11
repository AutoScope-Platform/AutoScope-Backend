"""Helpers that create rows directly in the database for tests."""

from __future__ import annotations

import itertools

from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.permissions import BUSINESS_ROLES, Role
from app.core.security import create_token, hash_password
from app.modules.identity.models import KycStatus, User, UserRole

PASSWORD = "AutoScope#2026"
_counter = itertools.count(1)


def make_user(
    db: Session,
    *roles: Role,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    two_factor: bool = False,
    business_name: str | None = None,
    kyc: KycStatus | None = None,
    city: str = "Lagos",
) -> User:
    """Create a verified user holding `roles` (default: vehicle owner)."""
    n = next(_counter)
    roles = roles or (Role.VEHICLE_OWNER,)
    is_business = any(r in BUSINESS_ROLES for r in roles)
    user = User(
        full_name=name or f"Test User {n}",
        business_name=business_name or (f"Test Business {n}" if is_business else None),
        email=email or f"user{n}@example.com",
        phone=phone,
        password_hash=hash_password(PASSWORD),
        city=city,
        verified_at=utcnow(),
        two_factor_enabled=two_factor,
        kyc_status=kyc or (KycStatus.NOT_SUBMITTED if is_business else KycStatus.NOT_REQUIRED),
    )
    user.roles = [UserRole(role=r) for r in roles]
    db.add(user)
    db.commit()
    return user


def auth_headers(user: User) -> dict[str, str]:
    """Bearer header for `user`, skipping the sign-in flow."""
    return {"Authorization": f"Bearer {create_token(user.id)}"}
