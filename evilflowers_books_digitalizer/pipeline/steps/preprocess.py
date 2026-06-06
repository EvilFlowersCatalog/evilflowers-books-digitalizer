"""Preprocess step: raw scanner frames -> clean single-page images.

The faculty scans are two-page spreads on a black scanner bed; this step
splits, crops and deskews them using :mod:`...imaging` and optionally
converts text-only pages to grayscale (big compression win, no visible loss).
"""

from __future__ import annotations

import logging

import cv2

from evilflowers_books_digitalizer.imaging import (
    deskew,
    is_color_page,
    load_bgr,
    split_pages,
    to_grayscale,
    whiten_paper,
)
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)


class PreprocessScans(PipelineStep):
    """``ctx.tiffs`` (raw frames) -> cleaned per-page PNGs in ``work_dir/pages``.

    ``color_mode``:
    * ``"keep"`` — pages stay RGB (default: grayscale conversion measurably
      hurts OCR confidence on the yellowed faculty scans, ~91 -> ~88, while
      saving only ~4% file size — see notebook 03)
    * ``"gray"`` — everything becomes grayscale
    * ``"auto"`` — only pages with real color content (photos, stamps) stay RGB
    """

    name = "preprocess"

    def __init__(
        self,
        split: bool = True,
        do_deskew: bool = True,
        whiten: bool = True,
        color_mode: str = "keep",
        margin: int = 16,
        paper_threshold: int = 80,
    ):
        if color_mode not in ("keep", "gray", "auto"):
            raise ValueError(f"unknown color_mode: {color_mode!r}")
        self.split = split
        self.do_deskew = do_deskew
        self.whiten = whiten
        self.color_mode = color_mode
        self.margin = margin
        self.paper_threshold = paper_threshold

    def run(self, ctx: BookContext) -> BookContext:
        if not ctx.tiffs:
            raise ValueError(f"no scans for {ctx.slug} — run the download step first")

        pages_dir = ctx.work_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        outputs = []
        n_spreads = n_color = 0
        for frame_path in sorted(ctx.tiffs):
            frame = load_bgr(frame_path)
            if self.split:
                pages = split_pages(
                    frame, margin=self.margin, paper_threshold=self.paper_threshold
                )
                n_spreads += len(pages) > 1
            else:
                pages = [frame]

            for side, page in enumerate(pages):
                if self.do_deskew:
                    page, _ = deskew(page)
                if self.whiten:
                    page = whiten_paper(page)
                if self.color_mode == "gray" or (
                    self.color_mode == "auto" and not is_color_page(page)
                ):
                    page = to_grayscale(page)
                else:
                    n_color += 1
                out_path = pages_dir / f"{frame_path.stem}_{side}.png"
                cv2.imwrite(str(out_path), page)
                outputs.append(out_path)

        logger.info(
            "%s: %d frames -> %d pages (%d spreads split, %d color pages kept)",
            ctx.slug, len(ctx.tiffs), len(outputs), n_spreads, n_color,
        )
        ctx.metadata["n_frames"] = len(ctx.tiffs)
        ctx.metadata["n_pages"] = len(outputs)
        ctx.tiffs = outputs  # downstream steps consume the cleaned pages
        return ctx
