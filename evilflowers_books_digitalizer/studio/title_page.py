"""Title page -> title, authors, publisher, year: OCR line candidates for the decider (#15).

Scanned books arrive without a catalogue record. After OCR, the first leaves (title page and
its verso) and the last ones (colophon) hold nearly everything. Lines are taken from their
hOCR with their size and position, ranked per field by cheap signals, and the best few become
the ``criteria`` of one ``choice`` question per field to ``decider`` (Laya, multilingual):

* ``title``: the largest lines of the first leaves, centred, without a year;
* ``authors``: other large, centred lines of the first leaves that read like names;
* ``publisher``: lines naming or following an imprint word (vydal, vydavateľstvo,
  nakladateľstvo, ©, press, verlag …) on any of the leaves;
* ``year``: lines with a plausible four-digit year, imprint lines first.

Every question also offers ``none``. The answers are only *suggestions*: they prefill the
operator's metadata form with their ``p`` and are never imported unconfirmed.
Everything here is pure (no I/O); :mod:`studio.workers.stages.title_page` does the rest.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from evilflowers_books_digitalizer.studio.hocr import Page

FIELDS = ("title", "authors", "publisher", "year")
NONE = "none"
INSTRUCTIONS: dict[str, str] = {
    "title": "Which line of this scanned book's title page is the book's title?",
    "authors": "Which line names the book's author or authors?",
    "publisher": "Which line names the publisher of this book?",
    "year": "Which line gives the year this book was published?",
}
_YEAR = re.compile(r"(?<![\d\-/.])(1[5-9]\d\d|20\d\d)(?![\d\-/])")
_IMPRINT = re.compile(
    r"©|\(c\)|copyright|vydal|vydavate[lľ]|nakladate[lľ]|edi[čc]n[ée]\s+stredisk|vydanie"
    r"|publisher|published|press\b|verlag|nakladatelství|univerzit|university|STU\b",
    re.IGNORECASE)
#: a word that starts with a capital: names read like "Ján Novák", "J. Novák, P. Kováč"
_NAME_WORD = re.compile(r"^[A-ZÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ][\wáäčďéíĺľňóôŕšťúýž.\-']*,?$")
_AUTHOR_SPLIT = re.compile(r"\s*(?:,|;|\s+a\s+|\s+and\s+|\s+&\s+|\s+und\s+)\s*")
_STATE_LINES = 120


@dataclass(frozen=True, slots=True)
class Candidate:
    id: str              # "L<leaf>-<n>": the criteria key the decider answers with
    leaf: int
    text: str
    size: float          # glyph height (hOCR x_size, else the line box height)
    centred: float       # 1 = centred on the page, 0 = at an edge
    order: int           # reading order over the leaves


def lines_of(leaf: int, page: Page, first_order: int = 0) -> list[Candidate]:
    width = page.width or max((ln.bbox[2] for ln in page.lines), default=1)
    out = []
    for n, line in enumerate(page.lines):
        text = " ".join(line.text.split())
        if len(text) < 2:
            continue
        cx = (line.bbox[0] + line.bbox[2]) / 2
        centred = max(0.0, 1.0 - abs(cx - width / 2) / max(1.0, width / 2))
        out.append(Candidate(f"L{leaf}-{n}", leaf, text[:200], line.size or float(line.height),
                             round(centred, 3), first_order + n))
    return out


def _relative(c: Candidate, lines: Sequence[Candidate]) -> float:
    sizes = [x.size for x in lines if x.leaf == c.leaf] or [c.size]
    top = max(sizes) or 1.0
    return c.size / top


def _after_imprint(c: Candidate, lines: Sequence[Candidate]) -> bool:
    prev = next((x for x in lines if x.leaf == c.leaf and x.order == c.order - 1), None)
    return prev is not None and bool(_IMPRINT.search(prev.text))


def _looks_like_names(text: str) -> bool:
    words = [w for w in re.split(r"[\s,;]+", text) if w]
    if not 1 < len(words) <= 12 or _YEAR.search(text):
        return False
    return sum(bool(_NAME_WORD.match(w)) for w in words) / len(words) >= 0.6


def rank(field: str, lines: Sequence[Candidate], first_leaves: set[int], n: int
         ) -> list[Candidate]:
    """The ``n`` best candidates of ``field``, best first."""
    def score(c: Candidate) -> float | None:
        on_title = c.leaf in first_leaves
        rel = _relative(c, lines)
        year = bool(_YEAR.search(c.text))
        imprint = bool(_IMPRINT.search(c.text)) or _after_imprint(c, lines)
        match field:
            case "title":
                if not on_title or year or not any(ch.isalpha() for ch in c.text):
                    return None
                return 2 * rel + c.centred - 0.01 * c.order
            case "authors":
                if not on_title or year or imprint:
                    return None
                return rel + c.centred + (1.5 if _looks_like_names(c.text) else 0.0)
            case "publisher":
                return (2.0 if imprint else 0.0) + 0.5 * c.centred + (0.3 if on_title else 0.0) \
                    if imprint or on_title else None
            case "year":
                if not year:
                    return None
                return (2.0 if imprint else 0.0) + (0.5 if on_title else 0.0) + c.centred
        return None

    scored = [(s, c) for c in lines if (s := score(c)) is not None]
    return [c for _, c in sorted(scored, key=lambda sc: -sc[0])[:n]]


def criteria(candidates: Sequence[Candidate]) -> dict[str, str]:
    return {**{c.id: c.text for c in candidates}, NONE: "none of these lines"}


def state(lines: Sequence[Candidate]) -> str:
    """The pages' lines in reading order with their size: what the decider reads."""
    rows, leaf = [], None
    for c in sorted(lines, key=lambda x: x.order)[:_STATE_LINES]:
        if c.leaf != leaf:
            rows.append(f"--- leaf {c.leaf} ---")
            leaf = c.leaf
        rows.append(f"[{c.id}] (size {c.size:.0f}) {c.text}")
    return "\n".join(rows)


def value(field: str, text: str) -> str | list[str] | None:
    """The metadata value a chosen line gives: the year alone, authors as a list."""
    match field:
        case "year":
            m = _YEAR.search(text)
            return m[1] if m else None
        case "authors":
            names = [n.strip(" .") for n in _AUTHOR_SPLIT.split(text) if n.strip(" .")]
            return names or None
        case _:
            return text.strip() or None


#: suggestion field -> the IA metadata field it fills (``ItemMetadata``)
METADATA_FIELD = {"title": "title", "authors": "creator", "publisher": "publisher", "year": "date"}
