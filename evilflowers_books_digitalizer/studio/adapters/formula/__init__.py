"""FormulaRecognizer adapters: the PaddleOCR-VL sidecar (``reflow.vlm``) and a null object.

The HTTP client for the ``vlm`` service lives in :mod:`.http` (``HttpFormulaRecognizer``).

The VLM runs out of process in its own venv (``reflow.vlm.run_jobs``); this adapter
only translates job shapes and picks the per-book cache. Output guards
(hallucination check, LaTeX fix-ups) stay in the caller so every recognizer is
judged by the same rules and rejections can be counted for the quality gate.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from evilflowers_books_digitalizer.reflow import vlm
from evilflowers_books_digitalizer.studio.ports import FormulaJob, PortError

#: per-book answer cache, next to the cleaned pages' directory
CACHE_NAME = "vlm_cache.json"


def _page_number(image: Path) -> int:
    """Stable int for ``vlm.Job.page``: the leaf number when the file is ``<leaf>.tif``.

    The VLM cache is keyed by ``page + bbox + task``; a stable page id is what
    keeps a cached answer from attaching to another page's region.
    """
    return int(image.stem) if image.stem.isdigit() else zlib.crc32(str(image).encode()) % 10**8


class NoFormula:
    """``formula_recognizer = "none"``: every job fails, formulas stay image crops."""

    def recognize(self, jobs: Sequence[FormulaJob]) -> dict[str, str]:
        return {}


class VlmSidecar:
    def __init__(self, settings: vlm.VlmSettings) -> None:
        self.settings = settings

    def recognize(self, jobs: Sequence[FormulaJob]) -> dict[str, str]:
        by_cache: dict[Path, list[FormulaJob]] = {}
        for job in jobs:
            by_cache.setdefault(job.image.parent.parent / CACHE_NAME, []).append(job)
        answers: dict[str, str] = {}
        for cache, group in by_cache.items():
            answers.update(self._run(cache, group))
        return answers

    def _run(self, cache: Path, jobs: list[FormulaJob]) -> dict[str, str]:
        translated = {vlm.Job(_page_number(j.image), j.image, _box(j), cast(vlm.Task, j.task)): j.key
                      for j in jobs}
        try:
            raw = vlm.run_jobs(list(translated), cache, self.settings)
        except (RuntimeError, OSError) as exc:
            raise PortError(f"VLM sidecar failed: {exc}") from exc
        return {translated[job]: raw[job.key] for job in translated if job.key in raw}


def _box(job: FormulaJob) -> tuple[int, int, int, int]:
    b = job.bbox
    return (b.x0, b.y0, b.x1, b.y1)
