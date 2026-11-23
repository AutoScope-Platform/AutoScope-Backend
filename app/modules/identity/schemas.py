"""Request and response bodies for the identity module (these also drive the Swagger docs)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.permissions import BUSINESS_ROLES, SELF_SERVICE_ROLES, Role
from app.modules.identity.models import (
    AccountStatus,
    IdCheckStatus,
    KycDocType,
    KycStatus,
    KycSubmissionStatus,
    SocialProvider,
)

PASSWORD_HELP = "At least 8 characters with at least one letter and one number."


def check_password_strength(value: str) -> str:
    """Shared password rule used at sign-up, reset and change."""
    if len(value) < 8 or not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        raise ValueError(PASSWORD_HELP)
    if len(value) > 128:
        raise ValueError("Password is too long")
    return value


def check_signup_roles(roles: list[Role]) -> list[Role]:
    """Shared by e-mail/phone and social sign-up: self-service roles only, no duplicates."""
    if not roles:
        raise ValueError("Choose at least one role")
    if any(r not in SELF_SERVICE_ROLES for r in roles):
        raise ValueError("That role cannot be chosen at sign-up")
    return list(dict.fromkeys(roles))  # de-duplicate, keep order


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120, examples=["Ada Obi"])
    email: EmailStr | None = Field(None, examples=["ada@example.com"])
    phone: str | None = Field(
        None,
        description="Nigerian numbers may start with 0 (08012345678) or +234.",
        examples=["+2348012345678"],
    )
    business_name: str | None = Field(
        None,
        max_length=160,
        description="Required for business roles.",
        examples=["Obi Auto Gallery"],
    )
    password: str = Field(description=PASSWORD_HELP)
    roles: list[Role] = Field(
        default=[Role.VEHICLE_OWNER],
        description="Primary role first. Business roles require KYC before listings go live.",
    )
    city: str | None = Field(None, max_length=80, examples=["Lagos"])
    accept_terms: bool = Field(description="Must be true: consent to the terms and privacy policy.")

    _password = field_validator("password")(check_password_strength)

    @field_validator("roles")
    @classmethod
    def _roles_allowed(cls, roles: list[Role]) -> list[Role]:
        return check_signup_roles(roles)

    @model_validator(mode="after")
    def _contact_and_terms(self) -> RegisterIn:
        if not self.email and not self.phone:
            raise ValueError("Provide an email address or a phone number")
        if not self.accept_terms:
            raise ValueError("You must accept the terms to create an account")
        if any(r in BUSINESS_ROLES for r in self.roles) and not (
            self.business_name and self.business_name.strip()
        ):
            raise ValueError("Business accounts need a business name")
        return self


class RegisterOut(BaseModel):
    user_id: uuid.UUID
    verification_sent_to: str = Field(description="Where the OTP went: `phone` or `email`.")


class OtpVerifyIn(BaseModel):
    identifier: str = Field(description="The email or phone number used at sign-up.")
    code: str = Field(min_length=4, max_length=8, examples=["123456"])


class OtpResendIn(BaseModel):
    identifier: str


class LoginIn(BaseModel):
    identifier: str = Field(description="Email or phone number.", examples=["adaora@autoscope.ng"])
    password: str
    remember_me: bool = False


class LoginVerifyIn(BaseModel):
    challenge_token: str = Field(description="From the login response when `requires_otp` is true.")
    code: str = Field(min_length=4, max_length=8)


class LoginResendIn(BaseModel):
    challenge_token: str = Field(description="From the login response when `requires_otp` is true.")


class SocialLoginIn(BaseModel):
    """Sign in (or sign up) with Google or Apple (FR-1.2).

    Send the provider's **ID token**. For a returning user that is all. The first time,
    the API answers `422 signup_details_required`; show the sign-up form (role, terms,
    business name for business roles) and send the same token again with those fields.
    """

    provider: SocialProvider
    id_token: str = Field(min_length=5, max_length=10_000)
    nonce: str | None = Field(
        None, max_length=200, description="The raw nonce the app used, if it used one."
    )
    full_name: str | None = Field(
        None,
        min_length=2,
        max_length=120,
        description="Apple gives the name to the app only on the first sign-in: pass it here.",
    )
    roles: list[Role] = Field(default=[Role.VEHICLE_OWNER], description="New accounts only.")
    business_name: str | None = Field(None, max_length=160, description="New business accounts.")
    city: str | None = Field(None, max_length=80)
    accept_terms: bool = Field(False, description="Must be true when the account is created.")
    remember_me: bool = True

    @field_validator("roles")
    @classmethod
    def _roles_allowed(cls, roles: list[Role]) -> list[Role]:
        return check_signup_roles(roles)


class EraseIn(BaseModel):
    """Confirm account deletion with the password, or with a code from
    `POST /me/erase/code` (accounts created with Google/Apple have no password)."""

    password: str | None = None
    code: str | None = Field(None, min_length=4, max_length=8)

    @model_validator(mode="after")
    def _one_proof(self) -> EraseIn:
        if not self.password and not self.code:
            raise ValueError("Confirm with your password or a code")
        return self


class RefreshIn(BaseModel):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: str


class ForgotPasswordIn(BaseModel):
    identifier: str


class ResetPasswordIn(BaseModel):
    identifier: str
    code: str = Field(min_length=4, max_length=8)
    new_password: str = Field(description=PASSWORD_HELP)

    _password = field_validator("new_password")(check_password_strength)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(description=PASSWORD_HELP)

    _password = field_validator("new_password")(check_password_strength)


class ProfileUpdateIn(BaseModel):
    full_name: str | None = Field(None, min_length=2, max_length=120)
    business_name: str | None = Field(None, max_length=160)
    city: str | None = Field(None, max_length=80)


class AddRoleIn(BaseModel):
    role: Role


class TwoFactorIn(BaseModel):
    enabled: bool


class UserOut(BaseModel):
    # populate_by_name lets us rebuild the model from `model_dump()` (see MeOut in the router).
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    ref: str = Field(examples=["USR-1099"])
    full_name: str
    business_name: str | None
    email: str | None
    phone: str | None
    city: str | None
    roles: list[Role] = Field(validation_alias="active_roles")
    status: AccountStatus
    kyc_status: KycStatus
    two_factor_enabled: bool
    has_password: bool = Field(
        True, description="False for Google/Apple accounts until they set one via reset."
    )
    created_at: datetime
    last_active_at: datetime | None


class MeOut(UserOut):
    permissions: list[str] = Field(
        description="Capabilities this user holds. The dashboard hides menu items it cannot use; "
        "the API still enforces every rule."
    )


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")
    user: UserOut


class LoginOut(BaseModel):
    """Either tokens (no second factor needed) or an OTP challenge."""

    requires_otp: bool = False
    challenge_token: str | None = None
    otp_sent_to: str | None = None
    tokens: TokenOut | None = None


class KycDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_type: KycDocType
    filename: str
    content_type: str
    url: str | None = Field(
        None, description="Short-lived download link (admins and the owner only)."
    )


class KycOut(BaseModel):
    id: uuid.UUID
    ref: str = Field(examples=["KYC-2423"])
    status: KycSubmissionStatus
    id_check_status: IdCheckStatus
    id_check_reason: str | None
    missing_documents: list[KycDocType]
    documents: list[KycDocumentOut]
    submitted_at: datetime | None
    rejection_reason: str | None
    id_type: str | None = Field(None, description="nin or drivers_license")
    id_number_last4: str | None = Field(None, description="Only the last 4 digits are kept.")
    registration_number: str | None = Field(None, examples=["RC1234567"])
    verified_person_name: str | None = Field(
        None, description="Name on the government record (shown to admins to compare)."
    )
    verified_business_name: str | None = Field(None, description="Company name at CAC.")


NIN_PATTERN = re.compile(r"\d{11}")
LICENCE_PATTERN = re.compile(r"[A-Z0-9]{5,20}")
CAC_PATTERN = re.compile(r"(RC|BN|IT|LP|LLP)\d{4,10}")


class KycDetailsIn(BaseModel):
    """ID numbers for the automated check. Send these *before* the last document: the
    check runs when the third document arrives. Both are optional for now; a submission
    without them goes to the admin with a note that nothing was checked automatically."""

    id_type: Literal["nin", "drivers_license"] | None = Field(
        None, description="Required with id_number."
    )
    id_number: str | None = Field(None, description="11-digit NIN, or driver's licence number.")
    registration_number: str | None = Field(
        None,
        description="CAC number, e.g. RC1234567 (BN for business names). Digits alone are read as RC.",
    )
    consent: bool = Field(
        description="The provider agreed to have these numbers checked against government "
        "records (NDPR). Must be true when a number is given."
    )

    @model_validator(mode="after")
    def _check(self) -> KycDetailsIn:
        if self.id_number is not None:
            number = re.sub(r"[\s-]", "", self.id_number).upper()
            if self.id_type is None:
                raise ValueError("Choose the ID type (nin or drivers_license)")
            pattern = NIN_PATTERN if self.id_type == "nin" else LICENCE_PATTERN
            if not pattern.fullmatch(number):
                raise ValueError(
                    "A NIN is 11 digits"
                    if self.id_type == "nin"
                    else "Enter the licence number as printed (letters and digits)"
                )
            self.id_number = number
        if self.registration_number is not None:
            number = re.sub(r"[\s-]", "", self.registration_number).upper()
            if number.isdigit():
                number = f"RC{number}"
            if not CAC_PATTERN.fullmatch(number):
                raise ValueError("Enter the CAC number, e.g. RC1234567")
            self.registration_number = number
        if (self.id_number or self.registration_number) and not self.consent:
            raise ValueError("Consent is needed to check the numbers")
        if not (self.id_number or self.registration_number):
            raise ValueError("Give an ID number, a CAC number, or both")
        return self


class KycDecisionIn(BaseModel):
    approve: bool
    reason: str | None = Field(None, max_length=1000, description="Required when rejecting.")

    @model_validator(mode="after")
    def _reason_when_rejecting(self) -> KycDecisionIn:
        if not self.approve and not (self.reason and self.reason.strip()):
            raise ValueError("Give a reason when rejecting, so the provider knows what to fix")
        return self


class KycQueueItem(BaseModel):
    """One row of the admin dashboard's Verification screen."""

    id: uuid.UUID
    ref: str
    user_id: uuid.UUID
    provider_name: str
    contact_name: str | None = None
    roles: list[Role]
    city: str | None
    id_check_status: IdCheckStatus
    id_check_reason: str | None
    status: KycSubmissionStatus
    submitted_at: datetime | None


class UserListItem(BaseModel):
    """One row of the admin dashboard's Users screen."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ref: str
    full_name: str
    business_name: str | None
    email: str | None
    phone: str | None
    roles: list[Role] = Field(validation_alias="active_roles")
    city: str | None
    kyc_status: KycStatus
    status: AccountStatus
    created_at: datetime
    last_active_at: datetime | None


class SuspendIn(BaseModel):
    reason: str | None = Field(None, max_length=500)
