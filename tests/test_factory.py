"""Factory wiring: correct step order for the single MRC engine."""

from __future__ import annotations

from evilflowers_books_digitalizer.cache import LocalCache
from evilflowers_books_digitalizer.metadata import MetadataCatalog
from evilflowers_books_digitalizer.pipeline.factory import build_pipeline
from evilflowers_books_digitalizer.sources import FilesystemBookSource

BASE_CONFIG = {
    "scantailor": {},
    "render": {"outputs": ["distribution", "archival"]},
    "ocr": {"language": "auto"},  # production default -> includes DetectLanguage
    "finalize": {"enabled": True},
}


def _source(tmp_path):
    return FilesystemBookSource("svf", tmp_path)


def _names(pipeline):
    return [s.name for s in pipeline.steps]


def test_pipeline_without_metadata_or_cover(tmp_path):
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=BASE_CONFIG)
    assert _names(pipe) == [
        "download", "scantailor", "language", "ocr", "render", "enrich", "finalize", "pdfa",
    ]


def test_metadata_and_cover_inserted_in_order(tmp_path):
    config = {**BASE_CONFIG, "metadata": {"enabled": True}, "cover": {"enabled": True}}
    catalog = MetadataCatalog([])
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config, catalog=catalog)
    # metadata is attached early (before render bakes it into XMP); cover after enrich
    assert _names(pipe) == [
        "download", "metadata", "scantailor", "language", "ocr", "render",
        "enrich", "cover", "finalize", "pdfa",
    ]


def test_cover_without_catalog_still_runs(tmp_path):
    config = {**BASE_CONFIG, "cover": {"enabled": True}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config, catalog=None)
    names = _names(pipe)
    assert "cover" in names and "metadata" not in names
    assert names.index("cover") == names.index("enrich") + 1


def test_explicit_language_skips_detection(tmp_path):
    config = {**BASE_CONFIG, "ocr": {"language": "slk"}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    assert "language" not in _names(pipe)


def test_single_output_profile(tmp_path):
    config = {**BASE_CONFIG, "render": {"outputs": ["distribution"]}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    render = next(s for s in pipe.steps if s.name == "render")
    assert [p.name for p in render.profiles] == ["distribution"]


def test_layout_propagates_to_render(tmp_path):
    config = {**BASE_CONFIG, "render": {"outputs": ["distribution"], "layout": "split"}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    render = next(s for s in pipe.steps if s.name == "render")
    assert render.layout == "split"
    # default is flat when unspecified
    flat = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=BASE_CONFIG)
    assert next(s for s in flat.steps if s.name == "render").layout == "flat"


def test_catalog_manifest_step_appended_last(tmp_path):
    config = {
        **BASE_CONFIG,
        "metadata": {"enabled": True},
        "cover": {"enabled": True},
        "catalog": {"enabled": True, "catalog": "stu-books"},
    }
    catalog = MetadataCatalog([])
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config, catalog=catalog)
    assert _names(pipe)[-1] == "manifest"  # needs the final PDF + cover, so runs last


def test_catalog_manifest_skipped_without_target(tmp_path):
    config = {**BASE_CONFIG, "catalog": {"enabled": True}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    assert "manifest" not in _names(pipe)
