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
from fastapi.responses import JSONResponse

from app.core import files
from app.core.concurrency import ConcurrencyLimitMiddleware, concurrency_limit
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.headers import SecurityHeadersMiddleware
from app.modules.admin.router import router as admin_router
from app.modules.booking.router import router as booking_router
from app.modules.catalog.router import router as catalog_router
from app.modules.haulage.router import router as haulage_router
from app.modules.identity.router import router as identity_router
from app.modules.insurance.router import router as insurance_router
from app.modules.messaging.router import router as messaging_router
from app.modules.notifications.router import router as notifications_router
from app.modules.orders.router import router as orders_router
from app.modules.payments.router import router as payments_router
from app.modules.reviews.router import router as reviews_router

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
    {
        "name": "Payments",
        "description": "Paying, escrow, wallet, payouts, disputes and finance settings.",
    },
]

# Every module's router is registered here.
ROUTERS: list[APIRouter] = [
    identity_router,
    admin_router,
    catalog_router,
    booking_router,
    haulage_router,
    insurance_router,
    messaging_router,
    notifications_router,
    orders_router,
    payments_router,
    reviews_router,
]


_monitoring_started = False


def setup_monitoring(settings) -> bool:
    """Send requests, errors and logs to Azure Monitor (Application Insights) when its
    connection string is set. Called once per process, before the app is built."""
    global _monitoring_started
    if _monitoring_started or not settings.applicationinsights_connection_string:
        return False
    from azure.monitor.opentelemetry import configure_azure_monitor  # noqa: PLC0415

    configure_azure_monitor(connection_string=settings.applicationinsights_connection_string)
    _monitoring_started = True
    return True


def create_app() -> FastAPI:
    settings = get_settings()
    setup_monitoring(settings)
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=DESCRIPTION,
        openapi_tags=TAGS,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    # Innermost, so a "busy" answer still gets CORS and the security headers.
    app.add_middleware(
        ConcurrencyLimitMiddleware,
        limit=concurrency_limit(
            settings.db_pool_size, settings.db_max_overflow, settings.max_concurrent_requests
        ),
        queue_seconds=settings.request_queue_seconds,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last, so it is the outermost layer: CORS answers and error pages get the headers too.
    app.add_middleware(SecurityHeadersMiddleware)
    register_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router, prefix=settings.api_prefix)
    app.include_router(files.router, prefix=settings.api_prefix)

    @app.get("/health", tags=["Health"], summary="Liveness check")
    async def health() -> dict[str, str]:
        """Liveness: the process is up. Does not touch the database, so a database outage
        does not make the platform restart every instance. Async, so it answers even when
        every worker thread is busy."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"], summary="Readiness check")
    def ready() -> JSONResponse:
        """Readiness: can this instance serve requests (database, and Redis when set)?
        Load balancers stop sending traffic to an instance that answers 503."""
        checks = readiness_checks()
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "unavailable", "checks": checks},
        )

    return app


app = create_app()


def readiness_checks() -> dict[str, str]:
    """`ok` or a short error per dependency (no secrets in the output)."""
    from sqlalchemy import text  # noqa: PLC0415

    from app.core import db as db_module  # noqa: PLC0415

    checks: dict[str, str] = {}
    try:
        with db_module.new_session() as session:
            session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {type(exc).__name__}"
    url = get_settings().redis_url
    if url:
        try:
            import redis  # noqa: PLC0415

            redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2).ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {type(exc).__name__}"
    return checks
