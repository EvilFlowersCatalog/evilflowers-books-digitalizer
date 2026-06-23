"""Language detection step: decide the OCR language per book."""

from __future__ import annotations

from evilflowers_books_digitalizer.language import detect_book_language
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep


class DetectLanguage(PipelineStep):
    """Sample cleaned pages -> ``metadata['language']`` (ISO) and
    ``metadata['ocr_language']`` (Tesseract combo, consumed by :class:`OcrPages`).
    """

    name = "language"

    def __init__(self, sample: int = 3, default_iso: str = "sk"):
        self.sample = sample
        self.default_iso = default_iso

    def run(self, ctx: BookContext) -> BookContext:
        iso, tesseract_langs = detect_book_language(
            ctx.tiffs, sample=self.sample, default_iso=self.default_iso
        )
        ctx.metadata["language"] = iso
        ctx.metadata["ocr_language"] = tesseract_langs
        return ctx
