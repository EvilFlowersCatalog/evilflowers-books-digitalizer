"""Use cases around runs: create (full / rerun / render / publish), read, cancel.

A ``runs`` row is the only touchpoint between the interactive and the batch side
(docs/design/studio-spec.md): the API commits the row, *then* enqueues
``studio.run`` with its id, so a worker never receives an id it cannot read.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from evilflowers_books_digitalizer.studio.application import dto
from evilflowers_books_digitalizer.studio.application.errors import Conflict, Invalid, Unavailable
from evilflowers_books_digitalizer.studio.application.lookup import get_book, get_run
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.application.uow import (
    Book,
    Page,
    Run,
    Session,
    UnitOfWork,
    select,
    utcnow,
)
from evilflowers_books_digitalizer.studio.domain import (
    BookStatus,
    RunKind,
    RunStatus,
    TransitionError,
    check_transition,
)
from evilflowers_books_digitalizer.studio.events import Event, EventType

logger = logging.getLogger(__name__)

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)
#: kinds that work on existing leaves (a full run must have planned them first)
_NEEDS_PAGES = (RunKind.RERUN, RunKind.RENDER)


@dataclass(frozen=True, slots=True)
class RunSpec:
    kind: RunKind
    pages: list[int] | None = None
    interactive: bool = False


# ---------------------------------------------------------------- create
def create_run(svc: Services, book_id: int, cmd: dto.RunIn, author: str | None) -> dto.RunOut:
    """Validate, record and enqueue one run (reruns default to the interactive queue)."""
    interactive = cmd.interactive if cmd.interactive is not None else cmd.kind == RunKind.RERUN
    spec = RunSpec(cmd.kind, cmd.pages, interactive)
    with svc.uow() as uow:
        book = get_book(uow.session, book_id)
        check_active(uow.session, book, spec.kind, spec.pages)
        _check_pages(uow.session, book, spec)
        run = add_run(uow, book, spec, author)
    return enqueue(svc, [run])[0]


def add_run(uow: UnitOfWork, book: Book, spec: RunSpec, author: str | None) -> Run:
    """Insert the row and move the book's status (full -> queued; publish only checked)."""
    run = Run(book=book, kind=spec.kind, status=RunStatus.QUEUED, interactive=spec.interactive,
              pages=sorted(set(spec.pages)) if spec.pages else None, created_by=author, log=[])
    uow.session.add(run)
    uow.session.flush()
    if spec.kind == RunKind.PUBLISH:
        _guard_transition(book.status, BookStatus.PUBLISHED)
    if spec.kind == RunKind.FULL:
        set_status(uow, book, BookStatus.QUEUED)
        book.queued_at, book.error = utcnow(), None
    uow.emit(Event(EventType.BOOK_QUEUED, book.project or "", book.id,
                   {"run_id": run.id, "kind": str(spec.kind)}))
    return run


def set_status(uow: UnitOfWork, book: Book, status: BookStatus) -> None:
    """Change a book's status through the domain lifecycle and announce it."""
    previous = book.status
    book.status = _guard_transition(previous, status)
    if previous != book.status:
        uow.emit(Event(EventType.BOOK_STATUS, book.project or "", book.id,
                       {"status": book.status, "previous": previous}))


def _guard_transition(current: str, new: BookStatus) -> BookStatus:
    try:
        return check_transition(current, new)
    except TransitionError as exc:
        raise Conflict(str(exc)) from None


def _overlaps(a: list[int] | None, b: list[int] | None) -> bool:
    """``None`` is the whole book, which overlaps everything."""
    return a is None or b is None or bool(set(a) & set(b))


def _blocks(active: Run, kind: RunKind, pages: list[int] | None) -> bool:
    """Whether an active run forbids starting a new one of ``kind`` on ``pages``.

    Two runs that clean the same leaf would reset it under each other and write
    the same cleaned file, so leaf overlap is what matters: a full run (it
    re-plans and re-cleans every leaf) conflicts with any run, reruns conflict
    when their leaves overlap, and render/publish keep one batch run per book.
    """
    if active.kind == RunKind.FULL or kind == RunKind.FULL:
        return True
    if kind == RunKind.RERUN:
        return active.kind == RunKind.RERUN and _overlaps(active.pages, pages)
    return not active.interactive


