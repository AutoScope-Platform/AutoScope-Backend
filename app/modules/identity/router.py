"""Identity endpoints: sign-up, sign-in, tokens, passwords, profile and KYC upload (FSD 3.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import error_responses
from app.core.permissions import Capability, has_capability
from app.core.ratelimit import client_ip, rate_limit
from app.modules.identity import service
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import KycDocType
from app.modules.identity.schemas import (
    AddRoleIn,
    ChangePasswordIn,
    EraseIn,
    ForgotPasswordIn,
    KycOut,
    LoginIn,
    LoginOut,
    LoginResendIn,
    LoginVerifyIn,
    LogoutIn,
    MeOut,
    OtpResendIn,
    OtpVerifyIn,
    ProfileUpdateIn,
    RefreshIn,
    RegisterIn,
    RegisterOut,
    ResetPasswordIn,
    SocialLoginIn,
    TokenOut,
    TwoFactorIn,
    UserOut,
)

router = APIRouter(tags=["Identity"])


def client_name(request: Request) -> str:
    """Which app made the call (shown in the audit log): web-console, mobile-app..."""
    return (request.headers.get("x-autoscope-client") or "api")[:40]


def me_out(user) -> MeOut:
    """The caller's profile plus the capabilities their roles grant."""
    permissions = [c.value for c in Capability if has_capability(user.active_roles, c)]
    return MeOut(**UserOut.model_validate(user).model_dump(), permissions=permissions)


# --- Sign-up and verification --------------------------------------------------------
@router.post(
    "/auth/register",
    response_model=RegisterOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses=error_responses(409, 422, 429),
    dependencies=[Depends(rate_limit("register", 10, 60))],
)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)) -> RegisterOut:
    """Sign up with an email address or phone number (FR-1.1) and choose one or more roles
    (FR-1.3). A verification code is sent; confirm it with `POST /auth/otp/verify`."""
    return service.register(db, body, ip=client_ip(request), client=client_name(request))


