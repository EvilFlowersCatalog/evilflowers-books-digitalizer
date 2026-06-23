"""Finalize step: reading-experience polish applied to every produced PDF.

1. **Outline bookmarks** — headings detected in the Tesseract hOCR (lines
   noticeably larger than the body text) become a PDF outline. Consecutive
   heading lines on one page are *merged* into a single bookmark — a wrapped
   title like "2 DRUHY A KATEGÓRIE / INŽINIERSKYCH SIETÍ" is one entry, not two
   (the old code emitted a bookmark per line, fragmenting navigation).
2. **Page labels** — printed page numbers near the page edges fix a physical→
   printed offset; the PDF then carries ``/PageLabels`` (front matter as roman).
3. **Linearization** — web-optimized save for the distribution copy (fast first
   page over HTTP range requests); the archival master is saved un-linearized.

All three degrade gracefully and never fail the pipeline over a heuristic.
"""

from __future__ import annotations

import logging
import re
import statistics

import pikepdf

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep
from evilflowers_books_digitalizer.pipeline.hocr import HocrLine, parse_hocr

logger = logging.getLogger(__name__)

_ROMAN = re.compile(r"^[ivxlcdm]{1,6}$", re.IGNORECASE)
_ARABIC = re.compile(r"^\d{1,4}$")
#: characters that betray a table ruling, caption, author line or OCR misread —
#: not a chapter heading. Commas/colons exclude author lines ("Doc. Ing. X,
#: CSc.") and garbage ("Aja: PI") that the old heuristic wrongly bookmarked.
_NOISE_CHARS = set("|[]{}<>=_~\\:;,")


def _roman_to_int(s: str) -> int:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total, prev = 0, 0
    for ch in reversed(s.lower()):
        v = values[ch]
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total


