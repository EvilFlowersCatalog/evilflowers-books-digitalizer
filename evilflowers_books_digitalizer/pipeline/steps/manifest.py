"""Catalog manifest step: write a ``<slug>.entry.json`` sidecar per book.

Emits a catalog-ready :class:`~evilflowers_books_digitalizer.catalog.mapping.EntryManifest`
next to the PDF so the book can later be published to an EvilFlowers Catalog
(see :mod:`evilflowers_books_digitalizer.catalog.publisher`) without re-running
the catalog lookup. Runs last (it needs the final PDF and the cover).

Standalone, fail-safe artifact: any error is logged and the pipeline continues —
a manifest is never worth failing a digitization batch over (it can always be
rebuilt later with ``export-manifests``).
"""

from __future__ import annotations

import logging
from typing import Any

from evilflowers_books_digitalizer.catalog.manifest import write_manifest
from evilflowers_books_digitalizer.catalog.mapping import build_manifest
from evilflowers_books_digitalizer.metadata.models import BookMetadata
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)


class WriteCatalogManifest(PipelineStep):
    """Write ``artifacts['manifest']`` — the catalog entry sidecar JSON."""

    name = "manifest"

    def __init__(
        self,
        catalog: str,
        *,
        default_language: str = "slk",
        relation: str = "open-access",
        entry_config: dict[str, Any] | None = None,
        author_name_order: str = "given_first",
    ):
        self.catalog = catalog
        self.default_language = default_language
        self.relation = relation
        self.entry_config = dict(entry_config or {})
        self.author_name_order = author_name_order

    def run(self, ctx: BookContext) -> BookContext:
        try:
            pdf = ctx.artifacts.get("pdf")
            if pdf is None:
                logger.warning("%s: no PDF artifact — skipping manifest", ctx.slug)
                return ctx

            manifest = build_manifest(
                self._metadata(ctx),
                source=ctx.source,
                book_id=ctx.book_id,
                pdf=pdf,
                cover=ctx.artifacts.get("cover"),
                catalog=self.catalog,
                ocr_language=ctx.metadata.get("ocr_language"),
                default_language=self.default_language,
                relation=self.relation,
                entry_config=self.entry_config,
                page_count=ctx.metadata.get("n_pages"),
                author_name_order=self.author_name_order,
            )
            ctx.output_dir.mkdir(parents=True, exist_ok=True)
            dest = ctx.output_dir / f"{ctx.slug}.entry.json"
            write_manifest(manifest, dest)
            ctx.artifacts["manifest"] = dest
            logger.info("%s: wrote catalog manifest", ctx.slug)
        except Exception as exc:  # noqa: BLE001 — never fail a batch over a manifest
            logger.warning("%s: manifest step failed: %s", ctx.slug, exc)
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
            language=ctx.metadata.get("language"),
        )
