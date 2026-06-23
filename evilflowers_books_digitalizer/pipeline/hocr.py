"""hOCR parsing shared by the OCR and finalize steps.

Tesseract emits one multi-page hOCR file per book. Two consumers need it:

* the OCR step, for a plain-text sidecar (enrichment / search input);
* the finalize step, for outline bookmarks and printed-page-number labels.

Parsing it once, here, keeps a single source of truth for the geometry and
line model (previously duplicated across two ``HTMLParser`` subclasses).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_XML_DECL = re.compile(r"<\?xml[^>]*\?>")
_HEADING_CLASSES = ("ocr_line", "ocr_header", "ocr_caption", "ocr_textfloat")


@dataclass(frozen=True, slots=True)
class HocrLine:
    """One OCR line, aligned to a 0-based page index (= PDF page index)."""

    page: int
    text: str
    size: float  # hOCR x_size: glyph height in px at scan dpi
    bbox: tuple[int, int, int, int]


@dataclass(slots=True)
class HocrDocument:
    """Parsed hOCR: per-page pixel dimensions + flat list of lines."""

    page_dims: list[tuple[int, int]] = field(default_factory=list)
    lines: list[HocrLine] = field(default_factory=list)

    def plain_text(self) -> str:
        """Words joined per line, lines per page, pages separated by ``\\f``."""
        by_page: list[list[str]] = [[] for _ in self.page_dims]
        for line in self.lines:
            by_page[line.page].append(line.text)
        return "\f".join("\n".join(page) for page in by_page)


class _HocrParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.doc = HocrDocument()
        self._words: list[str] = []
        self._meta: tuple[float, tuple[int, int, int, int]] | None = None
        self._in_word = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        classes = a.get("class", "")
        title = a.get("title", "")
        if "ocr_page" in classes:
            self._flush()
            m = re.search(r"bbox \d+ \d+ (\d+) (\d+)", title)
            self.doc.page_dims.append((int(m.group(1)), int(m.group(2))) if m else (0, 0))
        elif any(c in classes for c in _HEADING_CLASSES):
            self._flush()
            size = re.search(r"x_size ([\d.]+)", title)
            bbox = re.search(r"bbox (\d+) (\d+) (\d+) (\d+)", title)
            if size and bbox:
                groups = bbox.groups()
                self._meta = (float(size.group(1)), (int(groups[0]), int(groups[1]),
                                                      int(groups[2]), int(groups[3])))
        elif "ocrx_word" in classes:
            self._in_word = True

    def handle_endtag(self, tag: str) -> None:
        self._in_word = False

    def handle_data(self, data: str) -> None:
        if self._in_word and data.strip():
            self._words.append(data.strip())

    def _flush(self) -> None:
        if self._meta and self._words and self.doc.page_dims:
            size, bbox = self._meta
            page = len(self.doc.page_dims) - 1
            self.doc.lines.append(HocrLine(page, " ".join(self._words), size, bbox))
        self._words, self._meta = [], None

    def close(self) -> None:
        self._flush()
        super().close()


def parse_hocr(markup: str) -> HocrDocument:
    """Parse hOCR markup into a :class:`HocrDocument` (XML declaration tolerated)."""
    parser = _HocrParser()
    parser.feed(_XML_DECL.sub("", markup))
    parser.close()
    return parser.doc
