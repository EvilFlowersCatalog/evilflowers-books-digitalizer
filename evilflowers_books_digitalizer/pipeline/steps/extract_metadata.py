"""Metadata from the OCR text: ISBN/year/publisher extraction + ISBN lookup.

Runs between :class:`~.ocr.OcrPages` and :class:`~.render.RenderPdf`, because
RenderPdf bakes title and authors into each PDF's XMP and rewriting XMP later
breaks PDF/A. Downstream, GenerateCover (OPAC cover by ISBN, generated cover)
and WriteCatalogManifest (``identifiers.isbn``, ``publisher``, ``published_at``)
read what this step writes.

1. Take the OCR text of the first ``head_pages`` and last ``tail_pages`` pages
   from ``artifacts['text']`` (pages are ``\\f``-separated; no second OCR pass).
2. Extract ISBN candidates, year and publisher (:mod:`~...metadata.ocr_extract`).
3. With ``lookup`` on and an ISBN known, ask the existing
   :class:`~...metadata.isbn_lookup.IsbnEnricher` (STU OPAC, then Open Library).
4. Merge into ``ctx.metadata['book_metadata']`` field by field, by source rank:
   ``excel > opac/openlibrary/google > ocr > dirname``. A dirname-derived stub
   title counts as a placeholder, so better sources replace it. With
   ``override = true`` the lookup outranks Excel (OCR still only fills gaps).
   ``extra['provenance']`` records ``{field: source}``; ``extra['ocr_candidates']``
   what OCR saw. A book the lookup resolved is no longer a stub (``matched``).

Never fatal: any failure (network included) is logged as a warning and the book
continues with whatever was gathered.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from evilflowers_books_digitalizer.metadata.catalog import MetadataCatalog
from evilflowers_books_digitalizer.metadata.draft import derive_isbn
from evilflowers_books_digitalizer.metadata.isbn_lookup import normalize_isbn, to_isbn13
from evilflowers_books_digitalizer.metadata.models import BookMetadata
from evilflowers_books_digitalizer.metadata.ocr_extract import extract, select_pages
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)

#: Fields this step may fill.
FIELDS = ("title", "subtitle", "authors", "year", "publisher", "isbn")

EXCEL = "excel"
OCR = "ocr"
DIRNAME = "dirname"
#: IsbnEnricher ``_source`` label -> provenance name
LOOKUP_SOURCES = {"STU IPAC": "stu_opac", "Open Library": "openlibrary", "Google Books": "google"}


class IsbnLookup(Protocol):
    """Anything with :meth:`IsbnEnricher.lookup`'s shape (injectable for tests)."""

    def lookup(self, isbn: str) -> dict[str, Any]: ...


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


