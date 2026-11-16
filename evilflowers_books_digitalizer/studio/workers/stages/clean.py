"""Stage clean, the orchestrator's half: a cleaned page from scan-service into its row.

``evilflowers_scan.clean`` cleans the leaves (ScanTailor, DocRes) and writes each page with
its preview and tiles into the attempt dir; ``appliers.apply_clean`` points the page at them
with :func:`apply_cleaned`. Every new image bumps ``pages.version`` — the SPA swaps tiles on it.
"""

from __future__ import annotations

from pathlib import Path

from evilflowers_books_digitalizer.db.models import Page, Region
from evilflowers_books_digitalizer.studio.domain import PageStatus, RegionStatus
from evilflowers_books_digitalizer.studio.events import EventType
from evilflowers_books_digitalizer.studio.ports import CleanedPage
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow

#: deskew differences below this move content by under a pixel on a page edge
SKEW_TOLERANCE_DEG = 0.05
MOVED_REASON = "page geometry changed by a re-run: check the box and decide again"


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
    page.status = PageStatus.CLEANED
    page.marks = None       # a new cleaned image: the applier sets the outcome's marks after
    replace_image(uow, page, cleaned.image, dzi, webp)


def replace_image(uow: Uow, page: Page, image: Path, dzi: Path, webp: Path) -> None:
    """Point a page at a new rendition of itself (same geometry: an erase), version + 1."""
    page.clean_path = str(image)
    page.dzi_path = str(dzi)
    page.preview_path = str(webp) if webp.exists() else None
    page.version = (page.version or 0) + 1
    uow.emit(EventType.PAGE_UPDATED, runs.project_of(page.book), page.book_pk, page_id=page.id,
             index=page.index, version=page.version, status=page.status)