class FinalizePdf(PipelineStep):
    """Add bookmarks + page labels and (optionally) linearize each produced PDF."""

    name = "finalize"

    def __init__(
        self,
        bookmarks: bool = True,
        page_labels: bool = True,
        linearize: bool = True,
        # heading heuristics, calibrated on the lab06 fad sample:
        size_ratio: float = 1.18,  # heading x_size >= body median * ratio
        max_words: int = 6,  # per merged title
        alpha_ratio: float = 0.78,  # letters / non-space chars
        max_per_page: int = 2,
        # printed page numbers live within this margin band (fraction of height)
        edge_band: float = 0.08,
        min_number_hits: int = 3,  # pages agreeing on one offset before we trust it
    ) -> None:
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

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"[^\w)]+$", "", text.strip())

    def _is_heading_line(self, line: HocrLine, body_size: float) -> bool:
        if line.size < body_size * self.size_ratio:
            return False
        text = self._clean(line.text)
        words = text.split()
        if not words or len(words) > self.max_words:
            return False
        if _NOISE_CHARS & set(text):
            return False
        if not (text[0].isupper() or text[0].isdigit()):
            return False
        if text.endswith("-"):  # hyphenated continuation, not a heading start
            return False
        if words[-1].isdigit():  # table scores / page refs ("... — 1")
            return False
        # a lone capital inside a mixed-case line is OCR debris, not an initial
        if not text.isupper() and any(len(w) == 1 and w.isupper() for w in words):
            return False
        letters = sum(ch.isalpha() for ch in text)
        non_space = sum(not ch.isspace() for ch in text)
        return letters >= 4 and non_space > 0 and letters / non_space >= self.alpha_ratio

    def _detect_headings(self, lines: list[HocrLine]) -> list[tuple[int, str]]:
        """One bookmark per heading, merging wrapped lines on the same page."""
        body_sizes = [ln.size for ln in lines if len(ln.text.split()) >= 3]
        if len(body_sizes) < 10:
            return []
        body = statistics.median(body_sizes)

        # group consecutive heading lines on the same page into one title
        found: list[tuple[int, str]] = []
        per_page: dict[int, int] = {}
        i = 0
        while i < len(lines):
            if not self._is_heading_line(lines[i], body):
                i += 1
                continue
            page = lines[i].page
            parts = [self._clean(lines[i].text)]
            ref_size = lines[i].size
            j = i + 1
            # absorb following lines on the same page of similar size (wrapped title)
            while (
                j < len(lines)
                and lines[j].page == page
                and abs(lines[j].size - ref_size) <= ref_size * 0.15
                and self._is_heading_line(lines[j], body)
            ):
                parts.append(self._clean(lines[j].text))
                j += 1
            title = " ".join(parts)
            words = title.split()
            if 1 <= len(words) <= self.max_words and per_page.get(page, 0) < self.max_per_page:
                if not found or found[-1] != (page, title):  # dedup
                    found.append((page, title))
                    per_page[page] = per_page.get(page, 0) + 1
            i = j
        return found

    # -- printed page numbers -> /PageLabels ---------------------------------

    def _detect_page_offset(
        self, lines: list[HocrLine], page_dims: list[tuple[int, int]]
    ) -> int | None:
        votes: dict[int, int] = {}
        for line in lines:
            if line.page >= len(page_dims):
                continue
            _w, h = page_dims[line.page]
            if not h:
                continue
            top, bottom = line.bbox[1], line.bbox[3]
            if top > h * self.edge_band and bottom < h * (1 - self.edge_band):
                continue
            text = line.text.strip()
            if _ARABIC.fullmatch(text):
                printed = int(text)
            else:
                continue  # roman front matter handled via the arabic offset
            votes[printed - line.page] = votes.get(printed - line.page, 0) + 1
        if not votes:
            return None
        offset, hits = max(votes.items(), key=lambda kv: kv[1])
        return offset if hits >= self.min_number_hits else None

    def _apply_page_labels(self, pdf: pikepdf.Pdf, offset: int) -> None:
        nums: list = []
        start = max(0, 1 - offset)  # first physical page with printed number >= 1
        if start > 0:
            nums += [0, pikepdf.Dictionary(S=pikepdf.Name.r)]
        nums += [start, pikepdf.Dictionary(S=pikepdf.Name.D, St=start + offset)]
        pdf.Root.PageLabels = pikepdf.Dictionary(Nums=pikepdf.Array(nums))

    # -- step ----------------------------------------------------------------

    def run(self, ctx: BookContext) -> BookContext:
        outputs = ctx.pdf_outputs()
        if not outputs:
            raise ValueError(f"no pdf for {ctx.slug} — run the render step first")

        hocr = ctx.artifacts.get("hocr")
        lines: list[HocrLine] = []
        page_dims: list[tuple[int, int]] = []
        if hocr is not None and hocr.exists():
            doc = parse_hocr(hocr.read_text(errors="ignore"))
            lines, page_dims = doc.lines, doc.page_dims
        elif self.bookmarks or self.page_labels:
            logger.warning("%s: no hOCR artifact — skipping bookmarks/page labels", ctx.slug)

        headings = self._detect_headings(lines) if (self.bookmarks and lines) else []
        offset = self._detect_page_offset(lines, page_dims) if (self.page_labels and lines) else None
        out_meta = ctx.metadata.get("outputs", {})

        for name, pdf_path in outputs:
            linearize = out_meta.get(name, {}).get("linearize", self.linearize)
            with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
                n_pages = len(pdf.pages)
                page_headings = [(p, t) for p, t in headings if p < n_pages]
                if page_headings:
                    with pdf.open_outline() as outline:
                        outline.root.extend(
                            pikepdf.OutlineItem(title, page) for page, title in page_headings
                        )
                if offset is not None:
                    self._apply_page_labels(pdf, offset)
                pdf.save(pdf_path, linearize=linearize)

        if headings:
            ctx.metadata["n_bookmarks"] = len(headings)
            logger.info("%s: %d outline bookmarks", ctx.slug, len(headings))
        if offset is not None:
            ctx.metadata["page_label_offset"] = offset
        return ctx
