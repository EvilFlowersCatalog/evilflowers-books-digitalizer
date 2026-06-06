"""Domain models shared across exploration notebooks and the pipeline."""

from __future__ import annotations

from pydantic import BaseModel

#: File extensions considered to be page scans.
TIFF_EXTENSIONS = {".tif", ".tiff"}


class PageScan(BaseModel):
    """A single scanned page (one TIFF file on the remote share)."""

    path: str  # path relative to the source root
    size: int  # bytes


class BookRef(BaseModel):
    """A book on a WebDAV share — one top-level directory of TIFF pages."""

    source: str  # source key, e.g. "svf"
    book_id: str  # directory name on the share
    pages: list[PageScan] = []

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    @property
    def total_bytes(self) -> int:
        return sum(page.size for page in self.pages)


class SourceStats(BaseModel):
    """Aggregated content statistics for one WebDAV source."""

    source: str
    books: list[BookRef]

    @property
    def n_books(self) -> int:
        return len(self.books)

    @property
    def n_pages(self) -> int:
        return sum(book.n_pages for book in self.books)

    @property
    def total_bytes(self) -> int:
        return sum(book.total_bytes for book in self.books)