def check_active(session: Session, book: Book, kind: RunKind,
                 pages: list[int] | None = None) -> None:
    """Refuse (409) a run that would work on leaves an active run is still working on."""
    active = session.scalars(select(Run).where(Run.book_pk == book.id, Run.status.in_(ACTIVE))).all()
    blocking = [r for r in active if _blocks(r, kind, pages)]
    if blocking:
        raise Conflict(f"book {book.id} already has run {blocking[0].id} ({blocking[0].kind}) "
                       f"{blocking[0].status}")


def _check_pages(session: Session, book: Book, spec: RunSpec) -> None:
    if spec.kind not in _NEEDS_PAGES:
        if spec.pages:
            raise Invalid(f"a {spec.kind} run works on the whole book; omit pages")
        return
    existing = set(session.scalars(select(Page.index).where(Page.book_pk == book.id)))
    if not existing:
        raise Conflict(f"book {book.id} has no pages yet; run a full run first")
    if spec.kind == RunKind.RERUN and not spec.pages:
        raise Invalid("a rerun needs pages (leaf indices)")
    unknown = sorted(set(spec.pages or ()) - existing)
    if unknown:
        raise Invalid(f"unknown page index(es) {unknown}")


def enqueue(svc: Services, runs: Sequence[Run]) -> list[dto.RunOut]:
    """Send committed runs to the queue and store their task ids; a broker failure fails the run."""
    task_ids: dict[int, str] = {}
    for run in runs:
        try:
            task_ids[run.id] = svc.queue.enqueue_run(run.id, interactive=bool(run.interactive))
        except Exception as exc:  # noqa: BLE001 — any broker error means "not scheduled"
            logger.warning("enqueue of run %s failed: %s", run.id, exc)
            _mark_unscheduled(svc, run.id, str(exc))
            raise Unavailable(f"task queue unavailable: {exc}") from exc
    with svc.uow() as uow:
        stored = [get_run(uow.session, run_id) for run_id in task_ids]
        for row in stored:
            row.task_id = task_ids[row.id]
        uow.session.flush()
        return [dto.run_out(row) for row in stored]


def _mark_unscheduled(svc: Services, run_id: int, error: str) -> None:
    with svc.uow() as uow:
        run = get_run(uow.session, run_id)
        run.status, run.error, run.finished_at = RunStatus.FAILED, f"not enqueued: {error}", utcnow()
        if run.kind == RunKind.FULL and run.book.status == BookStatus.QUEUED:
            set_status(uow, run.book, BookStatus.ERROR)
        uow.emit(_finished(run))


# ---------------------------------------------------------------- read / cancel
def read_run(svc: Services, run_id: int) -> dto.RunOut:
    with svc.uow() as uow:
        return dto.run_out(get_run(uow.session, run_id))


def cancel_run(svc: Services, run_id: int, author: str | None) -> dto.RunOut:
    """Stop a queued/running run; the worker sees ``cancelled`` when it re-checks the row."""
    with svc.uow() as uow:
        run = get_run(uow.session, run_id)
        if run.status not in ACTIVE:
            raise Conflict(f"run {run_id} is already {run.status}")
        run.status, run.finished_at = RunStatus.CANCELLED, utcnow()
        run.error = f"cancelled by {author or 'operator'}"
        _release_book(uow, run)
        uow.emit(_finished(run))
        task_id, out = run.task_id, dto.run_out(run)
    if task_id:
        svc.queue.revoke(task_id)
    return out


def _release_book(uow: UnitOfWork, run: Run) -> None:
    """A book left queued/running by its last active run goes back to a re-queueable state."""
    book = run.book
    others = [r for r in book.runs if r.id != run.id and r.status in ACTIVE]
    if others or book.status not in (BookStatus.QUEUED, BookStatus.RUNNING):
        return
    target = BookStatus.DISCOVERED if book.status == BookStatus.QUEUED and not book.pages \
        else BookStatus.ERROR
    set_status(uow, book, target)
    if target == BookStatus.ERROR:
        book.error = run.error


def _finished(run: Run) -> Event:
    return Event(EventType.RUN_FINISHED, run.book.project or "", run.book_pk,
                 {"run_id": run.id, "status": run.status, "error": run.error})
