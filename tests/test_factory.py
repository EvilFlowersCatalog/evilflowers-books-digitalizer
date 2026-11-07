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
        "download", "scantailor", "language", "ocr", "previews", "render", "enrich", "finalize",
        "pdfa",
    ]


def test_previews_can_be_disabled(tmp_path):
    config = {**BASE_CONFIG, "viewer": {"previews": False}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    assert "previews" not in _names(pipe)


def test_reflow_is_opt_in_and_runs_after_pdfa(tmp_path):
    assert "reflow" not in _names(build_pipeline(_source(tmp_path), LocalCache(tmp_path),
                                                  config=BASE_CONFIG))
    config = {**BASE_CONFIG, "reflow": {"enabled": True},
              "catalog": {"enabled": True, "catalog": "stu-books"}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    names = _names(pipe)
    assert names.index("reflow") == names.index("pdfa") + 1
    assert names[-1] == "manifest"
    assert pipe.steps[names.index("reflow")].settings.vlm is None   # vlm defaults to "none"


def test_reflow_vlm_settings(tmp_path):
    config = {**BASE_CONFIG, "reflow": {"enabled": True, "vlm": "paddleocr-vl",
                                        "vlm_python": "/opt/vlm/bin/python",
                                        "vlm_model": "/opt/models/pocr"}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    vlm = next(s for s in pipe.steps if s.name == "reflow").settings.vlm
    assert str(vlm.python) == "/opt/vlm/bin/python" and str(vlm.model) == "/opt/models/pocr"


def test_metadata_and_cover_inserted_in_order(tmp_path):
    config = {**BASE_CONFIG, "metadata": {"enabled": True}, "cover": {"enabled": True}}
    catalog = MetadataCatalog([])
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config, catalog=catalog)
    # metadata is attached early (before render bakes it into XMP); cover after enrich
    assert _names(pipe) == [
        "download", "metadata", "scantailor", "language", "ocr", "previews", "render",
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


def test_catalog_manifest_with_by_source_and_no_default(tmp_path):
    config = {**BASE_CONFIG, "catalog": {"enabled": True, "catalog": "", "by_source": True}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    manifest = pipe.steps[-1]
    assert manifest.name == "manifest" and manifest.by_source is True


def test_extract_metadata_sits_between_ocr_and_render(tmp_path):
    config = {**BASE_CONFIG, "enrich": {"enabled": True, "head_pages": 5, "tail_pages": 2,
                                        "lookup": False, "override": True}}
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config)
    names = _names(pipe)
    assert names.index("extract_metadata") == names.index("ocr") + 1
    assert names.index("render") == names.index("extract_metadata") + 2  # previews between
    step = pipe.steps[names.index("extract_metadata")]
    assert (step.head_pages, step.tail_pages, step.lookup, step.override) == (5, 2, False, True)
    assert step.cache_dir == tmp_path / "isbn_lookup"


def test_extract_metadata_off_by_default(tmp_path):
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=BASE_CONFIG)
    assert "extract_metadata" not in _names(pipe)


def test_k8s_config_builds_the_expected_pipeline(tmp_path):
    import tomllib

    from test_runtime import K8S_CONFIG

    config = tomllib.loads(K8S_CONFIG)
    pipe = build_pipeline(_source(tmp_path), LocalCache(tmp_path), config=config, catalog=None)
    assert _names(pipe) == [
        "download", "scantailor", "language", "ocr", "extract_metadata", "previews", "render",
        "enrich", "cover", "finalize", "pdfa", "manifest",
    ]
    assert next(s for s in pipe.steps if s.name == "render").layout == "split"
