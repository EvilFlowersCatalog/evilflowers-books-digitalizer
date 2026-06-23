"""Bibliographic metadata for a book, fed by the Excel catalog."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BookMetadata(BaseModel):
    """Catalog record for one book, resolved by directory name.

    Only ``title`` is required; everything else is best-effort and may be
    absent for books missing a catalog row (a de-slugged stub title is used
    then — see :meth:`MetadataCatalog.stub`).
    """

    book_id: str  # source directory name this record was matched to
    title: str
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    publisher: str | None = None
    isbn: str | None = None
    faculty: str | None = None
    language: str | None = None  # catalog hint (ISO), may override detection
    catalog: str | None = None  # target EvilFlowers catalog override (id/url_name)
    matched: bool = True  # False -> stub (no catalog row found)
    extra: dict[str, str] = Field(default_factory=dict)

    @property
    def author_line(self) -> str:
        """Authors joined for display on the cover / in PDF metadata."""
        return ", ".join(self.authors)
