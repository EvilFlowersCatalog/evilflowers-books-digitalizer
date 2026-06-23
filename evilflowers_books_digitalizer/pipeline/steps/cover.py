"""Cover step: a thumbnail per book — real OPAC cover when available, else generated.

``source`` (``[cover].source``):

* ``"opac_then_generated"`` (default) — try the real obalkyknih/OPAC cover by
  ISBN; if the book has no ISBN or no cover, render a styled cover from metadata.
* ``"opac"`` — only the real cover (no cover artifact when none exists).
* ``"generated"`` — always the styled cover (ignore the OPAC).

Standalone artifact (the PDF is left untouched). Fail-safe: any error is logged
and the pipeline continues — a cover is never worth failing a batch over.
"""

from __future__ import annotations

import logging

from evilflowers_books_digitalizer.covers.opac import fetch_opac_cover
from evilflowers_books_digitalizer.covers.renderer import CoverRenderer
from evilflowers_books_digitalizer.metadata.models import BookMetadata
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)

_SOURCES = ("opac_then_generated", "opac", "generated")


class GenerateCover(PipelineStep):
    """Write ``artifacts['cover']`` — OPAC cover by ISBN, else generated."""

    name = "cover"

    def __init__(
        self,
        renderer: CoverRenderer,
        source: str = "opac_then_generated",
        min_px: int = 80,
    ):
        if source not in _SOURCES:
            raise ValueError(f"unknown cover source {source!r}; have {_SOURCES}")
        self.renderer = renderer
        self.source = source
        self.min_px = min_px

    def run(self, ctx: BookContext) -> BookContext:
        try:
            ctx.output_dir.mkdir(parents=True, exist_ok=True)
            dest = ctx.output_dir / f"{ctx.slug}.cover.jpg"

            if self.source in ("opac", "opac_then_generated"):
                isbn = ctx.metadata.get("isbn")
                if isbn and fetch_opac_cover(isbn, dest, min_px=self.min_px):
                    ctx.artifacts["cover"] = dest
                    ctx.metadata["cover_source"] = "opac"
                    logger.info("%s: cover from OPAC (ISBN %s)", ctx.slug, isbn)
                    return ctx

            if self.source in ("generated", "opac_then_generated"):
                # renderer's suffix may be .png; honour it
                out = ctx.output_dir / f"{ctx.slug}.cover{self.renderer.suffix}"
                self.renderer.render_to_file(self._metadata(ctx), out)
                ctx.artifacts["cover"] = out
                ctx.metadata["cover_source"] = "generated"
                logger.info("%s: cover generated", ctx.slug)
            else:
                logger.info("%s: no OPAC cover, source=opac -> skipped", ctx.slug)
        except Exception as exc:  # noqa: BLE001 — never fail a batch over a cover
            logger.warning("%s: cover step failed: %s", ctx.slug, exc)
        return ctx

    @staticmethod
    def _metadata(ctx: BookContext) -> BookMetadata:
        stored = ctx.metadata.get("book_metadata")
        if stored:
            return BookMetadata(**stored)
        return BookMetadata(
            book_id=ctx.book_id,
            title=ctx.metadata.get("title", ctx.book_id),
            authors=ctx.metadata.get("authors", []),
            year=ctx.metadata.get("year"),
            publisher=ctx.metadata.get("publisher"),
            isbn=ctx.metadata.get("isbn"),
            faculty=ctx.metadata.get("faculty", ctx.source.upper()),
        )
