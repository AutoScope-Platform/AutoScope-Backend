"""AutoScope API entry point.

Run locally with ``make run`` (or ``uvicorn app.main:app --reload``). Interactive
docs are served at ``/docs`` (Swagger UI) and ``/redoc``; the machine-readable
schema is at ``/openapi.json``.

Adding a module: create ``app/modules/<name>/`` following the identity module's
layout, then add its router to `ROUTERS` below.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core import files
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.modules.identity.router import router as identity_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DESCRIPTION = """
Backend for **AutoScope**, a multi-sided marketplace for vehicles, parts, repairs,
haulage and insurance.

* All endpoints live under `/api/v1`. Send `Authorization: Bearer <access token>`
  on every route except sign-up and sign-in.
* Money is always an integer in minor units (kobo for NGN), with a `currency` code.
* Errors share one shape: `{"error": {"code", "message", "details"}}`.
* Lists use cursor pagination (`cursor`, `limit`); admin tables use `page`/`page_size`.
* Writes that move money need an `Idempotency-Key` header so retries never charge twice.
"""

TAGS = [
    {"name": "Identity", "description": "Sign-up, sign-in, profile, roles and KYC upload."},
]

# Every module's router is registered here.
ROUTERS: list[APIRouter] = [identity_router]


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=DESCRIPTION,
        openapi_tags=TAGS,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router, prefix=settings.api_prefix)
    app.include_router(files.router, prefix=settings.api_prefix)

    @app.get("/health", tags=["Health"], summary="Liveness check")
    def health() -> dict[str, str]:
        """Used by Render and the load balancer. Does not touch the database."""
        return {"status": "ok"}

    return app


app = create_app()
