"""EvilFlowersPublisher: create / republish-replace / 409 / external_url refusal, against a fake catalog API (no network)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from evilflowers_protocol.client import CatalogClient
from evilflowers_protocol.errors import CatalogError, Conflict, NotFound
from evilflowers_protocol.forms import (
    AcquisitionRef,
    CatalogRef,
    EntryCreate,
    EntryRef,
    EntryUpdate,
)

from evilflowers_books_digitalizer.studio.adapters.catalog import (
    EvilFlowersPublisher,
    NullPublisher,
    PublishConflict,
    PublishError,
    TransportDeleter,
    build_publisher,
    delete_acquisition,
)
from evilflowers_books_digitalizer.studio.adapters.catalog.payload import (
    author_of,
    publisher_name,
    year_of,
)
from evilflowers_books_digitalizer.studio.config import PublishConfig
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import CatalogPublisher, PortError

ISBN = "9788022726634"


@dataclass
class Stored:
    ref: EntryRef
    payload: EntryCreate


@dataclass
class FakeCatalog:
    """In-memory catalog speaking the StudioCatalogApi slice; records every call."""

    catalog: CatalogRef = field(default_factory=lambda: CatalogRef(id=uuid4(), url_name="fei", title="FEI"))
    entries: dict[UUID, Stored] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    conflict_on_create: bool = False
    retried_uploads: bool = False

    def resolve_catalog(self, url_name_or_uuid: str) -> CatalogRef:
        self.calls.append(("resolve", url_name_or_uuid))
        if url_name_or_uuid != self.catalog.url_name:
            raise NotFound(404, None, "GET", "catalogs", "no catalog")
        return self.catalog

    def create_entry(self, catalog_id: UUID, entry: EntryCreate) -> EntryRef:
        self.calls.append(("create", entry))
        if self.conflict_on_create:
            raise Conflict(409, {"title": "Conflict"}, "POST", "entries")
        return self._put(uuid4(), entry)

    def update_entry(self, catalog_id: UUID, entry_id: UUID, entry: EntryUpdate) -> EntryRef:
        self.calls.append(("update", entry))
        if entry_id not in self.entries:
            raise NotFound(404, None, "PUT", "entries")
        return self._put(entry_id, entry, keep=self.entries[entry_id].ref.acquisitions)

    def get_entry(self, catalog_id: UUID, entry_id: UUID) -> EntryRef:
        return self.entries[entry_id].ref

    def find_entries(self, catalog_id: UUID, *, title: str) -> list[EntryRef]:
        return [s.ref.model_copy(update={"identifiers": {}}) for s in self.entries.values()
                if s.ref.title.casefold() == title.casefold()]

    def upload_acquisition(self, catalog_id: UUID, entry_id: UUID, path: Path, *,
                           mime: str = "application/pdf", relation: str = "open-access"
                           ) -> AcquisitionRef:
        self.calls.append(("upload", (path.name, mime, relation)))
        if self.retried_uploads:   # a lost response + retry: the POST attached twice
            self._acquire(entry_id, mime, relation)
        return self._acquire(entry_id, mime, relation)

    def delete_acquisition(self, acquisition_id: UUID) -> None:
        self.calls.append(("delete", acquisition_id))
        for stored in self.entries.values():
            stored.ref = stored.ref.model_copy(update={"acquisitions": [
                a for a in stored.ref.acquisitions if a.id != acquisition_id]})

    # -- helpers ---------------------------------------------------------------
    def seed(self, title: str, isbn: str | None, mimes: tuple[str, ...] = ()) -> UUID:
        entry_id = uuid4()
        self.entries[entry_id] = Stored(EntryRef(id=entry_id, title=title,
                                                 identifiers={"isbn": isbn} if isbn else {}),
                                        EntryCreate(title=title, language_code="sk"))
        for mime in mimes:
            self._acquire(entry_id, mime, "open-access")
        return entry_id

    def _put(self, entry_id: UUID, entry: EntryCreate, keep: list[AcquisitionRef] | None = None) -> EntryRef:
        ref = EntryRef(id=entry_id, title=entry.title or "", identifiers=entry.identifiers or {},
                       acquisitions=list(keep or []))
        self.entries[entry_id] = Stored(ref, entry)
        return ref

    def _acquire(self, entry_id: UUID, mime: str, relation: str) -> AcquisitionRef:
        acq = AcquisitionRef(id=uuid4(), relation=relation, mime=mime)
        stored = self.entries[entry_id]
        stored.ref = stored.ref.model_copy(update={"acquisitions": [*stored.ref.acquisitions, acq]})
        return acq

    def ops(self) -> list[str]:
        return [name for name, _ in self.calls if name != "resolve"]

    def mimes(self, entry_id: UUID) -> list[str]:
        return sorted(a.mime for a in self.entries[entry_id].ref.acquisitions)

    def ids(self, entry_id: UUID) -> set[str]:
        return {str(a.id) for a in self.entries[entry_id].ref.acquisitions}


def metadata() -> ItemMetadata:
    return ItemMetadata("stu-fei-vektory", title="Vektory a matice",
                        creator=["Červeň, Ivan, 1950-", "Novák, Ján"], publisher="Bratislava : STU",
                        date="[2007]", language=["slo"], isbn=["978-80-227-2663-4"], collection=["fei"])


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    item = tmp_path / "artifacts" / "stu" / "stu-fei-vektory"
    item.mkdir(parents=True)
    out = {}
    for fmt, name in {"pdf": "stu-fei-vektory.pdf", "epub": "stu-fei-vektory.epub",
                      "pdfa": "stu-fei-vektory_archival.pdf"}.items():
        (item / name).write_bytes(b"data")
        out[fmt] = item / name
    return out


def publisher(api: FakeCatalog, **kw: Any) -> EvilFlowersPublisher:
    cfg = PublishConfig(target="evilflowers", base_url="https://catalog.test/api/v1",
                        catalog="{collection}", files_base_url=kw.pop("files_base_url", ""),
                        options=kw.pop("options", {}))
    return EvilFlowersPublisher(cfg, client=api, **kw)


# ---------------------------------------------------------------- payload
def test_payload_helpers_translate_ia_conventions():
    assert year_of("[2007]") == "2007" and year_of("1999-05-01") == "1999" and year_of("[n.d.]") is None
    assert publisher_name("Bratislava : STU") == "STU" and publisher_name("") is None
    a = author_of("Červeň, Ivan, 1950-")
    assert (a.surname, a.name) == ("Červeň", "Ivan")


# ---------------------------------------------------------------- create
def test_create_maps_metadata_and_uploads_distribution_files_only(files: dict[str, Path]):
    api = FakeCatalog()
    result = publisher(api).publish("stu-fei-vektory", metadata(), files)

    assert result.created and set(result.acquisitions) == {"pdf", "epub"}
    sent = api.entries[UUID(result.entry_id)].payload
    assert sent.title == "Vektory a matice" and sent.language_code == "sk"
    assert sent.publisher == "STU" and sent.published_at == "2007"
    assert sent.identifiers == {"isbn": ISBN}
    assert [(x.surname, x.name) for x in sent.authors] == [("Červeň", "Ivan"), ("Novák", "Ján")]
    uploads = [args for name, args in api.calls if name == "upload"]
    assert uploads == [("stu-fei-vektory.pdf", "application/pdf", "open-access"),
                       ("stu-fei-vektory.epub", "application/epub+zip", "open-access")]
    assert ("resolve", "fei") in api.calls
    assert result.url is None


def test_republish_replaces_files_upload_first_then_delete(files: dict[str, Path]):
    api = FakeCatalog()
    pub = publisher(api)
    first = pub.publish("stu-fei-vektory", metadata(), files)
    entry = UUID(first.entry_id)
    api.calls.clear()

    meta = metadata()
    meta.title = "Vektory a matice (2. vyd.)"
    again = pub.publish("stu-fei-vektory", meta, files, entry_id=first.entry_id)

    assert not again.created and again.entry_id == first.entry_id
    assert api.ops() == ["update", "upload", "upload", "delete", "delete"]
    deleted = {str(arg) for name, arg in api.calls if name == "delete"}
    assert deleted == set(first.acquisitions.values())
    assert api.ids(entry) == set(again.acquisitions.values())
    assert api.mimes(entry) == ["application/epub+zip", "application/pdf"]
    assert api.entries[entry].payload.title == "Vektory a matice (2. vyd.)"


def test_republish_is_idempotent(files: dict[str, Path]):
    api = FakeCatalog()
    pub = publisher(api)
    first = pub.publish("stu-fei-vektory", metadata(), files)
    for _ in range(3):
        last = pub.publish("stu-fei-vektory", metadata(), files, entry_id=first.entry_id)
    entry = UUID(first.entry_id)
    assert api.mimes(entry) == ["application/epub+zip", "application/pdf"]
    assert api.ids(entry) == set(last.acquisitions.values())


def test_republish_leaves_other_formats_alone(files: dict[str, Path]):
    api = FakeCatalog()
    entry = api.seed("Vektory a matice", ISBN, ("application/pdf", "application/epub+zip",
                                                 "application/x-mobipocket-ebook"))
    old = {a.mime: a.id for a in api.entries[entry].ref.acquisitions}
    only_pdf = {"pdf": files["pdf"]}

    result = publisher(api).publish("stu-fei-vektory", metadata(), only_pdf, entry_id=str(entry))

    assert [arg for name, arg in api.calls if name == "delete"] == [old["application/pdf"]]
    ids = api.ids(entry)
    assert str(old["application/epub+zip"]) in ids and str(old["application/x-mobipocket-ebook"]) in ids
    assert result.acquisitions["pdf"] in ids and str(old["application/pdf"]) not in ids


def test_duplicate_from_a_retried_upload_is_removed(files: dict[str, Path]):
    api = FakeCatalog(retried_uploads=True)
    result = publisher(api).publish("stu-fei-vektory", metadata(), files)
    entry = UUID(result.entry_id)
    assert api.mimes(entry) == ["application/epub+zip", "application/pdf"]
    assert api.ids(entry) == set(result.acquisitions.values())
    assert api.ops().count("delete") == 2


def test_failed_upload_keeps_the_old_file(files: dict[str, Path]):
    api = FakeCatalog()
    pub = publisher(api)
    first = pub.publish("stu-fei-vektory", metadata(), files)

    def broken(*_: Any, **__: Any) -> AcquisitionRef:
        raise CatalogError(500, None, "POST", "entries", "boom")

    api.upload_acquisition = broken  # type: ignore[method-assign]
    with pytest.raises(PublishError):
        pub.publish("stu-fei-vektory", metadata(), files, entry_id=first.entry_id)
    assert api.ids(UUID(first.entry_id)) == set(first.acquisitions.values())
    assert "delete" not in api.ops()


def test_entry_id_from_another_target_means_create(files: dict[str, Path]):
    api = FakeCatalog()
    result = publisher(api).publish("stu-fei-vektory", metadata(), files, entry_id="stu-fei-vektory")
    assert result.created and api.ops()[0] == "create"


def test_republish_of_a_deleted_entry_creates_it_again(files: dict[str, Path]):
    api = FakeCatalog()
    result = publisher(api).publish("stu-fei-vektory", metadata(), files, entry_id=str(uuid4()))
    assert result.created and api.ops()[:2] == ["update", "create"]


# ---------------------------------------------------------------- 409
def test_conflict_with_the_same_isbn_adopts_the_entry(files: dict[str, Path]):
    api = FakeCatalog(conflict_on_create=True)
    existing = api.seed("Vektory a matice", ISBN, ("application/pdf",))
    result = publisher(api).publish("stu-fei-vektory", metadata(), files)

    assert not result.created and result.entry_id == str(existing)
    assert result.acquisitions["pdf"] == str(api.entries[existing].ref.acquisitions[0].id)
    assert [args for name, args in api.calls if name == "upload"] == [
        ("stu-fei-vektory.epub", "application/epub+zip", "open-access")]
    assert "delete" not in api.ops()   # an adopted (librarian's) entry's files are never replaced


def test_conflict_with_another_book_attaches_nothing(files: dict[str, Path]):
    api = FakeCatalog(conflict_on_create=True)
    api.seed("Vektory a matice", "9780306406157")
    with pytest.raises(PublishConflict):
        publisher(api).publish("stu-fei-vektory", metadata(), files)
    assert "upload" not in api.ops()


def test_conflict_without_our_isbn_is_never_adopted(files: dict[str, Path]):
    api = FakeCatalog(conflict_on_create=True)
    api.seed("Vektory a matice", None)
    meta = metadata()
    meta.isbn = []
    with pytest.raises(PublishConflict):
        publisher(api).publish("stu-fei-vektory", meta, files)


def test_entry_url_template(files: dict[str, Path]):
    api = FakeCatalog()
    pub = publisher(api, options={"entry_url": "https://catalog.test/{catalog}/{entry_id}"})
    result = pub.publish("stu-fei-vektory", metadata(), files)
    assert result.url == f"https://catalog.test/fei/{result.entry_id}"


# ---------------------------------------------------------------- external_url (unsupported)
def test_files_base_url_fails_before_any_catalog_call(files: dict[str, Path]):
    api = FakeCatalog()
    pub = publisher(api, files_base_url="https://elvira.stuba.sk/digitized/")
    with pytest.raises(PortError, match="external_url") as info:
        pub.publish("stu-fei-vektory", metadata(), files)
    assert "catalog-protocol-requests.md" in str(info.value)
    assert api.calls == []


def test_external_url_outside_the_root_falls_back_to_item_path(tmp_path: Path):
    pub = publisher(FakeCatalog(), files_base_url="https://x.test/d")
    assert pub.external_url("abc12", tmp_path / "a b.pdf") == "https://x.test/d/abc12/a%20b.pdf"


# ---------------------------------------------------------------- raw DELETE
class FakeTransport:
    def __init__(self, status: int | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.status = status

    def request(self, method: str, path: str, **kw: Any) -> None:
        self.sent.append((method, path))
        if self.status == 404:
            raise NotFound(404, None, method, path, "gone")
        if self.status is not None:
            raise CatalogError(self.status, None, method, path, "boom")


def client_with(transport: FakeTransport) -> CatalogClient:
    client = CatalogClient("https://catalog.test/api/v1", "token")
    client._transport = transport   # `transport` is a read-only property over this
    return client


def test_delete_acquisition_sends_delete_and_treats_404_as_done():
    acq = uuid4()
    ok = FakeTransport()
    TransportDeleter(client_with(ok)).delete_acquisition(acq)
    assert ok.sent == [("DELETE", f"acquisitions/{acq}")]

    gone = FakeTransport(404)
    delete_acquisition(client_with(gone), acq)        # no raise
    assert gone.sent == [("DELETE", f"acquisitions/{acq}")]

    with pytest.raises(CatalogError):
        delete_acquisition(client_with(FakeTransport(500)), acq)


def test_real_client_gets_the_transport_deleter(files: dict[str, Path]):
    transport = FakeTransport()
    pub = publisher(FakeCatalog())
    pub.client = client_with(transport)
    deleter = pub._deleter()
    assert isinstance(deleter, TransportDeleter)
    acq = uuid4()
    deleter.delete_acquisition(acq)
    assert transport.sent == [("DELETE", f"acquisitions/{acq}")]


# ---------------------------------------------------------------- config / factory
def test_missing_collection_or_api_key_is_a_port_error(files: dict[str, Path], monkeypatch):
    meta = metadata()
    meta.collection = []
    with pytest.raises(PortError, match="no target catalog"):
        publisher(FakeCatalog()).publish("stu-fei-vektory", meta, files)

    monkeypatch.delenv("EVILFLOWERS_API_KEY", raising=False)
    pub = EvilFlowersPublisher(PublishConfig(target="evilflowers", base_url="https://x.test/api/v1"))
    with pytest.raises(PortError, match="EVILFLOWERS_API_KEY"):
        pub.publish("stu-fei-vektory", metadata(), files)


def test_build_publisher_and_null_publisher(files: dict[str, Path]):
    null = build_publisher(PublishConfig())
    assert isinstance(null, NullPublisher) and isinstance(null, CatalogPublisher)
    first = null.publish("stu-fei-vektory", metadata(), files)
    assert first.entry_id == "stu-fei-vektory" and first.created and first.acquisitions == {}
    assert not null.publish("stu-fei-vektory", metadata(), files, entry_id=first.entry_id).created

    ef = build_publisher(PublishConfig(target="evilflowers"), client=FakeCatalog())
    assert isinstance(ef, EvilFlowersPublisher) and isinstance(ef, CatalogPublisher)
    with pytest.raises(PortError):
        build_publisher(PublishConfig(target="dspace"))
