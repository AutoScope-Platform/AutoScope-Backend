"""Background jobs (Celery).

Every job is a thin wrapper: open a session, call ONE service function, commit. The business
rules live in the service modules so tests call them directly without Celery or Redis.

Run locally:
    make worker      # runs the worker and the beat scheduler together

Schedule (UTC unless noted):
    every minute   dispatch retry for haulage jobs, scheduled announcements
    every 10 min   escrow auto-release, auto-confirm shipped orders
    daily 06:00    schedule and pay out wallets (06:00 UTC = 07:00 Lagos), policy lifecycle
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Callable
from typing import Any

from celery import Celery
from celery.schedules import crontab

from app.core import db as db_module
from app.core.config import get_settings

log = logging.getLogger(__name__)


def broker_ssl_options(url: str | None) -> dict | None:
    """TLS settings for a `rediss://` broker (Azure Cache for Redis only accepts TLS).
    Celery refuses a rediss:// URL unless certificate checking is set explicitly."""
    if url and url.startswith("rediss://"):
        return {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    return None


_broker_url = get_settings().redis_url or "memory://"
celery_app = Celery("autoscope", broker=_broker_url, backend=None)
celery_app.conf.broker_use_ssl = broker_ssl_options(_broker_url)
celery_app.conf.timezone = "UTC"
celery_app.conf.task_acks_late = (
    True  # a crashed worker's job is retried, so jobs must be idempotent
)


def _run(job: Callable[[Any], Any]) -> Any:
    """Run `job(db)` in its own session and commit. One failing job never blocks the others."""
    with db_module.new_session() as db:
        try:
            result = job(db)
            db.commit()
            return result
        except Exception:
            db.rollback()
            log.exception("Background job failed")
            raise


@celery_app.task(name="notifications.send")
def send_notification(payload: dict) -> None:
    """Push/SMS/e-mail for one notice, queued by `notifications.service.dispatch` so the
    request that caused it does not wait for SMS or e-mail providers (FSD 7.2)."""
    from app.modules.notifications import service  # noqa: PLC0415

    service.send_now(payload)


@celery_app.task(name="haulage.redispatch")
def redispatch_haulage() -> int:
    from app.modules.haulage import service  # noqa: PLC0415

    return _run(service.redispatch_searching)


@celery_app.task(name="notifications.send_due")
def send_due_announcements() -> int:
    from app.modules.notifications import service  # noqa: PLC0415

    return _run(service.send_due_announcements)


@celery_app.task(name="payments.release_escrow")
def release_escrow() -> int:
    from app.modules.payments import service  # noqa: PLC0415

    return _run(service.release_due_escrow)


@celery_app.task(name="orders.auto_confirm")
def auto_confirm_orders() -> int:
    from app.modules.orders import service  # noqa: PLC0415

    return _run(service.auto_confirm_shipped)


@celery_app.task(name="payments.payouts")
def run_payouts() -> dict[str, int]:
    """Schedule payouts from wallet balances, then pay everything that is due."""
    from app.modules.payments import service  # noqa: PLC0415

    scheduled = _run(service.schedule_payouts)
    paid = _run(service.run_due_payouts)
    return {"scheduled": scheduled, "paid": paid}


@celery_app.task(name="insurance.lifecycle")
def policy_lifecycle() -> dict[str, int]:
    from app.modules.insurance import service  # noqa: PLC0415

    return _run(service.run_policy_lifecycle)


celery_app.conf.beat_schedule = {
    "haulage-redispatch": {"task": "haulage.redispatch", "schedule": 60.0},
    "announcements": {"task": "notifications.send_due", "schedule": 60.0},
    "escrow-release": {"task": "payments.release_escrow", "schedule": 600.0},
    "orders-auto-confirm": {"task": "orders.auto_confirm", "schedule": 600.0},
    "payouts": {"task": "payments.payouts", "schedule": crontab(hour=6, minute=0)},
    "policy-lifecycle": {"task": "insurance.lifecycle", "schedule": crontab(hour=6, minute=15)},
}
