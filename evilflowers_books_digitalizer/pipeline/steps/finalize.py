"""Finalize step: reading-experience polish for the produced PDF.

Three independent improvements (notebook 08 roadmap item #2):

1. **Outline bookmarks** — headings detected in the Tesseract hOCR (lines
   noticeably larger than the body text, filtered for OCR noise) become a PDF
   outline. Nothing makes a scan "feel" digitized like a working outline.
2. **Page labels** — printed page numbers found near the page edges are fitted
   to a consistent physical->printed offset; when one exists, the PDF gets
   /PageLabels so viewers show the printed numbers (front matter as roman).
3. **Linearization** — the PDF is saved web-optimized (fast first page over
   HTTP range requests).

All three degrade gracefully: no hOCR -> bookmarks/labels are skipped, no
consistent page numbering -> labels are skipped, and the step never fails the
pipeline over a heuristic.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pikepdf

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)

_ROMAN = re.compile(r"^[ivxlcdm]{1,6}$", re.IGNORECASE)
_ARABIC = re.compile(r"^\d{1,4}$")
#: characters that betray a table ruling / diagram misread, not a heading
_NOISE_CHARS = set("|[]{}<>=_~\\")


@dataclass
class _Line:
    page: int  # 0-based, aligned with PDF page index
    text: str
    size: float  # hOCR x_size (px at scan dpi)
    bbox: tuple[int, int, int, int]


class _HocrLineParser(HTMLParser):
    """hOCR -> per-page dimensions + lines with text, x_size and bbox."""

    def __init__(self) -> None:
        super().__init__()
        self.page_dims: list[tuple[int, int]] = []
        self.lines: list[_Line] = []
        self._words: list[str] = []
        self._meta: tuple[float, tuple[int, int, int, int]] | None = None
        self._in_word = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = a.get("class", "")
        title = a.get("title", "")
        if "ocr_page" in classes:
            self._flush()
            m = re.search(r"bbox \d+ \d+ (\d+) (\d+)", title)
            self.page_dims.append((int(m.group(1)), int(m.group(2))) if m else (0, 0))
        elif any(c in classes for c in ("ocr_line", "ocr_header", "ocr_caption", "ocr_textfloat")):
            self._flush()
            size = re.search(r"x_size ([\d.]+)", title)
            bbox = re.search(r"bbox (\d+) (\d+) (\d+) (\d+)", title)
            if size and bbox:
                self._meta = (float(size.group(1)), tuple(int(v) for v in bbox.groups()))
        elif "ocrx_word" in classes:
            self._in_word = 1

    def handle_endtag(self, tag):
        self._in_word = 0

    def handle_data(self, data):
        if self._in_word and data.strip():
            self._words.append(data.strip())

    def _flush(self):
        if self._meta and self._words and self.page_dims:
            size, bbox = self._meta
            self.lines.append(_Line(len(self.page_dims) - 1, " ".join(self._words), size, bbox))
        self._words, self._meta = [], None

    def close(self):
        self._flush()
        super().close()


def _roman_to_int(s: str) -> int:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total, prev = 0, 0
    for ch in reversed(s.lower()):
        v = values[ch]
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total


class FinalizePdf(PipelineStep):
    """``artifacts['pdf']`` [+ ``artifacts['hocr']``] -> bookmarked, labeled, linearized PDF."""

    name = "finalize"

    def __init__(
        self,
        bookmarks: bool = True,
        page_labels: bool = True,
        linearize: bool = True,
        # heading heuristics, calibrated on the lab06 fad sample (notebook 09):
        size_ratio: float = 1.18,  # heading x_size >= body median * ratio
        max_words: int = 6,
        alpha_ratio: float = 0.78,  # letters / non-space chars
        max_per_page: int = 2,
        # printed page numbers live within this margin band (fraction of height)
        edge_band: float = 0.08,
        min_number_hits: int = 3,  # pages agreeing on one offset before we trust it
    ):
        self.bookmarks = bookmarks
        self.page_labels = page_labels
        self.linearize = linearize
        self.size_ratio = size_ratio
        self.max_words = max_words
        self.alpha_ratio = alpha_ratio
        self.max_per_page = max_per_page
        self.edge_band = edge_band
        self.min_number_hits = min_number_hits

    # -- heading detection ---------------------------------------------------

    def _is_heading(self, line: _Line, body_size: float) -> bool:
        if line.size < body_size * self.size_ratio:
            return False
        text = line.text.strip()
        # strip decorative trailing junk ("Metodika ““" -> "Metodika")
        text = re.sub(r"[^\w)]+$", "", text)
        words = text.split()
        if not 1 <= len(words) <= self.max_words:
            return False
        if _NOISE_CHARS & set(text):
            return False
        if not (text[0].isupper() or text[0].isdigit()):
            return False
        if text.endswith("-"):  # hyphenated continuation, not a heading
            return False
        if len(words[-1]) == 1 and words[-1].isalpha():  # stray orphan glyph
            return False
        if words[-1].isdigit():  # table scores / page refs ("... — 1")
            return False
        # a lone capital inside a mixed-case line is OCR debris, not an initial
        # (all-caps headings keep their single-letter conjunctions: "ŠKOLA A PARK")
        if not text.isupper() and any(len(w) == 1 and w.isupper() for w in words):
            return False
        letters = sum(ch.isalpha() for ch in text)
        non_space = sum(not ch.isspace() for ch in text)
        return letters >= 4 and non_space > 0 and letters / non_space >= self.alpha_ratio

    def _detect_headings(self, lines: list[_Line]) -> list[tuple[int, str]]:
        body_sizes = [ln.size for ln in lines if len(ln.text.split()) >= 3]
        if len(body_sizes) < 10:
            return []
        body = statistics.median(body_sizes)
        found: list[tuple[int, str]] = []
        per_page: dict[int, int] = {}
        for line in lines:
            if per_page.get(line.page, 0) >= self.max_per_page:
                continue
            if self._is_heading(line, body):
                title = re.sub(r"[^\w)]+$", "", line.text.strip())
                found.append((line.page, title))
                per_page[line.page] = per_page.get(line.page, 0) + 1
        return found

    # -- printed page numbers -> /PageLabels ----------------------------------

    def _detect_page_offset(
        self, lines: list[_Line], page_dims: list[tuple[int, int]]
    ) -> int | None:
        """Modal ``printed - physical_index`` offset, or None without consensus."""
        votes: dict[int, int] = {}
        for line in lines:
            w, h = page_dims[line.page]
            if not h:
                continue
            top, bottom = line.bbox[1], line.bbox[3]
            if top > h * self.edge_band and bottom < h * (1 - self.edge_band):
                continue
            text = line.text.strip()
            if _ARABIC.fullmatch(text):
                printed = int(text)
            elif _ROMAN.fullmatch(text) and len(text) > 1:  # lone i/x/c are too noisy
                continue  # roman front matter handled via the arabic offset below
            else:
                continue
            offset = printed - line.page  # printed number of 0-based page `page`
            votes[offset] = votes.get(offset, 0) + 1
        if not votes:
            return None
        offset, hits = max(votes.items(), key=lambda kv: kv[1])
        return offset if hits >= self.min_number_hits else None

    def _apply_page_labels(self, pdf: pikepdf.Pdf, offset: int) -> None:
        """Physical page i is printed ``i + offset``; earlier pages get roman."""
        nums: list = []
        start = max(0, 1 - offset)  # first physical page with printed number >= 1
        if start > 0:
            nums += [0, pikepdf.Dictionary(S=pikepdf.Name.r)]
        nums += [start, pikepdf.Dictionary(S=pikepdf.Name.D, St=start + offset)]
        pdf.Root.PageLabels = pikepdf.Dictionary(Nums=pikepdf.Array(nums))

    # -- step ------------------------------------------------------------------

    def run(self, ctx: BookContext) -> BookContext:
        pdf_path = ctx.artifacts.get("pdf")
        if pdf_path is None:
            raise ValueError(f"no pdf for {ctx.slug} — run the OCR/MRC step first")

        lines: list[_Line] = []
        page_dims: list[tuple[int, int]] = []
        hocr: Path | None = ctx.artifacts.get("hocr")
        if hocr is not None and hocr.exists():
            parser = _HocrLineParser()
            parser.feed(re.sub(r"<\?xml[^>]*\?>", "", hocr.read_text(errors="ignore")))
            parser.close()
            lines, page_dims = parser.lines, parser.page_dims
        elif self.bookmarks or self.page_labels:
            logger.warning("%s: no hOCR artifact — skipping bookmarks/page labels", ctx.slug)

        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            n_pages = len(pdf.pages)

            if self.bookmarks and lines:
                headings = [(p, t) for p, t in self._detect_headings(lines) if p < n_pages]
                if headings:
                    with pdf.open_outline() as outline:
                        outline.root.extend(
                            pikepdf.OutlineItem(title, page) for page, title in headings
                        )
                ctx.metadata["n_bookmarks"] = len(headings)
                logger.info("%s: %d outline bookmarks", ctx.slug, len(headings))

            if self.page_labels and lines:
                offset = self._detect_page_offset(lines, page_dims)
                if offset is not None:
                    self._apply_page_labels(pdf, offset)
                    ctx.metadata["page_label_offset"] = offset
                    logger.info("%s: page labels with offset %+d", ctx.slug, offset)
                else:
                    logger.info("%s: no consistent printed page numbers", ctx.slug)

            pdf.save(pdf_path, linearize=self.linearize)
        return ctx