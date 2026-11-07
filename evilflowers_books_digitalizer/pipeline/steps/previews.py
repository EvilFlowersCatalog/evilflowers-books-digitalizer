"""Page previews: keep a small WebP of every cleaned page for the web viewer.

The cleaned ScanTailor pages live in the book's work dir, which low-storage
batch mode deletes when the book finishes. The viewer still needs "the
transformed page" to compare against the original TIFF, so this step writes a
downscaled copy next to the book's other outputs::

    <output_dir>/<slug>.pages/0001.webp, 0002.webp, ...

Placed right before rendering, so the previews show exactly the pages that went
into the PDFs (after DocRes, if enabled). Bitonal text pages compress to tens
of KiB at the default 1600 px height.
"""

from __future__ import annotations

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = None  # 600 dpi pages exceed Pillow's decompression-bomb guard


def preview_dir(output_dir: Path, slug: str) -> Path:
    return output_dir / f"{slug}.pages"


def preview_name(index: int) -> str:
    """1-based, zero-padded file name of page ``index`` (0-based)."""
    return f"{index + 1:04d}.webp"


class PagePreviews(PipelineStep):
    """``ctx.tiffs`` -> ``<output_dir>/<slug>.pages/NNNN.webp`` (``artifacts['previews']``)."""

    name = "previews"

    def __init__(self, max_px: int = 1600, quality: int = 80, workers: int = 4) -> None:
        self.max_px = max_px
        self.quality = quality
        self.workers = workers

    def _write(self, src: Path, dst: Path) -> None:
        with Image.open(src) as im:
            im.draft("L", (self.max_px, self.max_px))  # cheap JPEG downscale when possible
            img = im.convert("L") if im.mode in ("1", "L", "I;16") else im.convert("RGB")
            img.thumbnail((self.max_px, self.max_px), Image.Resampling.LANCZOS)
            img.save(dst, "WEBP", quality=self.quality, method=4)

    def run(self, ctx: BookContext) -> BookContext:
        if not ctx.tiffs:
            raise ValueError(f"no pages for {ctx.slug} — run the scantailor step first")
        out = preview_dir(ctx.output_dir, ctx.slug)
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        jobs = [(p, out / preview_name(i)) for i, p in enumerate(ctx.tiffs)]
        with ThreadPoolExecutor(self.workers) as pool:
            list(pool.map(lambda job: self._write(*job), jobs))
        # page -> original frame map travels with the previews (db import reads it)
        (out / "pages.json").write_text(json.dumps(ctx.metadata.get("page_map", []), indent=0))
        ctx.artifacts["previews"] = out
        size_mb = sum(p.stat().st_size for p in out.iterdir()) / 1e6
        logger.info("%s: %d page previews (%.1f MB)", ctx.slug, len(jobs), size_mb)
        return ctx
