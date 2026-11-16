"""Hand labels for decision-model calibration (evilflowers-books-digitalizer#7): pure rules.

The label sets are the *exact keys* the callers send to the decider, so a fitted
threshold maps one to one onto a caller's answer:

* ``figure_kind``: evilflowers-image-service#6 (``decider-vision`` choice ``kind``)
* ``is_data_table`` / ``table_quality``: evilflowers-text-service#20
* ``section_relation``: the catalog's edge sweep, EvilFlowersCatalog#190 (choice ``relation``,
  section A then section B, in the sweep's orientation)
* ``subject``: EvilFlowersCatalog#191, a choice among the catalog's own categories; the label is
  the category id (the set differs per catalog, so it is not fixed here)
* ``scan_mark``: the studio's own book-wide mark groups (#14, decider-vision question
  ``scan_mark``, scan-service#2): one crop per group, not one per occurrence
* ``title_page``: the title-page prefill (#15). Not labelled on a screen: every metadata form an
  operator confirms is a labelled case per field (accepted / changed / none), exported in the
  same JSONL

Every task also accepts :data:`SKIP`: stored, never exported as a label.
No I/O here; the application layer (``studio.application.labelling``) stores and serves.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

SKIP = "skip"
#: share of labelled items that a second person also labels (Cohen's kappa in the summary)
SECOND_LABELLER_SHARE = 0.1
#: characters of section text shown per side: what the edge sweep sends (EvilFlowersCatalog#190)
SECTION_TEXT_CHARS = 1500


class ItemKind(StrEnum):
    FIGURE = "figure"
    TABLE = "table"
    SECTION_PAIR = "section_pair"
    ENTRY = "entry"
    MARK_GROUP = "mark_group"          # a studio book's mark group (studio DB, not the catalog)
    BOOK = "book"                      # a studio book's confirmed metadata form


#: item kinds sampled from the studio's own database (the rest come from the catalog)
STUDIO_KINDS = (ItemKind.MARK_GROUP,)


@dataclass(frozen=True, slots=True)
class LabelTask:
    name: str
    item_kind: ItemKind
    #: allowed answers besides ``skip``; empty = the item carries its own options (``subject``)
    labels: tuple[str, ...]
    #: non-skip labels the calibration needs ("Done when" of #7)
    target: int
    title: str
    #: answered on the screen of another task (same item), never asked alone first
    companion_of: str | None = None
    #: labelled by hand on an item (False: collected elsewhere, e.g. the metadata form)
    by_hand: bool = True

    def accepts(self, value: str, options: Iterable[str] = ()) -> bool:
        return value == SKIP or value in (self.labels or tuple(options))


TASKS: dict[str, LabelTask] = {t.name: t for t in (
    LabelTask("figure_kind", ItemKind.FIGURE,
              ("chart", "diagram", "photo", "map", "table", "formula", "stamp", "decoration", "scan",
               "blank"), 200, "What is this image?"),
    LabelTask("is_data_table", ItemKind.TABLE, ("yes", "no"), 200,
              "Is this a data table (not a ToC, a form or page layout)?"),
    LabelTask("table_quality", ItemKind.TABLE, ("correct", "minor_errors", "broken"), 100,
              "Is the markdown a faithful copy of the table?", companion_of="is_data_table"),
    LabelTask("section_relation", ItemKind.SECTION_PAIR, ("same", "pre", "example", "loose", "none"),
              200, "How is section B related to section A?"),
    LabelTask("subject", ItemKind.ENTRY, (), 200, "Which subject (UDC category) is this book?"),
    LabelTask("scan_mark", ItemKind.MARK_GROUP,
              ("stamp", "sticker", "bleed_through", "edge", "content"), 200,
              "Is this mark part of the printed page, or something added to or around it?"),
    LabelTask("title_page", ItemKind.BOOK, ("accepted", "changed", "none"), 200,
              "Did the title-page suggestion hold (per field, from confirmed metadata forms)?",
              by_hand=False),
)}
#: tasks a person answers on an item (SPA, CLI, MCP); ``sample-labels`` / ``label`` take these
SAMPLED_TASKS: dict[str, LabelTask] = {k: t for k, t in TASKS.items() if t.by_hand}

#: what each label means, as the callers' question criteria word it
LABEL_HINTS: dict[str, str] = {
    "table": "table drawn as an image", "stamp": "stamp, signature or logo", "scan": "scanned full page",
    "no": "ToC, form or page layout", "minor_errors": "a few wrong cells", "broken": "unusable",
    "same": "explains the same idea", "pre": "A is a prerequisite of B", "example": "B is an example of A",
    "loose": "loosely related", "none": "unrelated",
    "sticker": "sticker or label: call number, barcode", "bleed_through": "shows through from the other side",
    "edge": "scan edge, finger, shadow", "content": "part of the printed page",
}


def task(name: str) -> LabelTask:
    try:
        return TASKS[name]
    except KeyError:
        raise KeyError(f"unknown labelling task {name!r} (known: {', '.join(TASKS)})") from None


def tasks_for(kind: ItemKind | str) -> list[LabelTask]:
    return [t for t in TASKS.values() if t.item_kind == kind]


# ---------------------------------------------------------------- keys
_REGION_ABBR = {"figure": "fig", "table": "tab", "equation": "eq"}


def region_key(kind: str, page: int, seq: int) -> str:
    """``p012-fig01``: the catalog returns no region key (EvilFlowersCatalog AdminRegion has no
    ``locator_key``), so it is built from the 1-based PDF page and the region's ``seq``."""
    return f"p{page:03d}-{_REGION_ABBR.get(kind, kind[:3])}{seq:02d}"


