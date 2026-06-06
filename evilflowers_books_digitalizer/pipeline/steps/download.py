"""Download step: pull a book's TIFF pages from WebDAV into the local cache."""

from __future__ import annotations

from evilflowers_books_digitalizer.cache import LocalCache
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep
from evilflowers_books_digitalizer.webdav import BookSource


class DownloadBook(PipelineStep):
    """Fetch all pages of ``ctx.book_id`` from WebDAV (resumable)."""

    name = "download"

    def __init__(self, source: BookSource, cache: LocalCache, progress: bool = True):
        self.source = source
        self.cache = cache
        self.progress = progress

    def run(self, ctx: BookContext) -> BookContext:
        dest_dir = self.cache.book_dir(ctx.source, ctx.book_id)
        ctx.tiffs = self.source.download_book(ctx.book_id, dest_dir, progress=self.progress)
        ctx.work_dir = dest_dir
        return ctx
