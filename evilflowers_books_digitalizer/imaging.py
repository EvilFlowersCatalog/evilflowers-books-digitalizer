"""Scan image preprocessing: spread detection, splitting, cropping, deskew.

The faculty scans are two-page spreads photographed on a black scanner bed.
To get "nice books" the raw frames need to be turned into clean single pages
before OCR:

1. detect the bright paper area on the black background
2. split double-page spreads at the gutter
3. crop each page (small margin kept)
4. deskew each page using the paper edge angle

All functions work on OpenCV BGR/grayscale ``numpy`` arrays so they can be
tuned interactively in notebooks; :class:`...steps.preprocess.PreprocessScans`
wraps them for pipeline use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)

#: Downscale factor used for analysis (full resolution is only used for output).
ANALYSIS_SCALE = 8


@dataclass
class SpreadLayout:
    """Result of analysing one scanned frame."""

    bbox: tuple[int, int, int, int]  # paper area: x0, y0, x1, y1 (full-res coords)
    gutter_x: int | None  # split position (full-res), None for single pages
    paper_ratio: float  # fraction of the frame covered by paper

    @property
    def is_spread(self) -> bool:
        return self.gutter_x is not None


def load_bgr(path) -> np.ndarray:
    """Read an image file into a BGR array (what all functions here expect)."""
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
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return mask


def analyze_spread(
    img: np.ndarray,
    paper_threshold: int = 80,
    min_paper_ratio: float = 0.05,
    spread_aspect: float = 1.15,
    min_gutter_brightness: float = 0.85,
) -> SpreadLayout:
    """Locate the paper area in a frame and decide whether it is a spread.

    ``paper_threshold``: gray level separating paper from the black bed.
    ``min_paper_ratio``: below this paper fraction the frame is treated as-is
    (e.g. covers shot almost entirely on the black bed).
    ``spread_aspect``: width/height above which the paper area is considered
    a two-page spread and searched for a gutter.
    ``min_gutter_brightness``: the chosen gutter column must reach this
    fraction of the paper brightness — if not, content runs across the fold
    (wide tables, fold-outs) and the frame is kept as one page.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (w // ANALYSIS_SCALE, h // ANALYSIS_SCALE))
    mask = _paper_mask(small, paper_threshold)

    paper_ratio = float(np.count_nonzero(mask)) / mask.size
    if paper_ratio < min_paper_ratio:
        return SpreadLayout(bbox=(0, 0, w, h), gutter_x=None, paper_ratio=paper_ratio)

    ys, xs = np.nonzero(mask)
    x0, x1 = int(xs.min()), int(xs.max() + 1)
    y0, y1 = int(ys.min()), int(ys.max() + 1)

    gutter_x = None
    if (x1 - x0) / max(y1 - y0, 1) >= spread_aspect:
        # The fold shadow itself is faint, but the gutter sits inside a bright
        # band (facing page margins). Page margins elsewhere can be even
        # brighter, so bias the search toward the centre of the paper area
        # with a Gaussian prior before taking the brightest column.
        width = x1 - x0
        column_brightness = small[y0:y1, x0:x1].mean(axis=0)
        window = max(width // 30, 3)
        smoothed = np.convolve(column_brightness, np.ones(window) / window, mode="same")
        prior = np.exp(-0.5 * ((np.arange(width) - width / 2) / (width / 8)) ** 2)
        third = width // 3
        candidate = third + int(np.argmax((smoothed * prior)[third : 2 * third]))
        # content printed across the fold (wide tables, fold-outs) -> no split
        paper_brightness = float(np.percentile(smoothed, 75))
        if smoothed[candidate] >= min_gutter_brightness * paper_brightness:
            gutter_x = (x0 + candidate) * ANALYSIS_SCALE

    scale = ANALYSIS_SCALE
    return SpreadLayout(
        bbox=(x0 * scale, y0 * scale, min(x1 * scale, w), min(y1 * scale, h)),
        gutter_x=gutter_x,
        paper_ratio=paper_ratio,
    )


def _tight_crop(img: np.ndarray, paper_threshold: int, margin: int) -> np.ndarray:
    """Trim remaining scanner-bed black from the borders of a single page."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (max(w // ANALYSIS_SCALE, 1), max(h // ANALYSIS_SCALE, 1)))
    mask = _paper_mask(small, paper_threshold)
    if not np.count_nonzero(mask):
        return img
    ys, xs = np.nonzero(mask)
    x0 = max(int(xs.min()) * ANALYSIS_SCALE - margin, 0)
    x1 = min((int(xs.max()) + 1) * ANALYSIS_SCALE + margin, w)
    y0 = max(int(ys.min()) * ANALYSIS_SCALE - margin, 0)
    y1 = min((int(ys.max()) + 1) * ANALYSIS_SCALE + margin, h)
    return img[y0:y1, x0:x1]


def trim_edge_shadows(
    img: np.ndarray,
    search_frac: float = 0.06,
    dark_pixel: int = 100,
    dark_frac: float = 0.12,
    clean_frac: float = 0.06,
    margin: int = 6,
) -> np.ndarray:
    """Trim binding/spine artifacts hugging the page edges.

    Scans carry various junk in the outer ``search_frac`` of the page: black
    bed remnants, fold shadow slivers on the gutter side, dashed binding-hole
    strips, edges of the neighbouring page or the book's back. A column/row is
    junk when more than ``dark_frac`` of its pixels are darker than
    ``dark_pixel`` — a *fraction* test, because dashed strips keep a bright
    mean. Everything from the outermost junk line to the edge is cut.

    Full-bleed pages (photos/design running to the edge) are protected: the
    cut only happens if the band just inside it is clean (< ``clean_frac``
    dark pixels), i.e. real page margin follows the junk.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    dark = (gray < dark_pixel).astype(np.float32)
    col_frac = dark.mean(axis=0)
    row_frac = dark.mean(axis=1)

    def cut(fracs: np.ndarray, size: int) -> tuple[int, int]:
        zone = max(int(size * search_frac), 1)
        inner = max(int(size * 0.04), 4)

        low = 0
        junk = np.where(fracs[:zone] > dark_frac)[0]
        if len(junk):
            candidate = int(junk.max()) + margin
            if fracs[candidate : candidate + inner].mean() < clean_frac:
                low = candidate

        high = size
        junk = np.where(fracs[size - zone :] > dark_frac)[0]
        if len(junk):
            candidate = size - zone + int(junk.min()) - margin
            if fracs[max(candidate - inner, 0) : candidate].mean() < clean_frac:
                high = candidate

        return max(low, 0), min(max(high, low + 1), size)

    x0, x1 = cut(col_frac, w)
    y0, y1 = cut(row_frac, h)
    return img[y0:y1, x0:x1]


def split_pages(
    img: np.ndarray,
    layout: SpreadLayout | None = None,
    margin: int = 16,
    paper_threshold: int = 80,
    **analyze_kwargs,
) -> list[np.ndarray]:
    """Crop a frame to its paper area and split spreads into single pages.

    Each resulting page is re-cropped individually (the bbox of a spread is
    the union of two pages) and binding/fold shadow bands at the edges are
    trimmed.
    """
    if layout is None:
        layout = analyze_spread(img, paper_threshold=paper_threshold, **analyze_kwargs)
    h, w = img.shape[:2]
    x0, y0, x1, y1 = layout.bbox
    x0, y0 = max(x0 - margin, 0), max(y0 - margin, 0)
    x1, y1 = min(x1 + margin, w), min(y1 + margin, h)

    if not layout.is_spread:
        halves = [img[y0:y1, x0:x1]]
    else:
        gx = layout.gutter_x
        halves = [img[y0:y1, x0:gx], img[y0:y1, gx:x1]]
    return [trim_edge_shadows(_tight_crop(half, paper_threshold, margin)) for half in halves]


def deskew(
    img: np.ndarray,
    max_angle: float = 5.0,
) -> tuple[np.ndarray, float]:
    """Straighten a page using the dominant angle of its text lines.

    Text pixels are merged into horizontal line blobs; the median blob angle
    is the skew. Returns the rotated image and the applied angle in degrees.
    Angles above ``max_angle`` are treated as detection errors and skipped.
    (OCRmyPDF's ``--deskew`` is an alternative that runs after assembly.)
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (max(w // 4, 1), max(h // 4, 1)))
    # text = dark on bright paper
    binary = cv2.adaptiveThreshold(
        small, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15
    )
    # merge characters into text-line blobs
    merged = cv2.morphologyEx(
        binary, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (31, 1))
    )
    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    angles = []
    for contour in contours:
        (_, _), (cw, ch), angle = cv2.minAreaRect(contour)
        if cw < ch:
            cw, ch = ch, cw
            angle += 90
        if angle > 45:
            angle -= 90
        # only wide, flat blobs (real text lines) vote
        if cw > small.shape[1] * 0.25 and ch < cw * 0.2 and abs(angle) <= max_angle:
            angles.append(angle)
    if len(angles) < 3:
        return img, 0.0
    angle = float(np.median(angles))
    if abs(angle) < 0.05:
        return img, 0.0

    center = (w / 2, h / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        img, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )
    return rotated, float(angle)


def is_color_page(img: np.ndarray, saturation_threshold: int = 60, ratio: float = 0.02) -> bool:
    """Heuristic: does the page carry real color (photos, stamps, covers)?

    True if more than ``ratio`` of pixels exceed ``saturation_threshold``
    saturation — pages that are effectively grayscale compress much better
    when stored as such. The defaults are tuned on the faculty scans: yellowed
    paper stays below the threshold, library stamps and covers exceed it.
    """
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    saturated = np.count_nonzero(hsv[:, :, 1] > saturation_threshold)
    return saturated / hsv[:, :, 1].size > ratio


def to_grayscale(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def whiten_paper(
    img: np.ndarray,
    paper_percentile: float = 90.0,
    max_gain: float = 1.6,
    paper_threshold: int = 80,
) -> np.ndarray:
    """White-balance a page so the (yellowed) paper becomes white.

    Estimates the paper colour as the per-channel ``paper_percentile`` of the
    bright pixels and applies a per-channel gain mapping it to pure white —
    removing the yellow cast and lifting the background uniformly. Gains are
    capped at ``max_gain`` so dark or photo-heavy pages aren't blown out;
    photos and stamps keep their local contrast (a global linear gain only).
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    bright = gray > max(paper_threshold, int(np.percentile(gray, 50)))
    if not np.count_nonzero(bright):
        return img
    paper_color = np.array(
        [np.percentile(img[:, :, c][bright], paper_percentile) for c in range(3)]
    )
    gains = np.clip(255.0 / np.maximum(paper_color, 1.0), 1.0, max_gain)
    return np.clip(img.astype(np.float32) * gains, 0, 255).astype(np.uint8)
