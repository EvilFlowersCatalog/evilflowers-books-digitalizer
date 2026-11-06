"""Shared fixtures: an in-memory stand-in for the catalog API, clean logging."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from evilflowers_protocol.errors import CatalogError, Conflict, NotFound
from evilflowers_protocol.forms import AcquisitionRef, CatalogRef, EntryCreate, EntryRef

from evilflowers_books_digitalizer import logs
from evilflowers_books_digitalizer.metadata.isbn_lookup import to_isbn13


@dataclass
class _Entry:
    id: UUID
    catalog_id: UUID
    title: str
    identifiers: dict[str, str]
    acquisitions: list[AcquisitionRef] = field(default_factory=list)

    def ref(self, *, detailed: bool = True) -> EntryRef:
        return EntryRef(
            id=self.id, title=self.title, catalog_id=self.catalog_id,
            identifiers=self.identifiers if detailed else {},
            acquisitions=list(self.acquisitions),
        )


class FakeCatalog:
    """The ``CatalogClient`` methods the publisher uses, against in-memory state.

    Mirrors the backend rules that matter here: 409 on the same title (case-
    insensitive) or ISBN within a catalog; list results carry no identifiers.
    ``fail_uploads`` makes the next N uploads fail like a timeout.
    """

    def __init__(self, *catalogs: str) -> None:
        self.catalogs = {
            name: CatalogRef(id=uuid4(), url_name=name, title=name.upper())
            for name in (catalogs or ("svf",))
        }
        self.entries: dict[UUID, _Entry] = {}
        self.calls: list[tuple[str, Any]] = []
        self.uploads: list[Path] = []
        self.fail_uploads = 0

    # helpers for tests ---------------------------------------------------------
    def seed(self, catalog: str, title: str, isbn: str | None = None,
             *, with_pdf: bool = False) -> UUID:
        cid = self.catalogs[catalog].id
        entry = _Entry(uuid4(), cid, title, {"isbn": isbn} if isbn else {})
        if with_pdf:
            entry.acquisitions.append(
                AcquisitionRef(id=uuid4(), relation="open-access", mime="application/pdf"))
        self.entries[entry.id] = entry
        return entry.id

    def in_catalog(self, catalog: str) -> list[_Entry]:
        cid = self.catalogs[catalog].id
        return [e for e in self.entries.values() if e.catalog_id == cid]

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)

    # CatalogClient surface -----------------------------------------------------
    def ping(self) -> None:
        self.calls.append(("ping", None))

    def resolve_catalog(self, url_name_or_uuid: str) -> CatalogRef:
        self.calls.append(("resolve_catalog", url_name_or_uuid))
        for ref in self.catalogs.values():
            if url_name_or_uuid in (ref.url_name, str(ref.id)):
                return ref
        raise NotFound(None, None, "GET", "fake/catalogs", f"no catalog {url_name_or_uuid!r}")

    def create_entry(self, catalog_id: UUID, entry: EntryCreate) -> EntryRef:
        self.calls.append(("create_entry", entry))
        isbn = to_isbn13((entry.identifiers or {}).get("isbn"))
        for existing in self.entries.values():
            if existing.catalog_id != catalog_id:
                continue
            if existing.title.casefold() == entry.title.casefold() or (
                isbn and to_isbn13(existing.identifiers.get("isbn")) == isbn
            ):
                raise Conflict(409, {"title": "Entry already exists!"}, "POST", "fake/entries")
        created = _Entry(uuid4(), catalog_id, entry.title, dict(entry.identifiers or {}))
        self.entries[created.id] = created
        return created.ref()

    def get_entry(self, catalog_id: UUID, entry_id: UUID) -> EntryRef:
        self.calls.append(("get_entry", entry_id))
        entry = self.entries.get(entry_id)
        if entry is None or entry.catalog_id != catalog_id:
            raise NotFound(404, None, "GET", "fake/entry")
        return entry.ref()

    def find_entries(self, catalog_id: UUID, *, title: str) -> list[EntryRef]:
        self.calls.append(("find_entries", title))
        return [
            e.ref(detailed=False) for e in self.entries.values()
            if e.catalog_id == catalog_id and e.title.casefold() == title.casefold()
        ]

    def upload_acquisition(
        self, catalog_id: UUID, entry_id: UUID, path: Path, *,
        mime: str = "application/pdf", relation: str = "open-access",
    ) -> AcquisitionRef:
        self.calls.append(("upload_acquisition", path))
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise CatalogError(None, None, "POST", "fake/upload", "timed out")
        acquisition = AcquisitionRef(id=uuid4(), relation=relation, mime=mime)
        self.entries[entry_id].acquisitions.append(acquisition)
        self.uploads.append(Path(path))
        return acquisition


@pytest.fixture
def fake_catalog() -> FakeCatalog:
    return FakeCatalog("svf", "fei", "mtf")


@pytest.fixture(autouse=True)
def _clean_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with no digitalizer log config and no env overrides."""
    for name in (logs.FORMAT_ENV, logs.LEVEL_ENV, "EVILFLOWERS_DIGITALIZER_CONFIG",
                 "EVILFLOWERS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    logs.reset_logging()
    yield
    logs.reset_logging()