class ExtractMetadata(PipelineStep):
    """Enrich ``ctx.metadata`` from the OCR text and an ISBN lookup."""

    name = "extract_metadata"

    def __init__(
        self,
        *,
        head_pages: int = 8,
        tail_pages: int = 4,
        lookup: bool = True,
        override: bool = False,
        enricher: IsbnLookup | None = None,
        cache_dir: Path | None = None,
        faculty_map: dict[str, str] | None = None,
    ) -> None:
        self.head_pages = max(head_pages, 0)
        self.tail_pages = max(tail_pages, 0)
        self.lookup = lookup
        self.override = override
        self._enricher = enricher
        self.cache_dir = cache_dir
        self.faculty_map = faculty_map or {}

    def run(self, ctx: BookContext) -> BookContext:
        try:
            self._run(ctx)
        except Exception as exc:  # noqa: BLE001 — enrichment is never worth failing a book
            logger.warning("%s: metadata extraction failed: %s", ctx.slug, exc, exc_info=True)
        return ctx

    # -- internals ---------------------------------------------------------------

    def _enricher_or_none(self) -> IsbnLookup | None:
        if self._enricher is None and self.lookup:
            from evilflowers_books_digitalizer.metadata.isbn_lookup import IsbnEnricher

            self._enricher = IsbnEnricher(cache_dir=self.cache_dir)
        return self._enricher

    def _record(self, ctx: BookContext) -> BookMetadata:
        stored = ctx.metadata.get("book_metadata")
        if stored:
            return BookMetadata(**stored)
        faculty = self.faculty_map.get(ctx.source, ctx.source.upper())
        return MetadataCatalog.stub(ctx.book_id, faculty=faculty)

    @staticmethod
    def _page_texts(ctx: BookContext) -> list[str]:
        text_path = ctx.artifacts.get("text")
        if text_path is None or not text_path.exists():
            return []
        return text_path.read_text(encoding="utf-8", errors="ignore").split("\f")

    @staticmethod
    def _existing(record: BookMetadata) -> tuple[dict[str, Any], dict[str, str]]:
        """Current non-placeholder values and their provenance."""
        provenance: dict[str, str] = dict(record.extra.get("provenance") or {})
        values: dict[str, Any] = {}
        for name in FIELDS:
            value = getattr(record, name)
            if _empty(value):
                continue
            source = provenance.get(name) or (EXCEL if record.matched else DIRNAME)
            if source == DIRNAME:
                continue  # a placeholder: anything better replaces it
            values[name] = value
            provenance[name] = source
        return values, provenance

    def _lookup(self, isbn: str | None) -> tuple[str | None, dict[str, Any]]:
        if not (self.lookup and isbn):
            return None, {}
        enricher = self._enricher_or_none()
        if enricher is None:
            return None, {}
        try:
            found = enricher.lookup(isbn)
        except Exception as exc:  # noqa: BLE001 — network trouble must not stop the book
            logger.warning("ISBN lookup for %s failed: %s", isbn, exc)
            return None, {}
        if not found.get("title"):
            return None, {}
        source = LOOKUP_SOURCES.get(str(found.get("_source")), "lookup")
        values = {name: found.get(name) for name in FIELDS if not _empty(found.get(name))}
        if isinstance(values.get("authors"), str):
            values["authors"] = [values["authors"]]
        values.pop("isbn", None)  # the ISBN we asked with is not new information
        return source, values

    def _rank(self, source: str) -> int:
        lookup_rank, excel_rank = (0, 1) if self.override else (1, 0)
        if source == EXCEL:
            return excel_rank
        if source == OCR:
            return 2
        if source == DIRNAME:
            return 3
        return lookup_rank  # stu_opac / openlibrary / google

    def _run(self, ctx: BookContext) -> None:
        record = self._record(ctx)
        existing, provenance = self._existing(record)

        dirname_isbn = to_isbn13(derive_isbn(ctx.book_id))
        dirname_values: dict[str, Any] = {"title": MetadataCatalog.stub(ctx.book_id).title}
        if dirname_isbn:
            dirname_values["isbn"] = dirname_isbn

        pages = select_pages(self._page_texts(ctx), self.head_pages, self.tail_pages)
        known_isbn = existing.get("isbn") or dirname_isbn
        extraction = extract(pages, known_isbn=known_isbn)
        ocr_values = {
            name: value
            for name, value in (
                ("isbn", extraction.isbn), ("year", extraction.year),
                ("publisher", extraction.publisher),
            )
            if not _empty(value)
        }

        lookup_isbn = existing.get("isbn") or extraction.isbn or dirname_isbn
        lookup_source, lookup_values = self._lookup(
            normalize_isbn(str(lookup_isbn)) if lookup_isbn else None
        )

        layers: list[tuple[str, dict[str, Any]]] = [
            (provenance[name], {name: value}) for name, value in existing.items()
        ]
        if lookup_source:
            layers.append((lookup_source, lookup_values))
        layers += [(OCR, ocr_values), (DIRNAME, dirname_values)]
        layers.sort(key=lambda layer: self._rank(layer[0]))  # stable: ties keep order

        update: dict[str, Any] = {}
        new_provenance: dict[str, str] = {}
        for name in FIELDS:
            for source, values in layers:
                if not _empty(values.get(name)):
                    update[name] = values[name]
                    new_provenance[name] = source
                    break
        if "isbn" in update:
            update["isbn"] = str(update["isbn"])

        extra = {
            **record.extra,
            "provenance": new_provenance,
            "ocr_candidates": extraction.as_dict(),
        }
        merged = record.model_copy(update={
            **update,
            "matched": record.matched or bool(lookup_source),
            "extra": extra,
        })
        self._publish(ctx, merged)
        logger.info(
            "%s: metadata %s (isbn=%s, sources=%s)",
            ctx.slug, merged.title[:60], merged.isbn, sorted(set(new_provenance.values())),
        )

    @staticmethod
    def _publish(ctx: BookContext, record: BookMetadata) -> None:
        """Mirror the record into the flat keys RenderPdf/EnrichPdfMetadata/GenerateCover read."""
        ctx.metadata["title"] = record.title
        for name in ("authors", "year", "publisher", "isbn"):
            value = getattr(record, name)
            if _empty(value):
                ctx.metadata.pop(name, None)
            else:
                ctx.metadata[name] = value
        ctx.metadata["faculty"] = record.faculty
        ctx.metadata["catalog_matched"] = record.matched
        ctx.metadata["book_metadata"] = record.model_dump()
