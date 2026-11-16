"""``clean/marks`` (``digitalizer.local.clean_marks``): scan marks grouped across the book (#14).

Runs once after every clean job of a run is applied (``TAIL_PARTS``), before analyze fuses the
marks into regions. Three short steps, no transaction open during the network call:

1. group every mark of the book (:mod:`studio.marks`), give a group on enough leaves its rule
   verdict, and pick the groups that still need decider-vision (uncertain, never asked or the
   last ask failed, no operator verdict);
2. ask decider-vision once per such group, with its clearest occurrence;
3. write the verdicts to the groups and to every member mark.

Fail soft: without ``[decider] vision_url`` (or ``DECIDER_ENABLED=false``), or when a call fails,
the group keeps its members' bands (``review`` = a disputed region) and is asked again on the
next run. The run never fails on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from evilflowers_books_digitalizer.db.models import Book, MarkGroup, Page
from evilflowers_books_digitalizer.studio import marks as m
from evilflowers_books_digitalizer.studio.adapters.imaging.crops import crop_png
from evilflowers_books_digitalizer.studio.config import MarksConfig
from evilflowers_books_digitalizer.studio.ports import Decision, PortError
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext

logger = logging.getLogger(__name__)

#: sources whose verdict is a group's own (``signals`` = the members keep scan-service's bands)
OWN_VERDICT = ("rule", "decider", "operator")


@dataclass(frozen=True, slots=True)
class Ask:
    group_id: int
    image: Path
    bbox: tuple[int, int, int, int]
    category: str


@dataclass
class Tally:
    marks: int = 0
    groups: int = 0
    repeated: int = 0
    asked: int = 0
    failed: int = 0
    saved: int = 0


# ---------------------------------------------------------------- step 1: group (pure rows)
def _keys(groups: list[MarkGroup]) -> list[m.GroupKey]:
    return [m.GroupKey(g.id, g.category, m.parse_hash(g.dhash) or 0, g.aspect) for g in groups]


def _set_marks(page: Page, updates: dict[int, dict[str, object]]) -> None:
    """Rewrite ``page.marks`` (a new list: the JSON column sees the change)."""
    rows = [dict(r) for r in page.marks or []]
    for index, fields in updates.items():
        rows[index].update(fields)
    page.marks = rows


def _verdict_fields(group: MarkGroup) -> dict[str, object]:
    fields: dict[str, object] = {"band": group.band, "decision": group.decision,
                                 "model": group.decider_model or group.source}
    if group.p is not None:
        fields["p"] = group.p
    if group.source in ("decider", "operator") and group.decision in m.ARTEFACTS:
        fields["category"] = group.decision
    return fields


def regroup(uow: Uow, book: Book, cfg: MarksConfig, *, can_ask: bool) -> tuple[list[Ask], Tally]:
    """Step 1 on the rows: groups, rule verdicts, members' marks; returns what to ask."""
    tally = Tally()
    pages = {p.index: p for p in book.pages}
    refs = [r for p in book.pages for i, row in enumerate(p.marks or [])
            if (r := m.ref_of(p.index, i, row)) is not None]
    tally.marks = sum(len(p.marks or []) for p in book.pages)
    existing = list(uow.session.scalars(select(MarkGroup).where(MarkGroup.book_pk == book.id)))
    rows = {g.id: g for g in existing}
    asks: list[Ask] = []
    updates: dict[int, dict[int, dict[str, object]]] = {}
    for key in m.assign(refs, _keys(existing), cfg):
        if not key.members:
            if key.id is not None:
                uow.session.delete(rows[key.id])       # every member was re-cleaned away
            continue
        group = rows.get(key.id) if key.id is not None else None
        if group is None:
            group = MarkGroup(book_pk=book.id, category=key.category,
                              dhash=f"{key.dhash:016x}", aspect=key.aspect)
            uow.session.add(group)
            uow.session.flush()                         # its id goes into the members' marks
        best = m.clearest(key.members)
        group.members, group.leaves = len(key.members), m.leaves_of(key.members)
        group.crop_leaf = best.leaf
        group.crop_bbox = list((pages[best.leaf].marks or [])[best.index]["bbox"])
        if group.source != "operator" and m.repetition_hides(key.members, cfg):
            group.band, group.decision, group.source = "hide", m.REPEATED, "rule"
            group.p, group.decider_model = max(r.p for r in key.members), None
            tally.repeated += 1
        elif group.source == "signals":
            group.band, group.p = best.band, best.p
            if can_ask and m.is_uncertain(key.members):
                asks.append(Ask(group.id, Path(pages[best.leaf].clean_path or ""),
                                tuple(group.crop_bbox), group.category))  # type: ignore[arg-type]
        elif group.source == "decider" and group.decider_error and can_ask:
            asks.append(Ask(group.id, Path(pages[best.leaf].clean_path or ""),
                            tuple(group.crop_bbox), group.category))  # type: ignore[arg-type]
        fields: dict[str, object] = {"group": group.id}
        if group.source in OWN_VERDICT and not group.decider_error:
            fields |= _verdict_fields(group)
        for r in key.members:
            updates.setdefault(r.leaf, {})[r.index] = fields
        if group.source in ("rule", "decider") and not group.decider_error:
            tally.saved += max(0, group.members - (1 if group.source == "decider" else 0))
        tally.groups += 1
    for leaf, per_page in updates.items():
        _set_marks(pages[leaf], per_page)
    return asks, tally


