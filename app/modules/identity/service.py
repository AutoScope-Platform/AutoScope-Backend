"""Identity business logic: sign-up, sign-in, tokens, passwords, profile and KYC.

Every rule lives here, not in the router. Functions take a database session first
and commit once at the end of a successful write (see `core/db.py`). They raise
`AppError` subclasses for anything the client should see.

Section map (search for the banner comments):
    1. Contact helpers        - phone/email normalisation, user lookup
    2. One-time codes (OTP)   - issue and verify
    3. Tokens                 - access/refresh issue, rotate, revoke
    4. Registration & sign-in - FR-1.1, FR-1.3, FR-1.6, FR-1.7
    5. Passwords              - FR-1.5
    6. Profile & roles
    7. KYC                    - FR-1.4, FR-13.1
    8. Admin user management  - FR-13.2
    9. Social sign-in         - FR-1.2 (Google / Apple)
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.audit import AuditCategory
from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import (
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    ServiceUnavailable,
    TooManyRequests,
    Unauthorized,
    UnprocessableEntity,
)
from app.core.events import publish
from app.core.pagination import paginate_page
from app.core.permissions import ADMIN_ROLES, BUSINESS_ROLES, SELF_SERVICE_ROLES, Role
from app.core.security import (
    constant_time_equals,
    create_token,
    decode_token,
    hash_otp,
    hash_password,
    hash_token,
    new_otp_code,
    new_refresh_token,
    verify_password,
)
from app.core.uploads import checked_type
from app.integrations import registry
from app.integrations.base import IntegrationError, SocialIdentity, SocialTokenError
from app.modules.identity.models import (
    REQUIRED_KYC_DOCS,
    AccountStatus,
    IdCheckStatus,
    KycDocType,
    KycDocument,
    KycStatus,
    KycSubmission,
    KycSubmissionStatus,
    OtpCode,
    OtpPurpose,
    RefreshToken,
    SocialAccount,
    User,
    UserRole,
)
from app.modules.identity.schemas import (
    KycDocumentOut,
    KycOut,
    LoginOut,
    RegisterIn,
    RegisterOut,
    SocialLoginIn,
    TokenOut,
    UserOut,
)

OTP_RESEND_COOLDOWN_SECONDS = 30
LOCKOUT_MINUTES = 15
KYC_MAX_BYTES = 5 * 1024 * 1024
KYC_CONTENT_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}
# Hash used to burn the same time on unknown accounts as on real ones (no user enumeration by timing).
_DUMMY_HASH = hash_password("not-a-real-password-1")


# ======================================================================================
# 1. Contact helpers
# ======================================================================================
def normalize_phone(raw: str) -> str:
    """Convert a phone number to E.164. A leading 0 is treated as a Nigerian number."""
    digits = re.sub(r"[\s\-().]", "", raw)
    if digits.startswith("+"):
        number = digits
    elif digits.startswith("00"):
        number = "+" + digits[2:]
    elif digits.startswith("234"):
        number = "+" + digits
    elif digits.startswith("0"):
        number = "+234" + digits[1:]
    elif len(digits) == 10 and digits[0] in "789":
        # A Nigerian mobile typed without its leading 0 (apps often show "+234" in a
        # separate box). Treating it as "+803..." would silently store a wrong number.
        number = "+234" + digits
    else:
        raise BadRequest(
            "Enter the number with its country code, e.g. +2348012345678", code="invalid_phone"
        )
    if not re.fullmatch(r"\+\d{10,15}", number):
        raise BadRequest("Enter a valid phone number", code="invalid_phone")
    return number


def parse_identifier(identifier: str) -> tuple[str, str]:
    """Return ("email", lowercase address) or ("phone", E.164 number)."""
    value = identifier.strip()
    if "@" in value:
        return "email", value.lower()
    return "phone", normalize_phone(value)


def _find_user(db: Session, identifier: str) -> User | None:
    try:
        kind, value = parse_identifier(identifier)
    except BadRequest:
        return None
    column = User.email if kind == "email" else User.phone
    return db.scalar(select(User).where(column == value, User.erased_at.is_(None)))


def _contact_for(user: User) -> tuple[str, str]:
    """Where to send a code: the phone if there is one, otherwise the email."""
    if user.phone:
        return "phone", user.phone
    assert user.email is not None  # registration guarantees one of the two
    return "email", user.email


# ======================================================================================
# 2. One-time codes (OTP)
# ======================================================================================
def _issue_otp(db: Session, user: User, purpose: OtpPurpose, *, reuse_pending: bool = False) -> str:
    """Create a code, send it, and return where it went ("phone" or "email").

    The resend cooldown applies only while the previous code is still waiting to be used:
    once a code has been used (e.g. the person signed in), a new one can be sent at once.
    With `reuse_pending`, a code sent within the cooldown is reused instead of refusing,
    so a double-tapped or retried sign-in does not fail (the person already has the code).
    """
    settings = get_settings()
    now = utcnow()
    latest = db.scalar(
        select(OtpCode)
        .where(OtpCode.user_id == user.id, OtpCode.purpose == purpose)
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    pending = latest is not None and latest.consumed_at is None and latest.expires_at > now
    if pending and (now - latest.created_at).total_seconds() < OTP_RESEND_COOLDOWN_SECONDS:
        if reuse_pending:
            return _contact_for(user)[0]
        raise TooManyRequests(
            "Please wait a moment before requesting another code", code="otp_cooldown"
        )

    channel, destination = _contact_for(user)
    # Retire any earlier code for this purpose so only the newest one works.
    for old in db.scalars(
        select(OtpCode).where(
            OtpCode.user_id == user.id, OtpCode.purpose == purpose, OtpCode.consumed_at.is_(None)
        )
    ):
        old.consumed_at = now

    mock_delivery = (settings.sms_driver if channel == "phone" else settings.email_driver) == "mock"
    code = settings.mock_otp_code if mock_delivery else new_otp_code()
    otp = OtpCode(
        user_id=user.id,
        purpose=purpose,
        destination=destination,
        code_hash="",
        expires_at=now + timedelta(minutes=settings.otp_ttl_minutes),
    )
    db.add(otp)
    db.flush()  # assigns otp.id, which salts the hash
    otp.code_hash = hash_otp(code, str(otp.id))

    message = f"Your AutoScope code is {code}. It expires in {settings.otp_ttl_minutes} minutes."
    try:
        if channel == "phone":
            registry.get_sms().send(destination, message)
        else:
            registry.get_email().send(destination, "Your AutoScope verification code", message)
    except IntegrationError as exc:
        raise UnprocessableEntity(
            "We could not send the code right now. Please try again shortly.",
            code="otp_send_failed",
        ) from exc
    return channel


def _check_otp(db: Session, user: User, purpose: OtpPurpose, code: str) -> None:
    """Validate a code or raise. Wrong guesses are counted and persisted before raising."""
    settings = get_settings()
    now = utcnow()
    otp = db.scalar(
        select(OtpCode)
        .where(
            OtpCode.user_id == user.id,
            OtpCode.purpose == purpose,
            OtpCode.consumed_at.is_(None),
            OtpCode.expires_at > now,
        )
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    if otp is None:
        raise BadRequest("That code has expired. Request a new one.", code="otp_expired")
    if otp.attempts >= settings.otp_max_attempts:
        raise TooManyRequests("Too many wrong codes. Request a new one.", code="otp_locked")
    if not constant_time_equals(otp.code_hash, hash_otp(code.strip(), str(otp.id))):
        otp.attempts += 1
        db.commit()  # keep the count even though we are about to raise
        raise BadRequest("That code is not correct", code="invalid_otp")
    otp.consumed_at = now


# ======================================================================================
# 3. Tokens
# ======================================================================================
def _token_response(
    db: Session,
    user: User,
    *,
    remember_me: bool,
    client: str | None,
    expires_at: datetime | None = None,
) -> TokenOut:
    settings = get_settings()
    refresh_days = settings.refresh_token_days if remember_me else 1
    refresh = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh),
            expires_at=expires_at or utcnow() + timedelta(days=refresh_days),
            last_used_at=utcnow(),
            client=client,
        )
    )
    return TokenOut(
        access_token=create_token(user.id),
        refresh_token=refresh,
        expires_in=settings.access_token_minutes * 60,
        user=UserOut.model_validate(user),
    )


def _revoke_all_tokens(db: Session, user_id: uuid.UUID) -> None:
    now = utcnow()
    for token in db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
    ):
        token.revoked_at = now


def refresh_tokens(
    db: Session, refresh_token: str, *, ip: str | None, client: str | None
) -> TokenOut:
    """Exchange a refresh token for a new pair (rotation).

    * A token that was already used is a sign of theft: every session of that user is revoked.
    * Staff sessions end after `admin_session_idle_minutes` without a refresh (the dashboard's
      "signed out after 30 minutes of inactivity").
    """
    settings = get_settings()
    now = utcnow()
    token = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
    )
    if token is None:
        raise Unauthorized("Please sign in again", code="invalid_refresh_token")
    user = db.get(User, token.user_id)
    if token.revoked_at is not None:
        _revoke_all_tokens(db, token.user_id)
        db.commit()
        raise Unauthorized("Please sign in again", code="invalid_refresh_token")
    if token.expires_at <= now or user is None or user.status != AccountStatus.ACTIVE:
        raise Unauthorized("Please sign in again", code="invalid_refresh_token")
    if user.has_role(*ADMIN_ROLES):
        idle = now - (token.last_used_at or token.created_at)
        if idle > timedelta(minutes=settings.admin_session_idle_minutes):
            token.revoked_at = now
            audit.record(
                db,
                category=AuditCategory.AUTH,
                action="Signed out after 30 minutes of inactivity",
                actor_id=user.id,
                actor_name=user.full_name,
                target=client,
                ip_address=ip,
            )
            db.commit()
            raise Unauthorized(
                "Your session expired. Please sign in again.", code="session_expired"
            )
    token.revoked_at = now
    response = _token_response(
        db, user, remember_me=True, client=client, expires_at=token.expires_at
    )
    db.commit()
    return response


def logout(
    db: Session, refresh_token: str, *, user: User | None = None, ip: str | None = None
) -> None:
    """Revoke one refresh token. Always succeeds, so clients can call it blindly."""
    token = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
    )
    if token and token.revoked_at is None:
        token.revoked_at = utcnow()
        owner = user or db.get(User, token.user_id)
        if owner:
            audit.record(
                db,
                category=AuditCategory.AUTH,
                action="Signed out",
                actor_id=owner.id,
                actor_name=owner.full_name,
                target=token.client,
                ip_address=ip,
            )
        db.commit()


# ======================================================================================
# 4. Registration & sign-in
# ======================================================================================
def register(db: Session, data: RegisterIn, *, ip: str | None, client: str | None) -> RegisterOut:
    """Create an account and send the verification code (FR-1.1, FR-1.3)."""
    email = data.email.lower() if data.email else None
    phone = normalize_phone(data.phone) if data.phone else None
    conditions = []
    if email:
        conditions.append(User.email == email)
    if phone:
        conditions.append(User.phone == phone)
    clash = db.scalar(select(User).where(or_(*conditions)).limit(1))
    if clash:
        raise Conflict(
            "An account with that email or phone already exists", code="already_registered"
        )

    is_business = any(r in BUSINESS_ROLES for r in data.roles)
    user = User(
        full_name=data.full_name.strip(),
        business_name=data.business_name.strip() if data.business_name else None,
        email=email,
        phone=phone,
        city=data.city,
        password_hash=hash_password(data.password),
        accepted_terms_at=utcnow(),
        kyc_status=KycStatus.NOT_SUBMITTED if is_business else KycStatus.NOT_REQUIRED,
    )
    user.roles = [UserRole(role=r) for r in data.roles]
    db.add(user)
    db.flush()
    sent_to = _issue_otp(db, user, OtpPurpose.VERIFY_CONTACT)
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action="Account created",
        actor_id=user.id,
        actor_name=user.full_name,
        target=client,
        ip_address=ip,
    )
    db.commit()
    return RegisterOut(user_id=user.id, verification_sent_to=sent_to)


def verify_contact(
    db: Session, identifier: str, code: str, *, ip: str | None, client: str | None
) -> TokenOut:
    """Confirm the sign-up code; the user is signed in on success."""
    user = _find_user(db, identifier)
    if user is None:
        raise BadRequest("That code is not correct", code="invalid_otp")
    _check_otp(db, user, OtpPurpose.VERIFY_CONTACT, code)
    user.verified_at = user.verified_at or utcnow()
    user.last_login_at = utcnow()
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action="Signed in",
        actor_id=user.id,
        actor_name=user.full_name,
        target=client,
        ip_address=ip,
    )
    response = _token_response(db, user, remember_me=True, client=client)
    db.commit()
    return response


def resend_otp(db: Session, identifier: str) -> None:
    """Resend a sign-up code. Silent for unknown or already verified accounts."""
    user = _find_user(db, identifier)
    if user and user.verified_at is None:
        try:
            _issue_otp(db, user, OtpPurpose.VERIFY_CONTACT)
            db.commit()
        except TooManyRequests:
            # Answering 429 here (but 202 for unknown accounts) would reveal which
            # unverified accounts exist. The person can simply try again shortly.
            db.rollback()


def _needs_second_factor(user: User) -> bool:
    """Staff always need OTP; other users when they switched 2FA on (FR-1.6)."""
    return user.two_factor_enabled or user.has_role(*ADMIN_ROLES)


def _fail_login(
    db: Session, user: User | None, reason: str, *, ip: str | None, client: str | None
) -> None:
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action=f"Failed sign-in ({reason})",
        actor_id=user.id if user else None,
        actor_name=user.full_name if user else "Unknown",
        target=client,
        ip_address=ip,
    )


def login(
    db: Session,
    identifier: str,
    password: str,
    *,
    remember_me: bool,
    ip: str | None,
    client: str | None,
) -> LoginOut:
    """Check the password; return tokens, or an OTP challenge when a second factor is required."""
    settings = get_settings()
    now = utcnow()
    user = _find_user(db, identifier)
    if user is None:
        verify_password(password, _DUMMY_HASH)  # equalise timing with the real-user path
        _fail_login(db, None, "unknown account", ip=ip, client=client)
        db.commit()
        raise Unauthorized("Incorrect email/phone or password", code="invalid_credentials")
    if user.locked_until and user.locked_until > now:
        raise Forbidden(
            "Too many failed attempts. Try again in a few minutes.", code="account_locked"
        )
    if not verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= settings.login_max_failures:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_logins = 0
        _fail_login(db, user, "wrong password", ip=ip, client=client)
        db.commit()
        raise Unauthorized("Incorrect email/phone or password", code="invalid_credentials")
    if user.status == AccountStatus.SUSPENDED:
        raise Forbidden(
            "This account has been suspended. Contact support.", code="account_suspended"
        )
    if user.verified_at is None:
        raise Forbidden("Verify your phone or email to continue", code="contact_not_verified")

    return _complete_sign_in(db, user, remember_me=remember_me, ip=ip, client=client)


def _complete_sign_in(
    db: Session,
    user: User,
    *,
    remember_me: bool,
    ip: str | None,
    client: str | None,
    method: str | None = None,
) -> LoginOut:
    """Last step of every sign-in (password or social): OTP challenge or tokens."""
    settings = get_settings()
    user.failed_logins = 0
    if _needs_second_factor(user):
        sent_to = _issue_otp(db, user, OtpPurpose.LOGIN, reuse_pending=True)
        challenge = create_token(
            user.id,
            token_type="challenge",
            minutes=settings.otp_ttl_minutes,
            extra={"rm": remember_me},
        )
        db.commit()
        return LoginOut(requires_otp=True, challenge_token=challenge, otp_sent_to=sent_to)

    user.last_login_at = utcnow()
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action=f"Signed in ({method})" if method else "Signed in",
        actor_id=user.id,
        actor_name=user.full_name,
        target=client,
        ip_address=ip,
    )
    tokens = _token_response(db, user, remember_me=remember_me, client=client)
    db.commit()
    return LoginOut(tokens=tokens)


def _user_from_challenge(db: Session, challenge_token: str) -> tuple[User, dict]:
    import jwt  # noqa: PLC0415

    try:
        claims = decode_token(challenge_token, expected_type="challenge")
        user = db.get(User, uuid.UUID(claims["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise Unauthorized("Please sign in again", code="invalid_challenge") from exc
    if user is None or user.status != AccountStatus.ACTIVE:
        raise Unauthorized("Please sign in again", code="invalid_challenge")
    return user, claims


def resend_login_otp(db: Session, challenge_token: str) -> str:
    """Send a fresh sign-in code for an open challenge (the first one never arrived)."""
    user, _ = _user_from_challenge(db, challenge_token)
    sent_to = _issue_otp(db, user, OtpPurpose.LOGIN)
    db.commit()
    return sent_to


def verify_login(
    db: Session, challenge_token: str, code: str, *, ip: str | None, client: str | None
) -> TokenOut:
    """Second step of sign-in: check the OTP and issue tokens."""
    user, claims = _user_from_challenge(db, challenge_token)
    try:
        _check_otp(db, user, OtpPurpose.LOGIN, code)
    except BadRequest as exc:
        if exc.code == "invalid_otp":
            _fail_login(db, user, "wrong OTP", ip=ip, client=client)
            db.commit()
        raise
    user.last_login_at = utcnow()
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action="Signed in",
        actor_id=user.id,
        actor_name=user.full_name,
        target=client,
        ip_address=ip,
    )
    tokens = _token_response(db, user, remember_me=bool(claims.get("rm")), client=client)
    db.commit()
    return tokens


# ======================================================================================
# 5. Passwords (FR-1.5)
# ======================================================================================
def forgot_password(db: Session, identifier: str) -> None:
    """Send a reset code. Always succeeds from the caller's view (no account enumeration)."""
    user = _find_user(db, identifier)
    if user and user.status == AccountStatus.ACTIVE:
        try:
            _issue_otp(db, user, OtpPurpose.PASSWORD_RESET)
            db.commit()
        except TooManyRequests:
            db.rollback()


