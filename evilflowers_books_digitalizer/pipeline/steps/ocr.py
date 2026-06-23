"""OCR step: cleaned page TIFFs -> multi-page hOCR + plain-text sidecar.

Tesseract runs once over the whole book. The hOCR is the single OCR artifact
the rest of the pipeline builds on:

* :class:`~evilflowers_books_digitalizer.pipeline.steps.render.RenderPdf` feeds
  it to ``recode_pdf`` as the searchable text layer for every output profile;
* :class:`~evilflowers_books_digitalizer.pipeline.steps.finalize.FinalizePdf`
  mines it for outline bookmarks and printed-page-number labels.

Splitting OCR out of PDF assembly (it used to live inside the old ``MrcPdf``)
means the expensive Tesseract pass happens once even though we now render two
PDFs (distribution + archival).
"""

from __future__ import annotations

import logging
import subprocess

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep
from evilflowers_books_digitalizer.pipeline.hocr import parse_hocr

logger = logging.getLogger(__name__)


class OcrPages(PipelineStep):
    """``ctx.tiffs`` -> ``artifacts['hocr']`` + ``artifacts['text']``.

    ``language`` is the Tesseract language string (e.g. ``"slk+eng"``); ``None``
    defers to ``metadata['ocr_language']`` (set by the language-detection step)
    and finally ``"slk"``. ``dpi`` must match the cleaned pages' resolution
    (ScanTailor ``output_dpi``) so glyph geometry is correct.
    """

    name = "ocr"

    def __init__(
        self,
        language: str | None = None,
        dpi: int = 300,
        tesseract: str = "tesseract",
    ) -> None:
        self.language = language
        self.dpi = dpi
        self.tesseract = tesseract

    def run(self, ctx: BookContext) -> BookContext:
        if not ctx.tiffs:
            raise ValueError(f"no pages for {ctx.slug} — run the scantailor step first")
        # resolve(): Leptonica/Tesseract choke on paths through macOS /tmp symlinks
        pages = sorted(p.resolve() for p in ctx.tiffs)
        language = self.language or ctx.metadata.get("ocr_language", "slk")

        ctx.work_dir.mkdir(parents=True, exist_ok=True)
        list_file = ctx.work_dir / "pagelist.txt"
        list_file.write_text("\n".join(str(p) for p in pages))
        hocr_base = ctx.work_dir / "book"
        result = subprocess.run(
            [self.tesseract, str(list_file), str(hocr_base),
             "-l", language, "--dpi", str(self.dpi), "hocr"],
            capture_output=True,
            text=True,
        )
        hocr = hocr_base.with_suffix(".hocr")
        if result.returncode != 0 or not hocr.exists():
            raise RuntimeError(
                f"tesseract failed for {ctx.slug} (exit {result.returncode}): "
                f"{result.stderr[-2000:]}"
            )

        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        sidecar = ctx.output_dir / f"{ctx.slug}.txt"
        sidecar.write_text(parse_hocr(hocr.read_text(errors="ignore")).plain_text())

        ctx.artifacts["hocr"] = hocr
        ctx.artifacts["text"] = sidecar
        ctx.metadata["ocr_language"] = language
        logger.info("%s: OCR'd %d pages (lang=%s)", ctx.slug, len(pages), language)
        return ctx
