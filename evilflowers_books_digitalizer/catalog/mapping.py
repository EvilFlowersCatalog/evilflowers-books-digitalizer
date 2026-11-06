"""Map a digitized book onto an EvilFlowers Catalog *entry manifest*.

This is the pure (no HTTP) translation layer between our
:class:`~evilflowers_books_digitalizer.metadata.models.BookMetadata` plus the
on-disk artifacts (the PDF and cover) and the catalog's create-entry form,
:class:`evilflowers_protocol.forms.EntryCreate`.

The catalog import unit is an **Entry** (one publication) carrying metadata and
one or more **Acquisitions** (downloadable files). We emit one
:class:`EntryManifest` per book; :mod:`.manifest` serializes it to a sidecar
JSON next to the PDF, and :mod:`.publisher` turns it into API calls.

Field mapping (our field -> catalog Entry field):

* ``title``                       -> ``title`` (required, ≤ 255 chars)
* ``language`` / OCR language     -> ``language_code`` (ISO 639-1, alpha3 fallback)
* ``authors`` (``"Surname Given"``) -> ``authors`` ``[{name, surname}]``
* ``isbn``                        -> ``identifiers.isbn``
* ``year``                        -> ``published_at`` (a partial date — year alone is fine)
* ``publisher``                   -> ``publisher``
* the PDF                         -> an ``Acquisition`` (``application/pdf``)
* the cover image                 -> the entry ``image`` (a ``data:`` URI, read at publish time)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from evilflowers_protocol.forms import AuthorRef, EntryConfig, EntryCreate
from pydantic import BaseModel, Field

from evilflowers_books_digitalizer.metadata.isbn_lookup import is_valid_isbn, normalize_isbn
from evilflowers_books_digitalizer.metadata.models import BookMetadata

logger = logging.getLogger(__name__)

#: Identifier keys the catalog accepts (``settings.EVILFLOWERS_IDENTIFIERS``).
ALLOWED_IDENTIFIERS = ("isbn", "google", "doi", "dataverse_pid", "dataverse_dataset_id")

#: Acquisition relation for freely downloadable PDFs.
DEFAULT_RELATION = "open-access"

#: Language sent when neither the catalog row nor OCR detection gives one.
DEFAULT_LANGUAGE = "sk"

#: Catalog field length limits (``apps/api/forms/entries.py``).
MAX_TITLE = 255
MAX_PUBLISHER = 255

#: Order of given/family name in author strings. ``"given_first"`` reads
#: ``"Ján Novák"``; ``"surname_first"`` reads the Slovak library convention
#: ``"Novák Ján"`` (what ``catalog.xlsx`` uses — see ``metadata/draft.py``).
NAME_ORDERS = ("given_first", "surname_first")

#: ISO 639-2 (bibliographic and terminological) -> ISO 639-1 for the
#: collection's languages. Codes not listed pass through unchanged.
_ALPHA3_TO_ALPHA2 = {
    "slk": "sk", "slo": "sk",
    "ces": "cs", "cze": "cs",
    "eng": "en",
    "deu": "de", "ger": "de",
    "rus": "ru",
    "hun": "hu",
    "pol": "pl",
    "ukr": "uk",
    "fra": "fr", "fre": "fr",
    "ita": "it",
    "spa": "es",
}


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

    # provenance / bookkeeping (never sent to the catalog)
    source: str
    book_id: str
    slug: str
    faculty: str | None = None
    page_count: int | None = None
    matched: bool = True
    #: field -> where its value came from (excel, stu_opac, openlibrary, ocr, dirname)
    provenance: dict[str, str] = Field(default_factory=dict)
    #: what OCR extraction found (ISBN candidates, year, publisher), for review
    ocr_candidates: dict[str, Any] = Field(default_factory=dict)

    # The target catalog (UUID or url_name) as resolved when the manifest was
    # written. The publisher re-resolves it from ``catalog_override`` and the
    # current [catalog] config, so a config change needs no manifest rebuild.
    catalog: str
    #: per-book target from the Excel ``cieľový_katalog`` column (wins over config)
    catalog_override: str | None = None

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

    @property
    def isbn(self) -> str | None:
        return self.identifiers.get("isbn") or None

    def to_entry_create(self, *, image: str | None = None) -> EntryCreate:
        """The validated body for ``POST /catalogs/<id>/entries``.

        ``image`` is the cover as a ``data:image/...;base64,`` URI (see
        :func:`evilflowers_protocol.forms.image_data_uri`). The PDF is uploaded
        separately. Raises :class:`pydantic.ValidationError` on a bad payload.

        Authors without a given name are left out: the catalog requires both
        ``name`` and ``surname``, so sending them would reject the whole entry.
        """
        authors: list[AuthorRef] = []
        for author in self.authors:
            if author.name.strip() and author.surname.strip():
                authors.append(AuthorRef(name=author.name.strip(), surname=author.surname.strip()))
            else:
                logger.warning(
                    "%s: author %r has no given name; left out of the catalog entry",
                    self.slug, f"{author.name} {author.surname}".strip(),
                )
        identifiers = {
            key: value.strip()
            for key, value in self.identifiers.items()
            if key in ALLOWED_IDENTIFIERS and value and value.strip()
        }
        return EntryCreate(
            title=self.title.strip()[:MAX_TITLE],
            language_code=normalize_language(self.language_code) or DEFAULT_LANGUAGE,
            authors=authors,
            publisher=self.publisher.strip()[:MAX_PUBLISHER] if self.publisher else None,
            published_at=self.published_at or None,
            summary=self.summary or None,
            identifiers=identifiers or None,
            config=EntryConfig(**self.config) if self.config else None,
            image=image,
        )


def normalize_language(code: str | None) -> str | None:
    """A language code as ISO 639-1 where one exists, else the alpha3 as given.

    Accepts Tesseract combos (``"slk+eng"`` -> ``"sk"``) and locale tags
    (``"sk-SK"`` -> ``"sk"``). Returns ``None`` for an empty value.
    """
    if not code:
        return None
    primary = str(code).strip().lower().split("+")[0].replace("_", "-").split("-")[0].strip()
    if not primary:
        return None
    if len(primary) == 2:
        return primary
    return _ALPHA3_TO_ALPHA2.get(primary, primary)


def normalize_isbn_identifier(isbn: str | None) -> str | None:
    """A compact ISBN (digits + trailing X) for ``identifiers.isbn``.

    Valid ISBNs lose their hyphens and spaces so the catalog's duplicate check
    and our 409 handling compare like with like; anything else is kept as
    written (stripped), since it may still be a meaningful local identifier.
    """
    if not isbn or not str(isbn).strip():
        return None
    compact = normalize_isbn(isbn)
    return compact if is_valid_isbn(compact) else str(isbn).strip()


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
    default: str = DEFAULT_LANGUAGE,
) -> str:
    """Pick one ISO 639-1 code (alpha3 when no alpha2 exists) for ``language_code``.

    Prefers the catalog hint (``meta.language``), then the primary detected OCR
    language (Tesseract codes like ``"slk+eng"``), then ``default``.
    """
    for candidate in (meta.language, ocr_language, default):
        normalized = normalize_language(candidate)
        if normalized:
            return normalized
    return DEFAULT_LANGUAGE


def target_catalog(
    source: str, *, override: str | None, by_source: bool, default: str
) -> str:
    """The catalog a book imports into.

    Precedence: the per-book Excel override, then the source key when
    ``[catalog].by_source`` is on, then ``[catalog].catalog``. Raises
    :class:`ValueError` when none applies.
    """
    for candidate in (override, source if by_source else None, default):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    raise ValueError(
        f"no target catalog for source {source!r}: set [catalog].catalog, "
        "[catalog].by_source = true, or the per-book catalog column"
    )


def build_manifest(
    meta: BookMetadata,
    *,
    source: str,
    book_id: str,
    pdf: Path,
    catalog: str,
    cover: Path | None = None,
    ocr_language: str | None = None,
    default_language: str = DEFAULT_LANGUAGE,
    relation: str = DEFAULT_RELATION,
    entry_config: dict[str, Any] | None = None,
    page_count: int | None = None,
    author_name_order: str = "given_first",
    by_source: bool = False,
) -> EntryManifest:
    """Assemble an :class:`EntryManifest` from a book's metadata + artifacts.

    ``catalog`` is the configured default target; see :func:`target_catalog`
    for how the per-book override and ``by_source`` take precedence.
    """
    identifiers: dict[str, str] = {}
    isbn = normalize_isbn_identifier(meta.isbn)
    if isbn:
        identifiers["isbn"] = isbn

    authors = [
        split_author(a, order=author_name_order) for a in meta.authors if a and a.strip()
    ]
    provenance = meta.extra.get("provenance")
    ocr_candidates = meta.extra.get("ocr_candidates")

    return EntryManifest(
        source=source,
        book_id=book_id,
        slug=f"{source}_{book_id}",
        faculty=meta.faculty,
        page_count=page_count,
        matched=meta.matched,
        provenance=dict(provenance) if isinstance(provenance, dict) else {},
        ocr_candidates=dict(ocr_candidates) if isinstance(ocr_candidates, dict) else {},
        catalog=target_catalog(
            source, override=meta.catalog, by_source=by_source, default=catalog
        ),
        catalog_override=meta.catalog or None,
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
