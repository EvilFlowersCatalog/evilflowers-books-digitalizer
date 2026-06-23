"""Map a digitized book onto an EvilFlowers Catalog *entry manifest*.

This is the pure (no I/O, no HTTP) translation layer between our
:class:`~evilflowers_books_digitalizer.metadata.models.BookMetadata` plus the
on-disk artifacts (the PDF and cover) and the JSON the catalog's REST API
expects when creating an ``Entry`` (see ``apps/api/forms/entries.py`` in
EvilFlowersCatalog).

The catalog import unit is an **Entry** (one publication) carrying metadata and
one or more **Acquisitions** (downloadable files). We emit one
:class:`EntryManifest` per book; :mod:`.manifest` serializes it to a sidecar
JSON next to the PDF, and :mod:`.client` turns it into the actual API calls.

Field mapping (our field -> catalog Entry field):

* ``title``                       -> ``title`` (required)
* ``language`` / OCR language     -> ``language_code`` (alpha2/alpha3, required)
* ``authors`` (``"Surname Given"``) -> ``authors`` ``[{name, surname}]``
* ``isbn``                        -> ``identifiers.isbn`` (allowed keys: isbn/google/doi)
* ``year``                        -> ``published_at`` (a partial date — year alone is fine)
* ``publisher``                   -> ``publisher``
* the PDF                         -> an open-access ``Acquisition`` (``application/pdf``)
* the cover image                 -> the entry ``image`` (base64, attached at publish time)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evilflowers_books_digitalizer.metadata.models import BookMetadata

#: Identifier keys the catalog accepts (``settings.EVILFLOWERS_IDENTIFIERS``).
ALLOWED_IDENTIFIERS = ("isbn", "google", "doi")

#: Acquisition relation for freely downloadable PDFs.
DEFAULT_RELATION = "open-access"

#: Order of given/family name in author strings. ``"given_first"`` reads
#: ``"Ján Novák"``; ``"surname_first"`` reads the Slovak library convention
#: ``"Novák Ján"`` (what ``catalog.xlsx`` uses — see ``metadata/draft.py``).
NAME_ORDERS = ("given_first", "surname_first")


class EntryAuthor(BaseModel):
    """One author in the shape the catalog's ``AuthorForm`` expects."""

    name: str = ""
    surname: str


class EntryManifest(BaseModel):
    """A catalog-ready description of one digitized book.

    Everything needed to create the ``Entry`` and attach its PDF, kept
    JSON-serializable so it can live as a sidecar artifact next to the PDF and
    be published later (or by a different tool) without re-running the catalog
    lookup. ``pdf`` and ``cover`` are basenames resolved relative to the
    manifest's own directory.
    """

    # provenance / bookkeeping (not sent to the catalog)
    source: str
    book_id: str
    slug: str
    faculty: str | None = None
    page_count: int | None = None
    matched: bool = True

    # the catalog this entry imports into (UUID or url_name slug); resolved at
    # publish time. All faculties default to one catalog — see [catalog] config.
    catalog: str

    # the Entry payload
    title: str
    language_code: str
    authors: list[EntryAuthor] = Field(default_factory=list)
    publisher: str | None = None
    published_at: str | None = None  # partial date; we emit the year
    summary: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)

    # the Acquisition (PDF) + optional cover image, as sibling filenames
    relation: str = DEFAULT_RELATION
    pdf: str
    cover: str | None = None

    def entry_payload(self, *, image_data_uri: str | None = None) -> dict[str, Any]:
        """The JSON body for ``POST /catalogs/<id>/entries``.

        The PDF is attached separately (multipart) so it is never base64-inlined
        here. ``image_data_uri`` (a ``data:<mime>;base64,...`` string) is added
        as the entry cover when provided.
        """
        payload: dict[str, Any] = {
            "title": self.title,
            "language_code": self.language_code,
        }
        if self.authors:
            payload["authors"] = [a.model_dump() for a in self.authors]
        if self.identifiers:
            payload["identifiers"] = self.identifiers
        if self.publisher:
            payload["publisher"] = self.publisher
        if self.published_at:
            payload["published_at"] = self.published_at
        if self.summary:
            payload["summary"] = self.summary
        if self.config:
            payload["config"] = self.config
        if image_data_uri:
            payload["image"] = image_data_uri
        return payload


def split_author(full_name: str, *, order: str = "given_first") -> EntryAuthor:
    """Split an author string into the catalog's ``{name, surname}`` shape.

    A ``"Surname, Given"`` comma form is unambiguous and always honoured. For
    the space-separated form, ``order`` decides which side is the surname
    (default ``"given_first"`` -> last token is the surname). A single token
    becomes the surname with an empty given name.
    """
    name = " ".join(full_name.split())  # collapse whitespace
    if not name:
        return EntryAuthor(name="", surname="")
    if "," in name:
        surname, _, given = name.partition(",")
        return EntryAuthor(name=given.strip(), surname=surname.strip())
    parts = name.split(" ")
    if len(parts) == 1:
        return EntryAuthor(name="", surname=parts[0])
    if order == "surname_first":
        return EntryAuthor(name=" ".join(parts[1:]), surname=parts[0])
    return EntryAuthor(name=" ".join(parts[:-1]), surname=parts[-1])


def resolve_language(
    meta: BookMetadata,
    *,
    ocr_language: str | None = None,
    default: str = "slk",
) -> str:
    """Pick a single alpha2/alpha3 code for the required ``language_code``.

    Prefers the catalog hint (``meta.language``), then the primary detected OCR
    language (Tesseract codes like ``"slk+eng"`` are alpha3 and the catalog
    accepts alpha3), then the configured default.
    """
    if meta.language:
        return str(meta.language).strip().lower()
    if ocr_language:
        primary = str(ocr_language).split("+")[0].strip().lower()
        if primary:
            return primary
    return default


def build_manifest(
    meta: BookMetadata,
    *,
    source: str,
    book_id: str,
    pdf: Path,
    catalog: str,
    cover: Path | None = None,
    ocr_language: str | None = None,
    default_language: str = "slk",
    relation: str = DEFAULT_RELATION,
    entry_config: dict[str, Any] | None = None,
    page_count: int | None = None,
    author_name_order: str = "given_first",
) -> EntryManifest:
    """Assemble an :class:`EntryManifest` from a book's metadata + artifacts.

    ``catalog`` is the default target; a per-book ``meta.catalog`` override (the
    optional ``catalog`` column in ``catalog.xlsx``) wins when present, so all
    faculties land in one catalog today while staying routable later.
    """
    identifiers: dict[str, str] = {}
    if meta.isbn:
        identifiers["isbn"] = str(meta.isbn).strip()

    authors = [
        split_author(a, order=author_name_order) for a in meta.authors if a and a.strip()
    ]

    return EntryManifest(
        source=source,
        book_id=book_id,
        slug=f"{source}_{book_id}",
        faculty=meta.faculty,
        page_count=page_count,
        matched=meta.matched,
        catalog=(meta.catalog or catalog),
        title=meta.title,
        language_code=resolve_language(meta, ocr_language=ocr_language, default=default_language),
        authors=authors,
        publisher=meta.publisher,
        published_at=str(meta.year) if meta.year else None,
        identifiers=identifiers,
        config=dict(entry_config or {}),
        relation=relation,
        pdf=Path(pdf).name,
        cover=Path(cover).name if cover else None,
    )