def reset_password(
    db: Session,
    identifier: str,
    code: str,
    new_password: str,
    *,
    ip: str | None,
    client: str | None,
) -> None:
    user = _find_user(db, identifier)
    if user is None:
        raise BadRequest("That code is not correct", code="invalid_otp")
    _check_otp(db, user, OtpPurpose.PASSWORD_RESET, code)
    user.password_hash = hash_password(new_password)
    user.has_password = True  # also how a Google/Apple account sets its first password
    user.verified_at = user.verified_at or utcnow()  # the code proved they own the contact
    user.failed_logins = 0
    user.locked_until = None
    _revoke_all_tokens(db, user.id)  # sign out every device
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action="Password reset via OTP",
        actor_id=user.id,
        actor_name=user.full_name,
        target=client,
        ip_address=ip,
    )
    db.commit()


def change_password(db: Session, user: User, current: str, new: str, *, ip: str | None) -> None:
    if not user.has_password:
        raise BadRequest(
            "Your account has no password yet. Use 'Forgot password' to set one.",
            code="no_password_set",
        )
    if not verify_password(current, user.password_hash):
        raise BadRequest("Your current password is not correct", code="invalid_password")
    user.password_hash = hash_password(new)
    _revoke_all_tokens(db, user.id)
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action="Password changed",
        actor_id=user.id,
        actor_name=user.full_name,
        ip_address=ip,
    )
    db.commit()


