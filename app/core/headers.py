"""Security headers on every HTTP response.

Set by the app itself, not only by the proxy in front of it, so they are the same in staging
(Caddy) and production (Azure Container Apps, which adds none). Written as plain ASGI
middleware so WebSockets and streamed responses pass through untouched.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The API answers JSON (and file downloads): nothing in a response may run, load or be framed.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

# Swagger UI and ReDoc load their code from the jsDelivr CDN and start with an inline script.
DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "worker-src blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
DOCS_PATHS = ("/docs", "/redoc")

COMMON = [
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


def security_headers(path: str) -> list[tuple[bytes, bytes]]:
    """The security headers for a response to `path`."""
    is_docs = path.startswith(DOCS_PATHS)
    # Signed file links are shown as images by the admin dashboard, which runs on another
    # site; everything else is read only by our own apps through CORS.
    is_file = "/files/" in path
    return [
        *COMMON,
        (b"content-security-policy", (DOCS_CSP if is_docs else API_CSP).encode()),
        (b"cross-origin-resource-policy", b"cross-origin" if is_file else b"same-origin"),
    ]


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = security_headers(scope["path"])

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [(k, v) for k, v in extra if k not in present]
                # Answers carry personal data (profiles, orders, KYC files): no cache may
                # keep a copy unless a route says otherwise.
                if b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
