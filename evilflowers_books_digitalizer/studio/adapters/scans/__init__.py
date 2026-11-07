"""ScanStore over a read-only TIFF tree, located by the project's path pattern.

``book_pattern`` (e.g. ``"{collection}/*-skeny/{book}"``) names where a book
directory sits relative to ``scans_root``: ``{collection}`` and ``{book}`` are
captured, every other segment is literal or a glob. Turning the pattern into one
glob (to list) plus one regex (to capture) keeps a new library's layout a config
change instead of a new adapter.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from evilflowers_books_digitalizer.studio.ports import PortError, ScannedBook

_FIELD = re.compile(r"\{(collection|book)\}")
#: frames_glob fallback when a book directory has no match for the configured glob
_FALLBACK_GLOB = "*.tif"


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """``{collection}/*-skeny/{book}`` -> a regex capturing both fields per path segment."""
    out = []
    for piece in _FIELD.split(pattern):
        if piece in ("collection", "book"):
            out.append(f"(?P<{piece}>[^/]+)")
        else:   # glob -> regex that never crosses a path segment
            out.append(re.escape(piece).replace(r"\*", "[^/]*").replace(r"\?", "[^/]"))
    return re.compile("".join(out) + r"\Z")


@dataclass(frozen=True, slots=True)
class FilesystemScanStore:
    scans_root: Path
    book_pattern: str = "{collection}/{book}"
    frames_glob: str = "*.tif"

    def _glob(self, collection: str | None) -> Iterator[tuple[str, str, Path]]:
        """Every book directory as ``(collection, book, path)``, optionally one collection."""
        pattern = self.book_pattern.replace("{book}", "*")
        pattern = pattern.replace("{collection}", glob_escape(collection) if collection else "*")
        regex = _pattern_regex(self.book_pattern)
        for path in sorted(self.scans_root.glob(pattern)):
            if not path.is_dir():
                continue
            match = regex.match(path.relative_to(self.scans_root).as_posix())
            if match is not None:
                yield match["collection"], match["book"], path

    def frames(self, book_dir: Path) -> tuple[Path, ...]:
        """Frames in capture order (name order — scanners number them)."""
        found = sorted(p for p in book_dir.glob(self.frames_glob) if p.is_file())
        if not found and self.frames_glob != _FALLBACK_GLOB:
            found = sorted(p for p in book_dir.glob(_FALLBACK_GLOB) if p.is_file())
        return tuple(found)

    def collections(self) -> list[str]:
        return sorted({collection for collection, _, _ in self._glob(None)})

    def books(self, collection: str) -> Iterator[ScannedBook]:
        for coll, book, path in self._glob(collection):
            frames = self.frames(path)
            if frames:   # a directory without frames is not a book (yet)
                yield ScannedBook(coll, book, path, frames)

    def book(self, collection: str, external_id: str) -> ScannedBook:
        for coll, book, path in self._glob(collection):
            if book == external_id:
                return ScannedBook(coll, book, path, self.frames(path))
        raise PortError(f"no book {external_id!r} in collection {collection!r} "
                        f"under {self.scans_root}")


def glob_escape(value: str) -> str:
    """Escape glob metacharacters so a collection name is matched literally."""
    return re.sub(r"([*?\[\]])", r"[\1]", value)