# ======================================================================================
# 6. Profile & roles
# ======================================================================================
def update_profile(
    db: Session, user: User, *, full_name: str | None, business_name: str | None, city: str | None
) -> User:
    if full_name is not None:
        user.full_name = full_name.strip()
    if business_name is not None:
        user.business_name = business_name.strip() or None
    if city is not None:
        user.city = city
    db.commit()
    return user


def add_role(db: Session, user: User, role: Role, *, ip: str | None) -> User:
    """Let a user take on another role (FR-1.3). Business roles start KYC from scratch."""
    if role not in SELF_SERVICE_ROLES:
        raise Forbidden("That role cannot be chosen by yourself", code="role_not_allowed")
    existing = next((r for r in user.roles if r.role == role), None)
    if existing and existing.active:
        raise Conflict("You already have that role", code="role_exists")
    if role in BUSINESS_ROLES and not (user.business_name or "").strip():
        raise BadRequest("Add a business name to your profile first", code="business_name_required")
    if existing:
        existing.active = True
    else:
        user.roles.append(UserRole(role=role))
    if role in BUSINESS_ROLES and user.kyc_status == KycStatus.NOT_REQUIRED:
        user.kyc_status = KycStatus.NOT_SUBMITTED
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action=f"Role added ({role.value})",
        actor_id=user.id,
        actor_name=user.full_name,
        ip_address=ip,
    )
    db.commit()
    return user


