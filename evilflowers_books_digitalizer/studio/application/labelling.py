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
* **Provenance** (#16). Every label records ``labeller_kind`` (``human`` or ``agent``) and, for
  an agent, its ``model``. Agents label only through :func:`claim` and :func:`submit_agent`.
  A claim leases items, so parallel agents never get the same item. People keep
  :func:`next_item`. Until :data:`AGENT_SPOT_CHECK_SHARE` of agent-labelled items also have a
  human answer, ``next`` offers a person an item an agent answered (the spot check). The
  summary reports agent<->human and human<->human agreement separately.
"""

from __future__ import annotations

import logging
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evilflowers_books_digitalizer.studio.application.errors import BadRequest, NotFound, Unavailable
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.application.uow import (
    Book,
    IntegrityError,
    Label,
    LabelItem,
    LabelLease,
    MarkGroup,
    Session,
    delete,
    func,
    select,
    utcnow,
)
from evilflowers_books_digitalizer.studio.labelling import (
    AGENT,
    AGENT_KAPPA_MIN,
    AGENT_SPOT_CHECK_SHARE,
    HUMAN,
    LABEL_HINTS,
    LABELLER_KINDS,
    SECOND_LABELLER_SHARE,
    SKIP,
    SLOVAK_GLOSSARY,
    STUDIO_KINDS,
    TASKS,
    ItemKind,
    LabelTask,
    agent_guide,
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
    ItemKind.MARK_GROUP: (
        "the studio's book-wide scan-mark groups (#14), one crop per group (its clearest "
        "occurrence); stratified by language x band (rule-hidden repeated groups included)"),
    ItemKind.BOOK: (
        "every metadata form an operator confirmed after a title-page prefill (#15): one line "
        "per suggested field; label = accepted (the suggestion stood), changed (the operator "
        "typed another value) or none (the field was left empty)"),
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


class AgreementOut(BaseModel):
    """Agreement of two raters on the items both answered (non-skip)."""

    pairs: int = 0
    agreement: float | None = None
    kappa: float | None = None


class PairStatsOut(AgreementOut):
    #: the same per item language (Slovak reported on its own, #16)
    by_language: dict[str, AgreementOut] = Field(default_factory=dict)
    #: first rater's answer -> second rater's answer -> count (agent_human: human -> agent)
    confusion: dict[str, dict[str, int]] = Field(default_factory=dict)


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
    #: non-skip labels per labeller kind (human, agent) and per agent model (#16)
    by_labeller_kind: dict[str, int] = Field(default_factory=dict)
    by_model: dict[str, int] = Field(default_factory=dict)
    #: human<->human (the first two people of an item); kept for the screen and older readers
    double: int
    agreement: float | None
    kappa: float | None
    human_human: PairStatsOut = Field(default_factory=PairStatsOut)
    #: the spot check: an item's first human answer against its first agent answer
    agent_human: PairStatsOut = Field(default_factory=PairStatsOut)
    agent_items: int = 0                  # items with a non-skip agent label
    spot_checked: int = 0                 # ... of which a person also answered
    spot_check_target: int = 0            # ceil(AGENT_SPOT_CHECK_SHARE * agent_items)
    agent_kappa_min: float = AGENT_KAPPA_MIN
    #: agent_human.kappa >= agent_kappa_min (None: no kappa yet); knowledge#65 decides
    agent_trusted: bool | None = None
    sampling: str


class AgentLabelIn(BaseModel):
    """One answer of an agent: ``value`` is a label key of ``task`` or ``skip``."""

    model_config = ConfigDict(extra="forbid")
    item_id: int
    task: str
    value: str = Field(min_length=1, max_length=64)


class AgentItemOut(BaseModel):
    """What an agent labels from: the payload only, never the model's answer or other labels."""

    id: int
    kind: str
    language: str | None
    payload: dict[str, Any]
    #: the crop sent with the item: {"width", "height", "original_width", "original_height",
    #: "media_type"}; None for text items (or when the crop could not be fetched: see error)
    image: dict[str, Any] | None = None
    image_error: str | None = None


class ClaimOut(BaseModel):
    task: str
    labeller: str
    model: str
    lease_expires_at: str | None
    guide: dict[str, Any]
    items: list[AgentItemOut]
    #: subject: the options once per catalog (an item's payload.options_catalog names its list)
    options_by_catalog: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    left: int                             # unanswered items not leased to anyone after this claim
    note: str | None = None


class AgentSubmitOut(BaseModel):
    recorded: list[dict[str, Any]]        # {item_id, task, value}
    rejected: list[dict[str, Any]]        # {item_id, task, value, error}
    released: int                         # leases released


# ---------------------------------------------------------------- sampling
def sample(svc: Services, task_name: str, n: int, *, max_entries: int | None = None) -> SampleOut:
    """Add up to ``n`` new items for ``task_name``'s item kind, stratified by language × band."""
    t = _task(task_name)
    if n < 1:
        raise BadRequest("n must be at least 1")
    if not t.by_hand:
        raise BadRequest(f"{t.name} is not sampled: its cases come from confirmed metadata forms")
    source = _StudioSource(svc) if t.item_kind in STUDIO_KINDS else svc.label_source()
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


class _StudioSource:
    """Candidates from the studio's own rows: mark groups (#14). The group's verdict (band,
    decision, p, category) is ``hidden``: a labeller sees the crop and where it is, never what
    the rule or the decider said."""

    def __init__(self, svc: Services) -> None:
        self._svc = svc

    def candidates(self, kind: str, *, max_entries: int) -> Iterator[LabelCandidate]:
        with self._svc.uow() as uow:
            rows = uow.session.execute(select(MarkGroup, Book).join(Book, MarkGroup.book_pk == Book.id)
                                       .where(MarkGroup.crop_leaf.is_not(None))
                                       .order_by(MarkGroup.id)).all()
            found = [self._candidate(group, book) for group, book in rows]
        yield from found

    @staticmethod
    def _candidate(group: MarkGroup, book: Book) -> LabelCandidate:
        langs = (book.item_metadata or {}).get("language") or []
        language = {"slo": "sk", "cze": "cs", "eng": "en", "ger": "de", "hun": "hu"}.get(
            langs[0] if langs else "", langs[0] if langs else None)
        return LabelCandidate(
            kind=ItemKind.MARK_GROUP, key=f"mark_group:{group.id}",
            ref={"book_id": book.id, "item_id": book.item_id, "group_id": group.id,
                 "leaf": group.crop_leaf, "bbox": list(group.crop_bbox or [])},
            language=language,
            payload={"title": book.title, "leaf": group.crop_leaf, "leaves": group.leaves,
                     "occurrences": group.members},
            hidden={"band": group.band, "decision": group.decision, "p": group.p,
                    "source": group.source, "category": group.category})

    def complete(self, candidate: LabelCandidate) -> LabelCandidate:
        return candidate

    def image(self, ref: Any) -> ImageBytes:
        from evilflowers_books_digitalizer.studio.adapters.imaging.crops import crop_png

        with self._svc.uow() as uow:
            book = uow.session.get(Book, int(ref["book_id"]))
            page = next((p for p in (book.pages if book else []) if p.index == ref.get("leaf")),
                        None)
            image = page.clean_path if page else None
        if not image:
            raise PortError(f"mark group {ref.get('group_id')}: its page has no cleaned image")
        try:
            return ImageBytes(crop_png(Path(image), tuple(ref["bbox"]), max_side=640),
                              "image/png")
        except (OSError, ValueError) as exc:
            raise PortError(f"mark group {ref.get('group_id')}: {exc}") from exc


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
    if not t.by_hand:
        return _form_task_out(session, t)
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


def _form_task_out(session: Session, t: LabelTask) -> TaskOut:
    """``title_page``: cases are fields of confirmed metadata forms, nothing to answer here."""
    books = session.scalars(select(Book).where(Book.metadata_suggestions.is_not(None))).all()
    rows = [r for b in books for r in _form_rows(b)]
    return TaskOut(name=t.name, title=t.title, item_kind=str(t.item_kind),
                   labels=[LabelChoice(key=k, hint=None) for k in t.labels],
                   companion_of=None, target=t.target, items=len(books),
                   labelled=len(rows), skipped=0, double=0)


def next_item(svc: Services, task_name: str, labeller: str | None) -> LabelItemOut | None:
    """The next item for this labeller, or None when they have answered every item."""
    t = _task(task_name)
    if not t.by_hand:
        raise BadRequest(f"{t.name} is labelled by confirming a book's metadata form, not here")
    who = _require(labeller)
    with svc.uow() as uow:
        item = _pick(uow.session, t, who)
        return _item_out(item, who) if item else None


def _pick(session: Session, t: LabelTask, who: str) -> LabelItem | None:
    """A person's next item: a spot check of an agent label, else a second-labeller item,
    else a fresh one that nobody (person or agent) answered or holds a lease on."""
    rows = session.execute(select(Label.item_pk, Label.value, Label.labeller, Label.labeller_kind)
                           .where(Label.task == t.name)).all()
    humans: dict[int, set[str]] = defaultdict(set)        # non-skip answers of people
    agents: dict[int, set[str]] = defaultdict(set)        # non-skip answers of agents
    mine: set[int] = set()                                # this labeller's answers and skips
    for r in rows:
        if r.labeller == who:
            mine.add(r.item_pk)
        if r.value != SKIP:
            (agents if r.labeller_kind == AGENT else humans)[r.item_pk].add(r.labeller)
    # 1. the spot check: an agent answered it, no person yet (Slovak and the rest kept even)
    checked = [i for i in agents if humans.get(i)]
    spot = [i for i, s in agents.items() if len(s) == 1 and not humans.get(i) and i not in mine]
    if spot and len(checked) < math.ceil(AGENT_SPOT_CHECK_SHARE * len(agents)):
        langs = dict(session.execute(select(LabelItem.id, LabelItem.language)
                                     .where(LabelItem.id.in_([*spot, *checked]))).tuples().all())
        done_per_lang = Counter(langs.get(i) for i in checked)
        return session.get(LabelItem, min(spot, key=lambda i: (done_per_lang[langs.get(i)], i)))
    # 2. a second person on an item one person answered (human<->human kappa)
    double = sum(1 for s in humans.values() if len(s) >= 2)
    others_only = sorted(i for i, s in humans.items() if i not in mine and len(s) == 1)
    if others_only and double < math.ceil(SECOND_LABELLER_SHARE * len(humans)):
        return session.get(LabelItem, others_only[0])
    # 3. fresh: nobody answered it yet (someone else's skip does not count: they were unsure),
    #    and no agent holds it right now
    taken = set(humans) | set(agents) | mine | _leased(session, t.name)
    q = select(LabelItem).where(LabelItem.kind == t.item_kind)
    if taken:
        q = q.where(LabelItem.id.not_in(taken))
    return session.scalars(q.order_by(LabelItem.id).limit(1)).first()


def _leased(session: Session, task_name: str, *, now: datetime | None = None) -> set[int]:
    """Items an agent holds a live lease on for ``task_name``."""
    return set(session.scalars(select(LabelLease.item_pk).where(
        LabelLease.task == task_name, LabelLease.expires_at > (now or utcnow()))))


def get_item(svc: Services, item_id: int, labeller: str | None) -> LabelItemOut:
    with svc.uow() as uow:
        return _item_out(_get(uow.session, item_id), labeller)


#: the interfaces a label can come through (recorded on every label, exported as ``via``)
VIAS = ("spa", "cli", "mcp")


def submit(svc: Services, item_id: int, cmd: LabelsIn, labeller: str | None, *,
           via: str = "spa") -> LabelItemOut:
    """Record (or replace) a PERSON's answers on one item (agents use :func:`submit_agent`)."""
    who = _require(labeller)
    if via not in VIAS:
        raise BadRequest(f"via must be one of {', '.join(VIAS)}")
    with svc.uow() as uow:
        item = _get(uow.session, item_id)
        _record(uow.session, item, cmd.labels, who, via=via, kind=HUMAN, model=None)
        uow.session.flush()
        return _item_out(item, who)


def _record(session: Session, item: LabelItem, answers: Sequence[LabelIn | AgentLabelIn], who: str,
            *, via: str, kind: str, model: str | None) -> None:
    """Validate and store answers on one item: one row per (item, task, labeller), replaced on
    a new answer. A labeller name keeps its kind: a person's row is never overwritten as an
    agent's, or the reverse."""
    if kind not in LABELLER_KINDS:
        raise BadRequest(f"labeller_kind must be one of {', '.join(LABELLER_KINDS)}")
    options = [str(o.get("id")) for o in item.payload.get("options", []) if isinstance(o, dict)]
    for lab in answers:
        t = _task(lab.task)
        if t.item_kind != item.kind:
            raise BadRequest(f"task {t.name} labels {t.item_kind} items, item {item.id} is {item.kind}")
        if not t.accepts(lab.value, options):
            allowed = ", ".join((*(t.labels or ("<a category id of the item>",)), SKIP))
            raise BadRequest(f"{lab.value!r} is not a {t.name} label (allowed: {allowed})")
        row = session.scalars(select(Label).where(
            Label.item_pk == item.id, Label.task == t.name, Label.labeller == who)).first()
        if row is None:
            session.add(Label(item=item, task=t.name, value=lab.value, labeller=who, via=via,
                              labeller_kind=kind, model=model))
        elif row.labeller_kind != kind:
            raise BadRequest(f"{who!r} already answered item {item.id} as a {row.labeller_kind} "
                             f"labeller; use another labeller name")
        else:
            row.value, row.via, row.model, row.labelled_at = lab.value, via, model, utcnow()


# ---------------------------------------------------------------- agent labellers (#16)
#: items one claim returns at most, per item kind (MCP output budget: a crop costs ~1k tokens
#: at 1024 px, a section pair ~1k, a subject entry ~0.5k plus its catalog's options once)
CLAIM_MAX: dict[str, int] = {ItemKind.FIGURE: 10, ItemKind.TABLE: 8, ItemKind.MARK_GROUP: 10,
                             ItemKind.SECTION_PAIR: 12, ItemKind.ENTRY: 15}
#: answers one submit_agent call takes at most
SUBMIT_MAX = 100


def claim(svc: Services, task_name: str, n: int, labeller: str | None, model: str | None
          ) -> ClaimOut:
    """Lease up to ``n`` items of ``task_name`` that nobody answered (non-skip), this labeller
    did not answer or skip, and nobody holds, to one agent labeller for
    ``[labelling] agent_lease_minutes``. Each lease is an insert under a unique (item, task):
    two agents that claim at the same moment cannot both win an item."""
    t = _task(task_name)
    if not t.by_hand:
        raise BadRequest(f"{t.name} is labelled by confirming a book's metadata form, not here")
    if t.companion_of:
        raise BadRequest(f"{t.name} is answered together with {t.companion_of}: claim "
                         f"{t.companion_of} items and submit both answers")
    who, mdl = _require(labeller), _require_model(model)
    cap = CLAIM_MAX.get(t.item_kind, 10)
    if n < 1:
        raise BadRequest("n must be at least 1")
    n = min(n, cap)
    now = utcnow()
    expires = now + timedelta(minutes=svc.config.labelling.agent_lease_minutes)
    got: list[int] = []
    with svc.uow() as uow:
        s = uow.session
        s.execute(delete(LabelLease).where(LabelLease.expires_at <= now))
        rows = s.execute(select(Label.item_pk, Label.value, Label.labeller)
                         .where(Label.task == t.name)).all()
        taken = {r.item_pk for r in rows if r.value != SKIP or r.labeller == who}
        taken |= _leased(s, t.name, now=now)
        q = select(LabelItem.id).where(LabelItem.kind == t.item_kind)
        if taken:
            q = q.where(LabelItem.id.not_in(taken))
        candidates = list(s.scalars(q.order_by(LabelItem.id)))   # sample order: strata interleaved
        for item_id in candidates:
            try:
                with s.begin_nested():
                    s.add(LabelLease(item_pk=item_id, task=t.name, labeller=who, model=mdl,
                                     leased_at=now, expires_at=expires))
            except IntegrityError:   # a parallel claim took it first
                continue
            got.append(item_id)
            if len(got) >= n:
                break
        items = [s.get(LabelItem, i) for i in got]
        snapshot = [(i.id, i.kind, i.language, dict(i.payload), dict(i.ref)) for i in items if i]
    left = max(0, len(candidates) - len(got))
    out_items, options = _agent_items(snapshot)
    note = None if got else (
        "Nothing to claim: every item is answered, skipped by you or leased to another agent. "
        "Sample more with sample_label_items, or wait for leases to expire.")
    return ClaimOut(task=t.name, labeller=who, model=mdl,
                    lease_expires_at=_iso(expires) if got else None,
                    guide=_guide_for_agents(t), items=out_items, options_by_catalog=options,
                    left=left, note=note)


def _guide_for_agents(t: LabelTask) -> dict[str, Any]:
    guide = agent_guide(t.name)
    companion = guide.get("companion")
    if isinstance(companion, str):
        guide["companion_guide"] = agent_guide(companion)
    if t.item_kind in (ItemKind.FIGURE, ItemKind.TABLE, ItemKind.SECTION_PAIR, ItemKind.ENTRY,
                       ItemKind.MARK_GROUP):
        guide["slovak_glossary"] = SLOVAK_GLOSSARY
    return guide


def _agent_items(snapshot: list[tuple[int, str, str | None, dict[str, Any], dict[str, Any]]]
                 ) -> tuple[list[AgentItemOut], dict[str, list[dict[str, Any]]]]:
    """Payloads for agents; a subject item's options move to one list per catalog."""
    out: list[AgentItemOut] = []
    options: dict[str, list[dict[str, Any]]] = {}
    for item_id, kind, language, payload, ref in snapshot:
        if "options" in payload:
            catalog = str(ref.get("catalog_id") or "default")
            options.setdefault(catalog, payload.pop("options") or [])
            payload["options_catalog"] = catalog
        out.append(AgentItemOut(id=item_id, kind=kind, language=language, payload=payload))
    return out, options


def agent_image(svc: Services, item_id: int, kind: str) -> tuple[ImageBytes, dict[str, Any]]:
    """The item's crop for a vision model: at most ``agent_image_max_side`` px on the long edge
    (``agent_table_image_max_side`` for tables, read cell by cell). The vision docs: images
    are downscaled anyway, and under ~200 px a model misreads more, so the sizes travel with
    the item."""
    from evilflowers_books_digitalizer.studio.adapters.imaging.crops import fit_image

    cfg = svc.config.labelling
    side = cfg.agent_table_image_max_side if kind == ItemKind.TABLE else cfg.agent_image_max_side
    img = item_image(svc, item_id)
    try:
        content, media, (w, h), (ow, oh) = fit_image(img.content, side)
    except (OSError, ValueError):   # not decodable here: send it as the catalog served it
        return img, {"media_type": img.media_type}
    return ImageBytes(content, media), {"width": w, "height": h, "original_width": ow,
                                        "original_height": oh, "media_type": media}


#: estimated tokens of an image whose size is unknown (a 1092 x 1092 crop, the vision docs)
_UNKNOWN_IMAGE_TOKENS = 1600
#: image bytes one claim result may carry: Claude Code fails an HTTP MCP response over
#: 16 MiB, and base64 grows the bytes by a third
_CLAIM_IMAGE_BYTES = 8 * 1024 * 1024


def _image_tokens(info: dict[str, Any]) -> int:
    """Claude's visual tokens: one per 28 x 28 patch (platform docs, vision)."""
    w, h = info.get("width"), info.get("height")
    if isinstance(w, int) and isinstance(h, int):
        return math.ceil(w / 28) * math.ceil(h / 28)
    return _UNKNOWN_IMAGE_TOKENS


def _text_tokens(text: str) -> int:
    return math.ceil(len(text) / 3)   # conservative for Slovak and JSON


def claim_with_images(svc: Services, task_name: str, n: int, labeller: str | None,
                      model: str | None) -> tuple[ClaimOut, dict[int, ImageBytes]]:
    """:func:`claim` plus the crops, cut to ``[labelling] agent_claim_token_budget``: a result
    over the client's MCP output limit is dropped by the client, so items that do not fit are
    released at once instead of sitting leased (the first item is always kept)."""
    import json

    out = claim(svc, task_name, n, labeller, model)
    budget = svc.config.labelling.agent_claim_token_budget
    used = _text_tokens(json.dumps(out.model_dump(exclude={"items"}), ensure_ascii=False))
    images: dict[int, ImageBytes] = {}
    kept: list[AgentItemOut] = []
    image_bytes = 0
    for item in out.items:
        cost, size = 0, 0
        if item.kind in IMAGE_KINDS:
            try:
                img, info = agent_image(svc, item.id, item.kind)
            except (NotFound, Unavailable) as exc:
                item.image_error = f"{exc}: answer skip unless the text settles it"
            else:
                item.image, images[item.id] = info, img
                cost, size = _image_tokens(info), len(img.content)
        cost += _text_tokens(item.model_dump_json())
        if kept and (used + cost > budget or image_bytes + size > _CLAIM_IMAGE_BYTES):
            break
        kept.append(item)
        used, image_bytes = used + cost, image_bytes + size
    cut = [i.id for i in out.items[len(kept):]]
    if cut:
        release(svc, out.labeller, out.task, item_ids=cut)
        for i in cut:
            images.pop(i, None)
        out.left += len(cut)
        out.note = (f"{len(cut)} more item(s) did not fit this result's size budget and were "
                    "released: claim again for them.")
    out.items = kept
    return out, images


def submit_agent(svc: Services, labeller: str | None, model: str | None,
                 answers: Sequence[AgentLabelIn]) -> AgentSubmitOut:
    """Record an agent's answers (``labeller_kind=agent``, ``model``) and release its leases.
    Each item is its own transaction: one bad answer rejects that item, not the batch. An item
    another labeller holds a live lease on is refused."""
    who, mdl = _require(labeller), _require_model(model)
    if not answers:
        raise BadRequest("no labels")
    if len(answers) > SUBMIT_MAX:
        raise BadRequest(f"at most {SUBMIT_MAX} labels per call")
    by_item: dict[int, list[AgentLabelIn]] = defaultdict(list)
    for a in answers:
        by_item[a.item_id].append(a)
    recorded: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    released = 0
    for item_id, group in by_item.items():
        try:
            with svc.uow() as uow:
                s = uow.session
                item = _get(s, item_id)
                tasks = {a.task for a in group}
                holders = s.execute(select(LabelLease.labeller, LabelLease.expires_at).where(
                    LabelLease.item_pk == item_id, LabelLease.task.in_(tasks),
                    LabelLease.expires_at > utcnow())).all()
                other = next((h for h in holders if h.labeller != who), None)
                if other is not None:
                    raise BadRequest(f"item {item_id} is leased to {other.labeller!r} until "
                                     f"{_iso(other.expires_at)}")
                _record(s, item, group, who, via="mcp", kind=AGENT, model=mdl)
                gone = s.execute(delete(LabelLease).where(LabelLease.item_pk == item_id,
                                                          LabelLease.labeller == who))
                released += int(getattr(gone, "rowcount", 0) or 0)
        except (BadRequest, NotFound) as exc:
            rejected += [{**a.model_dump(), "error": str(exc)} for a in group]
            continue
        recorded += [a.model_dump() for a in group]
    return AgentSubmitOut(recorded=recorded, rejected=rejected, released=released)


def release(svc: Services, labeller: str | None, task_name: str | None = None, *,
            item_ids: Sequence[int] | None = None) -> int:
    """Drop this labeller's leases on ``item_ids`` (or, without them, all of its leases, or one
    task's): items it will not answer go back. Parallel agents sharing one name should pass
    ``item_ids``, or they release each other's items."""
    who = _require(labeller)
    with svc.uow() as uow:
        q = delete(LabelLease).where(LabelLease.labeller == who)
        if task_name:
            q = q.where(LabelLease.task == _task(task_name).name)
        if item_ids is not None:
            q = q.where(LabelLease.item_pk.in_(list(item_ids)))
        return int(getattr(uow.session.execute(q), "rowcount", 0) or 0)


def item_image(svc: Services, item_id: int) -> ImageBytes:
    with svc.uow() as uow:
        item = _get(uow.session, item_id)
        kind, ref = item.kind, dict(item.ref)
    if kind not in IMAGE_KINDS:
        raise NotFound(f"label item {item_id} has no image")
    source = _StudioSource(svc) if kind in STUDIO_KINDS else svc.label_source()
    if source is None:
        raise Unavailable("labelling is not configured ([labelling] base_url)")
    try:
        return source.image(ref)
    except PortError as exc:
        raise Unavailable(f"catalog: {exc}") from exc


# ---------------------------------------------------------------- export
def _form_rows(book: Book) -> list[dict[str, Any]]:
    """The labelled cases of one book's confirmed metadata form, one per suggested field."""
    conf = book.metadata_confirmed or {}
    out = []
    for field, case in (conf.get("fields") or {}).items():
        if not isinstance(case, dict) or case.get("outcome") not in TASKS["title_page"].labels:
            continue
        out.append({"field": field, **case, "labeller": conf.get("labeller"),
                    "at": conf.get("at"), "decider_model": conf.get("decider_model")})
    return out


def _form_lines(svc: Services) -> list[dict[str, Any]]:
    with svc.uow() as uow:
        books = uow.session.scalars(select(Book).where(Book.metadata_confirmed.is_not(None))
                                    .order_by(Book.id)).all()
        lines = []
        for book in books:
            langs = (book.item_metadata or {}).get("language") or []
            for row in _form_rows(book):
                lines.append({
                    "task": "title_page",
                    "item": {"book_id": book.id, "item_id": book.item_id, "field": row["field"],
                             "leaf": row.get("leaf")},
                    "language": langs[0] if langs else None, "label": row["outcome"],
                    "suggested": row.get("suggested"), "line": row.get("line"),
                    "p": row.get("p"), "value": row.get("final"),
                    "decider_model": row.get("decider_model"),
                    "labeller": row.get("labeller"), "labelled_at": row.get("at"), "via": "spa",
                    "labeller_kind": HUMAN, "model": None})
    return lines


def labeller_kind_or_400(kind: str | None) -> str | None:
    if kind and kind not in LABELLER_KINDS:
        raise BadRequest(f"labeller_kind must be one of {', '.join(LABELLER_KINDS)}")
    return kind or None


def export_lines(svc: Services, task_name: str, labeller_kind: str | None = None
                 ) -> Iterator[dict[str, Any]]:
    """One dict per non-skip label (the JSONL line of #7), oldest first; ``labeller_kind``
    keeps only people's or only agents' labels (#16)."""
    t = _task(task_name)
    kind = labeller_kind_or_400(labeller_kind)
    if not t.by_hand:
        yield from (ln for ln in _form_lines(svc) if kind in (None, HUMAN))
        return
    with svc.uow() as uow:
        q = (select(Label, LabelItem).join(LabelItem, Label.item_pk == LabelItem.id)
             .where(Label.task == t.name, Label.value != SKIP))
        if kind:
            q = q.where(Label.labeller_kind == kind)
        rows = uow.session.execute(q.order_by(Label.labelled_at, Label.id)).all()
        lines = [_line(t, label, item) for label, item in rows]
    yield from lines


def _line(t: LabelTask, label: Label, item: LabelItem) -> dict[str, Any]:
    line: dict[str, Any] = {"task": t.name, "item": dict(item.ref), "language": item.language,
                            "label": label.value, "labeller": label.labeller,
                            "labelled_at": _iso(label.labelled_at), "via": label.via,
                            "labeller_kind": label.labeller_kind, "model": label.model}
    if t.name == "subject":   # the label is a category id; its UDC term travels with it
        term = next((o.get("term") for o in item.payload.get("options", [])
                     if isinstance(o, dict) and str(o.get("id")) == label.value), None)
        line["label_term"] = term
    return line


def summary(svc: Services, task_name: str) -> SummaryOut:
    t = _task(task_name)
    if not t.by_hand:
        lines = _form_lines(svc)
        return SummaryOut(
            task=t.name, target=t.target, labels=len(lines), skipped=0,
            done=len(lines) >= t.target,
            by_label=dict(Counter(f"{ln['item']['field']}/{ln['label']}" for ln in lines)
                          .most_common()),
            by_language=dict(Counter(ln["language"] or "??" for ln in lines).most_common()),
            labellers=dict(Counter(ln["labeller"] or "?" for ln in lines).most_common()),
            by_via={"spa": len(lines)} if lines else {},
            by_labeller_kind={HUMAN: len(lines)} if lines else {},
            double=0, agreement=None, kappa=None, sampling=SAMPLING_NOTES[t.item_kind])
    with svc.uow() as uow:
        rows = uow.session.execute(
            select(Label.item_pk, Label.value, Label.labeller, Label.via, Label.labelled_at,
                   Label.labeller_kind, Label.model, LabelItem.language)
            .join(LabelItem, Label.item_pk == LabelItem.id).where(Label.task == t.name)
            .order_by(Label.labelled_at, Label.id)).all()
    answered = [r for r in rows if r.value != SKIP]
    human: dict[int, list[str]] = defaultdict(list)       # people's answers per item, in order
    agent: dict[int, list[str]] = defaultdict(list)
    language: dict[int, str] = {}
    for r in answered:
        (agent if r.labeller_kind == AGENT else human)[r.item_pk].append(r.value)
        language[r.item_pk] = r.language or "??"
    hh = [(i, v[0], v[1]) for i, v in human.items() if len(v) >= 2]       # the first two people
    ah = [(i, human[i][0], v[0]) for i, v in agent.items() if human.get(i)]   # human -> agent
    human_human, agent_human = _pair_stats(hh, language), _pair_stats(ah, language)
    spot_target = math.ceil(AGENT_SPOT_CHECK_SHARE * len(agent))
    return SummaryOut(
        task=t.name, target=t.target, labels=len(answered), skipped=len(rows) - len(answered),
        done=len(answered) >= t.target,
        by_label=dict(Counter(r.value for r in answered).most_common()),
        by_language=dict(Counter(r.language or "??" for r in answered).most_common()),
        labellers=dict(Counter(r.labeller for r in answered).most_common()),
        by_via=dict(Counter(r.via for r in answered).most_common()),
        by_labeller_kind=dict(Counter(r.labeller_kind for r in answered).most_common()),
        by_model=dict(Counter(r.model for r in answered if r.model).most_common()),
        double=human_human.pairs, agreement=human_human.agreement, kappa=human_human.kappa,
        human_human=human_human, agent_human=agent_human,
        agent_items=len(agent), spot_checked=agent_human.pairs, spot_check_target=spot_target,
        # judged only once the spot check is complete: kappa on a handful of pairs swings
        agent_trusted=(agent_human.kappa >= AGENT_KAPPA_MIN
                       if agent_human.kappa is not None and spot_target
                       and agent_human.pairs >= spot_target else None),
        sampling=SAMPLING_NOTES[t.item_kind])


def _agreement(pairs: Sequence[tuple[str, str]]) -> AgreementOut:
    kappa = cohens_kappa(pairs)
    return AgreementOut(pairs=len(pairs),
                        agreement=round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None,
                        kappa=round(kappa, 4) if kappa is not None else None)


def _pair_stats(triples: Sequence[tuple[int, str, str]], language: dict[int, str]) -> PairStatsOut:
    """Agreement over (item, first, second) answers, overall and per item language."""
    pairs = [(a, b) for _, a, b in triples]
    per_lang: dict[str, list[tuple[str, str]]] = defaultdict(list)
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for item_pk, a, b in triples:
        per_lang[language.get(item_pk, "??")].append((a, b))
        confusion[a][b] += 1
    return PairStatsOut(**_agreement(pairs).model_dump(),
                        by_language={k: _agreement(v) for k, v in sorted(per_lang.items())},
                        confusion={a: dict(c.most_common()) for a, c in sorted(confusion.items())})


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


def _require_model(model: str | None) -> str:
    model = (model or "").strip()
    if not model:
        raise BadRequest("an agent label needs the model that answered (e.g. claude-opus-5-5)")
    if len(model) > 128:
        raise BadRequest("model: at most 128 characters")
    return model


def _get(session: Session, item_id: int) -> LabelItem:
    item = session.get(LabelItem, item_id)
    if item is None:
        raise NotFound(f"label item {item_id} not found")
    return item


#: item kinds with a crop to show
IMAGE_KINDS = (ItemKind.FIGURE, ItemKind.TABLE, ItemKind.MARK_GROUP)


def _item_out(item: LabelItem, labeller: str | None) -> LabelItemOut:
    has_image = item.kind in IMAGE_KINDS
    return LabelItemOut(
        id=item.id, kind=item.kind, key=item.key, language=item.language, ref=dict(item.ref),
        payload=dict(item.payload),
        image_url=f"/api/v1/labelling/items/{item.id}/image" if has_image else None,
        mine={lab.task: lab.value for lab in item.labels if lab.labeller == labeller} if labeller else {})


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"
