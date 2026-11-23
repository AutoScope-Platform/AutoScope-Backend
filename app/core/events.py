"""A tiny in-process event bus.

Modules never import each other's internals. When something notable happens (an
order is paid, a claim changes status) the owning service *publishes* an event
and other modules that care *subscribe*. This is how notifications, the activity
feed and review prompts react to changes without the original module knowing about them.

Rules:

* Publish only **after** the database commit, so subscribers never see data that
  could still be rolled back.
* Handlers must be idempotent and must not raise: a failing handler is logged and
  never breaks the request that published the event.
* Handlers get ids and plain values only, and open their own database session.

Production note: the spec calls for a queue (RabbitMQ/Celery) for slow work. This bus
is deliberately in-process so v1 stays simple; slow handlers can hand their work to
a Celery task (see `app/worker.py`) without changing publishers.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    """A thing that happened. `name` is dotted, e.g. ``order.paid``."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Event], None]
_handlers: dict[str, list[Handler]] = defaultdict(list)


def subscribe(event_name: str) -> Callable[[Handler], Handler]:
    """Decorator: run the function whenever `event_name` is published."""

    def register(handler: Handler) -> Handler:
        _handlers[event_name].append(handler)
        return handler

    return register


def publish(name: str, **payload: Any) -> None:
    """Deliver an event to every subscriber. Errors are logged, never raised."""
    event = Event(name, payload)
    for handler in list(_handlers.get(name, [])) + list(_handlers.get("*", [])):
        try:
            handler(event)
        except Exception:  # noqa: BLE001 - one bad subscriber must not break the others
            logger.exception("Event handler %s failed for %s", handler.__name__, name)


_AFTER_COMMIT_KEY = "events_after_commit"


def publish_after_commit(session, name: str, **payload: Any) -> None:
    """Publish `name` once `session` commits; drop it if the session rolls back.

    For code that cannot commit itself (e.g. inside `run_idempotent`), so a subscriber never
    sees data that is later rolled back.
    """
    from sqlalchemy import event as sa_event  # noqa: PLC0415

    # Make sure a transaction is open: rolling back a session that has not begun one does
    # nothing and fires no event, which would leave the queued event to a later commit.
    session.connection()
    queue = session.info.get(_AFTER_COMMIT_KEY)
    if queue is None:
        queue = session.info[_AFTER_COMMIT_KEY] = []

        def send(sess) -> None:
            for event_name, event_payload in sess.info.pop(_AFTER_COMMIT_KEY, []):
                publish(event_name, **event_payload)

        def drop(sess, _previous_transaction=None) -> None:  # (session, transaction)
            sess.info.pop(_AFTER_COMMIT_KEY, None)

        sa_event.listen(session, "after_commit", send, once=True)
        sa_event.listen(session, "after_soft_rollback", drop, once=True)
    queue.append((name, payload))
