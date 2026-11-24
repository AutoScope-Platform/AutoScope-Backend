"""Security headers (found missing by the OWASP ZAP scan of staging, Nov 2026)."""

from __future__ import annotations

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.headers import API_CSP, DOCS_CSP, SecurityHeadersMiddleware
from app.main import create_app

client = TestClient(create_app())


def test_api_answers_carry_the_security_headers():
    r = client.get("/health")
    assert r.status_code == 200
    h = r.headers
    assert h["content-security-policy"] == API_CSP
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert h["strict-transport-security"].startswith("max-age=")
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in h["permissions-policy"]
    assert h["cross-origin-opener-policy"] == "same-origin"
    assert h["cross-origin-resource-policy"] == "same-origin"
    assert h["cache-control"] == "no-store"


def test_error_answers_carry_them_too():
    r = client.get("/api/v1/no-such-route")
    assert r.status_code == 404
    assert r.headers["content-security-policy"] == API_CSP
    assert r.headers["cache-control"] == "no-store"


def test_docs_pages_get_a_policy_that_lets_swagger_and_redoc_load():
    for path in ("/docs", "/redoc"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-security-policy"] == DOCS_CSP
        assert r.headers["x-frame-options"] == "DENY"
    assert "https://cdn.jsdelivr.net" in DOCS_CSP


def test_file_links_can_be_shown_by_the_admin_dashboard_on_another_site():
    r = client.get("/api/v1/files/listings/x.png", params={"expires": 1, "sig": "x"})
    assert r.headers["cross-origin-resource-policy"] == "cross-origin"
    assert r.headers["cache-control"] == "no-store"


def test_cors_preflight_still_works_and_keeps_its_headers():
    origin = "https://autoscope-admin.netlify.app"
    app = create_app()
    if origin not in get_settings().cors_origin_list:
        return  # CORS origins come from the environment; nothing to check here
    r = TestClient(app).options(
        "/api/v1/auth/login",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    assert r.headers["x-frame-options"] == "DENY"


def test_a_route_can_still_choose_its_own_caching():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/cached")
    def cached(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "public, max-age=60"
        return {"ok": "yes"}

    r = TestClient(app).get("/cached")
    assert r.headers["cache-control"] == "public, max-age=60"
