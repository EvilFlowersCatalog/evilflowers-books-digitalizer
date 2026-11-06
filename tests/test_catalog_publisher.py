"""Publisher: discovery in both layouts, the create/upload flow, resume, 409 policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evilflowers_books_digitalizer.catalog.manifest import (
    manifest_path,
    read_manifest,
    write_manifest,
)
from evilflowers_books_digitalizer.catalog.publisher import (
    Publisher,
    discover_books,
    export_manifests,
    load_report,
    publish_source,
)
from evilflowers_books_digitalizer.pipeline.layout import book_dir, distribution_pdf
from evilflowers_books_digitalizer.runtime import RuntimeConfig

ISBN = "9788022750462"


def _runtime(tmp_path: Path, layout: str = "split", **catalog: object) -> RuntimeConfig:
    config = {
        "metadata": {"enabled": False, "faculty_names": {"svf": "Stavebná fakulta STU"}},
        "orchestration": {"sources": ["svf"]},
        "render": {"layout": layout},
        "catalog": {
            "enabled": True,
            "base_url": "https://x/api/v1",
            "catalog": "",
            "by_source": True,
            "default_language": "sk",
            "relation": "open-access",
            "entry_config": {"evilflowers_ocr_enabled": False},
            **catalog,
        },
    }
    return RuntimeConfig(config=config, config_path=tmp_path / "pipeline.toml",
                         cache_dir=tmp_path / "cache", output_dir=tmp_path / "output")


def _produce(rt: RuntimeConfig, source: str, book_id: str, *, title: str | None = None,
             isbn: str | None = None) -> Path:
    """Fake a finished book exactly where the renderer would put it."""
    slug = f"{source}_{book_id}"
    pdf = distribution_pdf(rt.output_dir, source, slug, rt.layout)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.7 distribution")
    directory = book_dir(rt.output_dir, source, rt.layout)
    (directory / f"{slug}.cover.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (directory / f"{slug}.txt").write_text("text")
    if rt.layout == "split":
        archival = rt.output_dir / "archival" / source / f"{slug}.pdf"
    else:
        archival = directory / f"{slug}.archival.pdf"
    archival.parent.mkdir(parents=True, exist_ok=True)
    archival.write_bytes(b"%PDF-1.7 archival")
    if title or isbn:
        manifest = Publisher(rt, dry_run=True).build_manifest(source, book_id, pdf)
        manifest = manifest.model_copy(update={
            "title": title or manifest.title,
            "identifiers": {"isbn": isbn} if isbn else {},
            "provenance": {"isbn": "ocr"},
        })
        write_manifest(manifest, manifest_path(rt.output_dir, source, book_id, rt.layout))
    return pdf


def _report(rt: RuntimeConfig, source: str = "svf") -> list[dict]:
    path = rt.output_dir / f"publish_report_{source}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


# -- A: discovery in both layouts ------------------------------------------------


@pytest.mark.parametrize("layout", ["split", "flat"])
def test_discover_books_finds_distribution_copies_only(tmp_path, layout):
    rt = _runtime(tmp_path, layout)
    _produce(rt, "svf", "b1")
    _produce(rt, "svf", "CVI_OPACID_SVF_9788022750462")
    found = discover_books(rt.output_dir, "svf", rt.layout)
    assert sorted(b for b, _ in found) == ["CVI_OPACID_SVF_9788022750462", "b1"]
    for _, pdf in found:
        assert pdf.read_bytes() == b"%PDF-1.7 distribution"  # never the archival master


def test_discover_books_layout_paths(tmp_path):
    split = _runtime(tmp_path / "s", "split")
    _produce(split, "svf", "b1")
    assert discover_books(split.output_dir, "svf", "split")[0][1] == (
        split.output_dir / "distribution" / "svf" / "svf_b1.pdf")
    flat = _runtime(tmp_path / "f", "flat")
    _produce(flat, "svf", "b1")
    assert discover_books(flat.output_dir, "svf", "flat")[0][1] == (
        flat.output_dir / "svf" / "svf_b1.distribution.pdf")


@pytest.mark.parametrize("layout", ["split", "flat"])
def test_export_manifests_writes_sidecars_next_to_distribution(tmp_path, layout):
    rt = _runtime(tmp_path, layout)
    pdf = _produce(rt, "svf", "b1")
    paths = export_manifests(rt)
    assert paths == [pdf.parent / "svf_b1.entry.json"]
    manifest = read_manifest(paths[0])
    assert manifest.catalog == "svf"  # by_source
    assert manifest.cover == "svf_b1.cover.jpg"


@pytest.mark.parametrize("layout", ["split", "flat"])
def test_publish_uploads_the_distribution_pdf(tmp_path, layout, fake_catalog):
    rt = _runtime(tmp_path, layout)
    pdf = _produce(rt, "svf", "b1")
    res = publish_source(rt, "svf", client=fake_catalog)

    assert res["counts"] == {"published": 1}
    assert fake_catalog.uploads == [pdf]
    entry = fake_catalog.in_catalog("svf")[0]
    row = res["rows"][0]
    assert row["entry_id"] == str(entry.id)
    assert row["bytes"] == pdf.stat().st_size and "duration_ms" in row
    assert [r["status"] for r in _report(rt)] == ["entry_created", "published"]
    created = next(arg for name, arg in fake_catalog.calls if name == "create_entry")
    assert created.image.startswith("data:image/jpeg;base64,")
    assert created.language_code == "sk"


def test_publish_is_resumable_and_skips_done_books(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1")
    _produce(rt, "svf", "b2")
    assert publish_source(rt, "svf", client=fake_catalog)["counts"] == {"published": 2}

    calls_before = len(fake_catalog.calls)
    res = publish_source(rt, "svf", client=fake_catalog)
    assert res["counts"] == {"skipped": 2}
    assert len(fake_catalog.calls) == calls_before  # no HTTP for done books
    assert len(_report(rt)) == 4  # nothing appended


# -- D: resume and 409 handling --------------------------------------------------


def test_resume_after_upload_failure_reuses_the_entry(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1", title="Betónové konštrukcie", isbn=ISBN)
    fake_catalog.fail_uploads = 1

    first = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert first["status"] == "error" and first["entry_id"]
    assert [r["status"] for r in _report(rt)] == ["entry_created", "error"]

    second = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert second["status"] == "published"
    assert second["entry_id"] == first["entry_id"]
    assert fake_catalog.count("create_entry") == 1  # never created twice
    assert len(fake_catalog.in_catalog("svf")) == 1


def test_resume_after_interruption_between_create_and_upload(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1")
    entry_id = fake_catalog.seed("svf", "Some Title")
    catalog_id = fake_catalog.catalogs["svf"].id
    report = rt.output_dir / "publish_report_svf.jsonl"
    report.write_text(json.dumps({"source": "svf", "book_id": "b1", "status": "entry_created",
                                  "entry_id": str(entry_id), "catalog_id": str(catalog_id)}) + "\n")

    row = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert row["status"] == "published" and row["entry_id"] == str(entry_id)
    assert fake_catalog.count("create_entry") == 0


def test_resume_when_the_upload_had_landed(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1")
    entry_id = fake_catalog.seed("svf", "T", with_pdf=True)  # upload went through, then crash
    report = rt.output_dir / "publish_report_svf.jsonl"
    report.write_text(json.dumps({"source": "svf", "book_id": "b1", "status": "entry_created",
                                  "entry_id": str(entry_id)}) + "\n")

    row = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert row["status"] == "published"
    assert fake_catalog.count("upload_acquisition") == 0  # no second copy attached


def test_conflict_with_same_isbn_and_no_acquisition_is_adopted(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1", title="Betónové konštrukcie", isbn=ISBN)
    # an earlier run created the entry but lost its report (e.g. a fresh output volume)
    existing = fake_catalog.seed("svf", "Betónové konštrukcie", "978-80-227-5046-2")

    row = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert row["status"] == "published"
    assert row["entry_id"] == str(existing)
    assert len(fake_catalog.in_catalog("svf")) == 1
    assert [r["status"] for r in _report(rt)] == ["entry_created", "published"]


def test_conflict_with_same_isbn_and_acquisition_is_skipped(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1", title="Betónové konštrukcie", isbn=ISBN)
    existing = fake_catalog.seed("svf", "Betónové konštrukcie", ISBN, with_pdf=True)

    row = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert row["status"] == "skipped" and row["entry_id"] == str(existing)
    assert fake_catalog.count("upload_acquisition") == 0


def test_conflict_with_a_different_book_is_never_attached(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1", title="Matematika I", isbn=ISBN)
    other = fake_catalog.seed("svf", "Matematika I", "8070950200")  # same title, other ISBN

    row = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert row["status"] == "conflict"
    assert row["candidates"] == [str(other)]
    assert fake_catalog.count("upload_acquisition") == 0
    assert fake_catalog.entries[other].acquisitions == []


def test_conflict_without_isbns_needs_the_exact_title(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1", title="Statika")
    fake_catalog.seed("svf", "STATIKA")  # same title ignoring case only
    row = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert row["status"] == "conflict"

    rt2 = _runtime(tmp_path / "2")
    _produce(rt2, "svf", "b1", title="Statika")
    exact = fake_catalog.seed("svf", "Statika")
    fake_catalog.entries.pop(next(e.id for e in fake_catalog.in_catalog("svf")
                                  if e.title == "STATIKA"))
    row = Publisher(rt2, client=fake_catalog).publish_book("svf", "b1")
    assert row["status"] == "published" and row["entry_id"] == str(exact)


def test_force_republish_never_duplicates(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1", title="Betónové konštrukcie", isbn=ISBN)
    assert Publisher(rt, client=fake_catalog).publish_book("svf", "b1")["status"] == "published"

    row = Publisher(rt, client=fake_catalog, force=True).publish_book("svf", "b1")
    assert row["status"] == "skipped"  # 409 -> our entry, already has the PDF
    assert fake_catalog.count("create_entry") == 2
    assert len(fake_catalog.in_catalog("svf")) == 1
    assert fake_catalog.count("upload_acquisition") == 1


# -- targets, dry-run, errors ----------------------------------------------------


def test_target_catalog_precedence_excel_then_source_then_default(tmp_path, fake_catalog):
    rt = _runtime(tmp_path, by_source=False, catalog="mtf")
    pdf = _produce(rt, "svf", "b1")
    assert Publisher(rt, client=fake_catalog).publish_book("svf", "b1")["catalog"] == "mtf"

    rt = _runtime(tmp_path / "2", by_source=True, catalog="mtf")
    pdf = _produce(rt, "svf", "b1")
    publisher = Publisher(rt, client=fake_catalog)
    manifest = publisher.build_manifest("svf", "b1", pdf).model_copy(
        update={"catalog_override": "fei"})
    write_manifest(manifest, manifest_path(rt.output_dir, "svf", "b1", rt.layout))
    row = publisher.publish_book("svf", "b1")
    assert row["catalog"] == "fei" and row["status"] == "published"
    assert len(fake_catalog.in_catalog("fei")) == 1


def test_old_manifest_target_is_re_resolved_from_config(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)  # by_source = true
    pdf = _produce(rt, "svf", "b1")
    stale = Publisher(rt, dry_run=True).build_manifest("svf", "b1", pdf).model_copy(
        update={"catalog": "stu-books", "language_code": "slk"})  # pre-change manifest
    write_manifest(stale, manifest_path(rt.output_dir, "svf", "b1", rt.layout))
    row = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert row["catalog"] == "svf" and row["status"] == "published"


def test_pipeline_manifest_is_used_not_rebuilt(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1", title="Title from OPAC", isbn=ISBN)
    Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    created = next(arg for name, arg in fake_catalog.calls if name == "create_entry")
    assert created.title == "Title from OPAC"
    assert created.identifiers == {"isbn": ISBN}
    mpath = manifest_path(rt.output_dir, "svf", "b1", rt.layout)
    assert read_manifest(mpath).provenance == {"isbn": "ocr"}


class _NoHttp:
    def __getattr__(self, name: str):
        raise AssertionError(f"dry-run made an HTTP call: {name}")


@pytest.mark.parametrize("layout", ["split", "flat"])
def test_dry_run_validates_and_writes_manifests_without_http(tmp_path, layout):
    rt = _runtime(tmp_path, layout)
    _produce(rt, "svf", "b1")
    res = publish_source(rt, "svf", dry_run=True, client=_NoHttp())  # type: ignore[arg-type]
    assert res["counts"] == {"dry-run": 1}
    row = res["rows"][0]
    assert row["catalog"] == "svf" and row["language_code"] == "sk" and row["payload_bytes"] > 0
    assert manifest_path(rt.output_dir, "svf", "b1", rt.layout).exists()
    assert not (rt.output_dir / "publish_report_svf.jsonl").exists()


def test_rebuilt_manifest_takes_the_isbn_from_the_directory_name(tmp_path):
    rt = _runtime(tmp_path)
    pdf = _produce(rt, "svf", "CVI_OPACID_SVF_9788022750462")
    manifest = Publisher(rt, dry_run=True).build_manifest(
        "svf", "CVI_OPACID_SVF_9788022750462", pdf)
    assert manifest.identifiers == {"isbn": ISBN}


def test_missing_pdf_is_an_error(tmp_path, fake_catalog):
    rt = _runtime(tmp_path)
    row = Publisher(rt, client=fake_catalog).publish_book("svf", "nope")
    assert row["status"] == "error" and "distribution PDF not found" in row["error"]


def test_unknown_catalog_is_an_error(tmp_path, fake_catalog):
    rt = _runtime(tmp_path, by_source=False, catalog="stu-books")
    _produce(rt, "svf", "b1")
    row = Publisher(rt, client=fake_catalog).publish_book("svf", "b1")
    assert row["status"] == "error" and "stu-books" in row["error"]


def test_missing_api_key_is_an_error_row(tmp_path, monkeypatch):
    monkeypatch.delenv("EVILFLOWERS_API_KEY", raising=False)
    rt = _runtime(tmp_path)
    _produce(rt, "svf", "b1")
    row = Publisher(rt).publish_book("svf", "b1")
    assert row["status"] == "error" and "EVILFLOWERS_API_KEY" in row["error"]


def test_load_report_keeps_the_last_row_per_book(tmp_path):
    report = tmp_path / "r.jsonl"
    report.write_text(
        '{"book_id": "a", "status": "entry_created", "entry_id": "x"}\n'
        "not json\n"
        '{"book_id": "a", "status": "published", "entry_id": "x"}\n'
        '{"book_id": "b", "status": "error"}\n'
    )
    state = load_report(report)
    assert state["a"]["status"] == "published" and state["b"]["status"] == "error"
