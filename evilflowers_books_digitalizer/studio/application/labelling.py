"""Labelling mode: sample catalog items, serve them, store answers, export JSONL (#7).

Rules (studio.labelling holds the task registry):

* **No anchoring.** An item's ``hidden`` (band, p, decision, relation, an entry's current
  categories) and its ``stratum`` (which names the band) never leave this module: the
  item DTO carries ``payload`` only.
* **Second labeller.** While fewer than :data:`SECOND_LABELLER_SHARE` of the labelled items
  have a second non-skip answer, ``next`` offers an item someone else answered first.
* **Skip** is stored (that labeller is not asked again; the item stays open for others) and
  never exported as a label.
* **Orientation** of a section pair: A is the section the sample started from (the chapters
  link's source, or the section whose ``related`` list was read), B the other one. That is not
  necessarily the edge sweep's ``a_key``; ``pre`` and ``example`` are read in the item's
  orientation, which the export carries as ``section_a`` / ``section_b``.
* One answer per (item, task, labeller): answering again replaces the previous answer.
"""

from __future__ import annotations

import logging
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evilflowers_books_digitalizer.studio.application.errors import BadRequest, NotFound, Unavailable
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.application.uow import (
    Label,
    LabelItem,
    Session,
    func,
    select,
    utcnow,
)
from evilflowers_books_digitalizer.studio.labelling import (
    LABEL_HINTS,
    SECOND_LABELLER_SHARE,
    SKIP,
    TASKS,
    ItemKind,
    LabelTask,
    cohens_kappa,
    confidence_bucket,
    stratified,
    stratum,
    task,
)
from evilflowers_books_digitalizer.studio.ports import ImageBytes, LabelCandidate, PortError

log = logging.getLogger(__name__)

#: what a sample of each kind cannot see (the export summary carries it to knowledge#65)
SAMPLING_NOTES: dict[str, str] = {
    ItemKind.SECTION_PAIR: (
        "intra-book pairs from entries/{id}/chapters links; cross-book pairs from "
        "sections/{key}/related, which drops pairs the catalog banded hide and returns at most "
        "10 per section; section text = first paragraphs under the heading (no section text route); "
        "pairs without text on both sides are left out; orientation: A = the section the sample "
        "started from, not necessarily the edge sweep's a_key (see item.section_a/section_b)"),
    ItemKind.FIGURE: "regions of every band (keep, review, hide, none) of the entries' active runs",
    ItemKind.TABLE: "regions of every band (keep, review, hide, none) of the entries' active runs",
    ItemKind.ENTRY: "entries with an active extraction run",
}


# ---------------------------------------------------------------- DTOs
class LabelChoice(BaseModel):
    key: str
    hint: str | None = None


class TaskOut(BaseModel):
    name: str
    title: str
    item_kind: str
    labels: list[LabelChoice]
    companion_of: str | None
    target: int
    items: int                 # sampled items of the task's kind
    labelled: int              # non-skip labels
    skipped: int
    double: int                # items with two non-skip labellers
    mine: int = 0              # this labeller's answers (skips included)
    left_for_me: int = 0       # items this labeller has not answered


class LabelItemOut(BaseModel):
    """What a labeller sees: never the model's answer (no ``hidden``, no ``stratum``)."""

    id: int
    kind: str
    key: str
    language: str | None
    ref: dict[str, Any]
    payload: dict[str, Any]
    image_url: str | None = None
    #: this labeller's earlier answers on the item, task -> value
    mine: dict[str, str] = Field(default_factory=dict)


class LabelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str
    value: str = Field(min_length=1, max_length=64)


class LabelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labels: list[LabelIn] = Field(min_length=1, max_length=4)


class SampleOut(BaseModel):
    kind: str
    requested: int
    added: int
    already_sampled: int
    candidates: int
    strata: dict[str, int]


class SummaryOut(BaseModel):
    task: str
    target: int
    labels: int
    skipped: int
    done: bool
    by_label: dict[str, int]
    by_language: dict[str, int]
    labellers: dict[str, int]
    by_via: dict[str, int]
    double: int
    agreement: float | None
    kappa: float | None
    sampling: str


