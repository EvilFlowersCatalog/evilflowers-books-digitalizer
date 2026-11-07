"""``studio.run`` (decision half): start the run, plan leaves once, choose the stages.

Leaf planning happens here, before any fan-out: parallel clean chunks must all
see the same, already persisted leaf numbers (an operator's override is keyed by
them). The task shell turns the returned :class:`WorkPlan` into a Celery canvas.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evilflowers_books_digitalizer.db.models import Book, Page, Run
from evilflowers_books_digitalizer.studio.domain import BookStatus, PageStatus, RunKind, RunStatus
from evilflowers_books_digitalizer.studio.ports import LeafPlan, PortError
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext

#: which stages each kind of run executes, in order
STAGES: dict[RunKind, tuple[str, ...]] = {
    RunKind.FULL: ("clean", "analyze", "formulas", "render", "gate"),
    RunKind.RERUN: ("clean", "analyze", "formulas"),
    RunKind.RENDER: ("render",),
    RunKind.PUBLISH: ("publish",),
}
DEFAULT_CHUNK = 8


@dataclass(frozen=True, slots=True)
class WorkPlan:
    run_id: int
    stages: tuple[str, ...]
    pages: tuple[int, ...]          # leaf numbers the page stages work on
    chunk: int                      # leaves per clean task
    interactive: bool

    def chunks(self) -> list[list[int]]:
        return [list(self.pages[i:i + self.chunk]) for i in range(0, len(self.pages), self.chunk)]


@dataclass(frozen=True, slots=True)
class LeafLayout:
    """Result of planning a book: its frames and one plan per leaf."""

    frames: tuple[Path, ...]
    plans: list[LeafPlan]


def plan_leaves(ctx: WorkerContext, project: str, collection: str, external_id: str
                ) -> LeafLayout:
    """PageCleaner.plan over the book's frames (reads every frame: no transaction open)."""
    scanned = ctx.adapters(project).scans.book(collection, external_id)
    plans = ctx.adapters(project).cleaner.plan(scanned.frames)
    if not plans:
        raise PortError(f"no readable frames for {collection}/{external_id}")
    return LeafLayout(scanned.frames, plans)


def persist_leaves(uow: Uow, book: Book, layout: LeafLayout) -> None:
    """One ``pages`` row per planned leaf (IA scandata fields)."""
    plans = layout.plans
    for p in plans:
        book.pages.append(Page(
            index=p.leaf_num, source_path=str(p.frame), side=p.side,
            n_sides=2 if p.side is not None else 1, x0=p.crop.x0, x1=p.crop.x1,
            crop_box=p.crop.to_xywh(), rotate_degree=p.rotate_degree, hand_side=p.hand_side,
            status=PageStatus.PENDING, version=0, override={}, leaf_extra={}))
    planned = {p.frame for p in plans}
    book.n_frames = len(layout.frames)
    book.n_pages = len(plans)
    book.stats = {**(book.stats or {}),
                  "unreadable_frames": sum(f not in planned for f in layout.frames)}
    uow.session.flush()


def _reset(pages: list[Page]) -> None:
    for page in pages:
        page.status = PageStatus.PENDING


def _needs_plan(ctx: WorkerContext, run_id: int) -> tuple[str, str, str] | None:
    """``(project, collection, book)`` when this queued full run must plan leaves first."""
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book = run.book
        if run.status != RunStatus.QUEUED or run.kind != RunKind.FULL or book.pages:
            return None
        return runs.project_of(book), book.source, book.book_id


#: run kinds that must wait for earlier active runs of these kinds on the same book
_WAITS_FOR: dict[RunKind, frozenset[RunKind]] = {
    RunKind.PUBLISH: frozenset({RunKind.FULL, RunKind.RENDER}),
}
_ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)


def blocked_by(ctx: WorkerContext, run_id: int) -> int | None:
    """An earlier, still active run this one must wait for, else None.

    Approving a book enqueues a render run and a publish run back to back on the
    ``io`` queue with no ordering guarantee; publishing before the render finished
    would ship the previous files. Only *earlier* runs count, so two runs can
    never wait for each other.
    """
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        waits_for = _WAITS_FOR.get(RunKind(run.kind), frozenset())
        return next((r.id for r in run.book.runs
                     if r.id < run.id and r.kind in waits_for and r.status in _ACTIVE), None)


def work_plan(ctx: WorkerContext, run: Run) -> WorkPlan:
    """The work of a started run, from the rows alone (orchestrate and the re-dispatch
    of a run whose canvas was never sent build the same plan)."""
    book = run.book
    kind = RunKind(run.kind)
    indices = (list(run.pages) if run.pages is not None and kind == RunKind.RERUN
               else [p.index for p in book.pages])
    chunk = int(ctx.project(runs.project_of(book)).pipeline.get("chunk_size", DEFAULT_CHUNK))
    return WorkPlan(run.id, STAGES[kind], tuple(sorted(indices)), max(1, chunk),
                    run.interactive)


def orchestrate(ctx: WorkerContext, run_id: int) -> WorkPlan | None:
    """Mark the run running and decide its work; ``None`` if it was already started."""
    target = _needs_plan(ctx, run_id)
    layout = plan_leaves(ctx, *target) if target else None
    with ctx.uow() as uow:
        # row lock: a duplicate studio.run (sweeper re-enqueue, redelivery) waits here
        # and then sees ``running`` instead of starting a second canvas
        run = runs.load_run(uow, run_id, lock=True)
        if run.status != RunStatus.QUEUED:
            return None             # redelivered / cancelled: nothing to do
        book = run.book
        kind = RunKind(run.kind)
        runs.start(uow, run)
        if kind == RunKind.FULL:
            runs.move_book(uow, book, BookStatus.RUNNING)
            if layout is not None and not book.pages:
                persist_leaves(uow, book, layout)
        plan = work_plan(ctx, run)
        if kind in (RunKind.FULL, RunKind.RERUN):
            wanted = set(plan.pages)
            _reset([p for p in book.pages if p.index in wanted])
        runs.log(uow, run, f"{kind} run: {len(plan.pages)} leaves, stages "
                           + " -> ".join(STAGES[kind]))
        return plan
