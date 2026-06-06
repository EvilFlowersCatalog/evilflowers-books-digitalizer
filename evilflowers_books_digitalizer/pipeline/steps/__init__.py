"""Concrete pipeline steps."""

from evilflowers_books_digitalizer.pipeline.steps.assemble import AssemblePdf
from evilflowers_books_digitalizer.pipeline.steps.classify import (
    Classifier,
    ClassifyBook,
    KeywordClassifier,
)
from evilflowers_books_digitalizer.pipeline.steps.detect_language import DetectLanguage
from evilflowers_books_digitalizer.pipeline.steps.download import DownloadBook
from evilflowers_books_digitalizer.pipeline.steps.enrich import EnrichPdfMetadata
from evilflowers_books_digitalizer.pipeline.steps.ocr import OcrPdf
from evilflowers_books_digitalizer.pipeline.steps.preprocess import PreprocessScans

__all__ = [
    "AssemblePdf",
    "Classifier",
    "ClassifyBook",
    "DetectLanguage",
    "DownloadBook",
    "EnrichPdfMetadata",
    "KeywordClassifier",
    "OcrPdf",
    "PreprocessScans",
]
