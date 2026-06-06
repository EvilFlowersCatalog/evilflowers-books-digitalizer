"""EvilFlowers Books Digitalizer.

Digitalization pipeline for scanned books: TIFF scans on WebDAV shares are
turned into searchable PDF/A documents, enriched with metadata and classified.
Later stages will produce embeddings (vector DB) and richer classifications
(graph DB) on top of the same pipeline.
"""

from evilflowers_books_digitalizer.cache import LocalCache
from evilflowers_books_digitalizer.config import Settings, WebDAVSource, load_settings
from evilflowers_books_digitalizer.pipeline import BookContext, Pipeline, PipelineStep
from evilflowers_books_digitalizer.webdav import BookSource

__version__ = "0.1.0"

__all__ = [
    "BookContext",
    "BookSource",
    "LocalCache",
    "Pipeline",
    "PipelineStep",
    "Settings",
    "WebDAVSource",
    "load_settings",
]