def set_two_factor(db: Session, user: User, enabled: bool, *, ip: str | None) -> User:
    if user.has_role(*ADMIN_ROLES) and not enabled:
        raise Forbidden(
            "Two-factor sign-in is required for staff accounts", code="two_factor_required"
        )
    user.two_factor_enabled = enabled
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action="2FA enabled" if enabled else "2FA disabled",
        actor_id=user.id,
        actor_name=user.full_name,
        ip_address=ip,
    )
    db.commit()
    return user


def send_erasure_code(db: Session, user: User) -> str:
    """Send a code that confirms "delete my account" (for accounts without a password)."""
    sent_to = _issue_otp(db, user, OtpPurpose.ACCOUNT_ERASURE)
    db.commit()
    return sent_to


def erase_account(
    db: Session,
    user: User,
    *,
    password: str | None = None,
    code: str | None = None,
    ip: str | None,
) -> None:
    """Anonymise a person's data on request (NDPR/GDPR erasure, FSD 5.7).

    Erasure cannot be undone, so a stolen access token alone is not enough: the caller
    must also give the password or a code sent to their phone/e-mail.

    The row stays so order and payment history keep pointing at a valid user, as
    financial record-keeping rules require; everything identifying is removed.
    """
    if password and user.has_password and verify_password(password, user.password_hash):
        pass
    elif code:
        _check_otp(db, user, OtpPurpose.ACCOUNT_ERASURE, code)
    else:
        raise BadRequest("Your password is not correct", code="invalid_password")
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action="Account erased on request",
        actor_id=user.id,
        actor_name="Erased user",
        target=user.ref,
        ip_address=ip,
    )
    _anonymise(db, user)
    db.commit()