# ---------------------------------------------------------------- step 3: verdicts
def apply_verdict(uow: Uow, book: Book, cfg: MarksConfig, group_id: int,
                  decision: Decision | None, error: str | None) -> None:
    group = uow.session.get(MarkGroup, group_id)
    if group is None or group.book_pk != book.id or group.source in ("operator", "rule"):
        return                     # split, regrouped or decided meanwhile
    if decision is None:
        group.source, group.decider_error = "decider", (error or "no answer")[:500]
        return
    group.source, group.decider_error = "decider", None
    group.decision, group.p, group.decider_model = decision.choice, decision.p, decision.model
    group.band = m.band_of(decision.choice, decision.p, cfg)
    fields = {"group": group.id, **_verdict_fields(group)}
    for page in book.pages:
        hit = {i: fields for i, row in enumerate(page.marks or []) if row.get("group") == group.id}
        if hit:
            _set_marks(page, hit)


# ---------------------------------------------------------------- the job body
def group_marks(ctx: WorkerContext, run_id: int) -> None:
    cfg = ctx.config.marks
    vision = ctx.decider("vision")
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        asks, tally = regroup(uow, run.book, cfg, can_ask=vision is not None)
    answers: list[tuple[int, Decision | None, str | None]] = []
    for ask in asks:
        if vision is None:
            break
        try:
            verdict = vision.choose(m.SCAN_MARK, m.SCAN_MARK_INSTRUCTIONS, m.SCAN_MARK_CRITERIA,
                                    state=f"Page region, signals: {ask.category}.",
                                    images=[crop_png(ask.image, ask.bbox)])
            answers.append((ask.group_id, verdict, None))
            tally.asked += 1
        except PortError as exc:
            # the service is down or slow: stop here; the other groups stay unasked and the
            # next run asks them (a dead service must not cost one timeout per group)
            logger.warning("decider-vision: group %s not judged, stopping: %s", ask.group_id, exc)
            answers.append((ask.group_id, None, str(exc)))
            tally.failed += 1
            break
        except (OSError, ValueError) as exc:          # this crop only (unreadable page)
            logger.warning("decider-vision: group %s not judged: %s", ask.group_id, exc)
            answers.append((ask.group_id, None, str(exc)))
            tally.failed += 1
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        for group_id, decision, error in answers:
            apply_verdict(uow, run.book, cfg, group_id, decision, error)
        if answers:          # the verdicts reached the members: count the calls they saved
            members = {g.id: g.members for g in uow.session.scalars(
                select(MarkGroup).where(MarkGroup.id.in_([a[0] for a in answers])))}
            tally.saved += sum(members.get(gid, 1) - 1 for gid, d, _ in answers if d)
        runs.log(uow, run, f"marks: {tally.marks} on the book, {tally.groups} group(s), "
                           f"{tally.repeated} repeated, decider-vision asked {tally.asked} "
                           f"({tally.failed} failed), {tally.saved} call(s) saved")
