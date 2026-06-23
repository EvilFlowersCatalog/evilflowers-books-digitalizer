"""Bibliographic metadata: Excel catalog matched to books by directory name."""

from evilflowers_books_digitalizer.metadata.catalog import (
    MetadataCatalog,
    deslug,
    extract_dir_id,
    normalize_id,
)
from evilflowers_books_digitalizer.metadata.models import BookMetadata

__all__ = [
    "BookMetadata",
    "MetadataCatalog",
    "deslug",
    "extract_dir_id",
    "normalize_id",
]
