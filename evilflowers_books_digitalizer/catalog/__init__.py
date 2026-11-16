"""Catalog mapping: book metadata -> an EvilFlowers Catalog ``Entry`` (authors, language, catalog).

Used by the studio's publisher (``studio.adapters.catalog``). The batch-era publisher of
produced books (``publish-book``, ``publish-catalog``, ``*.entry.json`` manifests) was retired
with the batch pipeline (evilflowers-books-digitalizer#6).
"""

from evilflowers_books_digitalizer.catalog.mapping import (
    EntryAuthor,
    EntryManifest,
    build_manifest,
    normalize_language,
    resolve_language,
    split_author,
    target_catalog,
)

__all__ = [
    "EntryAuthor",
    "EntryManifest",
    "build_manifest",
    "normalize_language",
    "resolve_language",
    "split_author",
    "target_catalog",
]
