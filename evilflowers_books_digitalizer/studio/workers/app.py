"""Celery application of the studio: ``evilflowers_books_digitalizer.studio.workers.app:celery``.

Redis is broker and result backend (``StudioConfig.redis_url``). Four queues,
served by three pools (docs/design/studio-spec.md, "Queues and tasks")::

    celery -A evilflowers_books_digitalizer.studio.workers.app worker -Q interactive,cpu -c 4
    celery -A evilflowers_books_digitalizer.studio.workers.app worker -Q ai -c 1
    celery -A evilflowers_books_digitalizer.studio.workers.app worker -Q io -B

Interactive work (an operator's re-run, its layout pass included) is served by
the cpu pool only, as in the spec and deploy/k8s/base/workers.yaml.

``acks_late`` + prefetch 1: a page job is acknowledged only when it is done, and a
worker never hoards long jobs while an operator's interactive re-run waits. The
Redis ``visibility_timeout`` (``adapters.queue.TRANSPORT_OPTIONS``) is set far above
the longest single task, so a slow whole-book stage is not redelivered mid-flight.
``STUDIO_EAGER=1`` runs every task inline (tests, single-process debugging) on an
in-memory broker, so no Redis is needed.

Importing this module reads no database and builds no adapter; the config is read
only for the broker URL (and tolerated missing).
"""

from __future__ import annotations

import logging
import os

from celery import Celery
from kombu import Exchange, Queue

from evilflowers_books_digitalizer.studio.adapters.queue import (
    AI,
    CPU,
    DEFAULT_PRIORITY,
    INTERACTIVE,
    IO,
    TRANSPORT_OPTIONS,
)

logger = logging.getLogger(__name__)

EAGER_ENV = "STUDIO_EAGER"
DEFAULT_REDIS = "redis://127.0.0.1:6379/0"
TASKS_MODULE = "evilflowers_books_digitalizer.studio.workers.tasks"

#: default queue per task; studio.run picks interactive variants per run at dispatch time
ROUTES: dict[str, dict[str, str]] = {
    "studio.discover": {"queue": IO},
    "studio.run": {"queue": IO},
    "studio.clean": {"queue": CPU},
    "studio.analyze": {"queue": AI},
    "studio.formulas": {"queue": AI},
    "studio.render": {"queue": CPU},
    "studio.gate": {"queue": IO},
    "studio.publish": {"queue": IO},
    "studio.export_training": {"queue": IO},
    "studio.recover_runs": {"queue": IO},
}

#: periodic tasks; beat runs embedded in the io worker (``worker -Q io -B``)
BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    # stale-run sweeper (workers.recovery): runs left queued/running by a lost worker
    "studio.recover_runs": {
        "task": "studio.recover_runs",
        "schedule": float(os.environ.get("STUDIO_RECOVER_INTERVAL_SECONDS", "300")),
        "options": {"queue": IO, "expires": 240},
    },
}


def eager() -> bool:
    return os.environ.get(EAGER_ENV) == "1"


def broker_url() -> str:
    """``$STUDIO_REDIS_URL`` > the studio config > localhost (memory broker when eager)."""
    if eager():
        return "memory://"
    if url := os.environ.get("STUDIO_REDIS_URL"):
        return url
    try:
        from evilflowers_books_digitalizer.studio.config import load_config

        return load_config().redis_url
    except (OSError, ValueError) as exc:
        logger.warning("studio config unavailable (%s); broker %s", exc, DEFAULT_REDIS)
        return DEFAULT_REDIS


def make_app() -> Celery:
    broker = broker_url()
    app = Celery("studio", broker=broker, backend="cache+memory://" if eager() else broker,
                 include=[TASKS_MODULE])
    app.conf.update(
        # one exchange + routing key per queue: a bare Queue(name) is bound to the
        # *default* exchange with the default key, so all four queues would share
        # key "io" and Redis would deliver every message to every queue
        task_queues=[Queue(name, Exchange(name, type="direct"), routing_key=name)
                     for name in (INTERACTIVE, CPU, AI, IO)],
        task_default_queue=IO,
        task_routes=ROUTES,
        beat_schedule=BEAT_SCHEDULE,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        result_expires=24 * 3600,
        # Redis emulates priorities with per-priority lists polled in ascending
        # order: 0 = operator re-runs (INTERACTIVE_PRIORITY), 5 = batch work
        task_queue_max_priority=10,
        task_default_priority=DEFAULT_PRIORITY,
        broker_transport_options=dict(TRANSPORT_OPTIONS),
        worker_hijack_root_logger=False,
        task_always_eager=eager(),
        task_eager_propagates=eager(),
    )
    return app


celery = make_app()
