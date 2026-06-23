"""Manifest sidecar round-trip + discovery."""

from __future__ import annotations


from evilflowers_books_digitalizer.catalog.manifest import (
    iter_manifests,
    manifest_path,
    read_manifest,
    write_manifest,
)
from evilflowers_books_digitalizer.catalog.mapping import EntryManifest


def _manifest(book_id: str = "b1") -> EntryManifest:
    return EntryManifest(
        source="svf",
        book_id=book_id,
        slug=f"svf_{book_id}",
        catalog="stu-books",
        title="Title",
        language_code="slk",
        pdf=f"svf_{book_id}.pdf",
    )


def test_manifest_path_layout(tmp_path):
    path = manifest_path(tmp_path, "svf", "b1")
    assert path == tmp_path / "svf" / "svf_b1.entry.json"


def test_write_read_roundtrip(tmp_path):
    path = manifest_path(tmp_path, "svf", "b1")
    write_manifest(_manifest(), path)
    assert path.exists()
    loaded = read_manifest(path)
    assert loaded.slug == "svf_b1"
    assert loaded.catalog == "stu-books"
    assert loaded.pdf == "svf_b1.pdf"


def test_iter_manifests_skips_malformed(tmp_path):
    write_manifest(_manifest("b1"), manifest_path(tmp_path, "svf", "b1"))
    write_manifest(_manifest("b2"), manifest_path(tmp_path, "svf", "b2"))
    # a malformed sidecar must not break iteration
    bad = tmp_path / "svf" / "svf_b3.entry.json"
    bad.write_text("{not json", encoding="utf-8")

    book_ids = sorted(m.book_id for m in iter_manifests(tmp_path))
    assert book_ids == ["b1", "b2"]

    assert sorted(m.book_id for m in iter_manifests(tmp_path, source="svf")) == ["b1", "b2"]
    assert list(iter_manifests(tmp_path, source="missing")) == []
