"""Enrichment step: write document metadata into the non-PDF/A outputs.

Writes Dublin Core / XMP (title, authors, language, publisher, date) into each
rendered PDF whose profile is **not** PDF/A, and records structural facts
(page count, OCR character count) on the context for the batch report.

PDF/A outputs are skipped on purpose: their title/authors/language are baked in
by ``recode_pdf`` at render time (see :class:`~.render.RenderPdf`), and rewriting
their XMP with pikepdf afterwards breaks PDF/A conformance (verified with
veraPDF — the document-info/XMP title sync fails, clause 6.6.2.3.1).
"""

from __future__ import annotations

import logging

import pikepdf

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)


class EnrichPdfMetadata(PipelineStep):
    """Write XMP metadata into each ``pdf_*`` artifact; record basic facts."""

    name = "enrich"

    def __init__(self, creator: str = "EvilFlowers Books Digitalizer") -> None:
        self.creator = creator

    def run(self, ctx: BookContext) -> BookContext:
        outputs = ctx.pdf_outputs()
        if not outputs:
            raise ValueError(f"no pdf for {ctx.slug} — run the render step first")

        title = ctx.metadata.get("title", ctx.book_id)
        authors = ctx.metadata.get("authors") or []
        year = ctx.metadata.get("year")
        publisher = ctx.metadata.get("publisher")
        language = ctx.metadata.get("language", "sk")
        pdfa_profiles = {
            name for name, info in ctx.metadata.get("outputs", {}).items() if info.get("pdfa")
        }

        n_pages = 0
        for name, pdf_path in outputs:
            with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
                n_pages = len(pdf.pages)
                if name in pdfa_profiles:
                    continue  # recode baked valid PDF/A XMP; don't disturb it
                with pdf.open_metadata() as meta:
                    meta["dc:title"] = title
                    meta["dc:language"] = [language]
                    meta["xmp:CreatorTool"] = self.creator
                    if authors:
                        meta["dc:creator"] = list(authors)
                    if publisher:
                        meta["dc:publisher"] = [publisher]
                    if year:
                        meta["dc:date"] = [str(year)]  # bare year is a valid ISO-8601 date
                pdf.save(pdf_path)

        ctx.metadata["n_pdf_pages"] = n_pages
        text_path = ctx.artifacts.get("text")
        if text_path is not None and text_path.exists():
            ctx.metadata["n_text_chars"] = len(text_path.read_text(errors="ignore"))
        return ctx
