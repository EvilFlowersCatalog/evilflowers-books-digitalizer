"""Local-mount backend for the scan shares (production default).

On the production VM the NAS volumes are mounted read-only under a single root,
e.g. ``/mnt/digital-library/raw-scans/<faculty>/<NAME>-skeny/<book>/stream_pages_tif``.
This backend reads books straight off that mount — no VPN, no WebDAV flakiness
(the batch log shows real ``Server disconnected`` errors over WebDAV).

Staging is **zero-copy by default**: a book's raw frames are *symlinked* into
the local working directory rather than copied, so processing multiple TB never
duplicates the source. ScanTailor reads through the symlinks and writes its
cleaned pages elsewhere in the working directory; low-storage cleanup only
removes the symlinks, never the originals on the mount. ``stage_mode = "copy"``
is available when a self-contained scratch copy is preferred.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from tqdm.auto import tqdm

from evilflowers_books_digitalizer.models import TIFF_EXTENSIONS, BookRef, PageScan
from evilflowers_books_digitalizer.sources.base import PAGES_SUBDIR, AbstractBookSource

logger = logging.getLogger(__name__)

#: NAS/system directories that are not books.
_SKIP_DIRS = {"lost+found"}


class FilesystemBookSource(AbstractBookSource):
    """Books read from a local directory tree (a mounted NAS volume)."""

    def __init__(
        self,
        name: str,
        base_dir: Path | str,
        *,
        pages_subdir: str = PAGES_SUBDIR,
        stage_mode: str = "symlink",
    ):
        if stage_mode not in ("symlink", "copy", "hardlink"):
            raise ValueError(f"unknown stage_mode: {stage_mode!r}")
        self.name = name
        self.base_dir = Path(base_dir)
        self.pages_subdir = pages_subdir
        self.stage_mode = stage_mode

    def __repr__(self) -> str:
        return f"FilesystemBookSource({self.name!r}, base_dir={str(self.base_dir)!r})"

    def _book_dir(self, book_id: str) -> Path:
        return self.base_dir / book_id

    def _pages_dir(self, book_id: str) -> Path:
        """Where a book's TIFFs live — ``stream_pages_tif`` or the book dir itself."""
        nested = self._book_dir(book_id) / self.pages_subdir
        return nested if nested.is_dir() else self._book_dir(book_id)

    # -- listing ---------------------------------------------------------

    def list_books(self) -> list[str]:
        if not self.base_dir.is_dir():
            raise FileNotFoundError(
                f"source base dir does not exist: {self.base_dir} — check the mount "
                "and [source] config"
            )
        return sorted(
            entry.name
            for entry in self.base_dir.iterdir()
            if entry.is_dir() and not entry.name.startswith("@") and entry.name not in _SKIP_DIRS
        )

    def get_book(self, book_id: str) -> BookRef:
        pages_dir = self._pages_dir(book_id)
        pages = sorted(
            (
                PageScan(path=str(p), size=p.stat().st_size)
                for p in pages_dir.iterdir()
                if p.is_file() and p.suffix.lower() in TIFF_EXTENSIONS
            ),
            key=lambda page: page.path,
        )
        return BookRef(source=self.name, book_id=book_id, pages=pages)

    # -- staging ---------------------------------------------------------

    def stage_book(
        self, book: BookRef | str, dest_dir: Path, *, progress: bool = True
    ) -> list[Path]:
        """Materialize a book's frames in ``dest_dir`` (symlink / copy / hardlink).

        Resumable: a destination that already points at / matches the source is
        left untouched, so an interrupted batch resumes cheaply.
        """
        if isinstance(book, str):
            book = self.get_book(book)
        dest_dir.mkdir(parents=True, exist_ok=True)

        pages = tqdm(book.pages, desc=book.book_id, unit="page") if progress else book.pages
        local_paths: list[Path] = []
        for page in pages:
            src = Path(page.path)
            dst = dest_dir / src.name
            self._materialize(src, dst, page.size)
            local_paths.append(dst)
        return local_paths

    def _materialize(self, src: Path, dst: Path, size: int) -> None:
        if self._already_staged(src, dst, size):
            return
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        if self.stage_mode == "symlink":
            dst.symlink_to(src)
        elif self.stage_mode == "hardlink":
            try:
                os.link(src, dst)
            except OSError:  # cross-device (mount vs local scratch) -> copy
                shutil.copy2(src, dst)
        else:  # "copy"
            shutil.copy2(src, dst)

    @staticmethod
    def _already_staged(src: Path, dst: Path, size: int) -> bool:
        if dst.is_symlink():
            return dst.resolve() == src.resolve()
        if dst.exists():
            return dst.stat().st_size == size
        return False
