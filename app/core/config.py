"""Application settings.

All configuration comes from environment variables (see `.env.example`). Nothing
in this file contains a secret; real values live in the host's environment
(Render dashboard in staging/production, a local `.env` file in development).

Every external service (SMS, email, push, payments, KYC, storage, VIN lookup) is
chosen by a `*_DRIVER` setting. The `mock` drivers let the whole API run with no
third-party accounts, which is how local development, tests and the staging demo
work. Mock drivers are refused in production (see `_reject_mocks_in_production`).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- General -----------------------------------------------------------------
    env: Environment = "local"
    app_name: str = "AutoScope API"
    api_prefix: str = "/api/v1"
    # Comma-separated list of origins allowed to call the API from a browser
    # (the admin dashboard and any web client). "*" is only accepted outside production.
    cors_origins: str = "*"

    # --- Database / cache --------------------------------------------------------
    # Accepts a plain `postgresql://` URL (what Neon and Render hand out); it is
    # rewritten to the psycopg 3 driver in `database_url_sync` below.
    database_url: str = "postgresql://autoscope:autoscope@127.0.0.1:5432/autoscope_dev"
    # Redis is optional in local/test (rate limits and chat fan-out fall back to memory).
    redis_url: str | None = None

    # --- Auth --------------------------------------------------------------------
    jwt_secret: str = Field(default="dev-only-change-me-dev-only-change-me")
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    # Failed password attempts before an account is temporarily locked.
    login_max_failures: int = 5
    # Business accounts and admins must pass a second factor (FR-1.6).
    admin_session_idle_minutes: int = 30

    # Turn off in tests that are not about rate limiting.
    rate_limit_enabled: bool = True

    # --- Integration drivers (see app/integrations) --------------------------------
    sms_driver: Literal["mock", "termii"] = "mock"
    email_driver: Literal["mock", "smtp"] = "mock"
    push_driver: Literal["mock", "fcm"] = "mock"
    payment_driver: Literal["mock", "paystack"] = "mock"
    # Only "mock" exists until a KYC vendor is chosen (see docs/INTEGRATIONS.md).
    kyc_driver: Literal["mock"] = "mock"
    storage_driver: Literal["local", "s3"] = "local"
    vin_driver: Literal["mock", "nhtsa"] = "mock"

    # Fixed OTP accepted by the mock SMS driver so the demo and tests are repeatable.
    mock_otp_code: str = "123456"

    # --- Driver credentials: leave empty until you have the real keys --------------
    paystack_secret_key: str | None = None
    paystack_public_key: str | None = None
    termii_api_key: str | None = None
    termii_sender_id: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_from: str = "AutoScope <no-reply@autoscope.ng>"
    fcm_service_account_json: str | None = None
    # S3-compatible object storage (Cloudflare R2, Neon storage, AWS S3, MinIO...).
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str = "auto"
    local_storage_dir: str = "./.storage"

    # --- Business rules defaults (all editable from the admin dashboard) ------------
    default_hold_days: int = 3

    @property
    def database_url_sync(self) -> str:
        """The URL rewritten for SQLAlchemy + psycopg 3."""
        url = self.database_url
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @model_validator(mode="after")
    def _reject_mocks_in_production(self) -> Settings:
        """Refuse to boot in production with fake integrations or default secrets."""
        if not self.is_production:
            return self
        mocks = [
            name
            for name, value in {
                "SMS_DRIVER": self.sms_driver,
                "EMAIL_DRIVER": self.email_driver,
                "PUSH_DRIVER": self.push_driver,
                "PAYMENT_DRIVER": self.payment_driver,
                "KYC_DRIVER": self.kyc_driver,
                "VIN_DRIVER": self.vin_driver,
            }.items()
            if value == "mock"
        ]
        if self.storage_driver == "local":
            mocks.append("STORAGE_DRIVER")
        if mocks:
            raise ValueError(f"Mock drivers are not allowed in production: {', '.join(mocks)}")
        if self.jwt_secret.startswith("dev-only"):
            raise ValueError("JWT_SECRET must be set to a strong random value in production")
        if "*" in self.cors_origin_list:
            raise ValueError("CORS_ORIGINS must list explicit origins in production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance. Tests clear the cache after changing env vars."""
    return Settings()
