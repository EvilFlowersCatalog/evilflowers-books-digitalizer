"""Download/stage step: make a book's TIFF frames available in the local cache.

Backend-agnostic: with the filesystem source this symlinks frames off the
mount (zero-copy); with WebDAV it downloads them. Both are resumable.
"""

from __future__ import annotations

from evilflowers_books_digitalizer.cache import LocalCache
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep
from evilflowers_books_digitalizer.sources.base import AbstractBookSource


class DownloadBook(PipelineStep):
    """Stage all pages of ``ctx.book_id`` into the local cache (resumable)."""

    name = "download"

    def __init__(self, source: AbstractBookSource, cache: LocalCache, progress: bool = True):
        self.source = source
        self.cache = cache
        self.progress = progress

    def run(self, ctx: BookContext) -> BookContext:
        dest_dir = self.cache.book_dir(ctx.source, ctx.book_id)
        ctx.tiffs = self.source.stage_book(ctx.book_id, dest_dir, progress=self.progress)
        ctx.work_dir = dest_dir
        return ctx
