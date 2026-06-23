"""Cover step: render a stylish thumbnail per book from catalog metadata.

Standalone artifact (the PDF is left untouched) intended for the EvilFlowers
catalog/UI. Fail-safe: any render error is logged and the pipeline continues —
a cover is never worth failing a months-long batch over.
"""

from __future__ import annotations

import logging

from evilflowers_books_digitalizer.covers.renderer import CoverRenderer
from evilflowers_books_digitalizer.metadata.models import BookMetadata
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)


class GenerateCover(PipelineStep):
    """Render ``artifacts['cover']`` from ``ctx.metadata`` (book_metadata)."""

    name = "cover"

    def __init__(self, renderer: CoverRenderer):
        self.renderer = renderer

    def run(self, ctx: BookContext) -> BookContext:
        try:
            record = self._metadata(ctx)
            path = ctx.output_dir / f"{ctx.slug}.cover{self.renderer.suffix}"
            ctx.output_dir.mkdir(parents=True, exist_ok=True)
            self.renderer.render_to_file(record, path)
            ctx.artifacts["cover"] = path
            logger.info("%s: cover -> %s", ctx.slug, path.name)
        except Exception as exc:  # noqa: BLE001 — never fail a batch over a cover
            logger.warning("%s: cover generation failed: %s", ctx.slug, exc)
        return ctx

    @staticmethod
    def _metadata(ctx: BookContext) -> BookMetadata:
        stored = ctx.metadata.get("book_metadata")
        if stored:
            return BookMetadata(**stored)
        # no AttachMetadata in this pipeline — fall back to what's in the context
        return BookMetadata(
            book_id=ctx.book_id,
            title=ctx.metadata.get("title", ctx.book_id),
            authors=ctx.metadata.get("authors", []),
            year=ctx.metadata.get("year"),
            publisher=ctx.metadata.get("publisher"),
            isbn=ctx.metadata.get("isbn"),
            faculty=ctx.metadata.get("faculty", ctx.source.upper()),
        )
