"""Local on-disk cache for downloaded scans and pipeline artifacts.

Layout::

    <root>/
        scans/<source>/<book_id>/*.tif      downloaded originals
        stats/<source>.json                 cached share statistics
"""

from __future__ import annotations

import json
from pathlib import Path

from evilflowers_books_digitalizer.models import TIFF_EXTENSIONS, SourceStats


class LocalCache:
    """Filesystem layout for everything we pull down or precompute."""

    def __init__(self, root: Path):
        self.root = root

    def __repr__(self) -> str:
        return f"LocalCache({str(self.root)!r})"

    # -- scans -----------------------------------------------------------

    def book_dir(self, source: str, book_id: str) -> Path:
        return self.root / "scans" / source / book_id

    def list_tiffs(self, source: str, book_id: str) -> list[Path]:
        """Locally cached pages of a book, in page order."""
        directory = self.book_dir(source, book_id)
        if not directory.exists():
            return []
        return sorted(
            path for path in directory.iterdir() if path.suffix.lower() in TIFF_EXTENSIONS
        )

    # -- stats -----------------------------------------------------------

    def stats_path(self, source: str) -> Path:
        return self.root / "stats" / f"{source}.json"

    def save_stats(self, stats: SourceStats) -> Path:
        """Persist share statistics so exploration doesn't re-walk WebDAV."""
        path = self.stats_path(stats.source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stats.model_dump_json(indent=2))
        return path

    def load_stats(self, source: str) -> SourceStats | None:
        path = self.stats_path(source)
        if not path.exists():
            return None
        return SourceStats.model_validate(json.loads(path.read_text()))
