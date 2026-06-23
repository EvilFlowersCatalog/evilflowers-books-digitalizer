"""Concrete pipeline steps."""

from evilflowers_books_digitalizer.pipeline.steps.attach_metadata import AttachMetadata
from evilflowers_books_digitalizer.pipeline.steps.cover import GenerateCover
from evilflowers_books_digitalizer.pipeline.steps.detect_language import DetectLanguage
from evilflowers_books_digitalizer.pipeline.steps.docres import DocResEnhance
from evilflowers_books_digitalizer.pipeline.steps.download import DownloadBook
from evilflowers_books_digitalizer.pipeline.steps.enrich import EnrichPdfMetadata
from evilflowers_books_digitalizer.pipeline.steps.finalize import FinalizePdf
from evilflowers_books_digitalizer.pipeline.steps.manifest import WriteCatalogManifest
from evilflowers_books_digitalizer.pipeline.steps.ocr import OcrPages
from evilflowers_books_digitalizer.pipeline.steps.pdfa import EnsurePdfA
from evilflowers_books_digitalizer.pipeline.steps.render import RenderPdf
from evilflowers_books_digitalizer.pipeline.steps.scantailor import ScanTailorScans

__all__ = [
    "AttachMetadata",
    "DetectLanguage",
    "DocResEnhance",
    "DownloadBook",
    "EnrichPdfMetadata",
    "EnsurePdfA",
    "FinalizePdf",
    "GenerateCover",
    "OcrPages",
    "RenderPdf",
    "ScanTailorScans",
    "WriteCatalogManifest",
]
