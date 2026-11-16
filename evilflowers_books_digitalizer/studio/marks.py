"""Scan marks across a whole book: group by dHash, one verdict per distinct mark (#14).

scan-service counts repeated marks within one clean job (≤ 32 leaves; an interactive job has
one leaf), so a library stamp on every 20th page of a 300-page book is never "furniture" there,
and each uncertain occurrence would cost a separate decider-vision call (~17 s, one replica).
Here every mark of the book is grouped once its leaves are cleaned:

* two marks belong together when their category matches, their 64-bit dHashes differ in at
  most ``hash_distance`` bits and their aspect ratios by at most ``aspect_tolerance``
  (relative): the rule image-service#6 and scan-service use, at book scale;
* a mark already in a group keeps it (a group's verdict and an operator's split survive later
  runs); a new mark joins the first matching group, else forms new groups with the other new
  marks (single linkage);
* a group on at least ``min_leaves`` leaves is ``hide`` / ``repeated`` without a model, unless
  it was found without colour (running heads, page numbers, ornaments repeat too) or a member
  was judged content: those take the decider path below;
* a group with an uncertain member (``review``) is asked to decider-vision **once**, with its
  clearest occurrence (highest ``p``, then the largest), and the verdict is every member's.

Nothing is erased: a hidden mark becomes a *disputed* stamp/sticker region at analyze, and
only an operator's confirmed ``remove`` reaches ``evilflowers_scan.erase``.
Everything here is pure (no I/O); :mod:`studio.workers.stages.marks` does the rows and calls.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from evilflowers_books_digitalizer.studio.config import MarksConfig

#: the decider-vision question scan-service asks per mark (evilflowers-scan-service
#: ``artefacts.QUESTION``): the same id and keys, so its calibration (knowledge#65) applies
SCAN_MARK = "scan_mark"
SCAN_MARK_INSTRUCTIONS = (
    "A region of a scanned library book page, shown with a little context. Is it part of the "
    "printed page, or something added to or around it?")
SCAN_MARK_CRITERIA: dict[str, str] = {
    "stamp": "an ink stamp: a library or ownership stamp, a date stamp, a signature",
    "sticker": "a sticker or label pasted on the page: call number, barcode, library label",
    "bleed_through": "faint text or images showing through from the other side of the leaf",
    "edge": "the edge of the scan: scanner bed, a finger, a shadow, the neighbouring page",
    "content": "part of the printed page itself: text, a figure, an ornament, a printed rule",
}
ARTEFACTS = frozenset({"stamp", "sticker", "bleed_through", "edge"})
#: categories that are grouped (an edge band has no hash: it is a page border, not a mark)
GROUPED = frozenset({"stamp", "sticker", "bleed_through"})
REPEATED = "repeated"


@dataclass(frozen=True, slots=True)
class MarkRef:
    """One mark of one leaf: ``pages.marks[index]`` of leaf ``leaf``."""

    leaf: int
    index: int
    category: str
    dhash: int
    aspect: float
    band: str
    p: float
    area: int
    group: int | None = None
    #: found without colour (a binarized page): may be a running head, a page number or an
    #: ornament, so repetition alone never hides it (scan-service's COLOURLESS_CAP rule)
    colourless: bool = False


@dataclass(slots=True)
class GroupKey:
    """What a mark is compared with: an existing group's representative, or a new mark."""

    id: int | None
    category: str
    dhash: int
    aspect: float
    members: list[MarkRef] = field(default_factory=list)


def parse_hash(value: object) -> int | None:
    try:
        return int(str(value), 16) if value not in (None, "") else None
    except ValueError:
        return None