def _anonymise(db: Session, user: User) -> None:
    """Remove everything that identifies the person; keep the row for financial records."""
    storage = registry.get_storage()
    for submission in db.scalars(select(KycSubmission).where(KycSubmission.user_id == user.id)):
        for doc in submission.documents:
            try:
                storage.delete(doc.storage_key)
            except Exception:  # noqa: BLE001 - erasure continues even if one file is already gone
                pass
        db.delete(submission)
    # Unlink Google/Apple so signing in with them again starts a fresh account.
    db.execute(delete(SocialAccount).where(SocialAccount.user_id == user.id))
    _revoke_all_tokens(db, user.id)
    user.full_name = "Erased user"
    user.business_name = None
    user.email = None
    user.phone = None
    user.city = None
    user.password_hash = hash_password(secrets.token_urlsafe(24))
    user.status = AccountStatus.SUSPENDED
    user.erased_at = utcnow()


# ======================================================================================
# 7. KYC (FR-1.4, FR-13.1)
# ======================================================================================
def _open_submission(db: Session, user: User) -> KycSubmission | None:
    return db.scalar(
        select(KycSubmission)
        .where(
            KycSubmission.user_id == user.id,
            KycSubmission.status.in_([KycSubmissionStatus.INCOMPLETE, KycSubmissionStatus.PENDING]),
        )
        .order_by(KycSubmission.created_at.desc())
        .limit(1)
    )


def latest_submission(db: Session, user: User) -> KycSubmission | None:
    return db.scalar(
        select(KycSubmission)
        .where(KycSubmission.user_id == user.id)
        .order_by(KycSubmission.created_at.desc())
        .limit(1)
    )


def kyc_to_out(submission: KycSubmission, *, with_urls: bool = False) -> KycOut:
    """Build the API view of a submission; `with_urls` adds short-lived download links."""
    have = {d.doc_type for d in submission.documents}
    storage = registry.get_storage()
    documents = [
        KycDocumentOut(
            id=d.id,
            doc_type=d.doc_type,
            filename=d.filename,
            content_type=d.content_type,
            url=storage.url(d.storage_key) if with_urls else None,
        )
        for d in submission.documents
    ]
    return KycOut(
        id=submission.id,
        ref=submission.ref,
        status=submission.status,
        id_check_status=submission.id_check_status,
        id_check_reason=submission.id_check_reason,
        missing_documents=sorted(REQUIRED_KYC_DOCS - have),
        documents=documents,
        submitted_at=submission.submitted_at,
        rejection_reason=submission.rejection_reason,
        id_type=submission.id_type,
        id_number_last4=submission.id_number_last4,
        registration_number=submission.registration_number,
        verified_person_name=submission.verified_person_name,
        verified_business_name=submission.verified_business_name,
    )


def set_kyc_details(
    db: Session,
    user: User,
    *,
    id_type: str | None,
    id_number: str | None,
    registration_number: str | None,
) -> KycSubmission:
    """Save the ID and CAC numbers checked when the submission is complete. Only while the
    documents are still being collected: send them before the last document."""
    if not user.has_role(*BUSINESS_ROLES):
        raise Forbidden("Only business accounts submit KYC details", code="kyc_not_required")
    if user.kyc_status == KycStatus.APPROVED:
        raise Conflict("Your account is already verified", code="kyc_already_approved")
    submission = _open_submission(db, user)
    if submission and submission.status == KycSubmissionStatus.PENDING:
        raise Conflict("Your documents are already under review", code="kyc_under_review")
    if submission is None:
        submission = KycSubmission(user_id=user.id)
        db.add(submission)
    if id_number:
        submission.id_type = id_type
        submission.id_number = id_number
        submission.id_number_last4 = id_number[-4:]
    if registration_number:
        submission.registration_number = registration_number
    db.commit()
    return submission


