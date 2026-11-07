"""``studio.clean``: planned leaves -> cleaned pages (+ operator overrides) -> previews/tiles.

Idempotent: only leaves still ``pending`` in this run are cleaned (the run start
resets the leaves it covers), so a redelivered chunk does no double work. The slow
part runs outside any transaction; rows are updated in one short unit afterwards.
Every actual clean bumps ``pages.version`` — the SPA swaps tiles on that.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evilflowers_books_digitalizer.db.models import Page, Region
from evilflowers_books_digitalizer.studio.domain import PageOverride, PageStatus, RegionStatus
from evilflowers_books_digitalizer.studio.events import EventType
from evilflowers_books_digitalizer.studio.ports import CleanedPage, LeafPlan
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages import common

PREVIEW_PX = 1600
#: deskew differences below this move content by under a pixel on a page edge
SKEW_TOLERANCE_DEG = 0.05
MOVED_REASON = "page geometry changed by a re-run: check the box and decide again"


@dataclass(frozen=True, slots=True)
class CleanJob:
    project: str
    item_id: str
    plans: list[LeafPlan]
    overrides: dict[int, PageOverride]


def _job(ctx: WorkerContext, run_id: int, page_indices: list[int]) -> CleanJob | None:
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book = run.book
        pages = [p for p in common.selected(book.pages, page_indices)
                 if p.status == PageStatus.PENDING]
        runs.enter_stage(uow, run, "clean", len(run.pages or book.pages))
        if not pages:
            return None
        overrides = {p.index: o for p in pages if (o := common.override(p)) is not None}
        return CleanJob(runs.project_of(book), common.item_id(book),
                        [common.leaf_plan(p) for p in pages], overrides)


def refresh_previews(ctx: WorkerContext, project: str, item: str, image: Path) -> Path:
    """Browser preview + DZI for one cleaned page; returns the .dzi."""
    adapters = ctx.adapters(project)
    return adapters.image_ops.previews(image, common.studio_path(adapters.artifacts, item),
                                       max_px=PREVIEW_PX)


def geometry_changed(page: Page, cleaned: CleanedPage) -> bool:
    """A re-clean moved the pixel space regions live in (size or deskew changed).

    The first clean has no previous geometry and no regions, so it never counts.
    """
    if page.width is None or page.height is None:
        return False
    old_skew, new_skew = page.skew_angle or 0.0, cleaned.skew_deg or 0.0
    return ((page.width, page.height) != (cleaned.width, cleaned.height)
            or abs(old_skew - new_skew) > SKEW_TOLERANCE_DEG)


def _clamp(region: Region, width: int, height: int) -> None:
    x0, y0 = min(max(region.x0, 0), width - 1), min(max(region.y0, 0), height - 1)
    region.x0, region.y0 = x0, y0
    region.x1, region.y1 = max(min(region.x1, width), x0 + 1), max(min(region.y1, height), y0 + 1)


def flag_moved_regions(page: Page, width: int, height: int) -> None:
    """Operator decisions on a page whose geometry changed need a second look.

    Boxes are stored in cleaned-page pixels; after a crop/split/deskew override
    they point at other content. Mapping them through ScanTailor's transform is
    not possible from here, so they become *disputed* (clamped into the new page,
    so they stay editable). ``analyze`` keeps operator rows across re-runs and
    erases ``remove`` boxes only when they are confirmed, so nothing is wiped
    with a stale box and nothing is lost.
    """
    for region in page.regions:
        if region.status in (RegionStatus.CONFIRMED, RegionStatus.REJECTED):
            _clamp(region, width, height)
            region.status = RegionStatus.DISPUTED
            region.reason = MOVED_REASON


def apply_cleaned(uow: Uow, page: Page, cleaned: CleanedPage, dzi: Path, webp: Path) -> None:
    if geometry_changed(page, cleaned):
        flag_moved_regions(page, cleaned.width, cleaned.height)
    page.clean_path = str(cleaned.image)
    page.width, page.height, page.dpi = cleaned.width, cleaned.height, cleaned.dpi
    page.skew_angle = cleaned.skew_deg
    page.dzi_path = str(dzi)
    page.preview_path = str(webp) if webp.exists() else None
    page.status = PageStatus.CLEANED
    page.version = (page.version or 0) + 1
    uow.emit(EventType.PAGE_UPDATED, runs.project_of(page.book), page.book_pk, page_id=page.id,
             index=page.index, version=page.version, status=page.status)


def clean(ctx: WorkerContext, run_id: int, page_indices: list[int]) -> None:
    job = _job(ctx, run_id, page_indices)
    if job is None:
        return
    adapters = ctx.adapters(job.project)
    out_dir = ctx.work_dir(job.project, job.item_id) / "clean"
    cleaned = adapters.cleaner.clean(job.plans, out_dir, job.overrides)
    dzis = {c.leaf_num: refresh_previews(ctx, job.project, job.item_id, c.image) for c in cleaned}
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        by_index = {p.index: p for p in run.book.pages}
        for c in cleaned:
            webp = adapters.artifacts.path(job.item_id, "leaf_webp", leaf=c.leaf_num)
            apply_cleaned(uow, by_index[c.leaf_num], c, dzis[c.leaf_num], webp)
        runs.advance(uow, run, len(cleaned))
