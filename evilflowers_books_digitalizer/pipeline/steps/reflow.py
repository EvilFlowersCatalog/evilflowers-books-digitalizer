"""Reflow step: cleaned pages + hOCR -> ``<slug>.epub`` (born-digital reading edition).

Opt-in (``[reflow] enabled = true``). Runs after the PDFs are final, while the
cleaned pages are still in the work dir. With ``vlm = "none"`` formulas stay as
image crops; with ``vlm = "paddleocr-vl"`` they become MathML (see
``docs/reflow_edition.md``).

Fail-safe by design: the EPUB is a *derived* edition, so any error is logged
and recorded in ``metadata['reflow']`` — it never fails the book.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep
from evilflowers_books_digitalizer.reflow import ReflowSettings, build_edition

logger = logging.getLogger(__name__)


class ReflowEdition(PipelineStep):
    """``ctx.tiffs`` + ``artifacts['hocr']`` -> ``artifacts['epub']``."""

    name = "reflow"

    def __init__(self, settings: ReflowSettings) -> None:
        self.settings = settings

    def run(self, ctx: BookContext) -> BookContext:
        hocr = ctx.artifacts.get("hocr")
        if hocr is None or not ctx.tiffs:
            ctx.metadata["reflow"] = {"status": "skipped", "error": "no hOCR / pages"}
            return ctx
        if shutil.which(self.settings.pandoc) is None:
            ctx.metadata["reflow"] = {"status": "error", "error": "pandoc not found"}
            logger.error("%s: reflow needs pandoc — skipping the EPUB", ctx.slug)
            return ctx
        out = ctx.output_dir / f"{ctx.slug}.epub"
        try:
            report = build_edition(
                ctx.tiffs, hocr, out, ctx.work_dir / "reflow", ctx.metadata, self.settings,
                cover=ctx.artifacts.get("cover"),
            )
        except Exception as exc:  # noqa: BLE001 — a derived edition must not fail the book
            logger.error("%s: reflow failed: %s", ctx.slug, exc, exc_info=True)
            ctx.metadata["reflow"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]}
            return ctx
        ctx.artifacts["epub"] = out
        ctx.metadata["reflow"] = {"status": "ok", "path": str(out),
                                  "mb": round(out.stat().st_size / 1e6, 2),
                                  **dataclasses.asdict(report)}
        logger.info("%s: EPUB %s (%d formulas as math, %d as images, %d page images)",
                    ctx.slug, out.name, report.formulas + report.mixed, report.formula_images,
                    report.page_images)
        return ctx
