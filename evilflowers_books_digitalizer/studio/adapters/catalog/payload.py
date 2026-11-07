"""ItemMetadata (IA semantics) -> EvilFlowers entry payload.

The catalog-side rules (author shape, identifier whitelist, length limits,
ISO 639-1 language) already live in :mod:`evilflowers_books_digitalizer.catalog.mapping`;
we translate IA fields into an :class:`EntryManifest` and let it build the
validated :class:`EntryCreate`, so both publishing paths send identical payloads.

IA -> catalog differences handled here:

* ``creator`` is LC heading form ``"Surname, Given, 1950-"``: only the first two
  comma parts are a name (the rest are dates/roles);
* ``publisher`` is IA's ``"Place : Publisher"``: the catalog wants the name only;
* ``date`` may be ``YYYY-MM-DD``, ``[1987]`` or ``[n.d.]``: the first 4-digit year wins;
* ``language`` is MARC (``slo``): ``normalize_language`` maps it to ``sk``;
* ``isbn`` is repeatable: the first valid one is the entry's identifier.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from evilflowers_protocol.forms import EntryCreate, EntryUpdate

from evilflowers_books_digitalizer.catalog.mapping import (
    DEFAULT_LANGUAGE,
    DEFAULT_RELATION,
    EntryAuthor,
    EntryManifest,
    normalize_isbn_identifier,
    normalize_language,
    split_author,
)
from evilflowers_books_digitalizer.metadata.isbn_lookup import is_valid_isbn, normalize_isbn
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata

_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def year_of(date: str) -> str | None:
    match = _YEAR.search(date or "")
    return match.group(1) if match else None


def author_of(creator: str) -> EntryAuthor:
    name = ", ".join(part.strip() for part in creator.split(",")[:2] if part.strip())
    return split_author(name)


def publisher_name(publisher: str) -> str | None:
    name = publisher.split(" : ", 1)[-1].strip() if publisher else ""
    return name or None


def primary_isbn(isbns: list[str]) -> str | None:
    """First *valid* ISBN (compact form); else the first one as written."""
    for isbn in isbns:
        if isbn and is_valid_isbn(normalize_isbn(isbn)):
            return normalize_isbn_identifier(isbn)
    return normalize_isbn_identifier(isbns[0]) if isbns else None


def to_manifest(meta: ItemMetadata, *, catalog: str, collection: str = "",
                default_language: str = DEFAULT_LANGUAGE, relation: str = DEFAULT_RELATION,
                entry_config: dict[str, Any] | None = None, pdf: Path | None = None
                ) -> EntryManifest:
    isbn = primary_isbn(meta.isbn)
    return EntryManifest(
        source=collection or catalog,
        book_id=meta.identifier,
        slug=meta.identifier,
        catalog=catalog,
        title=meta.title or meta.identifier,
        language_code=next((c for c in map(normalize_language, meta.language) if c), default_language),
        authors=[author_of(c) for c in meta.creator if c.strip()],
        publisher=publisher_name(meta.publisher),
        published_at=year_of(meta.date),
        summary=meta.description[0] if meta.description else None,
        identifiers={"isbn": isbn} if isbn else {},
        config=dict(entry_config or {}),
        relation=relation,
        pdf=pdf.name if pdf else f"{meta.identifier}.pdf",
    )


def entry_create(manifest: EntryManifest, *, image: str | None = None) -> EntryCreate:
    return manifest.to_entry_create(image=image)


def entry_update(manifest: EntryManifest, *, image: str | None = None) -> EntryUpdate:
    """PUT body; the backend still requires title + language_code on PUT, which
    the full create payload always carries."""
    return EntryUpdate.model_validate(entry_create(manifest, image=image).model_dump())
