"""Richer hOCR model for reflow experiments: blocks -> paragraphs -> lines -> words."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import html

BBOX = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")
WCONF = re.compile(r"x_wconf (\d+)")
XSIZE = re.compile(r"x_size ([\d.]+)")

Box = tuple[int, int, int, int]


def _bbox(title: str) -> Box:
    m = BBOX.search(title)
    return tuple(int(g) for g in m.groups()) if m else (0, 0, 0, 0)  # type: ignore[return-value]


@dataclass
class Word:
    text: str
    conf: int
    bbox: Box
    italic: bool = False
    bold: bool = False


@dataclass
class Line:
    words: list[Word]
    bbox: Box
    size: float

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
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
    def mean_conf(self) -> float:
        ws = self.words
        return sum(w.conf for w in ws) / len(ws) if ws else 0.0


@dataclass
class Page:
    width: int
    height: int
    pars: list[Par] = field(default_factory=list)


def parse(path: Path) -> Page:
    """Single-page hOCR file -> Page."""
    return parse_pages(path.read_bytes())[0]


def parse_pages(markup: bytes) -> list[Page]:
    """Multi-page hOCR (Tesseract with a page list, as the pipeline's OcrPages emits)."""
    root = html.fromstring(markup)
    return [_page(el) for el in root.xpath("//*[@class='ocr_page']")]


def _page(page_el) -> Page:  # type: ignore[no-untyped-def]
    _, _, w, h = _bbox(page_el.get("title", ""))
    page = Page(w, h)
    for par_el in page_el.xpath(".//*[@class='ocr_par']"):
        lines: list[Line] = []
        for ln_el in par_el.xpath(
            ".//*[contains(@class,'ocr_line') or contains(@class,'ocr_header') "
            "or contains(@class,'ocr_caption') or contains(@class,'ocr_textfloat')]"
        ):
            words = []
            for w_el in ln_el.xpath(".//*[@class='ocrx_word']"):
                text = "".join(w_el.itertext()).strip()
                if not text:
                    continue
                t = w_el.get("title", "")
                m = WCONF.search(t)
                words.append(Word(
                    text, int(m.group(1)) if m else 0, _bbox(t),
                    italic=bool(w_el.xpath(".//em")), bold=bool(w_el.xpath(".//strong")),
                ))
            if words:
                t = ln_el.get("title", "")
                ms = XSIZE.search(t)
                lines.append(Line(words, _bbox(t), float(ms.group(1)) if ms else 0.0))
        if lines:
            page.pars.append(Par(lines, _bbox(par_el.get("title", ""))))
    return page
