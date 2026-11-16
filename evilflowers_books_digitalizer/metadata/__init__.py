"""Bibliographic metadata: Excel catalog matched to books by directory name, ISBN lookup, OCR facts."""

from evilflowers_books_digitalizer.metadata.catalog import (
    MetadataCatalog,
    deslug,
    extract_dir_id,
    normalize_id,
)
from evilflowers_books_digitalizer.metadata.isbn_lookup import IsbnEnricher, is_valid_isbn
from evilflowers_books_digitalizer.metadata.models import BookMetadata

__all__ = [
    "BookMetadata",
    "IsbnEnricher",
    "MetadataCatalog",
    "deslug",
    "extract_dir_id",
    "is_valid_isbn",
    "normalize_id",
]
