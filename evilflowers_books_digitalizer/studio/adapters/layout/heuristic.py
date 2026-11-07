"""LayoutDetector over the reflow heuristics (``reflow.analyze``): hOCR + pixels -> regions.

The reflow analysis was built to *write* an edition; here it only *proposes*
regions, and the operator plus :func:`domain.fuse` decide. Confidence comes from
Tesseract: a text block is as trustworthy as the words inside it. Non-text blocks
(formulas, figures) are classified precisely *because* OCR failed there, so OCR
confidence says nothing about them — they get a fixed prior instead.

Library stamps (saturated blue/violet ink, ``reflow.analyze.stamp_mask``) become
STAMP proposals; ``fuse`` then asks an operator before anything is erased.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from evilflowers_books_digitalizer.reflow.analyze import Block, analyze_page, stamp_mask
from evilflowers_books_digitalizer.reflow.model import Page, Word, parse_pages
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    RegionKind,
    RegionProposal,
    RegionSource,
)
from evilflowers_books_digitalizer.studio.ports import OcrPage, PortError

#: reflow block kind -> region kind ("heading" is resolved by level, see _kind)
BLOCK_KIND: dict[str, RegionKind] = {
    "para": RegionKind.TEXT, "list": RegionKind.LIST_ITEM, "olist": RegionKind.LIST_ITEM,
    "table": RegionKind.TABLE, "formula": RegionKind.FORMULA, "figure": RegionKind.PICTURE,
    "caption": RegionKind.CAPTION, "eqno": RegionKind.TEXT, "page": RegionKind.PICTURE,
}
_TEXT_KINDS = frozenset({RegionKind.TEXT, RegionKind.LIST_ITEM, RegionKind.CAPTION,
                         RegionKind.TITLE, RegionKind.SECTION_HEADER, RegionKind.TABLE})


def read_page_image(image: Path) -> np.ndarray:
    """8-bit BGR or gray, whatever the TIFF flavour (bitonal, 16-bit, alpha)."""
    img = cv2.imread(str(image), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise PortError(f"cannot read page image {image}")
    if img.dtype != np.uint8:
        img = cv2.convertScaleAbs(img, alpha=255.0 / max(1, int(img.max())))
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img


def _kind(block: Block) -> RegionKind:
    if block.kind == "heading":
        return RegionKind.TITLE if block.level <= 1 else RegionKind.SECTION_HEADER
    return BLOCK_KIND.get(block.kind, RegionKind.OTHER)


def _text(block: Block) -> str | None:
    if block.items:
        return "\n".join(block.items)
    if block.rows:
        return "\n".join("\t".join(row) for row in block.rows)
    return block.text or None


def words_confidence(words: list[Word], box: BBox, default: float) -> float:
    """Mean Tesseract confidence (0..1) of the words whose centre lies in ``box``."""
    inside = [w.conf for w in words
              if box.x0 <= (w.bbox[0] + w.bbox[2]) / 2 < box.x1
              and box.y0 <= (w.bbox[1] + w.bbox[3]) / 2 < box.y1]
    return sum(inside) / len(inside) / 100 if inside else default


class HeuristicLayout:
    name = "heuristic"

    def __init__(self, *, non_text_confidence: float = 0.7, empty_text_confidence: float = 0.5,
                 stamp_min_area: float = 0.0005, stamp_confidence: float = 0.8) -> None:
        self.non_text_confidence = non_text_confidence
        self.empty_text_confidence = empty_text_confidence
        self.stamp_min_area = stamp_min_area      # fraction of the page area
        self.stamp_confidence = stamp_confidence

    def detect(self, image: Path, *, ocr: OcrPage | None = None) -> list[RegionProposal]:
        img = read_page_image(image)
        proposals = self._stamps(img)
        page = self._hocr_page(ocr, img)
        if page is not None:
            words = page.words
            for block in analyze_page(page, img, 0, heads=set()):
                if (proposal := self._proposal(block, words)) is not None:
                    proposals.append(proposal)
        return proposals

    @staticmethod
    def _hocr_page(ocr: OcrPage | None, img: np.ndarray) -> Page | None:
        pages = parse_pages(ocr.hocr) if ocr is not None and ocr.hocr else []
        if not pages or not pages[0].pars:
            return None
        page = pages[0]
        if not page.width or not page.height:
            page.height, page.width = img.shape[:2]
        return page

    def _proposal(self, block: Block, words: list[Word]) -> RegionProposal | None:
        try:
            box = BBox.of(block.bbox)
        except ValueError:      # degenerate block (a single OCR speck)
            return None
        kind = _kind(block)
        conf = (words_confidence(words, box, self.empty_text_confidence) if kind in _TEXT_KINDS
                else self.non_text_confidence)
        return RegionProposal(kind, box, RegionSource.HEURISTIC, round(conf, 3), _text(block))

    def _stamps(self, img: np.ndarray) -> list[RegionProposal]:
        mask = stamp_mask(img)
        if not mask.any():
            return []
        min_area = self.stamp_min_area * img.shape[0] * img.shape[1]
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        return [RegionProposal(RegionKind.STAMP, BBox(int(x), int(y), int(x + w), int(y + h)),
                               RegionSource.HEURISTIC, self.stamp_confidence)
                for x, y, w, h, area in stats[1:n] if area >= min_area]
