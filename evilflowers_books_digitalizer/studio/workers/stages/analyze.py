"""``studio.analyze``: one cleaned page -> OCR + layout detectors -> ``fuse()`` -> regions.

Operator decisions survive re-runs:

* confirmed regions with action ``remove`` are erased from the page image itself
  (it feeds OCR, previews and every rendition) before anything reads it;
* confirmed ``as_image`` regions are erased only from the copy Tesseract reads;
* confirmed and rejected regions are kept as rows and enter ``fuse`` as operator
  proposals, so a detector's overlapping guess is absorbed by the operator's
  decision instead of re-appearing (a rejected stamp stays rejected).

Only machine ``proposed``/``disputed`` rows are replaced (their notes move to the
page); operator rows disputed after a geometry change are kept for the operator,
but neither erased nor fused until they are confirmed again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from evilflowers_books_digitalizer.db.models import Note, Region
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    FusedRegion,
    PageStatus,
    RegionAction,
    RegionKind,
    RegionProposal,
    RegionSource,
    RegionStatus,
    fuse,
)
from evilflowers_books_digitalizer.studio.events import EventType
from evilflowers_books_digitalizer.studio.ports import OcrPage
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages import common
from evilflowers_books_digitalizer.studio.workers.stages.clean import refresh_previews

#: region statuses that are an operator's decision (kept across re-runs)
DECIDED = (RegionStatus.CONFIRMED, RegionStatus.REJECTED)


@dataclass(frozen=True, slots=True)
class Decided:
    kind: RegionKind
    bbox: BBox
    status: RegionStatus
    action: RegionAction


@dataclass(frozen=True, slots=True)
class AnalyzeJob:
    project: str
    item_id: str
    leaf: int
    image: Path
    language: str
    dpi: int
    decided: list[Decided]

    def boxes(self, action: RegionAction) -> list[BBox]:
        return [d.bbox for d in self.decided
                if d.status == RegionStatus.CONFIRMED and d.action == action]


def _job(ctx: WorkerContext, run_id: int, page_index: int) -> AnalyzeJob | None:
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book = run.book
        runs.enter_stage(uow, run, "analyze", len(run.pages or book.pages))
        page = next((p for p in book.pages if p.index == page_index), None)
        if page is None or page.status != PageStatus.CLEANED or not page.clean_path:
            return None             # not cleaned, or already analyzed in this run
        project = runs.project_of(book)
        override = common.override(page)
        decided = [Decided(common.region_kind(r), common.region_bbox(r), RegionStatus(r.status),
                           RegionAction(r.action)) for r in page.regions if r.status in DECIDED]
        return AnalyzeJob(project, common.item_id(book), page.index, Path(page.clean_path),
                          (override.ocr_language if override and override.ocr_language
                           else ctx.project(project).ocr_language),
                          page.dpi or 300, decided)


def _ocr_input(ctx: WorkerContext, job: AnalyzeJob) -> Path:
    ops = ctx.adapters(job.project).image_ops
    removed = job.boxes(RegionAction.REMOVE)
    if removed:
        ops.erase(job.image, removed, job.image)
        refresh_previews(ctx, job.project, job.item_id, job.image)
    pictures = job.boxes(RegionAction.AS_IMAGE)
    if not pictures:
        return job.image
    ocr_copy = ctx.work_dir(job.project, job.item_id) / "ocr" / job.image.name
    return ops.erase(job.image, pictures, ocr_copy)


def detect(ctx: WorkerContext, job: AnalyzeJob) -> tuple[OcrPage, list[FusedRegion]]:
    adapters = ctx.adapters(job.project)
    ocr = adapters.ocr.recognize(_ocr_input(ctx, job), language=job.language, dpi=job.dpi)
    proposals = [p for d in adapters.detectors for p in d.detect(job.image, ocr=ocr)]
    operator = [RegionProposal(d.kind, d.bbox, RegionSource.OPERATOR) for d in job.decided]
    fused = fuse(operator + proposals)
    return ocr, [f for f in fused if f.source != RegionSource.OPERATOR]


def _region(f: FusedRegion) -> Region:
    b = f.bbox
    return Region(kind=str(f.kind), x0=b.x0, y0=b.y0, x1=b.x1, y1=b.y1, source=str(f.source),
                  confidence=f.confidence, status=str(f.status), action=str(f.action),
                  text=f.text, alternatives=[str(k) for k in f.alternatives] or None,
                  reason=f.reason or None)


def _replace_undecided(session: Session, page_pk: int) -> None:
    """Drop the page's machine proposals (a re-analysis replaces them), keeping every
    operator decision. Notes written on a dropped region move to its page first:
    ``notes.region_pk`` cascades, and an operator's note must survive a re-run."""
    # operator rows a geometry change turned back into disputes (clean.flag_moved_regions)
    # stay: only the operator may drop their own decision
    stale = select(Region.id).where(Region.page_pk == page_pk,
                                    Region.source != RegionSource.OPERATOR,
                                    Region.status.in_([RegionStatus.PROPOSED,
                                                       RegionStatus.DISPUTED]))
    session.execute(update(Note).where(Note.region_pk.in_(stale))
                    .values(region_pk=None, page_pk=page_pk))
    session.execute(delete(Region).where(Region.id.in_(stale)))


def analyze(ctx: WorkerContext, run_id: int, page_index: int) -> None:
    job = _job(ctx, run_id, page_index)
    if job is None:
        return
    ocr, fused = detect(ctx, job)
    hocr_path = ctx.work_dir(job.project, job.item_id) / "hocr" / f"{job.leaf:04d}.html"
    hocr_path.parent.mkdir(parents=True, exist_ok=True)
    hocr_path.write_text(ocr.hocr, encoding="utf-8")
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        page = next(p for p in run.book.pages if p.index == page_index)
        _replace_undecided(uow.session, page.id)
        uow.session.expire(page, ["regions"])
        page.regions.extend(_region(f) for f in fused)
        disputed = sum(r.status == RegionStatus.DISPUTED for r in page.regions)
        page.ocr_text, page.ocr_conf, page.hocr_path = ocr.text, ocr.mean_conf, str(hocr_path)
        page.status = PageStatus.DISPUTED if disputed else PageStatus.ANALYZED
        project = runs.project_of(run.book)
        uow.emit(EventType.REGIONS_UPDATED, project, page.book_pk, page_id=page.id,
                 index=page.index, disputed=disputed)
        uow.emit(EventType.PAGE_UPDATED, project, page.book_pk, page_id=page.id,
                 index=page.index, version=page.version, status=page.status)
        runs.advance(uow, run)
