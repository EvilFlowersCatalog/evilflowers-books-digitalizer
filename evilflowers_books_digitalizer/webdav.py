"""WebDAV access to the scan shares.

Thin wrapper around `webdav4 <https://github.com/skshetry/webdav4>`_ exposing
the domain vocabulary (books, pages) instead of raw paths.

Observed share layout (QNAP NAS volumes)::

    /                                  share root (+ @Recycle, @Recently-Snapshot)
    └── <root>/                        e.g. "SVF-skeny" — set per source in credentials.toml
        └── CVI_OPACID_<...>/          one directory per book (OPAC ID or title slug)
            └── stream_pages_tif/      page scans
                └── OPACID_..._p0001.tif
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from tqdm.auto import tqdm
from webdav4.client import Client, ResourceNotFound

from evilflowers_books_digitalizer.config import WebDAVSource
from evilflowers_books_digitalizer.models import TIFF_EXTENSIONS, BookRef, PageScan, SourceStats

logger = logging.getLogger(__name__)

#: Subdirectory inside each book directory that holds the page scans.
PAGES_SUBDIR = "stream_pages_tif"


class BookSource:
    """A WebDAV share holding books as directories of TIFF page scans."""

    def __init__(self, source: WebDAVSource, timeout: int = 60):
        self.config = source
        self.client = Client(
            source.url,
            auth=(source.username, source.password),
            timeout=timeout,
        )

    def __repr__(self) -> str:
        return f"BookSource({self.config.name!r}, url={self.config.url!r})"

    def _book_path(self, book_id: str) -> str:
        return f"{self.config.root}/{book_id}" if self.config.root else book_id

    # -- listing ---------------------------------------------------------

    def list_books(self) -> list[str]:
        """Book directories under the content root (NAS system dirs excluded)."""
        entries = self.client.ls(self.config.root or "/", detail=True)
        names = (
            PurePosixPath(entry["name"]).name
            for entry in entries
            if entry["type"] == "directory"
        )
        return sorted(name for name in names if not name.startswith("@"))

    def get_book(self, book_id: str) -> BookRef:
        """Book with its full page listing (TIFF files only)."""
        pages_path = f"{self._book_path(book_id)}/{PAGES_SUBDIR}"
        try:
            entries = self.client.ls(pages_path, detail=True)
        except ResourceNotFound:
            # Some books may keep TIFFs directly in the book directory.
            entries = self.client.ls(self._book_path(book_id), detail=True)
        pages = sorted(
            (
                PageScan(path=entry["name"], size=entry.get("content_length") or 0)
                for entry in entries
                if entry["type"] == "file"
                and PurePosixPath(entry["name"]).suffix.lower() in TIFF_EXTENSIONS
            ),
            key=lambda page: page.path,
        )
        return BookRef(source=self.config.name, book_id=book_id, pages=pages)

    def iter_books(self, progress: bool = True) -> Iterator[BookRef]:
        """Iterate over all books on the share, listing pages for each."""
        book_ids = self.list_books()
        iterator = tqdm(book_ids, desc=self.config.name, unit="book") if progress else book_ids
        for book_id in iterator:
            yield self.get_book(book_id)

    def stats(self, progress: bool = True) -> SourceStats:
        """Walk the whole share and aggregate content statistics."""
        return SourceStats(source=self.config.name, books=list(self.iter_books(progress=progress)))

    # -- downloading -----------------------------------------------------

    def download_book(
        self,
        book: BookRef | str,
        dest_dir: Path,
        skip_existing: bool = True,
        progress: bool = True,
    ) -> list[Path]:
        """Download all pages of a book into ``dest_dir``, returning local paths.

        Files already present with the expected size are skipped, so the
        download is resumable.
        """
        if isinstance(book, str):
            book = self.get_book(book)
        dest_dir.mkdir(parents=True, exist_ok=True)

        pages = tqdm(book.pages, desc=book.book_id, unit="page") if progress else book.pages
        local_paths: list[Path] = []
        for page in pages:
            local_path = dest_dir / PurePosixPath(page.path).name
            if skip_existing and local_path.exists() and local_path.stat().st_size == page.size:
                logger.debug("skipping existing %s", local_path)
            else:
                self.client.download_file(page.path, local_path)
            local_paths.append(local_path)
        return local_paths
