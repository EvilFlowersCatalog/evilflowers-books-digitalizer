"""Bibliographic metadata: Excel catalog matched to books by directory name."""

from evilflowers_books_digitalizer.metadata.catalog import (
    MetadataCatalog,
    deslug,
    extract_dir_id,
    normalize_id,
)
from evilflowers_books_digitalizer.metadata.draft import (
    HEADERS,
    DraftBook,
    build_draft_rows,
    derive_isbn,
    guess_title,
    write_draft_xlsx,
)
from evilflowers_books_digitalizer.metadata.isbn_lookup import IsbnEnricher, is_valid_isbn
from evilflowers_books_digitalizer.metadata.models import BookMetadata

__all__ = [
    "HEADERS",
    "BookMetadata",
    "DraftBook",
    "IsbnEnricher",
    "MetadataCatalog",
    "build_draft_rows",
    "derive_isbn",
    "deslug",
    "extract_dir_id",
    "guess_title",
    "is_valid_isbn",
    "normalize_id",
    "write_draft_xlsx",
]
