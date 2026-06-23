"""EvilFlowers REST client: encoding, catalog resolution, publish flow, 409."""

from __future__ import annotations


import pytest

from evilflowers_books_digitalizer.catalog.client import (
    CatalogError,
    EvilFlowersClient,
    _encode_multipart,
    _is_uuid,
)
from evilflowers_books_digitalizer.catalog.mapping import EntryManifest

UUID = "123e4567-e89b-12d3-a456-426614174000"


def test_is_uuid():
    assert _is_uuid(UUID)
    assert not _is_uuid("stu-books")


def test_encode_multipart_has_boundary_fields_and_file():
    body, content_type = _encode_multipart(
        fields={"metadata": '{"relation": "open-access"}'},
        file_field="content",
        filename="book.pdf",
        data=b"%PDF-1.7 ...",
        mime="application/pdf",
    )
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="metadata"' in body
    assert b'name="content"; filename="book.pdf"' in body
    assert b"Content-Type: application/pdf" in body
    assert b"%PDF-1.7 ..." in body


def _client(monkeypatch, responses):
    """A client whose `_request` pops canned (or callable) responses by call order."""
    client = EvilFlowersClient("https://x/api/v1", "key")
    calls: list[tuple] = []

    def fake_request(method, path, *, json_body=None, body=None, content_type=None, query=None):
        calls.append((method, path, json_body, query))
        result = responses.pop(0)
        if callable(result):
            return result()
        return result

    monkeypatch.setattr(client, "_request", fake_request)
    return client, calls


def test_resolve_catalog_uuid_passthrough(monkeypatch):
    client, calls = _client(monkeypatch, [])
    assert client.resolve_catalog(UUID) == UUID
    assert calls == []  # no network for a UUID


def test_resolve_catalog_by_url_name(monkeypatch):
    page = {"items": [{"id": UUID, "url_name": "stu-books", "title": "STU"}],
            "metadata": {"pages": 1}}
    client, calls = _client(monkeypatch, [page])
    assert client.resolve_catalog("stu-books") == UUID
    assert calls[0][0] == "GET"


def test_resolve_catalog_not_found(monkeypatch):
    page = {"items": [], "metadata": {"pages": 1}}
    client, _ = _client(monkeypatch, [page])
    with pytest.raises(CatalogError):
        client.resolve_catalog("nope")


def _manifest(tmp_path) -> EntryManifest:
    book_dir = tmp_path / "svf"
    book_dir.mkdir()
    (book_dir / "svf_b1.pdf").write_bytes(b"%PDF-1.7 fake")
    (book_dir / "svf_b1.cover.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    return EntryManifest(
        source="svf", book_id="b1", slug="svf_b1", catalog=UUID,
        title="Title", language_code="slk", pdf="svf_b1.pdf", cover="svf_b1.cover.jpg",
    )


def test_publish_manifest_success(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)
    entry_resp = {"response": {"id": "entry-1"}}
    acq_resp = {"response": {"id": "acq-1"}}
    client, calls = _client(monkeypatch, [entry_resp, acq_resp])

    row = client.publish_manifest(manifest, output_dir=tmp_path)
    assert row["status"] == "published"
    assert row["entry_id"] == "entry-1"
    assert row["acquisition_id"] == "acq-1"
    # create entry then attach pdf
    assert calls[0][1] == f"catalogs/{UUID}/entries"
    assert calls[1][1] == f"catalogs/{UUID}/entries/entry-1"
    # cover was inlined as a data URI on the create payload
    assert calls[0][2]["image"].startswith("data:image/jpeg;base64,")


def test_publish_manifest_duplicate_is_skipped(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)

    def conflict():
        raise CatalogError("exists", status=409)

    client, _ = _client(monkeypatch, [conflict])
    row = client.publish_manifest(manifest, output_dir=tmp_path)
    assert row["status"] == "skipped"


def test_publish_manifest_propagates_non_409(monkeypatch, tmp_path):
    manifest = _manifest(tmp_path)

    def server_error():
        raise CatalogError("boom", status=500)

    client, _ = _client(monkeypatch, [server_error])
    with pytest.raises(CatalogError):
        client.publish_manifest(manifest, output_dir=tmp_path)


def test_missing_api_key_raises():
    with pytest.raises(CatalogError):
        EvilFlowersClient("https://x/api/v1", "")