def _run_number_checks(user: User, submission: KycSubmission, kyc) -> tuple[list[str], list[str]]:
    """Check the ID and CAC numbers, if given. Returns (problems, notes) for the admin."""
    problems: list[str] = []
    notes: list[str] = []
    if submission.id_number and submission.id_type:
        result = kyc.verify_person(
            id_type=submission.id_type, id_number=submission.id_number, full_name=user.full_name
        )
        submission.verified_person_name = result.name
        if not result.passed:
            problems.append(result.reason or "ID number check failed")
    else:
        notes.append("ID number")
    if submission.registration_number:
        result = kyc.verify_business(
            registration_number=submission.registration_number,
            business_name=user.business_name or user.full_name,
        )
        submission.verified_business_name = result.name
        if not result.passed:
            problems.append(result.reason or "CAC number check failed")
    else:
        notes.append("CAC number")
    return problems, notes


def upload_kyc_document(
    db: Session, user: User, doc_type: KycDocType, filename: str, content_type: str, data: bytes
) -> KycSubmission:
    """Store one KYC document. When all required documents are in, run the automated ID
    check and put the submission in the admin review queue."""
    if not user.has_role(*BUSINESS_ROLES):
        raise Forbidden("Only business accounts submit KYC documents", code="kyc_not_required")
    if user.kyc_status == KycStatus.APPROVED:
        raise Conflict("Your account is already verified", code="kyc_already_approved")
    content_type = checked_type(
        data,
        KYC_CONTENT_TYPES,
        message="Upload a JPG, PNG or PDF file",
        code="unsupported_file_type",
    )
    if not data or len(data) > KYC_MAX_BYTES:
        raise BadRequest("The file must be between 1 byte and 5 MB", code="invalid_file_size")

    submission = _open_submission(db, user)
    if submission and submission.status == KycSubmissionStatus.PENDING:
        raise Conflict("Your documents are already under review", code="kyc_under_review")
    if submission is None:
        submission = KycSubmission(user_id=user.id)
        db.add(submission)
        db.flush()

    # Keys are random and never contain the user's file name (it may hold personal data).
    key = f"kyc/{user.id}/{submission.id}/{doc_type.value}-{uuid.uuid4().hex}{KYC_CONTENT_TYPES[content_type]}"
    storage = registry.get_storage()
    storage.put(key, data, content_type)
    clean_name = re.sub(r"[^\w.\- ]", "_", filename)[:200] or "document"

    existing = next((d for d in submission.documents if d.doc_type == doc_type), None)
    if existing:
        storage.delete(existing.storage_key)
        existing.storage_key, existing.filename, existing.content_type = (
            key,
            clean_name,
            content_type,
        )
    else:
        submission.documents.append(
            KycDocument(
                doc_type=doc_type, storage_key=key, filename=clean_name, content_type=content_type
            )
        )
    db.flush()

    if REQUIRED_KYC_DOCS <= {d.doc_type for d in submission.documents}:
        _submit_kyc(db, user, submission)
    db.commit()
    if submission.status == KycSubmissionStatus.PENDING:
        publish("kyc.submitted", user_id=str(user.id), submission_id=str(submission.id))
    return submission


def _submit_kyc(db: Session, user: User, submission: KycSubmission) -> None:
    """Run the automated checks (documents, then ID and CAC numbers), then queue for human
    review. A failed or unavailable check flags the submission; the admin decides."""
    kyc = registry.get_kyc()
    problems: list[str] = []
    notes: list[str] = []
    try:
        for doc in submission.documents:
            result = kyc.check_document(
                doc_type=doc.doc_type.value,
                storage_key=doc.storage_key,
                filename=doc.filename,
                full_name=user.full_name,
            )
            if not result.passed:
                problems.append(result.reason or "Document check failed")
                break
        number_problems, notes = _run_number_checks(user, submission, kyc)
        problems += number_problems
    except IntegrationError as exc:
        problems.append(f"Automated check unavailable, check by hand ({exc})")
    # The full ID number is only needed for the lookup; keep the last 4 digits.
    submission.id_number = None
    submission.id_check_status = IdCheckStatus.FLAGGED if problems else IdCheckStatus.PASSED
    reason = "; ".join(problems) or (
        f"No {' or '.join(notes)} given: check the documents by hand" if notes else None
    )
    submission.id_check_reason = reason[:200] if reason else None
    submission.status = KycSubmissionStatus.PENDING
    submission.submitted_at = utcnow()
    user.kyc_status = KycStatus.PENDING


def list_kyc(
    db: Session, *, status: KycSubmissionStatus | None, role: Role | None, page: int, page_size: int
) -> tuple[list[KycSubmission], int]:
    """Admin queue. By default only submissions that were actually submitted (not drafts)."""
    stmt = select(KycSubmission).where(KycSubmission.status != KycSubmissionStatus.INCOMPLETE)
    if status:
        stmt = stmt.where(KycSubmission.status == status)
    if role:
        stmt = stmt.join(UserRole, UserRole.user_id == KycSubmission.user_id).where(
            UserRole.role == role, UserRole.active.is_(True)
        )
    return paginate_page(
        db, stmt.order_by(KycSubmission.submitted_at.asc()), page=page, page_size=page_size
    )


