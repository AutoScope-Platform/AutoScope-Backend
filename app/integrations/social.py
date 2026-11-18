"""Google and Apple sign-in: verify the ID token the mobile app got from the provider.

The app runs the provider's native sign-in (Google Sign-In SDK, Sign in with Apple) and
sends us the resulting **ID token**. We never see a password; we only check that the token
was signed by the provider, is still valid, and was issued to *our* app (the `aud` claim).

Settings (see docs/INTEGRATIONS.md):

* `SOCIAL_DRIVER=live`
* `GOOGLE_CLIENT_IDS`: the OAuth client ids of the iOS, Android and web apps, comma-separated.
* `APPLE_CLIENT_IDS`: the iOS bundle id (plus the Services id if web sign-in is added).

Nonce: if the app sends the nonce it used, the token must carry it. Google puts the raw
nonce in the token; Apple puts whatever the app passed, which by Apple's guidance is the
SHA-256 of the raw nonce, so both forms are accepted.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from typing import Any

import jwt

from app.core.config import Settings
from app.integrations.base import IntegrationError, SocialIdentity, SocialTokenError

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
LEEWAY_SECONDS = 30  # tolerate small clock differences between us and the provider


def _truthy(value: Any) -> bool:
    """Apple sends booleans as strings ("true"); Google sends real booleans."""
    return value is True or value == "true"


def _check_nonce(claims: dict[str, Any], nonce: str | None) -> None:
    if nonce is None:
        return
    claimed = str(claims.get("nonce") or "")
    hashed = hashlib.sha256(nonce.encode()).hexdigest()
    if not (hmac.compare_digest(claimed, nonce) or hmac.compare_digest(claimed, hashed)):
        raise SocialTokenError("Nonce does not match")


class LiveSocialVerifier:
    def __init__(
        self,
        settings: Settings,
        *,
        google_verify: Callable[[str], dict[str, Any]] | None = None,
        apple_jwks: Any | None = None,
    ) -> None:
        self._google_ids = settings.google_client_id_list
        self._apple_ids = settings.apple_client_id_list
        # Both can be swapped in tests so no network call is needed.
        self._google_verify = google_verify or self._default_google_verify
        self._apple_jwks = apple_jwks
        self._google_request: Any = None

    # --- dispatch ---------------------------------------------------------------------
    def verify(self, provider: str, id_token: str, nonce: str | None = None) -> SocialIdentity:
        if provider == "google":
            return self._verify_google(id_token, nonce)
        if provider == "apple":
            return self._verify_apple(id_token, nonce)
        raise SocialTokenError(f"Unknown provider {provider!r}")

    # --- Google -----------------------------------------------------------------------
    def _default_google_verify(self, token: str) -> dict[str, Any]:
        """Signature, expiry and issuer checks via google-auth (keys are cached by it)."""
        from google.auth import exceptions as google_exceptions  # noqa: PLC0415
        from google.auth.transport import requests as google_requests  # noqa: PLC0415
        from google.oauth2 import id_token as google_id_token  # noqa: PLC0415

        if self._google_request is None:
            self._google_request = google_requests.Request()
        try:
            return google_id_token.verify_oauth2_token(
                token, self._google_request, audience=None, clock_skew_in_seconds=LEEWAY_SECONDS
            )
        except google_exceptions.TransportError as exc:
            raise IntegrationError(f"Could not reach Google: {exc}") from exc
        except (ValueError, google_exceptions.GoogleAuthError) as exc:
            raise SocialTokenError(f"Invalid Google token: {exc}") from exc

    def _verify_google(self, token: str, nonce: str | None) -> SocialIdentity:
        if not self._google_ids:
            raise IntegrationError("Google sign-in is not configured (GOOGLE_CLIENT_IDS)")
        claims = self._google_verify(token)
        # Checked here rather than by google-auth so several client ids (iOS, Android, web)
        # can be accepted.
        if claims.get("aud") not in self._google_ids:
            raise SocialTokenError("Google token was issued to another app")
        _check_nonce(claims, nonce)
        return SocialIdentity(
            provider="google",
            subject=str(claims["sub"]),
            email=(claims.get("email") or "").lower() or None,
            email_verified=_truthy(claims.get("email_verified")),
            name=claims.get("name"),
        )

    # --- Apple ------------------------------------------------------------------------
    def _verify_apple(self, token: str, nonce: str | None) -> SocialIdentity:
        if not self._apple_ids:
            raise IntegrationError("Apple sign-in is not configured (APPLE_CLIENT_IDS)")
        if self._apple_jwks is None:
            self._apple_jwks = jwt.PyJWKClient(APPLE_KEYS_URL, cache_keys=True, lifespan=3600)
        try:
            key = self._apple_jwks.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientConnectionError as exc:
            raise IntegrationError(f"Could not reach Apple: {exc}") from exc
        except jwt.PyJWTError as exc:
            raise SocialTokenError(f"Invalid Apple token: {exc}") from exc
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self._apple_ids,
                issuer=APPLE_ISSUER,
                leeway=LEEWAY_SECONDS,
                options={"require": ["sub", "exp", "iat", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise SocialTokenError(f"Invalid Apple token: {exc}") from exc
        _check_nonce(claims, nonce)
        # Apple sends the person's name to the app only, on the first sign-in; the app
        # passes it to us in the request body.
        return SocialIdentity(
            provider="apple",
            subject=str(claims["sub"]),
            email=(claims.get("email") or "").lower() or None,
            email_verified=_truthy(claims.get("email_verified")),
        )
