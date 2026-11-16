"""Book-wide scan-mark groups for the operator (#14): list, look, decide, split.

The orchestrator groups a book's marks after every clean (``stages.marks``); here an operator
can overrule a group's verdict (``keep`` = it is content, ``hide`` = an artefact) or split
occurrences off a group that lumped different marks together. Either way the group's source
becomes ``operator``, which the next regrouping leaves alone. The members' marks follow at
once; their regions follow at the next analyze of those leaves (a re-run of the pages).
Nothing is erased: erasing stays a confirmed ``remove`` region (``evilflowers_scan.erase``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evilflowers_books_digitalizer.studio.application.errors import BadRequest, NotFound
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.application.uow import Book, MarkGroup, Session, select
from evilflowers_books_digitalizer.studio.marks import ARTEFACTS


class MarkGroupOut(BaseModel):
    id: int
    category: str
    band: str
    decision: str | None
    p: float | None
    source: str
    decider_model: str | None
    leaves: int
    members: int
    crop_leaf: int | None
    crop_bbox: list[int] | None
    #: the leaves its occurrences are on
    on_leaves: list[int]
    image_url: str
    split_from: int | None


class MarkGroupPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    band: Literal["keep", "review", "hide"]


class MarkGroupSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    leaves: list[int] = Field(min_length=1)


def _members(book: Book, group_id: int) -> dict[int, list[int]]:
    """leaf -> indices into its ``pages.marks`` of the group's occurrences."""
    out: dict[int, list[int]] = {}
    for page in book.pages:
        idx = [i for i, row in enumerate(page.marks or []) if row.get("group") == group_id]
        if idx:
            out[page.index] = idx
    return out


def _out(group: MarkGroup, book: Book) -> MarkGroupOut:
    return MarkGroupOut(
        id=group.id, category=group.category, band=group.band, decision=group.decision,
        p=group.p, source=group.source, decider_model=group.decider_model, leaves=group.leaves,
        members=group.members, crop_leaf=group.crop_leaf, crop_bbox=group.crop_bbox,
        on_leaves=sorted(_members(book, group.id)), split_from=group.split_from,
        image_url=f"/api/v1/mark-groups/{group.id}/image")


def _load(session: Session, group_id: int) -> tuple[MarkGroup, Book]:
    group = session.get(MarkGroup, group_id)
    if group is None:
        raise NotFound(f"mark group {group_id} not found")
    book = session.get(Book, group.book_pk)
    assert book is not None
    return group, book


def _write_members(book: Book, members: dict[int, list[int]], fields: dict[str, object]) -> None:
    for page in book.pages:
        if page.index in members:
            rows = [dict(r) for r in page.marks or []]
            for i in members[page.index]:
                rows[i].update(fields)
            page.marks = rows


def list_groups(svc: Services, book_id: int) -> list[MarkGroupOut]:
    with svc.uow() as uow:
        book = uow.session.get(Book, book_id)
        if book is None:
            raise NotFound(f"book {book_id} not found")
        groups = uow.session.scalars(select(MarkGroup).where(MarkGroup.book_pk == book_id)
                                     .order_by(MarkGroup.leaves.desc(), MarkGroup.id)).all()
        return [_out(g, book) for g in groups]


def decide(svc: Services, group_id: int, body: MarkGroupPatch, who: str | None) -> MarkGroupOut:
    """The operator's verdict on every occurrence of the mark."""
    with svc.uow() as uow:
        group, book = _load(uow.session, group_id)
        group.band, group.source, group.decider_error = body.band, "operator", None
        group.decision = {"keep": "content", "hide": group.category}.get(body.band)
        group.p, group.decider_model = None, f"operator:{who or '?'}"
        fields: dict[str, object] = {"band": group.band, "decision": group.decision,
                                     "model": group.decider_model}
        if group.decision in ARTEFACTS:
            fields["category"] = group.decision
        _write_members(book, _members(book, group.id), fields)
        return _out(group, book)


def split(svc: Services, group_id: int, body: MarkGroupSplit, who: str | None) -> MarkGroupOut:
    """Move the occurrences on ``leaves`` into a new group of their own (verdict ``review``)."""
    with svc.uow() as uow:
        group, book = _load(uow.session, group_id)
        members = _members(book, group.id)
        moved = {leaf: idx for leaf, idx in members.items() if leaf in set(body.leaves)}
        if not moved:
            raise BadRequest(f"mark group {group_id} has no occurrence on leaves {body.leaves}")
        if len(moved) == len(members):
            raise BadRequest("a split must leave at least one occurrence in the group")
        pages = {p.index: p for p in book.pages}
        first_leaf = min(moved)
        first = (pages[first_leaf].marks or [])[moved[first_leaf][0]]
        signals = first.get("signals") or {}
        new = MarkGroup(book_pk=book.id, category=str(first.get("category", group.category)),
                        dhash=str(signals.get("dhash") or group.dhash),
                        aspect=float(signals.get("aspect") or group.aspect),
                        leaves=len(moved), members=sum(len(i) for i in moved.values()),
                        band="review", source="operator", split_from=group.id,
                        decider_model=f"operator:{who or '?'}", crop_leaf=first_leaf,
                        crop_bbox=list(first.get("bbox") or []))
        uow.session.add(new)
        uow.session.flush()
        _write_members(book, moved, {"group": new.id, "band": "review", "decision": None,
                                     "model": new.decider_model})
        rest = {leaf: idx for leaf, idx in members.items() if leaf not in moved}
        group.leaves, group.members = len(rest), sum(len(i) for i in rest.values())
        if group.crop_leaf in moved:
            leaf = min(rest)
            group.crop_leaf = leaf
            group.crop_bbox = list((pages[leaf].marks or [])[rest[leaf][0]].get("bbox") or [])
        return _out(new, book)


def image(svc: Services, group_id: int) -> bytes:
    from evilflowers_books_digitalizer.studio.adapters.imaging.crops import crop_png

    with svc.uow() as uow:
        group, book = _load(uow.session, group_id)
        page = next((p for p in book.pages if p.index == group.crop_leaf), None)
        path, bbox = (page.clean_path if page else None), group.crop_bbox
    if not path or not bbox or not Path(path).is_file():
        raise NotFound(f"mark group {group_id} has no crop")
    return crop_png(Path(path), tuple(bbox), max_side=640)  # type: ignore[arg-type]