# ---------------------------------------------------------------- sampling
def sample(svc: Services, task_name: str, n: int, *, max_entries: int | None = None) -> SampleOut:
    """Add up to ``n`` new items for ``task_name``'s item kind, stratified by language × band."""
    t = _task(task_name)
    if n < 1:
        raise BadRequest("n must be at least 1")
    source = svc.label_source()
    if source is None:
        raise Unavailable("labelling is not configured ([labelling] base_url)")
    cfg = svc.config.labelling
    with svc.uow() as uow:
        known = set(uow.session.scalars(select(LabelItem.key).where(LabelItem.kind == t.item_kind)))
    by_key: dict[str, LabelCandidate] = {}
    groups: dict[str, list[str]] = defaultdict(list)
    seen = 0
    try:
        for cand in source.candidates(t.item_kind, max_entries=max_entries or cfg.max_entries):
            seen += 1
            if cand.key in known or cand.key in by_key:
                continue
            by_key[cand.key] = cand
            groups[stratum(cand.language, _band(cand))].append(cand.key)
        rng = random.Random(cfg.seed)
        for keys in groups.values():
            rng.shuffle(keys)
        chosen: list[LabelCandidate] = []
        for k in stratified(groups, len(by_key)):   # every candidate, in stratified order
            cand = source.complete(by_key[k])
            if _usable(cand):
                chosen.append(cand)
                if len(chosen) >= n:
                    break
    except PortError as exc:
        raise Unavailable(f"catalog: {exc}") from exc
    with svc.uow() as uow:   # a concurrent sample (CLI, SPA, MCP) may have added some meanwhile
        taken = set(uow.session.scalars(select(LabelItem.key).where(
            LabelItem.key.in_([c.key for c in chosen]))))
        chosen = [c for c in chosen if c.key not in taken]
        for c in chosen:
            uow.session.add(LabelItem(kind=str(c.kind), key=c.key, ref=dict(c.ref), language=c.language,
                                      stratum=stratum(c.language, _band(c)), payload=dict(c.payload),
                                      hidden=dict(c.hidden)))
    return SampleOut(kind=str(t.item_kind), requested=n, added=len(chosen),
                     already_sampled=len(known), candidates=seen,
                     strata=dict(Counter(stratum(c.language, _band(c)) for c in chosen)))


def sample_quietly(svc: Services, task_name: str, n: int) -> None:
    """:func:`sample` for a background run (API): the outcome goes to the log, not a caller."""
    try:
        out = sample(svc, task_name, n)
        log.info("labelling: sampled %s: %d added of %d candidates, strata %s",
                 task_name, out.added, out.candidates, out.strata)
    except Exception:   # a background task has nobody to raise to
        log.exception("labelling: sampling %s failed", task_name)


def task_or_404(name: str) -> LabelTask:
    return _task(name)


def require_labeller(labeller: str | None) -> str:
    return _require(labeller)


def _band(c: LabelCandidate) -> str | None:
    """Band, else the probability bucket (regions: ``p``; section pairs: ``relation_p``)."""
    return confidence_bucket(c.hidden.get("band"), c.hidden.get("p", c.hidden.get("relation_p")))


def _usable(c: LabelCandidate) -> bool:
    """A section pair needs text on both sides: a labeller cannot judge two headings."""
    if c.kind != ItemKind.SECTION_PAIR:
        return True
    sides = [c.payload.get(s) for s in ("a", "b")]
    return all(isinstance(x, dict) and str(x.get("text") or "").strip() for x in sides)


# ---------------------------------------------------------------- serving
def list_tasks(svc: Services, labeller: str | None = None) -> list[TaskOut]:
    with svc.uow() as uow:
        return [_task_out(uow.session, t, labeller) for t in TASKS.values()]


