"""Excel catalog → per-book :class:`BookMetadata`, matched by directory name.

Book directories look like ``CVI_OPACID_FA_9788022750462`` (an ISBN/OPAC id),
``CVI_OPACID_FA_807095020_X`` (id with an ``X`` check digit split by an
underscore), ``FEI_9788089422012`` (no ``CVI_OPACID`` prefix), or a title slug
like ``CVI_OPACID_FA_Architektonicka_kompozicia``. The catalog extracts the id
token, normalizes it (alphanumerics only, upper-cased — so ``978-80-227-...``,
``9788022...`` and ``807095020_X`` all collapse to a comparable key) and
matches it against a configured spreadsheet column.

The column→field mapping lives in ``[metadata]`` of ``pipeline.toml`` so the
catalog adapts to whatever the real sheet is named, without code changes.
Books with no matching row are not fatal: they get a de-slugged stub title.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.metadata.models import BookMetadata

logger = logging.getLogger(__name__)

#: Canonical fields a column can map to (everything except book_id/matched/extra).
KNOWN_FIELDS = {
    "title", "subtitle", "authors", "year", "publisher", "isbn", "faculty", "language", "catalog",
}

#: Special column that holds the exact book directory name — the most reliable
#: join key (176/880 books have title-slug dirs with no ISBN). When mapped, it
#: sets the record's ``book_id`` so lookup matches on the directory exactly.
JOIN_FIELD = "directory"

#: Default column mapping (override per-sheet in [metadata.columns]).
DEFAULT_COLUMNS = {
    "directory": "directory_id",
    "title": "title",
    "authors": "authors",
    "year": "year",
    "publisher": "publisher",
    "isbn": "isbn",
    "faculty": "faculty",
    "language": "language",
}

_DIR_ID_RE = re.compile(r"^(?:CVI_OPACID_)?(?:[A-Z]{2,4}_)?(?P<id>.+)$")
_AUTHOR_SPLIT_RE = re.compile(r"\s*[;/]\s*|\s+&\s+|\s+a\s+(?=[A-ZČŠŽ])")


def normalize_id(value: str) -> str:
    """Collapse an id/ISBN to a comparison key: alphanumerics only, upper-case."""
    return re.sub(r"[^0-9A-Za-z]", "", str(value)).upper()


def extract_dir_id(book_id: str) -> str:
    """Pull the id/slug token out of a book directory name."""
    match = _DIR_ID_RE.match(book_id)
    return match.group("id") if match else book_id


def deslug(book_id: str) -> str:
    """Human-ish title fallback for an unmatched book directory."""
    token = extract_dir_id(book_id)
    cleaned = re.sub(r"[_)(]+", " ", token).strip()
    return cleaned.title() if cleaned.isupper() or "_" in token else cleaned or book_id


class MetadataCatalog:
    """In-memory index of catalog rows, queryable by book directory name."""

    def __init__(self, records: list[BookMetadata], key_field: str = "isbn"):
        self._records = records
        self._by_key: dict[str, BookMetadata] = {}
        self._by_dirname: dict[str, BookMetadata] = {}
        for rec in records:
            key_value = getattr(rec, key_field, None) if key_field in KNOWN_FIELDS else None
            if key_value:
                self._by_key.setdefault(normalize_id(key_value), rec)
            # also index by the row's own book_id field if the sheet carried one
            self._by_dirname.setdefault(normalize_id(rec.book_id), rec)

    def __len__(self) -> int:
        return len(self._records)

    @classmethod
    def from_excel(
        cls,
        path: Path | str,
        *,
        sheet: Any = 0,
        columns: dict[str, str] | None = None,
        key_field: str = "isbn",
    ) -> MetadataCatalog:
        """Load a catalog from an ``.xlsx`` file.

        ``columns`` maps canonical field -> spreadsheet column header. Missing
        columns are simply skipped. ``key_field`` is the canonical field used to
        match the directory id (default ``isbn``).
        """
        import pandas as pd

        columns = columns or DEFAULT_COLUMNS
        unknown = set(columns) - KNOWN_FIELDS - {JOIN_FIELD}
        if unknown:
            raise ValueError(f"[metadata.columns] has unknown fields: {sorted(unknown)}")

        frame = pd.read_excel(path, sheet_name=sheet, dtype=object)
        present = {field: col for field, col in columns.items() if col in frame.columns}
        absent = set(columns) - set(present)
        if absent:
            logger.warning(
                "catalog %s missing columns for fields %s (have %s)",
                Path(path).name,
                sorted(absent),
                list(frame.columns),
            )

        parsed = [cls._row_to_record(row, present) for _, row in frame.iterrows()]
        records: list[BookMetadata] = [rec for rec in parsed if rec is not None]
        logger.info("loaded %d catalog rows from %s", len(records), Path(path).name)
        return cls(records, key_field=key_field)

    @staticmethod
    def _row_to_record(row: Any, present: dict[str, str]) -> BookMetadata | None:
        def cell(field: str) -> Any:
            return _clean(row[present[field]]) if field in present else None

        directory = cell(JOIN_FIELD)
        title = cell("title")
        isbn = cell("isbn")
        if not directory and not title and not isbn:
            return None  # blank row
        # book_id is the join key: prefer the exact directory name when present
        book_id = str(directory) if directory else str(isbn or title)
        if title:
            resolved_title = str(title)
        elif directory:
            resolved_title = deslug(book_id)  # row exists but title not filled yet
        else:
            resolved_title = str(isbn) if isbn else "Untitled"
        return BookMetadata(
            book_id=book_id,
            title=resolved_title,
            authors=_split_authors(cell("authors")),
            year=_coerce_year(cell("year")),
            publisher=_str_or_none(cell("publisher")),
            isbn=_str_or_none(isbn),
            faculty=_str_or_none(cell("faculty")),
            language=_str_or_none(cell("language")),
            catalog=_str_or_none(cell("catalog")),
            matched=True,
        )

    def lookup(self, book_id: str, *, faculty: str | None = None) -> BookMetadata:
        """Best match for a directory name, or a stub when nothing matches."""
        dir_id = normalize_id(extract_dir_id(book_id))
        rec = self._by_key.get(dir_id) or self._by_dirname.get(normalize_id(book_id))
        if rec is not None:
            return rec.model_copy(update={"book_id": book_id})
        logger.warning("no catalog row for %s (id %s) — using stub title", book_id, dir_id)
        return self.stub(book_id, faculty=faculty)

    @staticmethod
    def stub(book_id: str, *, faculty: str | None = None) -> BookMetadata:
        return BookMetadata(book_id=book_id, title=deslug(book_id), faculty=faculty, matched=False)

    def match_report(self, book_ids: list[str]) -> dict[str, Any]:
        """Match/miss summary for a set of directory names (CLI validate-catalog)."""
        misses = [bid for bid in book_ids if not self.lookup(bid).matched]
        return {
            "books": len(book_ids),
            "matched": len(book_ids) - len(misses),
            "missed": len(misses),
            "rows": len(self._records),
            "miss_sample": misses[:20],
        }


# -- cell coercion helpers ----------------------------------------------------


def _clean(value: Any) -> Any:
    import pandas as pd

    if value is None or (not isinstance(value, list) and pd.isna(value)):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value).strip() or None


def _split_authors(value: Any) -> list[str]:
    if value is None:
        return []
    return [a.strip() for a in _AUTHOR_SPLIT_RE.split(str(value)) if a.strip()]


def _coerce_year(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d{4}", str(value))
    return int(match.group()) if match else None
