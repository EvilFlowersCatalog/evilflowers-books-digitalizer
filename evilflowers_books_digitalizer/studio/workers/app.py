"""Celery application of the digitalizer: ``evilflowers_books_digitalizer.studio.workers.app:celery``.

Queues (``studio.config``) and who consumes them::

    digitalizer.portal       orchestrator: runs, job_done/job_failed callbacks, pump,
                             reconcile, discover, gate, publish   (+ beat, one replica)
    digitalizer.cpu          in-process clean / render            (until scan/render services)
    digitalizer.ai           in-process layout / formulas         (until analyzer/equation tasks)
    digitalizer.interactive  in-process operator re-runs          (one replica, always on)

    celery -A evilflowers_books_digitalizer.studio.workers.app worker -Q digitalizer.portal -B
    celery -A evilflowers_books_digitalizer.studio.workers.app worker -Q digitalizer.cpu
    celery -A evilflowers_books_digitalizer.studio.workers.app worker -Q digitalizer.ai -c 1
    celery -A evilflowers_books_digitalizer.studio.workers.app worker -Q digitalizer.interactive

No result backend: a job's result returns as a ``link`` callback (studio.orchestration).
``acks_late`` + ``reject_on_worker_lost`` + prefetch 1: a job is acknowledged only when
done, a lost worker's job is redelivered (and fenced by its token), and a worker never
hoards jobs. ``STUDIO_EAGER=1`` runs every task inline on an in-memory broker (tests).

Importing this module reads no database and builds no adapter; the config is read
only for the broker URL (and tolerated missing).
"""

from __future__ import annotations

import logging
import os

from celery import Celery
from celery.signals import worker_process_init
from kombu import Exchange, Queue

from evilflowers_books_digitalizer.studio.adapters.queue import QUEUES, TRANSPORT_OPTIONS
from evilflowers_books_digitalizer.studio.config import AI_QUEUE, CPU_QUEUE, PORTAL_QUEUE

logger = logging.getLogger(__name__)

EAGER_ENV = "STUDIO_EAGER"
DEFAULT_REDIS = "redis://127.0.0.1:6379/0"
TASKS_MODULE = "evilflowers_books_digitalizer.studio.workers.tasks"

#: default queue per task (jobs are always sent with an explicit queue by the pump)
ROUTES: dict[str, dict[str, str]] = {
    "digitalizer.run": {"queue": PORTAL_QUEUE},
    "digitalizer.job_done": {"queue": PORTAL_QUEUE},
    "digitalizer.job_failed": {"queue": PORTAL_QUEUE},
    "digitalizer.pump": {"queue": PORTAL_QUEUE},
    "digitalizer.reconcile": {"queue": PORTAL_QUEUE},
    "digitalizer.discover": {"queue": PORTAL_QUEUE},
    "digitalizer.export_training": {"queue": PORTAL_QUEUE},
    "digitalizer.local.clean": {"queue": CPU_QUEUE},
    "digitalizer.local.analyze": {"queue": AI_QUEUE},
    "digitalizer.local.formulas": {"queue": AI_QUEUE},
    "digitalizer.local.render": {"queue": CPU_QUEUE},
    "digitalizer.local.gate": {"queue": PORTAL_QUEUE},
    "digitalizer.local.publish": {"queue": PORTAL_QUEUE},
}

#: periodic tasks; beat runs embedded in the single orchestrator replica (``-B``)
BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    # admission: send planned jobs as capacity frees up, start held / lost runs
    "digitalizer.pump": {
        "task": "digitalizer.pump",
        "schedule": float(os.environ.get("DIGITALIZER_PUMP_INTERVAL_SECONDS", "15")),
        "options": {"queue": PORTAL_QUEUE, "expires": 14},
    },
    # settle what callbacks missed: lost outcomes, dead workers, jobs past their deadline
    "digitalizer.reconcile": {
        "task": "digitalizer.reconcile",
        "schedule": float(os.environ.get("DIGITALIZER_RECONCILE_INTERVAL_SECONDS", "60")),
        "options": {"queue": PORTAL_QUEUE, "expires": 55},
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
    app = Celery("digitalizer", broker=broker_url(), include=[TASKS_MODULE])
    app.conf.update(
        # one direct exchange + routing key per queue (what send_task(queue=…) declares
        # from any producer, the kit's services included)
        task_queues=[Queue(name, Exchange(name, type="direct"), routing_key=name)
                     for name in QUEUES],
        task_default_queue=PORTAL_QUEUE,
        task_routes=ROUTES,
        beat_schedule=BEAT_SCHEDULE,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        broker_transport_options=dict(TRANSPORT_OPTIONS),
        broker_connection_retry_on_startup=True,
        # task events: Flower and the Celery exporter read them (observability)
        worker_send_task_events=True,
        task_send_sent_event=True,
        worker_hijack_root_logger=False,
        task_always_eager=eager(),
        # never propagate: Celery re-raises *before* calling errbacks, so link_error (and
        # with it digitalizer.job_failed) would never run inline; a job's failure is the
        # errback's business in production too
        task_eager_propagates=False,
    )
    return app


celery = make_app()


@worker_process_init.connect
def _telemetry(**_: object) -> None:
    """Tracing starts in each pool process (after the fork), never in the parent."""
    from evilflowers_books_digitalizer.logs import setup_logging
    from evilflowers_books_digitalizer.studio import telemetry

    setup_logging()
    telemetry.configure("digitalizer-worker")
