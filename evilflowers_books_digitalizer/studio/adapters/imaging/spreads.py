"""LeafPlanner: spread detection on the raw frames (the orchestrator's only pixel work).

A full run fixes every leaf's frame, side and crop *before* any clean job is sent, so leaf
numbers are stable across batches and re-runs (operator overrides are keyed by them). The
faculty scans are two-page spreads photographed on a black scanner bed, or on white copier
glass; :func:`analyze_spread` finds the paper and, for a spread, the gutter. Cleaning itself
(ScanTailor, DocRes) is evilflowers-scan-service's: it cuts each planned leaf out of its frame.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from evilflowers_books_digitalizer.studio.domain import BBox
from evilflowers_books_digitalizer.studio.metadata import HandSide
from evilflowers_books_digitalizer.studio.ports import LeafPlan

logger = logging.getLogger(__name__)

#: Downscale factor used for analysis (full resolution is only used for the boxes).
ANALYSIS_SCALE = 8


@dataclass(frozen=True, slots=True)
class SpreadLayout:
    """Result of analysing one scanned frame."""

    bbox: tuple[int, int, int, int]  # paper area: x0, y0, x1, y1 (full-res coords)
    gutter_x: int | None  # split position (full-res), None for single pages
    paper_ratio: float  # fraction of the frame covered by paper

    @property
    def is_spread(self) -> bool:
        return self.gutter_x is not None


def load_bgr(path: Path | str) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot read image: {path}")
    return img


def _paper_mask(small_gray: np.ndarray, paper_threshold: int) -> np.ndarray:
    """Binary mask of the (bright) paper on the (black) scanner bed."""
    blurred = cv2.GaussianBlur(small_gray, (5, 5), 0)
    _, mask = cv2.threshold(blurred, paper_threshold, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)


def _ink_bbox(ink: np.ndarray, min_line_frac: float = 0.01) -> tuple[int, int, int, int] | None:
    """Bbox of real content: rows/columns with at least ``min_line_frac`` ink (ignores specks)."""
    cols = np.where(ink.mean(axis=0) > min_line_frac)[0]
    rows = np.where(ink.mean(axis=1) > min_line_frac)[0]
    if not len(cols) or not len(rows):
        return None
    return int(cols.min()), int(rows.min()), int(cols.max() + 1), int(rows.max() + 1)


def analyze_spread(img: np.ndarray, paper_threshold: int = 80, min_paper_ratio: float = 0.05,
                   spread_aspect: float = 1.15, max_gutter_density: float = 0.06,
                   centre_penalty: float = 0.2) -> SpreadLayout:
    """Locate the paper area in a frame and decide whether it is a spread.

    * **black scanner bed**: paper bbox < frame; the spread decision and gutter search run
      within the paper area;
    * **white copier glass**: everything is "paper" (``paper_ratio`` ≈ 1); the *ink* bbox
      locates the spread, which may sit anywhere on the glass.

    The spread decision uses the ink-content aspect; the gutter is the lowest ink-density
    column band near the content centre. If even the best band has more ink than
    ``max_gutter_density``, content runs across the fold (wide tables, fold-outs) and the
    frame is kept as one page.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (max(1, w // ANALYSIS_SCALE), max(1, h // ANALYSIS_SCALE)))
    mask = _paper_mask(small, paper_threshold)

    paper_ratio = float(np.count_nonzero(mask)) / mask.size
    if paper_ratio < min_paper_ratio:
        return SpreadLayout(bbox=(0, 0, w, h), gutter_x=None, paper_ratio=paper_ratio)

    ys, xs = np.nonzero(mask)
    x0, x1 = int(xs.min()), int(xs.max() + 1)
    y0, y1 = int(ys.min()), int(ys.max() + 1)

    # ink = dark pixels on paper; threshold relative to the paper brightness
    paper_region = small[y0:y1, x0:x1]
    paper_brightness = float(np.percentile(paper_region, 75))
    ink = (paper_region < 0.6 * paper_brightness).astype(np.float32)

    gutter_x = None
    content = _ink_bbox(ink)
    if content is not None:
        cx0, cy0, cx1, cy1 = content
        if (cx1 - cx0) / max(cy1 - cy0, 1) >= spread_aspect:
            density = ink[cy0:cy1, cx0:cx1].mean(axis=0)
            width = cx1 - cx0
            window = max(width // 25, 3)
            smoothed = np.convolve(density, np.ones(window) / window, mode="same")
            distance = np.abs(np.arange(width) - width / 2) / width
            third = width // 3
            score = (smoothed + centre_penalty * distance)[third: 2 * third]
            if len(score):
                candidate = third + int(np.argmin(score))
                if smoothed[candidate] <= max_gutter_density:
                    gutter_x = (x0 + cx0 + candidate) * ANALYSIS_SCALE

    scale = ANALYSIS_SCALE
    return SpreadLayout(bbox=(x0 * scale, y0 * scale, min(x1 * scale, w), min(y1 * scale, h)),
                        gutter_x=gutter_x, paper_ratio=paper_ratio)


def single_page_side(leaf_num: int) -> HandSide:
    """Hand side of a one-page frame: leaf 0 is a recto, then they alternate (lr books)."""
    return HandSide.RIGHT if leaf_num % 2 == 0 else HandSide.LEFT


class SpreadPlanner:
    """:class:`~evilflowers_books_digitalizer.studio.ports.LeafPlanner` on :func:`analyze_spread`.

    ``split = False`` keeps every frame as one leaf (``[pipeline.scantailor] split``, the same
    switch scan-service reads)."""

    def __init__(self, *, split: bool = True) -> None:
        self.split = split

    def plan(self, frames: Sequence[Path]) -> list[LeafPlan]:
        plans: list[LeafPlan] = []
        for frame in frames:
            try:
                img = load_bgr(frame)
            except ValueError:
                logger.warning("skipping unreadable frame %s", frame)
                continue
            plans.extend(self._plan_frame(frame, img, len(plans)))
        return plans

    def _plan_frame(self, frame: Path, img: np.ndarray, first: int) -> list[LeafPlan]:
        h, w = img.shape[:2]
        layout = analyze_spread(img) if self.split else None
        if layout is not None and layout.gutter_x is not None:
            gx = int(layout.gutter_x)
            return [LeafPlan(first, frame, 0, BBox(0, 0, gx, h), 0, HandSide.LEFT.value),
                    LeafPlan(first + 1, frame, 1, BBox(gx, 0, w, h), 0, HandSide.RIGHT.value)]
        return [LeafPlan(first, frame, None, BBox(0, 0, w, h), 0, single_page_side(first).value)]
