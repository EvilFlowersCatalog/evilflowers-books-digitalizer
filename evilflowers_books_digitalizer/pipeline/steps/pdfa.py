"""PDF/A step: declare archival outputs as PDF/A-2b (and optionally validate).

Runs last, after enrich + finalize have made their final edits, so the PDF/A
part identifier is the closing touch and no later save can disturb it. Only
profiles whose ``[render.<name>] pdfa`` is set are processed (see
:mod:`~evilflowers_books_digitalizer.pipeline.pdfa`).
"""

from __future__ import annotations

import logging

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep
from evilflowers_books_digitalizer.pipeline.pdfa import to_pdfa2b, validate_pdfa

logger = logging.getLogger(__name__)


class EnsurePdfA(PipelineStep):
    """Re-declare ``pdfa``-flagged outputs as PDF/A-2b; optionally validate."""

    name = "pdfa"

    def __init__(self, validate: bool = False) -> None:
        self.validate = validate

    def run(self, ctx: BookContext) -> BookContext:
        out_meta = ctx.metadata.get("outputs", {})
        for name, pdf_path in ctx.pdf_outputs():
            flavour = out_meta.get(name, {}).get("pdfa")
            if not flavour:
                continue
            if flavour != "2b":
                logger.warning("%s: only PDF/A-2b is supported, got %r — skipping", name, flavour)
                continue
            if to_pdfa2b(pdf_path) and self.validate:
                ok = validate_pdfa(pdf_path, "2b")
                ctx.metadata.setdefault("pdfa_valid", {})[name] = ok
                logger.info("%s: PDF/A-2b validation -> %s", name, ok)
        return ctx
