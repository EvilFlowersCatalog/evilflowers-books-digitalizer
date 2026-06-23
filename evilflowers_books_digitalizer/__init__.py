"""EvilFlowers Books Digitalizer.

Digitalization pipeline for scanned books: TIFF scans on WebDAV shares are
turned into searchable PDF/A documents, enriched with metadata and covers, and
imported into the EvilFlowers Catalog. Later stages will add embeddings (vector
DB) and richer classification (graph DB) on top of the same pipeline.
"""

from evilflowers_books_digitalizer.cache import LocalCache
from evilflowers_books_digitalizer.config import Settings, WebDAVSource, load_settings
from evilflowers_books_digitalizer.pipeline import BookContext, Pipeline, PipelineStep
from evilflowers_books_digitalizer.sources import (
    AbstractBookSource,
    BookSource,
    FilesystemBookSource,
    build_source,
)

__version__ = "0.1.0"

__all__ = [
    "AbstractBookSource",
    "BookContext",
    "BookSource",
    "FilesystemBookSource",
    "LocalCache",
    "Pipeline",
    "PipelineStep",
    "Settings",
    "WebDAVSource",
    "build_source",
    "load_settings",
]
