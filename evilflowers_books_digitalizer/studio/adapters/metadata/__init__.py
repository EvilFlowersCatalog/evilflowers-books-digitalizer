"""MetadataProvider adapters: Excel catalog, OPAC/Open Library by ISBN, OCR extraction.

Each wraps an existing module (``metadata.catalog``, ``metadata.isbn_lookup``,
``metadata.ocr_extract``) and speaks IA ``ItemMetadata``. :class:`CompositeMetadata`
asks them in the project's order and fills gaps with :meth:`ItemMetadata.merge`
(earlier = more trusted; never overwritten).

The port gives a provider only the scanned book, so every provider gets the
``ProjectConfig`` at construction to compute the permanent IA identifier.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.studio.config import ProjectConfig
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata, marc_language
from evilflowers_books_digitalizer.studio.ports import MetadataProvider, ScannedBook

logger = logging.getLogger(__name__)

#: ``ocr_text`` page separator (Tesseract/pipeline convention: form feed)
PAGE_BREAK = "\f"


def _identifier(project: ProjectConfig, book: ScannedBook) -> str:
    return project.item_identifier(book.collection, book.external_id)


def _languages(value: str | None) -> list[str]:
    return [marc_language(value)] if value else []


def from_record(identifier: str, *, title: str | None = None, authors: Sequence[str] = (),
                publisher: str | None = None, place: str | None = None,
                year: int | str | None = None, isbn: str | None = None,
                language: str | None = None, edition: str | None = None) -> ItemMetadata:
    """Common shape of every source -> ItemMetadata (IA ``publisher`` is "Place : Name")."""
    pub = f"{place} : {publisher}" if place and publisher else (publisher or "")
    return ItemMetadata(identifier=identifier, title=title or "", creator=list(authors),
                        publisher=pub, date=str(year) if year else "",
                        isbn=[isbn] if isbn else [], language=_languages(language),
                        edition=edition or "")


class ExcelMetadata:
    """The library's spreadsheet, joined on the book directory name (``metadata.catalog``)."""

    def __init__(self, project: ProjectConfig, excel_path: Path, *,
                 columns: dict[str, str] | None = None, key_field: str = "isbn") -> None:
        self.project = project
        self.excel_path = excel_path
        self.columns = columns
        self.key_field = key_field
        self._catalog: Any = None

    def _load(self) -> Any:
        if self._catalog is None:   # pandas + the workbook only when first needed
            from evilflowers_books_digitalizer.metadata.catalog import MetadataCatalog

            self._catalog = MetadataCatalog.from_excel(self.excel_path, columns=self.columns,
                                                       key_field=self.key_field)
        return self._catalog

    def lookup(self, book: ScannedBook, *, ocr_text: str | None = None) -> ItemMetadata:
        rec = self._load().lookup(book.external_id, faculty=book.collection)
        if not rec.matched:   # a de-slugged stub title must not outrank a real source
            return ItemMetadata(identifier=_identifier(self.project, book))
        return from_record(_identifier(self.project, book), title=rec.title, authors=rec.authors,
                           publisher=rec.publisher, year=rec.year, isbn=rec.isbn,
                           language=rec.language)


class OpacMetadata:
    """ISBN -> STU OPAC / Open Library (``metadata.isbn_lookup``), cached on disk.

    The ISBN comes from the directory name (most STU books are filed by ISBN) or,
    failing that, from the OCR text of the front/back matter.
    """

    def __init__(self, project: ProjectConfig, *, cache_dir: Path | None = None,
                 providers: Sequence[str] = ("stu_opac", "openlibrary"), enricher: Any = None
                 ) -> None:
        self.project = project
        if enricher is None:
            from evilflowers_books_digitalizer.metadata.isbn_lookup import IsbnEnricher

            enricher = IsbnEnricher(cache_dir=cache_dir, providers=tuple(providers))
        self.enricher = enricher

    @staticmethod
    def find_isbn(book: ScannedBook, ocr_text: str | None) -> str | None:
        from evilflowers_books_digitalizer.metadata.catalog import extract_dir_id
        from evilflowers_books_digitalizer.metadata.isbn_lookup import to_isbn13

        if isbn := to_isbn13(extract_dir_id(book.external_id)):
            return isbn
        return OcrExtractMetadata.extraction(ocr_text).isbn if ocr_text else None

    def lookup(self, book: ScannedBook, *, ocr_text: str | None = None) -> ItemMetadata:
        identifier = _identifier(self.project, book)
        isbn = self.find_isbn(book, ocr_text)
        found = self.enricher.lookup(isbn) if isbn else {}
        if not found:
            return ItemMetadata(identifier=identifier, isbn=[isbn] if isbn else [])
        return from_record(identifier, title=found.get("title"), authors=found.get("authors", ()),
                           publisher=found.get("publisher"), place=found.get("place"),
                           year=found.get("year"), isbn=isbn, edition=found.get("edition"))


class OcrExtractMetadata:
    """ISBN, year and publisher read off the colophon (``metadata.ocr_extract``)."""

    def __init__(self, project: ProjectConfig, *, head: int = 8, tail: int = 4) -> None:
        self.project = project
        self.head = head
        self.tail = tail

    @staticmethod
    def extraction(ocr_text: str, head: int = 8, tail: int = 4) -> Any:
        from evilflowers_books_digitalizer.metadata.ocr_extract import extract, select_pages

        return extract(select_pages(ocr_text.split(PAGE_BREAK), head, tail))

    def lookup(self, book: ScannedBook, *, ocr_text: str | None = None) -> ItemMetadata:
        identifier = _identifier(self.project, book)
        if not ocr_text:
            return ItemMetadata(identifier=identifier)
        found = self.extraction(ocr_text, self.head, self.tail)
        return from_record(identifier, publisher=found.publisher, year=found.year, isbn=found.isbn)


class CompositeMetadata:
    """Providers in priority order; gaps filled from later ones; project language last."""

    def __init__(self, project: ProjectConfig, providers: Sequence[MetadataProvider]) -> None:
        self.project = project
        self.providers = list(providers)

    def lookup(self, book: ScannedBook, *, ocr_text: str | None = None) -> ItemMetadata:
        merged = ItemMetadata(identifier=_identifier(self.project, book))
        for provider in self.providers:
            try:
                merged.merge(provider.lookup(book, ocr_text=ocr_text))
            except Exception as exc:  # noqa: BLE001 — one flaky source must not lose the others
                logger.warning("metadata provider %s failed for %s: %s",
                               type(provider).__name__, book.external_id, exc)
        if not merged.language:
            merged.language = _languages(self.project.language)
        return merged
