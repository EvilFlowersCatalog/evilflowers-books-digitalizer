"""Small mappings between DB rows and port values, shared by the stages."""

from __future__ import annotations

from pathlib import Path

from evilflowers_books_digitalizer.db.models import Book, Page, Region
from evilflowers_books_digitalizer.studio.adapters.artifacts import studio_dir
from evilflowers_books_digitalizer.studio.domain import BBox, PageOverride, RegionKind
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import ArtifactStore, LeafPlan, PortError, ScannedBook


def item_id(book: Book) -> str:
    if not book.item_id:
        raise PortError(f"book {book.id} has no item identifier")
    return book.item_id


def leaf_plan(page: Page) -> LeafPlan:
    """The persisted plan of a leaf (what :meth:`PageCleaner.plan` decided once)."""
    if not page.source_path or not page.crop_box:
        raise PortError(f"leaf {page.index} of book {page.book_pk} was never planned")
    return LeafPlan(page.index, Path(page.source_path), page.side, BBox.from_xywh(page.crop_box),
                    page.rotate_degree or 0, page.hand_side)


def override(page: Page) -> PageOverride | None:
    return PageOverride.from_dict(page.override) if page.override else None


def region_bbox(region: Region) -> BBox:
    return BBox(region.x0, region.y0, region.x1, region.y1)


def region_kind(region: Region) -> RegionKind:
    try:
        return RegionKind(region.kind)
    except ValueError:
        return RegionKind.OTHER


def book_metadata(book: Book) -> ItemMetadata:
    """Current metadata (operator edits included), identifier pinned to ``item_id``."""
    return ItemMetadata.for_item(item_id(book), book.item_metadata, book.title)


def scanned_book(book: Book) -> ScannedBook:
    """The book as the scan store knows it (frames are not needed for a metadata lookup)."""
    return ScannedBook(book.source, book.book_id, Path(book.source_dir or "."), ())


def studio_path(store: ArtifactStore, item: str) -> Path:
    """``<id>_studio`` — where ImageOps writes previews and tiles."""
    return studio_dir(store.item_dir(item), item)


def selected(pages: list[Page], indices: list[int] | None) -> list[Page]:
    if indices is None:
        return list(pages)
    wanted = set(indices)
    return [p for p in pages if p.index in wanted]
