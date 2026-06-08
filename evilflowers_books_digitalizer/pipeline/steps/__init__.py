"""Concrete pipeline steps."""

from evilflowers_books_digitalizer.pipeline.steps.assemble import AssemblePdf
from evilflowers_books_digitalizer.pipeline.steps.classify import (
    Classifier,
    ClassifyBook,
    KeywordClassifier,
)
from evilflowers_books_digitalizer.pipeline.steps.detect_language import DetectLanguage
from evilflowers_books_digitalizer.pipeline.steps.docres import DocResEnhance
from evilflowers_books_digitalizer.pipeline.steps.download import DownloadBook
from evilflowers_books_digitalizer.pipeline.steps.enrich import EnrichPdfMetadata
from evilflowers_books_digitalizer.pipeline.steps.finalize import FinalizePdf
from evilflowers_books_digitalizer.pipeline.steps.mrc import MrcPdf
from evilflowers_books_digitalizer.pipeline.steps.ocr import OcrPdf
from evilflowers_books_digitalizer.pipeline.steps.preprocess import PreprocessScans
from evilflowers_books_digitalizer.pipeline.steps.scantailor import ScanTailorScans

__all__ = [
    "AssemblePdf",
    "Classifier",
    "ClassifyBook",
    "DetectLanguage",
    "DocResEnhance",
    "DownloadBook",
    "EnrichPdfMetadata",
    "FinalizePdf",
    "KeywordClassifier",
    "MrcPdf",
    "OcrPdf",
    "PreprocessScans",
    "ScanTailorScans",
]