def ref_of(leaf: int, index: int, mark: dict[str, object]) -> MarkRef | None:
    """A groupable mark (category with a hash), else None."""
    signals = mark.get("signals") or {}
    if not isinstance(signals, dict):
        return None
    category = str(mark.get("category", ""))
    dhash = parse_hash(signals.get("dhash"))
    if category not in GROUPED or not dhash:
        return None
    bbox = mark.get("bbox") or (0, 0, 0, 0)
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    x0, y0, x1, y1 = (int(v) for v in bbox)
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    try:
        aspect = float(signals.get("aspect") or w / h)
    except (TypeError, ValueError):
        aspect = w / h
    group = mark.get("group")
    return MarkRef(leaf, index, category, dhash, aspect, str(mark.get("band", "review")),
                   float(mark.get("p", 0.0) or 0.0), w * h,  # type: ignore[arg-type]
                   int(group) if isinstance(group, int) else None,
                   colourless=signals.get("colour") is False)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def similar(a: MarkRef | GroupKey, b: MarkRef | GroupKey, cfg: MarksConfig) -> bool:
    if a.category != b.category or hamming(a.dhash, b.dhash) > cfg.hash_distance:
        return False
    if a.aspect <= 0 or b.aspect <= 0:
        return True
    return abs(math.log(a.aspect / b.aspect)) <= math.log1p(cfg.aspect_tolerance)


def assign(marks: Sequence[MarkRef], groups: Sequence[GroupKey], cfg: MarksConfig
           ) -> list[GroupKey]:
    """Every groupable mark in a group. Existing groups keep their members (``group`` set and
    known); a new mark joins the first similar existing group, or forms a new group (``id``
    None) with the other new marks it is similar to (single linkage, so a chain of near
    hashes is one mark). Returns the existing groups (members refreshed) then the new ones."""
    by_id = {g.id: g for g in groups}
    for g in groups:
        g.members = []
    loose: list[MarkRef] = []
    for m in marks:
        home = by_id.get(m.group)
        if home is not None and home.category == m.category:
            home.members.append(m)
        else:
            loose.append(m)
    still: list[MarkRef] = []
    for m in loose:
        home = next((g for g in groups if similar(m, g, cfg)), None)
        if home is not None:
            home.members.append(m)
        else:
            still.append(m)
    parent = list(range(len(still)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(still):
        for j in range(i + 1, len(still)):
            if similar(a, still[j], cfg):
                parent[find(j)] = find(i)
    fresh: dict[int, GroupKey] = {}
    for i, m in enumerate(still):
        root = find(i)
        if root not in fresh:
            first = still[root]
            fresh[root] = GroupKey(None, first.category, first.dhash, first.aspect)
        fresh[root].members.append(m)
    return [*groups, *fresh.values()]


def carry_groups(old: Sequence[dict[str, object]], new: list[dict[str, object]],
                 cfg: MarksConfig) -> list[dict[str, object]]:
    """A re-clean of a leaf reports its marks again, without their groups: each new mark
    takes the group of the leaf's old mark it matches, so a group's members and an operator's
    split survive the re-run."""
    before = [(r, row["group"]) for i, row in enumerate(old)
              if isinstance(row.get("group"), int) and (r := ref_of(0, i, row)) is not None]
    out = []
    for i, row in enumerate(new):
        ref = ref_of(0, i, row)
        group = next((g for r, g in before if ref is not None and similar(ref, r, cfg)), None)
        out.append({**row, "group": group} if group is not None else row)
    return out


def leaves_of(members: Iterable[MarkRef]) -> int:
    return len({m.leaf for m in members})


def clearest(members: Sequence[MarkRef]) -> MarkRef:
    """The occurrence the decider and a labeller see: highest p, then the largest."""
    return max(members, key=lambda m: (m.p, m.area))


def is_uncertain(members: Iterable[MarkRef]) -> bool:
    return any(m.band == "review" for m in members)


def repetition_hides(members: Sequence[MarkRef], cfg: MarksConfig) -> bool:
    """The rule: on ``min_leaves`` leaves, a mark is furniture, hidden without a model. Not for
    colourless marks (running heads, page numbers, ornaments repeat too) nor when a member was
    judged content (``keep``): those go to the decider like any uncertain group."""
    return (leaves_of(members) >= cfg.min_leaves
            and not any(m.colourless or m.band == "keep" for m in members))


def band_of(choice: str, p: float, cfg: MarksConfig) -> str:
    """The band of a decider verdict. Without calibrated thresholds (knowledge#65) every
    verdict is ``review``: stored with its p for the operator and the labels, never acted on."""
    if choice in ARTEFACTS and cfg.hide_above is not None and p >= cfg.hide_above:
        return "hide"
    if choice == "content" and cfg.keep_above is not None and p >= cfg.keep_above:
        return "keep"
    return "review"
