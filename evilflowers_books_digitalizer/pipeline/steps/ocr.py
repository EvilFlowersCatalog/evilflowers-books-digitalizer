"""OCR step: searchable PDF/A via OCRmyPDF (Tesseract under the hood)."""

from __future__ import annotations

import ocrmypdf

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep


class OcrPdf(PipelineStep):
    """``artifacts['raw_pdf']`` -> searchable ``artifacts['pdf']`` + ``artifacts['text']``.

    The sidecar text file holds the plain OCR text — the input for the later
    enrichment, classification and embedding stages.

    Notes on the knobs (see notebook 03 for the experiments behind them):

    * ``deskew``/``clean`` (unpaper) are off by default — the
      :class:`~.preprocess.PreprocessScans` step already straightens and
      crops pages; enable them when feeding raw frames instead.
    * ``optimize=2`` engages pngquant; with jbig2enc installed monochrome
      images get JBIG2-compressed as well.
    * ``jpg_quality`` controls the optimizer's JPEG re-encoding quality.
    * Any other ``ocrmypdf.ocr`` keyword can be passed via ``extra``.
    """

    name = "ocr"

    def __init__(
        self,
        language: str | None = None,  # None -> metadata['ocr_language'] (DetectLanguage) or "slk"
        deskew: bool = False,
        rotate_pages: bool = True,
        clean: bool = False,  # requires `unpaper`
        output_type: str = "pdfa-2",
        optimize: int = 2,
        jpg_quality: int | None = None,
        png_quality: int | None = None,
        oversample: int = 0,
        jobs: int | None = None,
        **extra,
    ):
        self.language = language
        self.deskew = deskew
        self.rotate_pages = rotate_pages
        self.clean = clean
        self.output_type = output_type
        self.optimize = optimize
        self.jpg_quality = jpg_quality
        self.png_quality = png_quality
        self.oversample = oversample
        self.jobs = jobs
        self.extra = extra

    def run(self, ctx: BookContext) -> BookContext:
        raw_pdf = ctx.artifacts.get("raw_pdf")
        if raw_pdf is None:
            raise ValueError(f"no raw_pdf for {ctx.slug} — run the assemble step first")

        pdf = ctx.output_dir / f"{ctx.slug}.pdf"
        sidecar = ctx.output_dir / f"{ctx.slug}.txt"
        options = dict(
            language=self.language or ctx.metadata.get("ocr_language", "slk"),
            deskew=self.deskew,
            rotate_pages=self.rotate_pages,
            clean=self.clean,
            output_type=self.output_type,
            optimize=self.optimize,
            oversample=self.oversample,
            jobs=self.jobs,
            sidecar=sidecar,
            progress_bar=False,
        )
        if self.jpg_quality is not None:
            options["jpg_quality"] = self.jpg_quality
        if self.png_quality is not None:
            options["png_quality"] = self.png_quality
        options.update(self.extra)

        ocrmypdf.ocr(raw_pdf, pdf, **options)
        ctx.artifacts["pdf"] = pdf
        ctx.artifacts["text"] = sidecar
        return ctx
