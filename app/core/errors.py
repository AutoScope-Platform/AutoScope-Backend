"""One error shape for the whole API.

Every failure the apps can see looks like this::

    {"error": {"code": "listing_not_found", "message": "Listing not found", "details": null}}

* `code` is stable and machine-readable (the mobile apps switch on it).
* `message` is safe to show to a user.
* `details` carries field-level information for validation errors.

Raise one of the `AppError` subclasses from a service; the handlers registered in
`register_error_handlers` turn it into the JSON above. Never raise `HTTPException`
from a service.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    """OpenAPI model for error responses (referenced by the route `responses=`)."""

    error: ErrorBody


class AppError(Exception):
    """Base class for expected, user-facing errors."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details: Any | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details


class BadRequest(AppError):
    status_code = 400
    code = "bad_request"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class UnprocessableEntity(AppError):
    """The request is well-formed but breaks a business rule (e.g. invalid state change)."""

    status_code = 422
    code = "unprocessable"


class TooManyRequests(AppError):
    status_code = 429
    code = "rate_limited"


class ServiceUnavailable(AppError):
    """An outside service we depend on (e.g. Google sign-in) cannot be reached right now."""

    status_code = 503
    code = "service_unavailable"


def _payload(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def register_error_handlers(app: FastAPI) -> None:
    """Attach handlers so every error uses the shared shape."""

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=_payload(exc.code, exc.message, exc.details)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Trim pydantic's verbose output to what a client needs to fix the request.
        fields = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_payload("validation_error", "The request is invalid", fields),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(code.get(exc.status_code, "http_error"), str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Log the traceback, but never leak internals to the client.
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500, content=_payload("internal_error", "Something went wrong")
        )


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Swagger helper: `responses=error_responses(401, 404)` documents the shared error shape."""
    descriptions = {
        400: "Bad request",
        401: "Not signed in or token invalid",
        403: "Not allowed",
        404: "Not found",
        409: "Conflict with current state",
        422: "Validation or business-rule error",
        429: "Too many requests",
        503: "An outside service is unavailable; try again shortly",
    }
    return {c: {"model": ErrorResponse, "description": descriptions.get(c, "Error")} for c in codes}
