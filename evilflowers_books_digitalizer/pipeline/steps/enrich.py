"""Enrichment step: attach metadata to the produced PDF and the context.

First enricher writes basic document info into the PDF (XMP via pikepdf) and
collects structural facts into ``ctx.metadata``. Further enrichers (authors,
publication year, MARC lookups, ...) should follow the same step interface.
"""

from __future__ import annotations

import pikepdf

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep


class EnrichPdfMetadata(PipelineStep):
    """Write document info into ``artifacts['pdf']`` and record basic facts."""

    name = "enrich"

    def __init__(self, creator: str = "EvilFlowers Books Digitalizer"):
        self.creator = creator

    def run(self, ctx: BookContext) -> BookContext:
        pdf_path = ctx.artifacts.get("pdf")
        if pdf_path is None:
            raise ValueError(f"no pdf for {ctx.slug} — run the OCR step first")

        title = ctx.metadata.get("title", ctx.book_id)
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            with pdf.open_metadata() as meta:
                meta["dc:title"] = title
                meta["dc:language"] = [ctx.metadata.get("language", "sk")]
                meta["xmp:CreatorTool"] = self.creator
            ctx.metadata["n_pdf_pages"] = len(pdf.pages)
            pdf.save(pdf_path)

        text_path = ctx.artifacts.get("text")
        if text_path is not None and text_path.exists():
            ctx.metadata["n_text_chars"] = len(text_path.read_text(errors="ignore"))
        return ctx
