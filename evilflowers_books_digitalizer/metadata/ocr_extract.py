"""Bibliographic facts from a book's OCR text: ISBN, year, publisher.

Pure and stdlib-only. Input is the per-page OCR text of the first and last
few pages (title page, colophon, back cover). Slovak academic books put the
colophon ("tiráž") on the title page verso or on the last page, so those pages
carry nearly everything: ``ISBN 80-227-1234-5``, ``© Ján Novák, 2005``,
``Vydavateľstvo STU, Bratislava 2005``.

OCR noise is expected: ``O`` for ``0`` and ``l``/``I`` for ``1`` inside digit runs,
``Vydavatel'stvo`` for ``Vydavateľstvo``. Every ISBN is checksum-validated, so
noise never yields a wrong-but-valid-looking ISBN unless the checksum agrees.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from evilflowers_books_digitalizer.metadata.isbn_lookup import is_valid_isbn, to_isbn13

#: Earliest publication year accepted.
MIN_YEAR = 1800

# "ISBN", tolerating OCR's l/1/| for I, optional "-10"/"-13", then separators.
_ISBN_LABELLED = re.compile(
    r"(?<![A-Za-z])[I1l|]\s?S\s?B\s?N(?:[\s\-]?1[03](?=\s*[:\s]))?[\s:.\-–]*"
    r"(?P<body>[0-9OoIl|](?:[\s\-–‐.]?[0-9OoIl|xX]){8,16})",
    re.IGNORECASE,
)
# An unlabelled ISBN-13: 978/979 + 10 more digits, separators allowed.
_ISBN13_BARE = re.compile(r"(?<![\d\-])(?P<body>97[89](?:[\s\-–‐]?[0-9OoIl|]){10})(?![\d])")
_NOISE = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "x": "X"})

_YEAR = r"(?<![\d\-/.])(?P<year>1[89]\d\d|20\d\d)(?![\d\-/])"
_COPYRIGHT_YEAR = re.compile(r"(?:©|\(c\)|copyright)[^\n]{0,60}?" + _YEAR, re.IGNORECASE)
_IMPRINT_YEAR = re.compile(
    r"(?:vydanie|vydal[aio]?\b|vydavate[lľ]['’]?stv\w*|nakladate[lľ]['’]?stv\w*"
    r"|edi[čc]n[ée]\s+stredisk\w*)[^\n]{0,80}?(?:\n[^\n]{0,40}?)?" + _YEAR,
    re.IGNORECASE,
)

_COLOPHON_HINTS = re.compile(
    r"©|\(c\)|copyright|vydanie|vydal|vydavate[lľ]|nakladate[lľ]|edi[čc]n[ée]\s+stredisk"
    r"|tla[čc]i|tla[čc]:|n[áa]klad\b|rozsah|AH\b|recenzent",
    re.IGNORECASE,
)

# Publisher phrases, most specific first.
_PUBLISHER_PATTERNS = (
    re.compile(r"(?P<pub>Vydavate[lľ]['’]?stv\w*\s+[^\n,;]{2,80})", re.IGNORECASE),
    re.compile(r"(?P<pub>Nakladate[lľ]['’]?stv\w*\s+[^\n,;]{2,80})", re.IGNORECASE),
    re.compile(r"(?P<pub>Edi[čc]n[ée]\s+stredisk\w*\s+[^\n,;]{2,80})", re.IGNORECASE),
    re.compile(r"\bVydal[aio]?\s+(?P<pub>[^\n;]{3,120})"),
)
_STU_LINE = re.compile(r"\bSTU\b")
_CANONICAL_WORDS = (
    (re.compile(r"^Vydavate[lľ]['’]?stv\w*", re.IGNORECASE), "Vydavateľstvo"),
    (re.compile(r"^Nakladate[lľ]['’]?stv\w*", re.IGNORECASE), "Nakladateľstvo"),
    (re.compile(r"^Edi[čc]n[ée]\s+stredisk\w*", re.IGNORECASE), "Edičné stredisko"),
)
# trailing ", Bratislava 2005", " 2005.", " - 1. vydanie"
_PUBLISHER_TAIL = re.compile(
    r"(?:[,\s]+(?:[A-ZÁ-Ž][\wáäčďéíĺľňóôŕšťúýž]+\s+)?" + _YEAR + r".*|[,\s]+\d+\.\s*vyd.*)$"
)


@dataclass(frozen=True)
class IsbnCandidate:
    """One checksum-valid ISBN seen in the OCR text."""

    isbn13: str  # canonical 13-digit form
    raw: str  # as printed (normalized whitespace)
    page: int  # 0-based page index in the book
    colophon: bool  # on the page judged to be the colophon
    labelled: bool  # preceded by an "ISBN" label


@dataclass
class OcrExtraction:
    """What :func:`extract` found; empty fields mean "not found"."""

    isbns: list[IsbnCandidate] = field(default_factory=list)
    year: int | None = None
    publisher: str | None = None
    colophon_page: int | None = None

    @property
    def isbn(self) -> str | None:
        """The preferred ISBN-13 (candidates are sorted best first)."""
        return self.isbns[0].isbn13 if self.isbns else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "isbns": [asdict(c) for c in self.isbns],
            "year": self.year,
            "publisher": self.publisher,
            "colophon_page": self.colophon_page,
        }


def _digits(body: str) -> str:
    """Strip separators and undo OCR letter/digit confusion."""
    cleaned = re.sub(r"[\s\-–‐.]", "", body).translate(_NOISE)
    return cleaned.upper()


def _isbn_from(body: str) -> str | None:
    """The ISBN-13 for a captured digit run (tries the 13- and 10-char prefixes)."""
    digits = _digits(body)
    for length in (13, 10):
        chunk = digits[:length]
        if len(chunk) != length:
            continue
        if length == 13 and not chunk.isdigit():
            continue
        if length == 10 and not re.fullmatch(r"\d{9}[\dX]", chunk):
            continue
        if is_valid_isbn(chunk):
            return to_isbn13(chunk)
    return None


def find_isbns(pages: dict[int, str], *, colophon_page: int | None = None) -> list[IsbnCandidate]:
    """All checksum-valid ISBNs in ``pages`` (page index -> text), unsorted, deduplicated
    per (ISBN, page)."""
    found: dict[tuple[str, int], IsbnCandidate] = {}
    for page, text in pages.items():
        labelled_spans: list[tuple[int, int]] = []
        for match in _ISBN_LABELLED.finditer(text):
            isbn = _isbn_from(match.group("body"))
            if isbn:
                labelled_spans.append(match.span("body"))
                found.setdefault((isbn, page), IsbnCandidate(
                    isbn, " ".join(match.group(0).split()), page, page == colophon_page, True
                ))
        for match in _ISBN13_BARE.finditer(text):
            start = match.start("body")
            if any(lo <= start < hi for lo, hi in labelled_spans):
                continue
            isbn = _isbn_from(match.group("body"))
            if isbn:
                found.setdefault((isbn, page), IsbnCandidate(
                    isbn, " ".join(match.group(0).split()), page, page == colophon_page, False
                ))
    return list(found.values())


def find_colophon(pages: dict[int, str]) -> int | None:
    """The page that looks most like a colophon: an ISBN plus imprint vocabulary."""
    best: tuple[int, int] | None = None  # (score, page)
    for page, text in pages.items():
        if not _ISBN_LABELLED.search(text):
            continue
        score = 1 + len({m.group(0).lower() for m in _COLOPHON_HINTS.finditer(text)})
        if best is None or score > best[0]:
            best = (score, page)
    return best[1] if best else None


def find_year(ordered_pages: list[str], *, max_year: int | None = None) -> int | None:
    """The publication year: a ©/Copyright year first, then an imprint year."""
    ceiling = max_year or date.today().year
    for pattern in (_COPYRIGHT_YEAR, _IMPRINT_YEAR):
        for text in ordered_pages:
            for match in pattern.finditer(text):
                year = int(match.group("year"))
                if MIN_YEAR <= year <= ceiling:
                    return year
    return None


def _clean_publisher(value: str) -> str | None:
    text = " ".join(value.split()).strip(" .,:;-–")
    text = _PUBLISHER_TAIL.sub("", text).strip(" .,:;-–")
    for pattern, canonical in _CANONICAL_WORDS:
        text = pattern.sub(canonical, text)
    return text[:255] if len(text) >= 3 else None


def find_publisher(ordered_pages: list[str], *, colophon: str | None = None) -> str | None:
    """Best-effort publisher from imprint phrases (colophon page searched first)."""
    for pattern in _PUBLISHER_PATTERNS:
        for text in ordered_pages:
            match = pattern.search(text)
            if match:
                cleaned = _clean_publisher(match.group("pub"))
                if cleaned:
                    return cleaned
    if colophon:  # a bare "STU" line only counts on the colophon itself
        for line in colophon.splitlines():
            if _STU_LINE.search(line) and not _ISBN_LABELLED.search(line) and len(line) <= 80:
                cleaned = _clean_publisher(line)
                if cleaned:
                    return cleaned
    return None


def select_pages(page_texts: list[str], head: int, tail: int) -> dict[int, str]:
    """The first ``head`` and last ``tail`` pages as ``{page_index: text}``."""
    total = len(page_texts)
    indices = list(range(min(head, total))) + list(range(max(total - tail, 0), total))
    return {i: page_texts[i] for i in sorted(set(indices))}


def extract(
    pages: dict[int, str],
    *,
    known_isbn: str | None = None,
    max_year: int | None = None,
) -> OcrExtraction:
    """Extract ISBN candidates, year and publisher from ``pages``.

    ISBN preference: one equal to ``known_isbn`` (e.g. the directory name),
    then one on the colophon page, then a labelled one, then the earliest.
    """
    colophon = find_colophon(pages)
    known = to_isbn13(known_isbn)
    candidates = find_isbns(pages, colophon_page=colophon)
    order = {page: rank for rank, page in enumerate(pages)}
    candidates.sort(key=lambda c: (
        c.isbn13 != known if known else False,
        not c.colophon,
        not c.labelled,
        order.get(c.page, len(order)),
    ))
    # colophon first, then the remaining pages in book order
    ordered = ([pages[colophon]] if colophon is not None else []) + [
        text for page, text in pages.items() if page != colophon
    ]
    return OcrExtraction(
        isbns=candidates,
        year=find_year(ordered, max_year=max_year),
        publisher=find_publisher(ordered, colophon=pages.get(colophon) if colophon is not None else None),
        colophon_page=colophon,
    )
