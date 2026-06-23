"""Factory wiring: correct step order per config."""

from __future__ import annotations

from evilflowers_books_digitalizer.cache import LocalCache
from evilflowers_books_digitalizer.metadata import MetadataCatalog
from evilflowers_books_digitalizer.pipeline.factory import build_pipeline
from evilflowers_books_digitalizer.sources import FilesystemBookSource

BASE_CONFIG = {
    "pipeline": {"engine": "scantailor_mrc"},
    "scantailor": {},
    "mrc": {},
    "ocr": {"language": "auto"},  # production default -> includes DetectLanguage
    "finalize": {"enabled": True},
}


def _source(tmp_path):
    return FilesystemBookSource("svf", tmp_path)


def _names(pipeline):
    return [s.name for s in pipeline.steps]


def test_scantailor_mrc_without_metadata_or_cover(tmp_path):
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=BASE_CONFIG)
    assert _names(pipe) == ["download", "scantailor", "language", "mrc", "enrich", "finalize"]


def test_metadata_and_cover_inserted_in_order(tmp_path):
    config = {
        **BASE_CONFIG,
        "metadata": {"enabled": True},
        "cover": {"enabled": True},
    }
    catalog = MetadataCatalog([])
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config, catalog=catalog)
    # metadata before enrich; cover right after enrich
    assert _names(pipe) == [
        "download",
        "scantailor",
        "language",
        "mrc",
        "metadata",
        "enrich",
        "cover",
        "finalize",
    ]


def test_cover_without_catalog_still_runs(tmp_path):
    config = {**BASE_CONFIG, "cover": {"enabled": True}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config, catalog=None)
    names = _names(pipe)
    assert "cover" in names and "metadata" not in names
    assert names.index("cover") == names.index("enrich") + 1


def test_legacy_engine_builds(tmp_path):
    config = {
        "pipeline": {"engine": "legacy"},
        "preprocess": {},
        "ocr": {"language": "auto"},
        "legacy_ocr": {},
    }
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    assert _names(pipe) == ["download", "preprocess", "language", "assemble", "ocr", "enrich"]
