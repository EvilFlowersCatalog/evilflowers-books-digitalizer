"""Source abstraction: where a book's raw TIFF frames come from.

Two backends implement :class:`AbstractBookSource`:

* :class:`~.filesystem.FilesystemBookSource` — a local mount (production: the
  scans are NFS/CIFS-mounted at ``/mnt/digital-library/raw-scans`` on the VM).
* :class:`~.webdav.BookSource` — the original WebDAV backend (needs VPN; kept
  for development and as an alternate deployment).

A source speaks the domain vocabulary (books, pages) and knows how to *stage*
a book's frames into a fast local working directory for processing. The shared
listing/stats helpers are defined here in terms of the two abstract methods so
both backends get them for free.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from tqdm.auto import tqdm

from evilflowers_books_digitalizer.models import BookRef, SourceStats

logger = logging.getLogger(__name__)

#: Subdirectory inside each book directory that holds the page scans.
PAGES_SUBDIR = "stream_pages_tif"


class AbstractBookSource(ABC):
    """A collection of books, each a directory of ordered TIFF page scans."""

    #: Source key, e.g. ``"svf"`` — used in slugs and output paths.
    name: str

    # -- listing (backend-specific) --------------------------------------

    @abstractmethod
    def list_books(self) -> list[str]:
        """Book directory names under the content root (system dirs excluded)."""

    @abstractmethod
    def get_book(self, book_id: str) -> BookRef:
        """Return a book with its full, page-ordered TIFF listing."""

    @abstractmethod
    def stage_book(
        self, book: BookRef | str, dest_dir: Path, *, progress: bool = True
    ) -> list[Path]:
        """Make a book's frames available locally in ``dest_dir``.

        Returns the local page paths in order. Implementations are resumable
        (already-present, correctly-sized files are reused) so an interrupted
        batch can pick up where it left off.
        """

    # -- shared helpers ---------------------------------------------------

    def iter_books(self, progress: bool = True) -> Iterator[BookRef]:
        """Iterate over every book on the source, listing pages for each."""
        book_ids = self.list_books()
        iterator = tqdm(book_ids, desc=self.name, unit="book") if progress else book_ids
        for book_id in iterator:
            yield self.get_book(book_id)

    def stats(self, progress: bool = True) -> SourceStats:
        """Walk the whole source and aggregate content statistics."""
        return SourceStats(source=self.name, books=list(self.iter_books(progress=progress)))

    # backwards-compatible alias: callers and notebooks used ``download_book``
    def download_book(
        self, book: BookRef | str, dest_dir: Path, *, progress: bool = True, **_: object
    ) -> list[Path]:
        return self.stage_book(book, dest_dir, progress=progress)