@router.post(
    "/auth/otp/verify",
    response_model=TokenOut,
    summary="Confirm the sign-up code",
    responses=error_responses(400, 429),
    dependencies=[Depends(rate_limit("otp-verify", 20, 60))],
)
def verify_otp(body: OtpVerifyIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    """Verify the code sent at sign-up. Returns tokens, so the user is signed in."""
    return service.verify_contact(
        db, body.identifier, body.code, ip=client_ip(request), client=client_name(request)
    )


@router.post(
    "/auth/otp/resend",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resend the sign-up code",
    dependencies=[Depends(rate_limit("otp-resend", 5, 60))],
)
def resend_otp(body: OtpResendIn, db: Session = Depends(get_db)) -> dict[str, bool]:
    """Always answers 202 so the endpoint cannot be used to discover which accounts exist."""
    service.resend_otp(db, body.identifier)
    return {"accepted": True}


# --- Sign-in and tokens --------------------------------------------------------------
@router.post(
    "/auth/login",
    response_model=LoginOut,
    summary="Sign in with password",
    responses=error_responses(401, 403, 429),
    dependencies=[Depends(rate_limit("login", 20, 60))],
)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> LoginOut:
    """Check email/phone and password. Staff and users with 2FA switched on get
    `requires_otp: true` and a `challenge_token`; finish with `POST /auth/login/verify`."""
    return service.login(
        db,
        body.identifier,
        body.password,
        remember_me=body.remember_me,
        ip=client_ip(request),
        client=client_name(request),
    )


@router.post(
    "/auth/login/verify",
    response_model=TokenOut,
    summary="Finish sign-in with the OTP",
    responses=error_responses(400, 401, 429),
    dependencies=[Depends(rate_limit("login-otp", 20, 60))],
)
def login_verify(body: LoginVerifyIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    return service.verify_login(
        db, body.challenge_token, body.code, ip=client_ip(request), client=client_name(request)
    )


@router.post(
    "/auth/login/resend",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resend the sign-in code",
    responses=error_responses(401, 429),
    dependencies=[Depends(rate_limit("login-otp-resend", 5, 60))],
)
def login_resend(body: LoginResendIn, db: Session = Depends(get_db)) -> dict[str, str]:
    """For a sign-in waiting on its OTP (`requires_otp: true`). Answers 429 `otp_cooldown`
    if the last code was sent under 30 seconds ago."""
    return {"otp_sent_to": service.resend_login_otp(db, body.challenge_token)}


@router.post(
    "/auth/social",
    response_model=LoginOut,
    summary="Sign in or sign up with Google or Apple",
    responses=error_responses(400, 401, 403, 409, 422, 429, 503),
    dependencies=[Depends(rate_limit("social-login", 20, 60))],
)
def social_login(body: SocialLoginIn, request: Request, db: Session = Depends(get_db)) -> LoginOut:
    """Send the ID token from the Google or Apple SDK (FR-1.2).

    * Returning user: tokens (or an OTP challenge when 2FA is on), like `POST /auth/login`.
    * New user: `422 signup_details_required` with `details.missing` (e.g. `accept_terms`,
      `business_name`) and the e-mail/name to prefill. Show the sign-up form, then send the
      same token again with `accept_terms: true`, `roles` and, for business roles,
      `business_name`.
    * An e-mail/password account with the same verified e-mail is linked automatically.
    """
    return service.social_sign_in(db, body, ip=client_ip(request), client=client_name(request))


@router.post(
    "/auth/refresh",
    response_model=TokenOut,
    summary="Get a new access token",
    responses=error_responses(401, 429),
    dependencies=[Depends(rate_limit("refresh", 30, 60))],
)
def refresh(body: RefreshIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    """Rotate the refresh token. The old one stops working immediately."""
    return service.refresh_tokens(
        db, body.refresh_token, ip=client_ip(request), client=client_name(request)
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
def logout(body: LogoutIn, request: Request, db: Session = Depends(get_db)) -> Response:
    service.logout(db, body.refresh_token, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Passwords -----------------------------------------------------------------------
@router.post(
    "/auth/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a reset code",
    dependencies=[Depends(rate_limit("password-forgot", 5, 60))],
)
def forgot_password(body: ForgotPasswordIn, db: Session = Depends(get_db)) -> dict[str, bool]:
    """Sends a code by SMS or email (FR-1.5). Always answers 202."""
    service.forgot_password(db, body.identifier)
    return {"accepted": True}


@router.post(
    "/auth/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password",
    responses=error_responses(400, 429),
    dependencies=[Depends(rate_limit("password-reset", 10, 60))],
)
def reset_password(
    body: ResetPasswordIn, request: Request, db: Session = Depends(get_db)
) -> Response:
    service.reset_password(
        db,
        body.identifier,
        body.code,
        body.new_password,
        ip=client_ip(request),
        client=client_name(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Current user --------------------------------------------------------------------
@router.get(
    "/me",
    response_model=MeOut,
    summary="My profile and permissions",
    responses=error_responses(401, 403),
)
def get_me(user: CurrentUser) -> MeOut:
    """Both clients call this after sign-in; the dashboard uses `permissions` to decide
    which menu items to show."""
    return me_out(user)


@router.patch(
    "/me", response_model=MeOut, summary="Update my profile", responses=error_responses(401)
)
def update_me(body: ProfileUpdateIn, user: CurrentUser, db: Session = Depends(get_db)) -> MeOut:
    return me_out(
        service.update_profile(
            db, user, full_name=body.full_name, business_name=body.business_name, city=body.city
        )
    )


@router.post(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change my password",
    responses=error_responses(400, 401),
)
def change_password(
    body: ChangePasswordIn, request: Request, user: CurrentUser, db: Session = Depends(get_db)
) -> Response:
    """Signs out every other device."""
    service.change_password(
        db, user, body.current_password, body.new_password, ip=client_ip(request)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/roles",
    response_model=MeOut,
    summary="Take on another role",
    responses=error_responses(400, 401, 403, 409),
)
def add_role(
    body: AddRoleIn, request: Request, user: CurrentUser, db: Session = Depends(get_db)
) -> MeOut:
    """One account can hold several roles (FR-1.3). Business roles need KYC."""
    return me_out(service.add_role(db, user, body.role, ip=client_ip(request)))


@router.put(
    "/me/two-factor",
    response_model=MeOut,
    summary="Turn two-factor sign-in on or off",
    responses=error_responses(401, 403),
)
def set_two_factor(
    body: TwoFactorIn, request: Request, user: CurrentUser, db: Session = Depends(get_db)
) -> MeOut:
    return me_out(service.set_two_factor(db, user, body.enabled, ip=client_ip(request)))


@router.post(
    "/me/erase/code",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a code to confirm account deletion",
    responses=error_responses(401, 429),
    dependencies=[Depends(rate_limit("erase-code", 5, 60))],
)
def erase_code(user: CurrentUser, db: Session = Depends(get_db)) -> dict[str, str]:
    """For accounts without a password (Google/Apple). Then call `DELETE /me` with the code."""
    return {"sent_to": service.send_erasure_code(db, user)}


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Erase my account",
    responses=error_responses(400, 401, 429),
)
def erase_me(
    body: EraseIn, request: Request, user: CurrentUser, db: Session = Depends(get_db)
) -> Response:
    """Anonymises personal data (NDPR/GDPR right to erasure). Transaction records are kept.

    Cannot be undone, so the body must confirm it: `{"password": "..."}`, or
    `{"code": "123456"}` from `POST /me/erase/code`. (This DELETE has a JSON body; on
    Android/Retrofit use `@HTTP(method = "DELETE", hasBody = true)`.)
    """
    service.erase_account(db, user, password=body.password, code=body.code, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- KYC -----------------------------------------------------------------------------
@router.get(
    "/me/kyc",
    response_model=KycOut | None,
    summary="My verification status",
    responses=error_responses(401),
)
def my_kyc(user: CurrentUser, db: Session = Depends(get_db)) -> KycOut | None:
    submission = service.latest_submission(db, user)
    return service.kyc_to_out(submission, with_urls=True) if submission else None


@router.post(
    "/me/kyc-documents",
    response_model=KycOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a KYC document",
    responses=error_responses(400, 401, 403, 409),
    dependencies=[Depends(rate_limit("kyc-upload", 20, 60))],
)
def upload_kyc_document(
    user: CurrentUser,
    doc_type: KycDocType = Form(description="government_id, business_registration or licence"),
    file: UploadFile = File(description="JPG, PNG or PDF, up to 5 MB"),
    db: Session = Depends(get_db),
) -> KycOut:
    """Business accounts upload three documents (FR-1.4). When the last one arrives, the
    automated ID check runs and the submission joins the admin review queue."""
    data = file.file.read(service.KYC_MAX_BYTES + 1)
    submission = service.upload_kyc_document(
        db, user, doc_type, file.filename or "document", file.content_type or "", data
    )
    return service.kyc_to_out(submission)
