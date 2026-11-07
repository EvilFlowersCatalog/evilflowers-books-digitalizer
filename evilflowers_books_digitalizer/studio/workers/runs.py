"""Run bookkeeping shared by every task: lifecycle, progress, log, book status.

All writes go through a :class:`~.context.Uow`, so their events leave only after
the commit. Progress counters are bumped with a single ``UPDATE … SET done = done
+ n`` because chunks of one run finish concurrently on several workers.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import update

from evilflowers_books_digitalizer.db.models import Book, Run, utcnow
from evilflowers_books_digitalizer.studio.domain import (
    BookStatus,
    RunKind,
    RunStatus,
    TransitionError,
    check_transition,
)
from evilflowers_books_digitalizer.studio.events import EventType
from evilflowers_books_digitalizer.studio.ports import PortError
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext

logger = logging.getLogger(__name__)

LOG_TAIL = 200
#: run statuses a task may still work on
ACTIVE = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})


def load_run(uow: Uow, run_id: int, *, lock: bool = False) -> Run:
    """The run row; ``lock`` takes a row lock (``SELECT … FOR UPDATE``, a no-op on SQLite)."""
    run = uow.session.get(Run, run_id, with_for_update=lock or None)
    if run is None:
        raise PortError(f"run {run_id} does not exist")
    return run


def project_of(book: Book) -> str:
    if not book.project:
        raise PortError(f"book {book.id} belongs to no studio project")
    return book.project


def is_active(ctx: WorkerContext, run_id: int) -> bool:
    with ctx.uow() as uow:
        run = uow.session.get(Run, run_id)
        return run is not None and run.status in ACTIVE


def touch(ctx: WorkerContext, run_id: int) -> None:
    """Heartbeat: a task is alive and working on this (still active) run."""
    with ctx.uow() as uow:
        uow.session.execute(update(Run).where(Run.id == run_id,
                                              Run.status.in_([str(s) for s in ACTIVE]))
                            .values(heartbeat_at=utcnow()))


def claim_dispatch(ctx: WorkerContext, run_id: int) -> bool:
    """Compare-and-set ``dispatched_at``: True for exactly one caller per running run.

    ``studio.run`` and the stale-run sweeper may both want to send a run's canvas;
    whoever claims it dispatches, the other backs off. Claiming *before* sending
    means a worker lost in between leaves a claimed run without tasks, which the
    sweeper fails (after the stale threshold) instead of running it twice.
    """
    now = utcnow()
    with ctx.uow() as uow:
        claimed = uow.session.execute(
            update(Run).where(Run.id == run_id, Run.status == str(RunStatus.RUNNING),
                              Run.dispatched_at.is_(None))
            .values(dispatched_at=now, heartbeat_at=now))
        return bool(claimed.rowcount)   # type: ignore[attr-defined]


def log(uow: Uow, run: Run, message: str, level: str = "info") -> None:
    entry = {"ts": utcnow().isoformat(timespec="seconds"), "level": level, "message": message}
    run.log = [*(run.log or []), entry][-LOG_TAIL:]
    run.heartbeat_at = utcnow()
    getattr(logger, level if level in ("info", "warning", "error") else "info")(
        "run %s: %s", run.id, message)
    uow.emit(EventType.RUN_LOG, project_of(run.book), run.book_pk, run_id=run.id, level=level,
             message=message)


def _progress_event(uow: Uow, run: Run) -> None:
    uow.emit(EventType.RUN_PROGRESS, project_of(run.book), run.book_pk, run_id=run.id,
             stage=run.stage, done=run.done, total=run.total)


def enter_stage(uow: Uow, run: Run, stage: str, total: int) -> None:
    """Switch the run to ``stage`` once, however many parallel tasks of it start."""
    switched = uow.session.execute(
        update(Run).where(Run.id == run.id, (Run.stage != stage) | Run.stage.is_(None))
        .values(stage=stage, done=0, total=total, heartbeat_at=utcnow()))
    if switched.rowcount:   # type: ignore[attr-defined]
        uow.session.refresh(run)
        _progress_event(uow, run)


def stage_done(run: Run) -> bool:
    """The current single-task stage already committed its work.

    Body and finish are separate commits; when the finish fails transiently the
    task retries, and its body must not publish (or render) a second time.
    ``enter_stage`` keeps ``done`` when the stage does not change, so it says so.
    """
    return run.total > 0 and run.done >= run.total


def advance(uow: Uow, run: Run, n: int = 1) -> None:
    uow.session.execute(update(Run).where(Run.id == run.id)
                        .values(done=Run.done + n, heartbeat_at=utcnow()))
    uow.session.refresh(run)
    _progress_event(uow, run)


def move_book(uow: Uow, book: Book, target: BookStatus) -> None:
    """Change the book status along the domain lifecycle (via QUEUED when it must)."""
    previous = book.status
    try:
        check_transition(previous, target)
    except TransitionError:
        check_transition(previous, BookStatus.QUEUED)
        check_transition(BookStatus.QUEUED, target)
    if previous == target:
        return
    book.status = target
    uow.emit(EventType.BOOK_STATUS, project_of(book), book.id, status=str(target),
             previous=previous)


def start(uow: Uow, run: Run) -> None:
    run.status = RunStatus.RUNNING
    run.started_at = run.heartbeat_at = utcnow()
    run.dispatched_at = None        # set by claim_dispatch once the canvas is sent
    run.error = None
    uow.emit(EventType.RUN_STARTED, project_of(run.book), run.book_pk, run_id=run.id,
             kind=run.kind, pages=run.pages)


def finish(ctx: WorkerContext, run_id: int) -> None:
    with ctx.uow() as uow:
        run = load_run(uow, run_id)
        if run.status not in ACTIVE:
            return
        run.status = RunStatus.SUCCEEDED
        run.finished_at = utcnow()
        log(uow, run, f"{run.kind} run finished")
        uow.emit(EventType.RUN_FINISHED, project_of(run.book), run.book_pk, run_id=run.id,
                 status=str(RunStatus.SUCCEEDED))


def fail(ctx: WorkerContext, run_id: int, exc: BaseException) -> None:
    """Record the failure; a failed *full* run also puts the book in ERROR."""
    error = f"{type(exc).__name__}: {exc}"[:4000]
    try:
        with ctx.uow() as uow:
            run = uow.session.get(Run, run_id)
            if run is None or run.status not in ACTIVE:
                return
            run.status = RunStatus.FAILED
            run.error = error
            run.finished_at = utcnow()
            log(uow, run, error, "error")
            if run.kind == RunKind.FULL:
                run.book.error = error
                _force_error(uow, run.book)
            uow.emit(EventType.RUN_FINISHED, project_of(run.book), run.book_pk, run_id=run.id,
                     status=str(RunStatus.FAILED), error=error)
    except Exception:  # noqa: BLE001 — never mask the original error
        logger.exception("could not record failure of run %s", run_id)


def _force_error(uow: Uow, book: Book) -> None:
    try:
        move_book(uow, book, BookStatus.ERROR)
    except TransitionError:
        logger.warning("book %s: no lifecycle path %s -> error", book.id, book.status)


def report(run: Run) -> dict[str, Any]:
    """Compact run summary for task return values (visible in Flower / result backend)."""
    return {"run_id": run.id, "kind": run.kind, "status": run.status, "stage": run.stage,
            "done": run.done, "total": run.total}
