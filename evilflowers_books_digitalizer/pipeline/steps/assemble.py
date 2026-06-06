"""Assemble step: pack page TIFFs into a single (not yet searchable) PDF.

img2pdf embeds the images losslessly — no recompression, the PDF pages *are*
the original scans. OCR then adds a hidden text layer on top.
"""

from __future__ import annotations

import img2pdf

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep


class AssemblePdf(PipelineStep):
    """TIFF pages (in order) -> ``artifacts['raw_pdf']``."""

    name = "assemble"

    def run(self, ctx: BookContext) -> BookContext:
        if not ctx.tiffs:
            raise ValueError(f"no TIFF pages for {ctx.slug} — run the download step first")

        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        raw_pdf = ctx.output_dir / f"{ctx.slug}_raw.pdf"
        with raw_pdf.open("wb") as fh:
            fh.write(img2pdf.convert([str(path) for path in sorted(ctx.tiffs)]))
        ctx.artifacts["raw_pdf"] = raw_pdf
        return ctx