def _task_out(session: Session, t: LabelTask, labeller: str | None) -> TaskOut:
    items = session.scalar(select(func.count()).select_from(LabelItem)
                           .where(LabelItem.kind == t.item_kind)) or 0
    rows = session.execute(select(Label.item_pk, Label.value, Label.labeller)
                           .where(Label.task == t.name)).all()
    answered = [r for r in rows if r.value != SKIP]
    mine = sum(1 for r in rows if r.labeller == labeller) if labeller else 0
    return TaskOut(name=t.name, title=t.title, item_kind=str(t.item_kind),
                   labels=[LabelChoice(key=k, hint=LABEL_HINTS.get(k)) for k in t.labels],
                   companion_of=t.companion_of, target=t.target, items=items,
                   labelled=len(answered), skipped=len(rows) - len(answered),
                   double=sum(1 for c in Counter(r.item_pk for r in answered).values() if c >= 2),
                   mine=mine, left_for_me=max(0, items - mine))


def next_item(svc: Services, task_name: str, labeller: str | None) -> LabelItemOut | None:
    """The next item for this labeller, or None when they have answered every item."""
    t = _task(task_name)
    who = _require(labeller)
    with svc.uow() as uow:
        item = _pick(uow.session, t, who)
        return _item_out(item, who) if item else None


def _pick(session: Session, t: LabelTask, who: str) -> LabelItem | None:
    rows = session.execute(select(Label.item_pk, Label.value, Label.labeller)
                           .where(Label.task == t.name)).all()
    answered_by: dict[int, set[str]] = defaultdict(set)   # non-skip answers
    mine: set[int] = set()                                # this labeller's answers and skips
    for r in rows:
        if r.labeller == who:
            mine.add(r.item_pk)
        if r.value != SKIP:
            answered_by[r.item_pk].add(r.labeller)
    labelled = len(answered_by)
    double = sum(1 for s in answered_by.values() if len(s) >= 2)
    others_only = sorted(i for i, s in answered_by.items() if i not in mine and len(s) == 1)
    if others_only and double < math.ceil(SECOND_LABELLER_SHARE * labelled):
        return session.get(LabelItem, others_only[0])
    # fresh: nobody answered it yet (someone else's skip does not count: they were unsure)
    taken = set(answered_by) | mine
    q = select(LabelItem).where(LabelItem.kind == t.item_kind)
    if taken:
        q = q.where(LabelItem.id.not_in(taken))
    return session.scalars(q.order_by(LabelItem.id).limit(1)).first()


def get_item(svc: Services, item_id: int, labeller: str | None) -> LabelItemOut:
    with svc.uow() as uow:
        return _item_out(_get(uow.session, item_id), labeller)


#: the interfaces a label can come through (recorded on every label, exported as ``via``)
VIAS = ("spa", "cli", "mcp")


def submit(svc: Services, item_id: int, cmd: LabelsIn, labeller: str | None, *,
           via: str = "spa") -> LabelItemOut:
    """Record (or replace) this labeller's answers on one item."""
    who = _require(labeller)
    if via not in VIAS:
        raise BadRequest(f"via must be one of {', '.join(VIAS)}")
    with svc.uow() as uow:
        item = _get(uow.session, item_id)
        options = [str(o.get("id")) for o in item.payload.get("options", []) if isinstance(o, dict)]
        for lab in cmd.labels:
            t = _task(lab.task)
            if t.item_kind != item.kind:
                raise BadRequest(f"task {t.name} labels {t.item_kind} items, item {item.id} is {item.kind}")
            if not t.accepts(lab.value, options):
                allowed = ", ".join((*(t.labels or ("<a category id of the item>",)), SKIP))
                raise BadRequest(f"{lab.value!r} is not a {t.name} label (allowed: {allowed})")
            row = uow.session.scalars(select(Label).where(
                Label.item_pk == item.id, Label.task == t.name, Label.labeller == who)).first()
            if row is None:
                uow.session.add(Label(item=item, task=t.name, value=lab.value, labeller=who, via=via))
            else:
                row.value, row.via, row.labelled_at = lab.value, via, utcnow()
        uow.session.flush()
        return _item_out(item, who)


