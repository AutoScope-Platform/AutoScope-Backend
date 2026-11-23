"""Identity tables: users, roles, KYC, one-time codes and refresh tokens (FSD 3.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, Timestamps, UUIDPrimaryKey, enum_type, ref_column
from app.core.permissions import Role


class AccountStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class KycStatus(StrEnum):
    NOT_REQUIRED = "not_required"  # vehicle owners and staff never need KYC
    NOT_SUBMITTED = "not_submitted"  # business account that has not uploaded documents yet
    PENDING = "pending"  # submitted, waiting for an admin
    APPROVED = "approved"
    REJECTED = "rejected"


class User(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "users"

    ref_no: Mapped[int] = ref_column()
    full_name: Mapped[str] = mapped_column(String(120))
    # Trading name for business accounts (e.g. "Nwosu Car Mart"); the dashboard shows it
    # instead of the person's name.
    business_name: Mapped[str | None] = mapped_column(String(160))
    # Email and phone are each unique when present; at least one is required (FR-1.1).
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True)  # E.164, e.g. +2348012345678
    password_hash: Mapped[str] = mapped_column(String(255))
    # False for accounts created through Google/Apple: they have a random hash nobody knows
    # and must use "forgot password" to set one (FR-1.2).
    has_password: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    city: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[AccountStatus] = mapped_column(
        enum_type(AccountStatus), default=AccountStatus.ACTIVE, index=True
    )
    kyc_status: Mapped[KycStatus] = mapped_column(
        enum_type(KycStatus), default=KycStatus.NOT_REQUIRED, index=True
    )
    # Becomes true once the sign-up OTP is confirmed; unverified accounts cannot sign in.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Consent captured at sign-up (NDPR / GDPR, FSD 5.7).
    accepted_terms_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the person asks for erasure; personal fields are anonymised (FSD 5.7).
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    roles: Mapped[list[UserRole]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def ref(self) -> str:
        return f"USR-{self.ref_no}"

    @property
    def display_name(self) -> str:
        return self.business_name or self.full_name

    @property
    def active_roles(self) -> list[Role]:
        return [r.role for r in self.roles if r.active]

    def has_role(self, *roles: Role) -> bool:
        return any(r in roles for r in self.active_roles)


class UserRole(UUIDPrimaryKey, Timestamps, Base):
    """One row per role a user holds, so one account can be e.g. owner and mechanic (FR-1.3)."""

    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[Role] = mapped_column(enum_type(Role))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="roles")


class OtpPurpose(StrEnum):
    VERIFY_CONTACT = "verify_contact"  # confirm the phone/email at sign-up
    LOGIN = "login"  # second factor at sign-in
    PASSWORD_RESET = "password_reset"
    ACCOUNT_ERASURE = "account_erasure"  # confirms "delete my account"


class OtpCode(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "otp_codes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[OtpPurpose] = mapped_column(enum_type(OtpPurpose))
    destination: Mapped[str] = mapped_column(String(254))  # where the code was sent
    code_hash: Mapped[str] = mapped_column(String(64))  # keyed hash, never the code itself
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SocialProvider(StrEnum):
    GOOGLE = "google"
    APPLE = "apple"


class SocialAccount(UUIDPrimaryKey, Timestamps, Base):
    """A Google or Apple identity linked to a user (FR-1.2).

    `subject` is the provider's stable user id (the token's `sub`), which never changes even
    if the person changes their e-mail address, so sign-in matches on it, not on e-mail.
    """

    __tablename__ = "social_accounts"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[SocialProvider] = mapped_column(enum_type(SocialProvider))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(254))  # as the provider reported it
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(UUIDPrimaryKey, Timestamps, Base):
    """A long-lived login. Rotated on every use; presenting a used token revokes the family."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client: Mapped[str | None] = mapped_column(String(40))  # "web-console" | "mobile-app"


class IdCheckStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FLAGGED = "flagged"


class KycDocType(StrEnum):
    GOVERNMENT_ID = "government_id"
    BUSINESS_REGISTRATION = "business_registration"
    LICENCE = "licence"  # licence or certification


# FR-1.4: every business account submits all three before its listings can go live.
REQUIRED_KYC_DOCS = frozenset(KycDocType)


class KycSubmissionStatus(StrEnum):
    INCOMPLETE = "incomplete"  # still collecting documents
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class KycSubmission(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "kyc_submissions"

    ref_no: Mapped[int] = ref_column(start=2000)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[KycSubmissionStatus] = mapped_column(
        enum_type(KycSubmissionStatus), default=KycSubmissionStatus.INCOMPLETE, index=True
    )
    id_check_status: Mapped[IdCheckStatus] = mapped_column(
        enum_type(IdCheckStatus), default=IdCheckStatus.PENDING
    )
    id_check_reason: Mapped[str | None] = mapped_column(String(200))
    # Numbers the provider gives for the automated check (PUT /me/kyc-details).
    id_type: Mapped[str | None] = mapped_column(String(20))  # nin | drivers_license
    id_number: Mapped[str | None] = mapped_column(String(40))  # cleared once checked
    id_number_last4: Mapped[str | None] = mapped_column(String(4))
    registration_number: Mapped[str | None] = mapped_column(String(30))  # CAC, e.g. RC123456
    # What the ID service returned, for the admin to compare with the documents.
    verified_person_name: Mapped[str | None] = mapped_column(String(200))
    verified_business_name: Mapped[str | None] = mapped_column(String(300))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    documents: Mapped[list[KycDocument]] = relationship(
        back_populates="submission", cascade="all, delete-orphan", lazy="selectin"
    )
    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")

    @property
    def ref(self) -> str:
        return f"KYC-{self.ref_no}"


class KycDocument(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "kyc_documents"
    __table_args__ = (UniqueConstraint("submission_id", "doc_type"),)

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kyc_submissions.id", ondelete="CASCADE"), index=True
    )
    doc_type: Mapped[KycDocType] = mapped_column(enum_type(KycDocType))
    storage_key: Mapped[str] = mapped_column(String(300))  # object-storage key, never a URL
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(100))

    submission: Mapped[KycSubmission] = relationship(back_populates="documents")
