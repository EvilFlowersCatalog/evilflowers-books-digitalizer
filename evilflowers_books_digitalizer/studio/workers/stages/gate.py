"""``studio.gate``: measured signals -> ``domain.quality_gate`` -> book ``review`` or ``approved``.

With ``auto_publish = "gate"`` a passing book gets a new *publish* run, returned to
the task shell, which enqueues it only after this run is committed as finished.
"""

from __future__ import annotations

from statistics import fmean

from evilflowers_books_digitalizer.db.models import Book, Run
from evilflowers_books_digitalizer.studio.domain import (
    BookStatus,
    QualityReport,
    QualitySignals,
    RegionKind,
    RegionStatus,
    RunKind,
    RunStatus,
    quality_gate,
)
from evilflowers_books_digitalizer.studio.events import EventType
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages.formulas import REJECTED

#: ``runs.created_by`` of the publish runs the gate creates
GATE_AUTHOR = "quality-gate"


def signals(book: Book) -> tuple[QualitySignals, int]:
    """Quality signals of a book + its number of (non-rejected) formulas."""
    access = [p for p in book.pages if p.add_to_access]
    confs = [p.ocr_conf for p in access if p.ocr_conf is not None and (p.ocr_text or "").strip()]
    live = [r for p in book.pages for r in p.regions if r.status != RegionStatus.REJECTED]
    formulas = [r for r in live if r.kind == RegionKind.FORMULA]
    stats = book.stats or {}
    return QualitySignals(
        pages=len(book.pages),
        ocr_mean_conf=fmean(confs) if confs else 0.0,
        disputed_regions=sum(r.status == RegionStatus.DISPUTED for r in live),
        formula_fallbacks=sum(not (r.text or "").strip() for r in formulas),
        vlm_rejected=sum(REJECTED in (r.reason or "") for r in live),
        unreadable_frames=int(stats.get("unreadable_frames", 0)),
        missing_pages=sum(p.add_to_access and not p.clean_path for p in book.pages),
    ), len(formulas)


def _decide(uow: Uow, book: Book, report: QualityReport) -> None:
    project = runs.project_of(book)
    if report.passed:
        runs.move_book(uow, book, BookStatus.APPROVED)
        uow.emit(EventType.BOOK_READY, project, book.id, score=round(report.score, 3))
    else:
        runs.move_book(uow, book, BookStatus.REVIEW)
        uow.emit(EventType.BOOK_REVIEW_NEEDED, project, book.id, flags=list(report.flags),
                 score=round(report.score, 3))


def _pending_publish(run: Run) -> int | None:
    """An active publish run this gate created on an earlier attempt (ids grow)."""
    return next((r.id for r in run.book.runs
                 if r.id > run.id and r.kind == RunKind.PUBLISH and r.created_by == GATE_AUTHOR
                 and r.status in runs.ACTIVE), None)


def gate(ctx: WorkerContext, run_id: int) -> list[int]:
    """Returns the publish run(s) to enqueue once this run has finished."""
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book = run.book
        runs.enter_stage(uow, run, "gate", 1)
        project = ctx.project(runs.project_of(book))
        measured, formulas = signals(book)
        report = quality_gate(measured, formulas, project.quality)
        book.quality = report.as_dict()
        book.quality_score = report.score
        _decide(uow, book, report)
        runs.advance(uow, run)
        runs.log(uow, run, "quality gate: " + ("passed" if report.passed else
                                               "flagged: " + "; ".join(report.flags)))
        if not (report.passed and project.auto_publish == "gate"):
            return []
        if (existing := _pending_publish(run)) is not None:
            return [existing]       # a retry of this gate: the follow-up exists already
        publish = Run(kind=RunKind.PUBLISH, status=RunStatus.QUEUED, interactive=False,
                      created_by=GATE_AUTHOR)
        book.runs.append(publish)
        uow.session.flush()
        uow.emit(EventType.BOOK_QUEUED, project.slug, book.id, run_id=publish.id,
                 kind=str(RunKind.PUBLISH))
        return [publish.id]
