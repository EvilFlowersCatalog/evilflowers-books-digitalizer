"""hOCR document model for reflow: page -> paragraph -> line -> word, with confidences.

Richer than :mod:`evilflowers_books_digitalizer.pipeline.hocr` (which keeps only
lines for bookmarks): reflow needs paragraphs, word boxes and ``x_wconf`` to
decide what Tesseract read reliably and what it did not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import html

type Box = tuple[int, int, int, int]

_BBOX = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")
_WCONF = re.compile(r"x_wconf (\d+)")
_XSIZE = re.compile(r"x_size ([\d.]+)")
_LINE_XPATH = (
    ".//*[contains(@class,'ocr_line') or contains(@class,'ocr_header') "
    "or contains(@class,'ocr_caption') or contains(@class,'ocr_textfloat')]"
)


def bbox_of(title: str) -> Box:
    m = _BBOX.search(title)
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4])) if m else (0, 0, 0, 0)


def union(boxes: list[Box]) -> Box:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


@dataclass(slots=True)
class Word:
    text: str
    conf: int
    bbox: Box


@dataclass(slots=True)
class Line:
    words: list[Word]
    bbox: Box
    size: float  # hOCR x_size: glyph height in px

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def conf(self) -> float:
        return sum(w.conf for w in self.words) / len(self.words)


@dataclass(slots=True)
class Par:
    lines: list[Line]
    bbox: Box

    @property
    def words(self) -> list[Word]:
        return [w for ln in self.lines for w in ln.words]

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)

    @property
    def conf(self) -> float:
        ws = self.words
        return sum(w.conf for w in ws) / len(ws) if ws else 0.0


@dataclass(slots=True)
class Page:
    width: int
    height: int
    pars: list[Par] = field(default_factory=list)

    @property
    def words(self) -> list[Word]:
        return [w for p in self.pars for w in p.words]


def parse_pages(markup: bytes | str) -> list[Page]:
    """All ``ocr_page`` elements of an hOCR document, in order."""
    # explicit UTF-8: lxml falls back to Latin-1 for bytes without a charset <meta>
    data = markup.encode() if isinstance(markup, str) else markup
    root = html.fromstring(data, parser=html.HTMLParser(encoding="utf-8"))
    return [_page(el) for el in root.xpath("//*[@class='ocr_page']")]


def parse_file(path: Path) -> list[Page]:
    return parse_pages(path.read_bytes())


def _page(page_el: html.HtmlElement) -> Page:
    _, _, w, h = bbox_of(page_el.get("title", ""))
    page = Page(w, h)
    for par_el in page_el.xpath(".//*[@class='ocr_par']"):
        lines: list[Line] = []
        for ln_el in par_el.xpath(_LINE_XPATH):
            words = []
            for w_el in ln_el.xpath(".//*[@class='ocrx_word']"):
                text = "".join(w_el.itertext()).strip()
                if text:
                    title = w_el.get("title", "")
                    m = _WCONF.search(title)
                    words.append(Word(text, int(m[1]) if m else 0, bbox_of(title)))
            if words:
                title = ln_el.get("title", "")
                ms = _XSIZE.search(title)
                lines.append(Line(words, bbox_of(title), float(ms[1]) if ms else 0.0))
        if lines:
            page.pars.append(Par(lines, bbox_of(par_el.get("title", ""))))
    return page
