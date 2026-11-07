"""Use cases around leaves (pages): list, detail, IA scandata edits and overrides.

Exclusion from PDF/EPUB is driven only by ``add_to_access`` (IA rule); setting a
page type merely re-seeds that flag unless the operator sets it in the same
request. Overrides are validated through ``domain.PageOverride`` so a re-run
never meets a value it cannot apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.studio.application import dto
from evilflowers_books_digitalizer.studio.application.books import artifact_path
from evilflowers_books_digitalizer.studio.application.errors import Invalid
from evilflowers_books_digitalizer.studio.application.lookup import get_book, get_page
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.application.uow import (
    Book,
    Page,
    Session,
    UnitOfWork,
    select,
)
from evilflowers_books_digitalizer.studio.domain import BBox, PageOverride
from evilflowers_books_digitalizer.studio.events import Event, EventType
from evilflowers_books_digitalizer.studio.metadata import Leaf, access_index, default_add_to_access

IMAGE_LAYERS = ("original", "spread", "clean", "pdf")
_OVERRIDE_KEYS = frozenset({"crop", "deskew_deg", "split_x", "despeckle", "color_mode",
                            "ocr_language", "extra"})
_OVERRIDE_CHOICES = {"despeckle": {"off", "cautious", "normal", "aggressive"},
                     "color_mode": {"mixed", "color_grayscale", "black_and_white"}}
_LEAF_EXTRA = {"book_start": bool, "toc_title": str, "toc_level": int}
#: ``books.stats`` key: {"<leaf>": pdf page} of the last render (written by studio.render)
PDF_PAGES_STAT = "pdf_pages"


def access_map(session: Session, book_id: int) -> dict[int, int]:
    """leaf index -> access index for one book (IA ``addToAccessFormats`` semantics)."""
    rows = session.execute(select(Page.index, Page.add_to_access)
                           .where(Page.book_pk == book_id).order_by(Page.index)).all()
    return access_index([Leaf(leaf_num=i, add_to_access=bool(flag)) for i, flag in rows])


# ---------------------------------------------------------------- read
def list_pages(svc: Services, book_id: int) -> list[dto.PageOut]:
    with svc.uow() as uow:
        book = get_book(uow.session, book_id)
        access = access_map(uow.session, book.id)
        return [dto.page_out(p, access.get(p.index)) for p in book.pages]


def get_page_detail(svc: Services, page_id: int) -> dto.PageDetail:
    with svc.uow() as uow:
        return page_detail(uow.session, get_page(uow.session, page_id))


def page_detail(session: Session, page: Page) -> dto.PageDetail:
    access = access_map(session, page.book_pk)
    notes = [n for n in page.book.notes if n.page_pk == page.id]
    prev_id, next_id = _neighbours(session, page)
    return dto.PageDetail(
        page=dto.page_out(page, access.get(page.index)),
        regions=[dto.region_out(r) for r in page.regions],
        notes=[dto.note_out(n) for n in notes],
        override=dict(page.override or {}), leaf_extra=dict(page.leaf_extra or {}),
        ocr_text=page.ocr_text, ocr_conf=page.ocr_conf, frame=dto.source_frame(page),
        prev_id=prev_id, next_id=next_id,
        files={"clean": dto.file_ref(page.clean_path), "dzi": dto.file_ref(page.dzi_path),
               "hocr": dto.file_ref(page.hocr_path), "preview": dto.file_ref(page.preview_path)},
        images={layer: f"{dto.API_PREFIX}/pages/{page.id}/image/{layer}" for layer in IMAGE_LAYERS}
        | {"dzi": f"{dto.API_PREFIX}/pages/{page.id}/dzi"})


def _neighbours(session: Session, page: Page) -> tuple[int | None, int | None]:
    """Ids of the previous and next leaf of the same book (None at either end)."""
    same_book = Page.book_pk == page.book_pk
    prev_id = session.scalar(select(Page.id).where(same_book, Page.index < page.index)
                             .order_by(Page.index.desc()).limit(1))
    next_id = session.scalar(select(Page.id).where(same_book, Page.index > page.index)
                             .order_by(Page.index.asc()).limit(1))
    return prev_id, next_id


@dataclass(frozen=True, slots=True)
class PageMedia:
    """Every file a viewer layer may read for one leaf — paths only from the database."""

    page_id: int
    version: int
    frame: Path | None             # original TIFF frame
    crop: tuple[int, int, int, int] | None   # leaf in the (rotated) frame, x0 y0 x1 y1
    half: tuple[int, int] | None   # legacy rows: the spread half's x-range only
    rotate: int
    clean: Path | None             # cleaned page image
    dzi: Path | None               # pre-rendered deep-zoom descriptor (batch), if any
    pdf: Path | None               # the book's distribution PDF
    access_index: int | None       # page of that PDF; None = not in access formats


def page_media(svc: Services, page_id: int) -> PageMedia:
    with svc.uow() as uow:
        page = get_page(uow.session, page_id)
        book = page.book
        store = svc.artifacts_for(book.project) if book.project else None
        pdf = artifact_path(book, "pdf", store)
        return PageMedia(page_id=page.id, version=page.version or 0,
                         frame=_existing(page.source_path), crop=_frame_crop(page),
                         half=(page.x0, page.x1) if page.x0 is not None and page.x1 else None,
                         rotate=page.rotate_degree or 0, clean=_existing(page.clean_path),
                         dzi=_existing(page.dzi_path), pdf=_existing(pdf),
                         access_index=_pdf_page(uow.session, book, page.index))


def _pdf_page(session: Session, book: Book, leaf: int) -> int | None:
    """The PDF page that shows ``leaf``, as it was when the PDF was rendered.

    ``studio.render`` records its leaf -> page map in ``books.stats["pdf_pages"]``;
    today's ``add_to_access`` flags may differ until the next render, and using
    them would shift every later leaf by one. Older books fall back to them.
    """
    rendered = (book.stats or {}).get(PDF_PAGES_STAT)
    if isinstance(rendered, dict):
        page = rendered.get(str(leaf))
        return int(page) if page is not None else None
    return access_map(session, book.id).get(leaf)


def _existing(path: str | None) -> Path | None:
    return Path(path) if path and Path(path).is_file() else None


def _frame_crop(page: Page) -> tuple[int, int, int, int] | None:
    """IA crop box (x, y, w, h) as x0 y0 x1 y1 (BBox is the in-memory form)."""
    if page.crop_box and len(page.crop_box) == 4:
        box = BBox.from_xywh(page.crop_box)
        return box.x0, box.y0, box.x1, box.y1
    return None


# ---------------------------------------------------------------- edit
def update_page(svc: Services, page_id: int, patch: dto.PagePatch) -> dto.PageDetail:
    sent = patch.model_fields_set
    with svc.uow() as uow:
        page = get_page(uow.session, page_id)
        if patch.page_type is not None:
            page.page_type = patch.page_type
            if "add_to_access" not in sent:
                page.add_to_access = default_add_to_access(patch.page_type)
        if patch.add_to_access is not None:
            page.add_to_access = patch.add_to_access
        if "page_number" in sent:
            page.page_number = (patch.page_number or "").strip() or None
        if patch.override is not None:
            page.override = validate_override(patch.override)
        if patch.leaf_extra is not None:
            page.leaf_extra = _merge_leaf_extra(page.leaf_extra or {}, patch.leaf_extra)
        announce_page(uow, page)
        uow.session.flush()
        return page_detail(uow.session, page)


def announce_page(uow: UnitOfWork, page: Page) -> None:
    uow.emit(Event(EventType.PAGE_UPDATED, page.book.project or "", page.book_pk,
                   {"page_id": page.id, "index": page.index, "version": page.version or 0,
                    "status": page.status}))


def validate_override(raw: dict[str, Any]) -> dict[str, Any]:
    """Round-trip through PageOverride: unknown keys and malformed values are refused."""
    unknown = sorted(set(raw) - _OVERRIDE_KEYS)
    if unknown:
        raise Invalid(f"unknown override field(s): {', '.join(unknown)}")
    for key, allowed in _OVERRIDE_CHOICES.items():
        if raw.get(key) is not None and raw[key] not in allowed:
            raise Invalid(f"override {key} must be one of {sorted(allowed)}")
    try:
        return PageOverride.from_dict(raw).as_dict()
    except (TypeError, ValueError) as exc:
        raise Invalid(f"invalid override: {exc}") from None


def _merge_leaf_extra(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Operator-only scandata fields (bookStart, outline title/level); ``null`` removes one."""
    merged = dict(current)
    for key, value in patch.items():
        expected = _LEAF_EXTRA.get(key)
        if expected is None:
            raise Invalid(f"unknown leaf field {key!r} (one of {', '.join(_LEAF_EXTRA)})")
        if value is None:
            merged.pop(key, None)
        elif type(value) is not expected:
            raise Invalid(f"{key} must be {expected.__name__}")
        else:
            merged[key] = value
    return merged
