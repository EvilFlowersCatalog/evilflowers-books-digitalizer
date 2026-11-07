"""The studio's Celery tasks (names and arguments per docs/design/studio-spec.md).

Thin shells: every body lives in :mod:`.stages`. The shell adds what is Celery's
business — the run guard (skip runs that are no longer active, mark the run
failed on an exception, retry only transient errors), the canvas ``studio.run``
dispatches, and enqueueing follow-up runs after the current one is committed.

``finish=True`` marks the last task of a run's canvas; it closes the run. Every
signature is immutable (``.si``) — tasks re-read state from the database instead
of passing results along.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from celery import chain, group
from celery.canvas import Signature
from sqlalchemy.exc import OperationalError

from evilflowers_books_digitalizer.db.models import Run
from evilflowers_books_digitalizer.studio.adapters.queue import (
    AI,
    CPU,
    INTERACTIVE,
    INTERACTIVE_PRIORITY,
    IO,
)
from evilflowers_books_digitalizer.studio.domain import RunStatus
from evilflowers_books_digitalizer.studio.workers import context, recovery, runs
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
from evilflowers_books_digitalizer.studio.workers.stages.orchestrate import (
    WorkPlan,
    blocked_by,
    orchestrate,
    work_plan,
)

#: worth a retry (DB failover, broker/network blips); everything else fails the run
TRANSIENT: tuple[type[BaseException], ...] = (OperationalError, ConnectionError, TimeoutError)
MAX_RETRIES = 3
#: how often a run held behind an earlier run of the same book looks again
WAIT_SECONDS = 15
#: how often a running task bumps its run's heartbeat
HEARTBEAT_SECONDS = 60.0

logger = logging.getLogger(__name__)

type Body[T] = Callable[[WorkerContext], T]


@contextmanager
def _heartbeat(ctx: WorkerContext, run_id: int, every: float | None = None) -> Iterator[None]:
    """Bump ``runs.heartbeat_at`` now, every ``every`` seconds while the body runs, and at
    the end — the stale-run sweeper (:mod:`.recovery`) reads it as "a worker is alive"."""
    interval = HEARTBEAT_SECONDS if every is None else every
    runs.touch(ctx, run_id)
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                runs.touch(ctx, run_id)
            except Exception as exc:  # noqa: BLE001 — a missed beat must not kill the task
                logger.warning("heartbeat of run %s failed: %s", run_id, exc)

    ticker = threading.Thread(target=beat, name=f"heartbeat-run-{run_id}", daemon=True)
    ticker.start()
    try:
        yield
    finally:
        stop.set()
        ticker.join(timeout=5)
        try:
            runs.touch(ctx, run_id)
        except Exception as exc:  # noqa: BLE001 — never mask the body's outcome
            logger.warning("heartbeat of run %s failed: %s", run_id, exc)


def _guarded[T](task: Any, run_id: int, body: Body[T]) -> T | None:
    """Run ``body`` for an active run; on failure record it on the run, then re-raise."""
    ctx = context.current()
    if not runs.is_active(ctx, run_id):
        return None
    try:
        with _heartbeat(ctx, run_id):
            return body(ctx)
    except TRANSIENT as exc:
        if task.request.retries < MAX_RETRIES:
            raise task.retry(exc=exc, countdown=5 * 2 ** task.request.retries) from exc
        runs.fail(ctx, run_id, exc)
        raise
    except Exception as exc:
        runs.fail(ctx, run_id, exc)
        raise


def _close(ctx: WorkerContext, run_id: int, follow_ups: list[int]) -> None:
    """Finish the run; if that fails, fail the follow-ups the body already committed.

    The stage body and the finish are two commits. A follow-up row left ``queued``
    with no task behind it would block every later batch run of the book (409), so
    it is failed here; a retry of the stage creates a fresh one (``gate`` looks
    for an active one first, so it never makes two).
    """
    try:
        runs.finish(ctx, run_id)
    except BaseException as exc:
        for follow_up in follow_ups:
            runs.fail(ctx, follow_up, RuntimeError(f"run {run_id} did not finish: {exc}"))
        raise


def _dispatch_follow_ups(ctx: WorkerContext, follow_ups: list[int]) -> None:
    """Enqueue follow-up runs; a broker failure fails that run instead of stranding it."""
    for follow_up in follow_ups:
        try:
            run.apply_async((follow_up,), queue=IO)
        except Exception as exc:  # noqa: BLE001 — any broker error means "not scheduled"
            runs.fail(ctx, follow_up, exc)


def _stage(task: Any, run_id: int, body: Body[list[int] | None], *, finish: bool
           ) -> dict[str, Any]:
    def step(ctx: WorkerContext) -> list[int]:
        follow_ups = body(ctx) or []
        if finish:
            _close(ctx, run_id, follow_ups)
        return follow_ups

    follow_ups = _guarded(task, run_id, step)
    if follow_ups:                          # after the commit that finished this run
        _dispatch_follow_ups(context.current(), follow_ups)
    return {"run_id": run_id, "skipped": follow_ups is None, "follow_ups": follow_ups or []}


# ---------------------------------------------------------------- canvas
def _page_stage(stage: str, plan: WorkPlan, finish: bool) -> Signature | None:
    if not plan.pages:
        return None
    # interactive children carry the urgent priority too, or they would queue
    # behind batch work of the same pool once studio.run has dispatched them
    options: dict[str, Any] = ({"queue": INTERACTIVE, "priority": INTERACTIVE_PRIORITY}
                               if plan.interactive else {})
    if stage == "clean":
        return group(clean.si(plan.run_id, chunk, finish=finish).set(**(options or {"queue": CPU}))
                     for chunk in plan.chunks())
    return group(analyze.si(plan.run_id, index, finish=finish).set(**(options or {"queue": AI}))
                 for index in plan.pages)


def canvas(plan: WorkPlan) -> Signature:
    """``WorkPlan`` -> chain of (groups of) immutable signatures, last one closes the run."""
    single = {"formulas": (formulas, AI), "render": (render, CPU), "gate": (gate, IO),
              "publish": (publish, IO)}
    steps: list[Signature] = []
    for i, stage in enumerate(plan.stages):
        finish = i == len(plan.stages) - 1
        if stage in single:
            task, queue = single[stage]
            steps.append(task.si(plan.run_id, finish=finish).set(queue=queue))
        elif (sig := _page_stage(stage, plan, finish)) is not None:
            steps.append(sig)
    return chain(*steps)


# ---------------------------------------------------------------- tasks
@celery.task(name="studio.discover", bind=True, max_retries=MAX_RETRIES)
def discover(self: Any, project: str, collection: str | None = None) -> dict[str, Any]:
    return discover_stage.discover(context.current(), project, collection)


def _hold(task: Any, run_id: int, waiting_for: int) -> dict[str, Any]:
    """Look again in ``WAIT_SECONDS`` by sending a *new* message, not ``self.retry``.

    ``retry(max_retries=None)`` falls back to the task's own ``max_retries`` and
    counts every wait, so a render longer than 3 x 15 s would end the wait with an
    exception outside the run guard and strand the run as ``queued``. A fresh
    message starts with ``retries == 0``: waits are unlimited and the transient
    retry budget stays untouched. Eager mode runs everything inline, so nothing
    could ever finish the earlier run: the hold fails the run instead of recursing.
    """
    ctx = context.current()
    if celery.conf.task_always_eager:
        runs.fail(ctx, run_id, RuntimeError(f"run {run_id} waits for run {waiting_for}, "
                                            "which cannot progress in eager mode"))
        return {"run_id": run_id, "skipped": True, "waiting_for": waiting_for}
    queue = (task.request.delivery_info or {}).get("routing_key") or IO
    run.apply_async((run_id,), queue=queue, countdown=WAIT_SECONDS)
    runs.touch(ctx, run_id)                 # held, not lost
    return {"run_id": run_id, "skipped": True, "waiting_for": waiting_for}


def _send(ctx: WorkerContext, plan: WorkPlan) -> None:
    """Send the canvas of a claimed run; a broker failure fails the run."""
    try:
        canvas(plan).apply_async()
    except Exception as exc:
        runs.fail(ctx, plan.run_id, exc)
        raise


def _dispatch(ctx: WorkerContext, plan: WorkPlan) -> bool:
    """Claim the run's dispatch (``runs.dispatched_at``) and send its canvas.

    False when another sender (a redelivered ``studio.run``, the stale-run
    sweeper) already claimed it: one canvas per run, never two.
    """
    if not runs.claim_dispatch(ctx, plan.run_id):
        return False
    _send(ctx, plan)
    return True


def _undispatched(ctx: WorkerContext, run_id: int) -> WorkPlan | None:
    """The plan of a run orchestrate started but whose canvas nobody sent (a worker
    lost between the two commits; this message is the redelivery), else None."""
    with ctx.uow() as uow:
        r = uow.session.get(Run, run_id)
        if (r is None or r.status != RunStatus.RUNNING or r.dispatched_at is not None
                or r.stage is not None or r.heartbeat_at is None):
            return None
        return work_plan(ctx, r)


@celery.task(name="studio.run", bind=True, max_retries=MAX_RETRIES)
def run(self: Any, run_id: int) -> dict[str, Any]:
    ctx = context.current()
    if runs.is_active(ctx, run_id) and (waiting_for := blocked_by(ctx, run_id)) is not None:
        return _hold(self, run_id, waiting_for)
    plan = _guarded(self, run_id, lambda c: orchestrate(c, run_id))
    if plan is None and (plan := _undispatched(ctx, run_id)) is None:
        return {"run_id": run_id, "skipped": True}
    if not _dispatch(ctx, plan):
        return {"run_id": run_id, "skipped": True}
    return {"run_id": run_id, "stages": list(plan.stages), "pages": len(plan.pages)}


@celery.task(name="studio.recover_runs")
def recover_runs() -> dict[str, Any]:
    """Beat task (``app.BEAT_SCHEDULE``): settle runs no worker works on any more."""
    return recovery.sweep(
        context.current(), recovery.RedisBrokerProbe(celery, (INTERACTIVE, CPU, AI, IO)),
        dispatch=_send, enqueue=lambda run_id: run.apply_async((run_id,), queue=IO),
        policy=recovery.RecoveryPolicy.from_env()).as_dict()


@celery.task(name="studio.clean", bind=True, max_retries=MAX_RETRIES)
def clean(self: Any, run_id: int, page_indices: list[int], finish: bool = False
          ) -> dict[str, Any]:
    return _stage(self, run_id, lambda ctx: clean_stage.clean(ctx, run_id, page_indices),
                  finish=finish)


@celery.task(name="studio.analyze", bind=True, max_retries=MAX_RETRIES)
def analyze(self: Any, run_id: int, page_index: int, finish: bool = False) -> dict[str, Any]:
    return _stage(self, run_id, lambda ctx: analyze_stage.analyze(ctx, run_id, page_index),
                  finish=finish)


@celery.task(name="studio.formulas", bind=True, max_retries=MAX_RETRIES)
def formulas(self: Any, run_id: int, finish: bool = False) -> dict[str, Any]:
    return _stage(self, run_id, lambda ctx: formulas_stage.formulas(ctx, run_id), finish=finish)


@celery.task(name="studio.render", bind=True, max_retries=MAX_RETRIES)
def render(self: Any, run_id: int, finish: bool = False) -> dict[str, Any]:
    return _stage(self, run_id, lambda ctx: render_stage.render(ctx, run_id), finish=finish)


@celery.task(name="studio.gate", bind=True, max_retries=MAX_RETRIES)
def gate(self: Any, run_id: int, finish: bool = False) -> dict[str, Any]:
    return _stage(self, run_id, lambda ctx: gate_stage.gate(ctx, run_id), finish=finish)


@celery.task(name="studio.publish", bind=True, max_retries=MAX_RETRIES)
def publish(self: Any, run_id: int, finish: bool = False) -> dict[str, Any]:
    return _stage(self, run_id, lambda ctx: publish_stage.publish(ctx, run_id), finish=finish)


@celery.task(name="studio.export_training", bind=True, max_retries=MAX_RETRIES)
def export_training(self: Any, project: str) -> str:
    path: Path = export_stage.export_training(context.current(), project)
    return str(path)