def kyc_counts(db: Session) -> dict[str, int]:
    """Totals for the tabs above the Verification table."""
    from sqlalchemy import func  # noqa: PLC0415

    rows = db.execute(
        select(KycSubmission.status, func.count())
        .where(KycSubmission.status != KycSubmissionStatus.INCOMPLETE)
        .group_by(KycSubmission.status)
    ).all()
    counts = {s.value: 0 for s in KycSubmissionStatus if s != KycSubmissionStatus.INCOMPLETE}
    counts.update({status.value: n for status, n in rows})
    return counts


def get_kyc(db: Session, submission_id: uuid.UUID) -> KycSubmission:
    submission = db.get(KycSubmission, submission_id)
    if submission is None:
        raise NotFound("KYC submission not found", code="kyc_not_found")
    return submission


def decide_kyc(
    db: Session,
    submission_id: uuid.UUID,
    *,
    approve: bool,
    reason: str | None,
    admin: User,
    ip: str | None,
) -> KycSubmission:
    """Approve or reject a pending submission and record the decision in the audit log."""
    submission = get_kyc(db, submission_id)
    if submission.status != KycSubmissionStatus.PENDING:
        raise UnprocessableEntity(
            "This submission has already been decided", code="kyc_not_pending"
        )
    user = submission.user
    now = utcnow()
    submission.reviewed_by_id, submission.reviewed_at = admin.id, now
    if approve:
        submission.status = KycSubmissionStatus.APPROVED
        user.kyc_status = KycStatus.APPROVED
    else:
        submission.status = KycSubmissionStatus.REJECTED
        submission.rejection_reason = reason
        user.kyc_status = KycStatus.REJECTED
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Approved KYC" if approve else "Rejected KYC",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=submission.ref,
        ip_address=ip,
    )
    db.commit()
    publish(
        "kyc.decided",
        user_id=str(user.id),
        submission_id=str(submission.id),
        approved=approve,
        reason=reason,
    )
    return submission


# ======================================================================================
# 8. Admin user management (FR-13.2)
# ======================================================================================
def search_users(
    db: Session, *, q: str | None, role: Role | None, account: str | None, page: int, page_size: int
) -> tuple[list[User], int]:
    """Users table. `account` is one of: active, suspended, verification_pending,
    verification_rejected (the dashboard's Status filter)."""
    stmt = select(User).where(User.erased_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                User.full_name.ilike(like),
                User.business_name.ilike(like),
                User.email.ilike(like),
                User.phone.ilike(like),
            )
        )
    if role:
        stmt = stmt.where(
            User.id.in_(
                select(UserRole.user_id).where(UserRole.role == role, UserRole.active.is_(True))
            )
        )
    if account == "active":
        stmt = stmt.where(User.status == AccountStatus.ACTIVE)
    elif account == "suspended":
        stmt = stmt.where(User.status == AccountStatus.SUSPENDED)
    elif account == "verification_pending":
        stmt = stmt.where(User.kyc_status == KycStatus.PENDING)
    elif account == "verification_rejected":
        stmt = stmt.where(User.kyc_status == KycStatus.REJECTED)
    elif account:
        raise BadRequest("Unknown account filter", code="invalid_filter")
    return paginate_page(db, stmt.order_by(User.created_at.desc()), page=page, page_size=page_size)


def get_user(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound("User not found", code="user_not_found")
    return user


def set_account_status(
    db: Session,
    user_id: uuid.UUID,
    status: AccountStatus,
    *,
    admin: User,
    reason: str | None,
    ip: str | None,
) -> User:
    """Suspend or reactivate an account. Suspending also signs the person out everywhere."""
    user = get_user(db, user_id)
    if user.id == admin.id:
        raise Forbidden("You cannot change your own account status", code="self_action")
    if user.has_role(Role.ADMIN) and not admin.has_role(Role.ADMIN):
        raise Forbidden(
            "Only a super admin can change another super admin", code="insufficient_role"
        )
    if user.status == status:
        return user
    user.status = status
    if status == AccountStatus.SUSPENDED:
        _revoke_all_tokens(db, user.id)
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action="Suspended user" if status == AccountStatus.SUSPENDED else "Reactivated user",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=user.ref,
        ip_address=ip,
    )
    db.commit()
    publish("user.status_changed", user_id=str(user.id), status=status.value, reason=reason)
    return user


def grant_staff_role(
    db: Session, user_id: uuid.UUID, role: Role, *, admin: User, ip: str | None
) -> User:
    """Super admins give staff roles (`admin` / `support`); nobody can self-assign them."""
    if role not in ADMIN_ROLES:
        raise BadRequest("Only staff roles can be granted here", code="not_a_staff_role")
    user = get_user(db, user_id)
    if any(r.role == role and r.active for r in user.roles):
        raise Conflict("That user already has this role", code="role_exists")
    user.roles.append(UserRole(role=role))
    user.two_factor_enabled = True
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action=f"Granted {role.value} role",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=user.ref,
        ip_address=ip,
    )
    db.commit()
    return user


