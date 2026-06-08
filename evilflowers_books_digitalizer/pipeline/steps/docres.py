"""DocRes step: AI appearance enhancement of cleaned pages (optional).

DocRes (CVPR 2024, https://github.com/ZZZHANG-jx/DocRes) is a single model
covering dewarping / deshadowing / appearance / deblurring / binarization.
The ``appearance`` task whitens paper and removes show-through while keeping
tones (library stamps, photos) — the right complement to ScanTailor's
``color_grayscale`` output, where binarization would destroy content.

Geometry note: at our page sizes (>1600px) the appearance task works via a
shadow map divided out of the *original-resolution* image, so pixels don't
move — OCR can run on the input pages and stays aligned with the output.

The model runs out-of-process (its own venv: torch + weights, ~2 min/page on
Apple Silicon MPS, slower on plain CPU) — keep ``enabled = false`` for bulk
text-only batches and turn it on for covers / photo-heavy books.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)


class DocResEnhance(PipelineStep):
    """``ctx.tiffs`` -> DocRes-enhanced pages in ``work_dir/docres_pages``."""

    name = "docres"

    def __init__(
        self,
        repo: str = "~/.local/share/evilflowers-tools/DocRes",
        python: str = "~/.local/share/evilflowers-tools/venv-docres/bin/python",
        task: str = "appearance",
    ):
        self.repo = Path(repo).expanduser()
        self.python = Path(python).expanduser()
        self.task = task

    def run(self, ctx: BookContext) -> BookContext:
        if not ctx.tiffs:
            raise ValueError(f"no pages for {ctx.slug} — run the scantailor step first")
        if not self.python.exists() or not (self.repo / "inference.py").exists():
            raise RuntimeError(
                f"DocRes not found (repo={self.repo}, python={self.python}) — "
                "see Dockerfile / README, or disable [docres] in pipeline.toml"
            )

        in_dir = ctx.tiffs[0].parent
        out_dir = ctx.work_dir / "docres_pages"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)

        result = subprocess.run(
            [
                str(self.python),
                "inference.py",
                "--task",
                self.task,
                "--im_path",
                str(in_dir),
                "--out_folder",
                str(out_dir),
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"DocRes failed for {ctx.slug} (exit {result.returncode}): {result.stderr[-2000:]}"
            )

        # inference.py writes <stem>_<task>.<ext> next to each input name
        pages = []
        for tiff in ctx.tiffs:
            out = out_dir / f"{tiff.stem}_{self.task}{tiff.suffix}"
            if not out.exists():
                raise RuntimeError(f"DocRes output missing for {ctx.slug}: {out.name}")
            pages.append(out)

        logger.info("%s: DocRes %s on %d pages", ctx.slug, self.task, len(pages))
        ctx.metadata["docres_task"] = self.task
        ctx.tiffs = pages
        return ctx
