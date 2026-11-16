"""The digitalizer's Celery tasks: orchestration (``digitalizer.*``) and in-process stages.

Orchestration lives in :mod:`studio.orchestration.ledger`; these are thin shells.

* ``digitalizer.run``            start a queued run: plan its first stage, then pump
* ``digitalizer.pump``           admit planned jobs (beat, and after every state change)
* ``digitalizer.job_done``       ``link`` callback of every job: settle, plan the next stage
* ``digitalizer.job_failed``     ``link_error`` callback (``bind=True``, see transport)
* ``digitalizer.reconcile``      beat: lost outcomes, dead workers, deadlines
* ``digitalizer.local.<stage>``  a stage body run in this application, as a ledger job;
                                 a service-backed stage sends the service's task instead

A local stage task starts only if its token is the job's current one (a redelivered or
superseded message does nothing), beats while its body runs (``reconcile`` reads the
heartbeat), records its error on the job before raising (the errback reads it) and
returns a compact summary (the ``link`` callback's argument).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.exc import OperationalError

from evilflowers_books_digitalizer.studio.orchestration import appliers, ledger
from evilflowers_books_digitalizer.studio.telemetry import bind
from evilflowers_books_digitalizer.studio.orchestration.transport import CeleryTransport
from evilflowers_books_digitalizer.studio.workers import context
from evilflowers_books_digitalizer.studio.workers.app import celery
from evilflowers_books_digitalizer.studio.workers.context import WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages import (
    analyze as analyze_stage,
)
from evilflowers_books_digitalizer.studio.workers.stages import (
    clean as clean_stage,
)
from evilflowers_books_digitalizer.studio.workers.stages import (
    discover as discover_stage,
)
from evilflowers_books_digitalizer.studio.workers.stages import (
    export as export_stage,
)
from evilflowers_books_digitalizer.studio.workers.stages import (
    formulas as formulas_stage,
)
from evilflowers_books_digitalizer.studio.workers.stages import (
    gate as gate_stage,
)
from evilflowers_books_digitalizer.studio.workers.stages import (
    publish as publish_stage,
)
from evilflowers_books_digitalizer.studio.workers.stages import (
    render as render_stage,
)

#: worth a retry (DB failover, broker/network blips); everything else fails the job
TRANSIENT: tuple[type[BaseException], ...] = (OperationalError, ConnectionError, TimeoutError)
MAX_RETRIES = 3
#: how often a running local job bumps its heartbeat
HEARTBEAT_SECONDS = 60.0

logger = logging.getLogger(__name__)
transport = CeleryTransport(celery)
appliers.install()

type Body = Callable[[WorkerContext, ledger.LocalJob], list[int] | None]


def _after(ctx: WorkerContext, settled: ledger.Settled | None = None) -> None:
    """After a commit that changed the ledger: start follow-up runs, revoke, pump."""
    if settled is not None:
        for run_id in settled.follow_ups:
            transport.start_run(run_id)
        if settled.revoke:
            transport.revoke(settled.revoke)
    ledger.pump(ctx, transport, ctx.admission)  # type: ignore[arg-type]


# ---------------------------------------------------------------- orchestration
@celery.task(name="digitalizer.run", bind=True, max_retries=MAX_RETRIES)
def run(self: Any, run_id: int) -> dict[str, Any]:
    ctx = context.current()
    with bind(run_id=run_id):
        try:
            state = ledger.start_run(ctx, run_id)
        except TRANSIENT as exc:
            raise self.retry(exc=exc, countdown=5 * 2 ** self.request.retries) from exc
        except Exception as exc:
            from evilflowers_books_digitalizer.studio.workers import runs

            runs.fail(ctx, run_id, exc)
            raise
        if state == "started":
            _after(ctx)
    return {"run_id": run_id, "state": state}


@celery.task(name="digitalizer.pump")
def pump() -> dict[str, Any]:
    ctx = context.current()
    return ledger.pump(ctx, transport, ctx.admission)  # type: ignore[arg-type]


@celery.task(name="digitalizer.job_done", bind=True, max_retries=MAX_RETRIES)
def job_done(self: Any, summary: Any, job_id: int, task_id: str | None = None
             ) -> dict[str, Any]:
    ctx = context.current()
    with bind(job_id=job_id, task_id=task_id):
        try:
            settled = ledger.settle(ctx, job_id, task_id,
                                    summary if isinstance(summary, dict) else {})
        except TRANSIENT as exc:
            raise self.retry(exc=exc, countdown=5 * 2 ** self.request.retries) from exc
        _after(ctx, settled)
    return {"job_id": job_id, "applied": settled.applied, "run_finished": settled.run_finished}


@celery.task(name="digitalizer.job_failed", bind=True, max_retries=MAX_RETRIES)
def job_failed(self: Any, failed_task_id: str, job_id: int) -> dict[str, Any]:
    """Errback: ``bind=True`` makes Celery apply it as a task with the failed task id."""
    ctx = context.current()
    with bind(job_id=job_id, task_id=failed_task_id):
        try:
            revoke = ledger.fail(ctx, job_id, failed_task_id)
        except TRANSIENT as exc:
            raise self.retry(exc=exc, countdown=5 * 2 ** self.request.retries) from exc
        _after(ctx, ledger.Settled(revoke=revoke))
    return {"job_id": job_id, "failed_task_id": failed_task_id}


@celery.task(name="digitalizer.reconcile")
def reconcile() -> dict[str, Any]:
    ctx = context.current()
    report = ledger.reconcile(ctx, transport)
    _after(ctx, ledger.Settled(follow_ups=report.get("follow_ups", [])))
    return report


@celery.task(name="digitalizer.discover", bind=True, max_retries=MAX_RETRIES)
def discover(self: Any, project: str, collection: str | None = None) -> dict[str, Any]:
    return discover_stage.discover(context.current(), project, collection)


@celery.task(name="digitalizer.export_training", bind=True, max_retries=MAX_RETRIES)
def export_training(self: Any, project: str) -> str:
    path: Path = export_stage.export_training(context.current(), project)
    return str(path)


# ---------------------------------------------------------------- local stage jobs
@contextmanager
def _heartbeat(ctx: WorkerContext, job_id: int, token: str,
               every: float | None = None) -> Iterator[None]:
    """Bump ``jobs.heartbeat_at`` while the body runs; ``reconcile`` reads silence as loss."""
    interval = HEARTBEAT_SECONDS if every is None else every
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                ledger.beat(ctx, job_id, token)
            except Exception as exc:  # noqa: BLE001 - a missed beat must not kill the job
                logger.warning("heartbeat of job %s failed: %s", job_id, exc)

    ticker = threading.Thread(target=beat, name=f"heartbeat-job-{job_id}", daemon=True)
    ticker.start()
    try:
        yield
    finally:
        stop.set()
        ticker.join(timeout=5)


def _local(task: Any, job_id: int, token: str, body: Body) -> dict[str, Any]:
    ctx = context.current()
    job = ledger.begin(ctx, job_id, token)
    if job is None:                 # superseded attempt, revoked job, ended run
        return {"job_id": job_id, "skipped": True}
    with bind(run_id=job.run_id, job_id=job.id, stage=job.stage):
        try:
            with _heartbeat(ctx, job.id, token):
                follow_ups = body(ctx, job) or []
        except TRANSIENT as exc:
            if task.request.retries < MAX_RETRIES:
                raise task.retry(exc=exc, countdown=5 * 2 ** task.request.retries) from exc
            ledger.record_error(ctx, job.id, token, exc)
            raise
        except Exception as exc:
            ledger.record_error(ctx, job.id, token, exc)
            raise
    return {"job_id": job.id, "stage": job.stage, "follow_ups": follow_ups}


@celery.task(name="digitalizer.local.clean", bind=True, max_retries=MAX_RETRIES)
def clean(self: Any, job_id: int, token: str, pages: list[int], **_: Any) -> dict[str, Any]:
    return _local(self, job_id, token,
                  lambda ctx, job: clean_stage.clean(ctx, job.run_id, pages))


@celery.task(name="digitalizer.local.analyze", bind=True, max_retries=MAX_RETRIES)
def analyze(self: Any, job_id: int, token: str, page: int, **_: Any) -> dict[str, Any]:
    return _local(self, job_id, token,
                  lambda ctx, job: analyze_stage.analyze(ctx, job.run_id, page))


@celery.task(name="digitalizer.local.formulas", bind=True, max_retries=MAX_RETRIES)
def formulas(self: Any, job_id: int, token: str, **_: Any) -> dict[str, Any]:
    return _local(self, job_id, token,
                  lambda ctx, job: formulas_stage.formulas(ctx, job.run_id))


@celery.task(name="digitalizer.local.render", bind=True, max_retries=MAX_RETRIES)
def render(self: Any, job_id: int, token: str, **_: Any) -> dict[str, Any]:
    return _local(self, job_id, token,
                  lambda ctx, job: render_stage.render(ctx, job.run_id))


@celery.task(name="digitalizer.local.gate", bind=True, max_retries=MAX_RETRIES)
def gate(self: Any, job_id: int, token: str, **_: Any) -> dict[str, Any]:
    return _local(self, job_id, token, lambda ctx, job: gate_stage.gate(ctx, job.run_id))


@celery.task(name="digitalizer.local.publish", bind=True, max_retries=MAX_RETRIES)
def publish(self: Any, job_id: int, token: str, **_: Any) -> dict[str, Any]:
    return _local(self, job_id, token,
                  lambda ctx, job: publish_stage.publish(ctx, job.run_id))