def item_image(svc: Services, item_id: int) -> ImageBytes:
    with svc.uow() as uow:
        item = _get(uow.session, item_id)
        kind, ref = item.kind, dict(item.ref)
    if kind not in (ItemKind.FIGURE, ItemKind.TABLE):
        raise NotFound(f"label item {item_id} has no image")
    source = svc.label_source()
    if source is None:
        raise Unavailable("labelling is not configured ([labelling] base_url)")
    try:
        return source.image(ref)
    except PortError as exc:
        raise Unavailable(f"catalog: {exc}") from exc


# ---------------------------------------------------------------- export
def export_lines(svc: Services, task_name: str) -> Iterator[dict[str, Any]]:
    """One dict per non-skip label (the JSONL line of #7), oldest first."""
    t = _task(task_name)
    with svc.uow() as uow:
        rows = uow.session.execute(
            select(Label, LabelItem).join(LabelItem, Label.item_pk == LabelItem.id)
            .where(Label.task == t.name, Label.value != SKIP)
            .order_by(Label.labelled_at, Label.id)).all()
        lines = [_line(t, label, item) for label, item in rows]
    yield from lines


def _line(t: LabelTask, label: Label, item: LabelItem) -> dict[str, Any]:
    line: dict[str, Any] = {"task": t.name, "item": dict(item.ref), "language": item.language,
                            "label": label.value, "labeller": label.labeller,
                            "labelled_at": _iso(label.labelled_at), "via": label.via}
    if t.name == "subject":   # the label is a category id; its UDC term travels with it
        term = next((o.get("term") for o in item.payload.get("options", [])
                     if isinstance(o, dict) and str(o.get("id")) == label.value), None)
        line["label_term"] = term
    return line


def summary(svc: Services, task_name: str) -> SummaryOut:
    t = _task(task_name)
    with svc.uow() as uow:
        rows = uow.session.execute(
            select(Label.item_pk, Label.value, Label.labeller, Label.via, Label.labelled_at,
                   LabelItem.language)
            .join(LabelItem, Label.item_pk == LabelItem.id).where(Label.task == t.name)
            .order_by(Label.labelled_at, Label.id)).all()
    answered = [r for r in rows if r.value != SKIP]
    per_item: dict[int, list[str]] = defaultdict(list)
    for r in answered:   # the first two labellers of an item, in answer order
        if len(per_item[r.item_pk]) < 2:
            per_item[r.item_pk].append(r.value)
    pairs: Sequence[tuple[str, str]] = [(v[0], v[1]) for v in per_item.values() if len(v) == 2]
    kappa = cohens_kappa(pairs)
    return SummaryOut(
        task=t.name, target=t.target, labels=len(answered), skipped=len(rows) - len(answered),
        done=len(answered) >= t.target,
        by_label=dict(Counter(r.value for r in answered).most_common()),
        by_language=dict(Counter(r.language or "??" for r in answered).most_common()),
        labellers=dict(Counter(r.labeller for r in answered).most_common()),
        by_via=dict(Counter(r.via for r in answered).most_common()),
        double=len(pairs),
        agreement=(sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
        kappa=round(kappa, 4) if kappa is not None else None,
        sampling=SAMPLING_NOTES[t.item_kind])


# ---------------------------------------------------------------- helpers
def _task(name: str) -> LabelTask:
    try:
        return task(name)
    except KeyError as exc:
        raise NotFound(str(exc.args[0])) from None


def _require(labeller: str | None) -> str:
    if not labeller:
        raise BadRequest("labelling needs the labeller's name (X-Operator header / --operator)")
    return labeller


def _get(session: Session, item_id: int) -> LabelItem:
    item = session.get(LabelItem, item_id)
    if item is None:
        raise NotFound(f"label item {item_id} not found")
    return item


def _item_out(item: LabelItem, labeller: str | None) -> LabelItemOut:
    has_image = item.kind in (ItemKind.FIGURE, ItemKind.TABLE)
    return LabelItemOut(
        id=item.id, kind=item.kind, key=item.key, language=item.language, ref=dict(item.ref),
        payload=dict(item.payload),
        image_url=f"/api/v1/labelling/items/{item.id}/image" if has_image else None,
        mine={lab.task: lab.value for lab in item.labels if lab.labeller == labeller} if labeller else {})


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"
