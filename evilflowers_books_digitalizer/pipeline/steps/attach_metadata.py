"""Attach catalog metadata to a book's context.

Looks the book up in the Excel-backed :class:`MetadataCatalog` by directory
name and writes bibliographic fields into ``ctx.metadata`` for the downstream
:class:`EnrichPdfMetadata` (XMP) and :class:`GenerateCover` steps. Fail-safe:
an unmatched book gets a de-slugged stub title, never an error.
"""

from __future__ import annotations

import logging

from evilflowers_books_digitalizer.metadata.catalog import MetadataCatalog
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)


class AttachMetadata(PipelineStep):
    """Populate ``ctx.metadata`` (title, authors, year, …) from the catalog."""

    name = "metadata"

    def __init__(self, catalog: MetadataCatalog, faculty_map: dict[str, str] | None = None):
        self.catalog = catalog
        # source key -> human faculty name, used as the stub/cover faculty
        self.faculty_map = faculty_map or {}

    def run(self, ctx: BookContext) -> BookContext:
        faculty = self.faculty_map.get(ctx.source, ctx.source.upper())
        record = self.catalog.lookup(ctx.book_id, faculty=faculty)
        record.faculty = record.faculty or faculty

        ctx.metadata["title"] = record.title
        if record.authors:
            ctx.metadata["authors"] = record.authors
        if record.year is not None:
            ctx.metadata["year"] = record.year
        if record.publisher:
            ctx.metadata["publisher"] = record.publisher
        if record.isbn:
            ctx.metadata["isbn"] = record.isbn
        ctx.metadata["faculty"] = record.faculty
        ctx.metadata["catalog_matched"] = record.matched
        # full record for the cover renderer (avoids a second lookup)
        ctx.metadata["book_metadata"] = record.model_dump()

        logger.info("%s: %s (matched=%s)", ctx.slug, record.title[:60], record.matched)
        return ctx