def item_key(kind: ItemKind | str, *parts: str) -> str:
    """Unique per catalog item; a section pair is the same item in either orientation."""
    if kind == ItemKind.SECTION_PAIR:
        parts = tuple(sorted(parts))
    return ":".join((str(kind), *parts))


def pair_language(a: str | None, b: str | None) -> str | None:
    if a == b:
        return a
    return "mixed" if a and b else (a or b)


def udc_top(term: str | None) -> str:
    """Top level of a UDC class: its first digit (``004.8`` -> ``0``), as the catalog graph's
    ``field``; categories are flat in the catalog, so the tree is derived from the term."""
    for ch in unicodedata.normalize("NFKC", term or ""):
        if ch.isdigit():
            return ch
    return "?"


# ---------------------------------------------------------------- sampling
def stratum(language: str | None, band: str | None) -> str:
    return f"{language or '??'}/{band or 'none'}"


def confidence_bucket(band: object, p: object) -> str | None:
    """The sampling bucket of the model's answer: its band when the catalog has one, else
    the raw probability in three steps. Bands stay empty until knowledge#65 sets thresholds
    (from these very labels), so on dev the unsure cases are found through ``p``."""
    if band:
        return str(band)
    if isinstance(p, (int, float)):
        return "p<0.5" if p < 0.5 else "p<0.8" if p < 0.8 else "p>=0.8"
    return None


def stratified(groups: Mapping[str, Sequence[str]], n: int) -> list[str]:
    """Take ``n`` keys round-robin over the strata (each already shuffled), so small strata,
    such as Slovak items or the decider's ``review`` band, are not drowned by a big one."""
    queues = {s: list(keys) for s, keys in sorted(groups.items()) if keys}
    out: list[str] = []
    while queues and len(out) < n:
        for s in list(queues):
            out.append(queues[s].pop(0))
            if not queues[s]:
                del queues[s]
            if len(out) >= n:
                break
    return out


# ---------------------------------------------------------------- agreement
def cohens_kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    """Cohen's kappa of two raters over ``(a, b)`` answers; None when undefined.

    With fewer than two items, or when chance agreement is 1 (both always gave one and the
    same answer), kappa is not defined; the summary then reports raw agreement only.
    """
    n = len(pairs)
    if n < 2:
        return None
    observed = sum(a == b for a, b in pairs) / n
    first = Counter(a for a, _ in pairs)
    second = Counter(b for _, b in pairs)
    expected = sum(first[k] * second.get(k, 0) for k in first) / (n * n)
    if expected >= 1.0:
        return None
    return (observed - expected) / (1.0 - expected)