# ======================================================================================
# 9. Social sign-in (FR-1.2)
# ======================================================================================
def social_sign_in(
    db: Session, data: SocialLoginIn, *, ip: str | None, client: str | None
) -> LoginOut:
    """Sign in, link or sign up with a Google/Apple ID token.

    1. A known provider identity signs straight in.
    2. Otherwise, a *verified* e-mail that matches an existing account links to it (the
       provider has proved the person owns that address).
    3. Otherwise a new account is created, which needs the sign-up details (role, terms).
    The last step is the same as a password sign-in, including the OTP for 2FA and staff.
    """
    provider = data.provider.value
    try:
        identity = registry.get_social().verify(provider, data.id_token, data.nonce)
    except SocialTokenError as exc:
        raise Unauthorized(
            "We could not verify that sign-in. Please try again.", code="invalid_social_token"
        ) from exc
    except IntegrationError as exc:
        raise ServiceUnavailable(
            f"{provider.title()} sign-in is not available right now.",
            code="social_login_unavailable",
        ) from exc

    now = utcnow()
    account = db.scalar(
        select(SocialAccount).where(
            SocialAccount.provider == data.provider, SocialAccount.subject == identity.subject
        )
    )
    user = db.get(User, account.user_id) if account else None
    created = False
    if user is None and identity.email:
        existing = db.scalar(
            select(User).where(User.email == identity.email, User.erased_at.is_(None))
        )
        if existing is not None and not identity.email_verified:
            raise Conflict(
                "An account with this e-mail already exists. Sign in with your password.",
                code="already_registered",
            )
        user = existing
    if user is None:
        user = _create_social_user(db, data, identity, ip=ip, client=client)
        created = True

    if account is None:
        db.add(
            SocialAccount(
                user_id=user.id,
                provider=data.provider,
                subject=identity.subject,
                email=identity.email,
                last_used_at=now,
            )
        )
        if not created:
            audit.record(
                db,
                category=AuditCategory.AUTH,
                action=f"Linked {provider} sign-in",
                actor_id=user.id,
                actor_name=user.full_name,
                target=client,
                ip_address=ip,
            )
    else:
        account.last_used_at = now

    if user.status == AccountStatus.SUSPENDED:
        raise Forbidden(
            "This account has been suspended. Contact support.", code="account_suspended"
        )
    if user.verified_at is None and identity.email_verified and user.email == identity.email:
        user.verified_at = now  # the provider proved this e-mail belongs to them
    if user.verified_at is None:
        raise Forbidden("Verify your phone or email to continue", code="contact_not_verified")
    return _complete_sign_in(
        db, user, remember_me=data.remember_me, ip=ip, client=client, method=provider
    )


def _create_social_user(
    db: Session,
    data: SocialLoginIn,
    identity: SocialIdentity,
    *,
    ip: str | None,
    client: str | None,
) -> User:
    if not identity.email:
        raise BadRequest(
            "Share your e-mail address when signing in, or sign up with e-mail or phone.",
            code="email_required",
        )
    if not identity.email_verified:
        raise BadRequest(
            "Verify your e-mail with the provider first, or sign up with e-mail or phone.",
            code="email_not_verified",
        )
    is_business = any(r in BUSINESS_ROLES for r in data.roles)
    missing = []
    if not data.accept_terms:
        missing.append("accept_terms")
    if is_business and not (data.business_name or "").strip():
        missing.append("business_name")
    if missing:
        # Not an error the user caused: the app shows the sign-up form, then sends the
        # same token again with these fields. `details` lets it prefill the form.
        raise UnprocessableEntity(
            "Finish creating your account",
            code="signup_details_required",
            details={"missing": missing, "email": identity.email, "name": identity.name},
        )

    name = (data.full_name or identity.name or identity.email.split("@")[0]).strip()[:120]
    user = User(
        full_name=name,
        business_name=data.business_name.strip() if data.business_name else None,
        email=identity.email,
        city=data.city,
        # Nobody knows this password; `has_password` tells the apps to offer "set password".
        password_hash=hash_password(secrets.token_urlsafe(32)),
        has_password=False,
        accepted_terms_at=utcnow(),
        verified_at=utcnow(),
        kyc_status=KycStatus.NOT_SUBMITTED if is_business else KycStatus.NOT_REQUIRED,
    )
    user.roles = [UserRole(role=r) for r in data.roles]
    db.add(user)
    db.flush()
    audit.record(
        db,
        category=AuditCategory.AUTH,
        action=f"Account created ({data.provider.value})",
        actor_id=user.id,
        actor_name=user.full_name,
        target=client,
        ip_address=ip,
    )
    return user


def remove_user(
    db: Session, user_id: uuid.UUID, *, admin: User, reason: str, ip: str | None
) -> None:
    """An admin removes an account for breaking the rules (FR-13.2).

    Same anonymisation as a person deleting their own account (transaction records stay),
    plus their pending and live listings come down (catalog listens for `user.removed`).
    """
    user = get_user(db, user_id)
    if user.id == admin.id:
        raise Forbidden("You cannot remove your own account here", code="self_action")
    if user.erased_at is not None:
        raise Conflict("This account was already removed", code="already_removed")
    if user.has_role(*ADMIN_ROLES):
        raise Forbidden(
            "Take away the staff role before removing a staff account", code="staff_account"
        )
    audit.record(
        db,
        category=AuditCategory.ADMIN,
        action=f"Removed user ({reason.strip()[:100]})",
        actor_id=admin.id,
        actor_name=admin.full_name,
        target=user.ref,
        ip_address=ip,
    )
    _anonymise(db, user)
    db.commit()
    publish("user.removed", user_id=str(user.id))
