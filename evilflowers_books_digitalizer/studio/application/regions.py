"""Operator region edits: draw, relabel/resize, reject.

Every operator touch makes the region ``source=operator`` and ``confirmed``
(unless the operator sets a status explicitly, e.g. ``rejected``): confirmed
regions are the labelled training set (``studio.export_training``), so an edit
is also a label. Regions are never hard-deleted; rejection keeps the negative
example. After every write the page's disputed state is recomputed.
"""

from __future__ import annotations

from evilflowers_books_digitalizer.studio.application import dto
from evilflowers_books_digitalizer.studio.application.errors import Invalid
from evilflowers_books_digitalizer.studio.application.lookup import get_page, get_region
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.application.uow import Page, Region, UnitOfWork
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    PageStatus,
    RegionSource,
    RegionStatus,
    default_action,
)
from evilflowers_books_digitalizer.studio.events import Event, EventType

#: page states that carry regions (before analysis there is nothing to dispute)
_ANALYSED = (PageStatus.ANALYZED, PageStatus.DISPUTED, PageStatus.DONE)


def create_region(svc: Services, page_id: int, cmd: dto.RegionIn, author: str | None) -> dto.RegionOut:
    with svc.uow() as uow:
        page = get_page(uow.session, page_id)
        box = _inside(page, BBox(*cmd.bbox))
        region = Region(page=page, kind=cmd.kind, x0=box.x0, y0=box.y0, x1=box.x1, y1=box.y1,
                        source=RegionSource.OPERATOR, confidence=1.0, status=RegionStatus.CONFIRMED,
                        action=cmd.action or default_action(cmd.kind), text=cmd.text,
                        alternatives=[], author=author)
        uow.session.add(region)
        uow.session.flush()
        _after_edit(uow, page)
        return dto.region_out(region)


def update_region(svc: Services, region_id: int, cmd: dto.RegionPatch, author: str | None
                  ) -> dto.RegionOut:
    with svc.uow() as uow:
        region = get_region(uow.session, region_id)
        if cmd.kind is not None and cmd.kind != region.kind:
            region.kind = cmd.kind
            region.action = default_action(cmd.kind)
        if cmd.bbox is not None:
            box = _inside(region.page, BBox(*cmd.bbox))
            region.x0, region.y0, region.x1, region.y1 = box.x0, box.y0, box.x1, box.y1
        if cmd.action is not None:
            region.action = cmd.action
        if "text" in cmd.model_fields_set:
            region.text = cmd.text
        region.status = cmd.status or RegionStatus.CONFIRMED
        _touch(region, author)
        uow.session.flush()
        _after_edit(uow, region.page)
        return dto.region_out(region)


def reject_region(svc: Services, region_id: int, author: str | None) -> None:
    with svc.uow() as uow:
        region = get_region(uow.session, region_id)
        region.status = RegionStatus.REJECTED
        _touch(region, author)
        uow.session.flush()
        _after_edit(uow, region.page)


def _touch(region: Region, author: str | None) -> None:
    region.source, region.confidence, region.reason = RegionSource.OPERATOR, 1.0, None
    region.author = author or region.author


def _inside(page: Page, box: BBox) -> BBox:
    """Regions live in cleaned-page pixels; a box outside the page is a client bug."""
    if page.width and page.height and (box.x0 < 0 or box.y0 < 0 or box.x1 > page.width
                                        or box.y1 > page.height):
        raise Invalid(f"bbox {box.as_list()} outside the page ({page.width}x{page.height})")
    return box


def _after_edit(uow: UnitOfWork, page: Page) -> None:
    disputed = sum(1 for r in page.regions if r.status == RegionStatus.DISPUTED)
    if page.status in _ANALYSED:
        page.status = PageStatus.DISPUTED if disputed else (
            PageStatus.ANALYZED if page.status == PageStatus.DISPUTED else page.status)
    uow.emit(Event(EventType.REGIONS_UPDATED, page.book.project or "", page.book_pk,
                   {"page_id": page.id, "index": page.index, "disputed": disputed}))
