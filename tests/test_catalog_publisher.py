"""Publisher: discovery, manifest export, dry-run, resumable publishing."""

from __future__ import annotations

from pathlib import Path

from evilflowers_books_digitalizer.catalog.publisher import (
    discover_books,
    export_manifests,
    publish_source,
)
from evilflowers_books_digitalizer.catalog.manifest import manifest_path
from evilflowers_books_digitalizer.runtime import RuntimeConfig


def _runtime(tmp_path) -> RuntimeConfig:
    output_dir = tmp_path / "output"
    config = {
        "metadata": {"enabled": False, "faculty_names": {"svf": "Stavebná fakulta STU"}},
        "orchestration": {"sources": ["svf"]},
        "catalog": {
            "enabled": True,
            "base_url": "https://x/api/v1",
            "catalog": "stu-books",
            "default_language": "slk",
            "relation": "open-access",
            "entry_config": {"evilflowers_ocr_enabled": False},
        },
    }
    return RuntimeConfig(
        config=config,
        config_path=tmp_path / "pipeline.toml",
        cache_dir=tmp_path / "cache",
        output_dir=output_dir,
    )


def _produce(output_dir: Path, source: str, book_id: str) -> None:
    book_dir = output_dir / source
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / f"{source}_{book_id}.pdf").write_bytes(b"%PDF-1.7 fake")
    (book_dir / f"{source}_{book_id}.cover.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")


def test_discover_books_strips_source_prefix(tmp_path):
    rt = _runtime(tmp_path)
    _produce(rt.output_dir, "svf", "b1")
    _produce(rt.output_dir, "svf", "CVI_OPACID_SVF_9788022750462")
    found = discover_books(rt.output_dir, "svf")
    assert sorted(b for b, _ in found) == ["CVI_OPACID_SVF_9788022750462", "b1"]


def test_export_manifests_writes_sidecars(tmp_path):
    rt = _runtime(tmp_path)
    _produce(rt.output_dir, "svf", "b1")
    paths = export_manifests(rt)
    assert len(paths) == 1
    mpath = manifest_path(rt.output_dir, "svf", "b1")
    assert mpath.exists()


def test_publish_source_dry_run_writes_manifest_only(tmp_path):
    rt = _runtime(tmp_path)
    _produce(rt.output_dir, "svf", "b1")
    res = publish_source(rt, "svf", dry_run=True)
    assert res["counts"] == {"dry-run": 1}
    assert manifest_path(rt.output_dir, "svf", "b1").exists()
    # dry-run does not write a publish report
    assert not (rt.output_dir / "publish_report_svf.jsonl").exists()


class _FakeClient:
    def __init__(self):
        self.published: list[str] = []

    def publish_manifest(self, manifest, *, output_dir):
        self.published.append(manifest.book_id)
        return {"source": manifest.source, "book_id": manifest.book_id,
                "status": "published", "entry_id": "e1"}


def test_publish_source_is_resumable(tmp_path):
    rt = _runtime(tmp_path)
    _produce(rt.output_dir, "svf", "b1")
    _produce(rt.output_dir, "svf", "b2")

    client = _FakeClient()
    res = publish_source(rt, "svf", client=client)
    assert res["counts"] == {"published": 2}
    assert sorted(client.published) == ["b1", "b2"]
    assert (rt.output_dir / "publish_report_svf.jsonl").exists()

    # a second run skips everything already recorded
    client2 = _FakeClient()
    res2 = publish_source(rt, "svf", client=client2)
    assert res2["rows"] == []
    assert client2.published == []

    # ...unless forced
    client3 = _FakeClient()
    res3 = publish_source(rt, "svf", client=client3, force=True)
    assert res3["counts"] == {"published": 2}
