"""Config: the shared storage root, the service routes, [decider], and the retired keys (#6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evilflowers_books_digitalizer.studio.config import (
    DEFAULT_ROUTES,
    STORAGE_ROOT_ENV,
    load_config,
)

BASE = """
[studio]
database_url = "sqlite://"
{studio_extra}
artifacts_root = "{artifacts}"
cache_dir = "/scratch/cache"

[projects.stu]
scans_root = "{scans}"
"""


def _write(tmp_path: Path, *, studio_extra: str = 'storage_root = "/data"',
           artifacts: str = "{storage_root}/digitized/studio",
           scans: str = "{storage_root}/raw-scans", extra: str = "", project: str = "") -> Path:
    path = tmp_path / "studio.toml"
    path.write_text(BASE.replace("{studio_extra}", studio_extra)
                    .replace("{artifacts}", artifacts).replace("{scans}", scans)
                    + project + extra)
    return path


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for name in (STORAGE_ROOT_ENV, "DECIDER_URL", "DECIDER_VISION_URL", "DECIDER_TIMEOUT",
                 "DECIDER_ENABLED", "DECIDER_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_placeholder_expands_under_the_configured_root(tmp_path):
    cfg = load_config(_write(tmp_path))
    assert cfg.storage_root == Path("/data")
    assert cfg.artifacts_root == Path("/data/digitized/studio")
    assert cfg.project("stu").scans_root == Path("/data/raw-scans")


def test_env_overrides_the_root_and_every_derived_path(tmp_path, monkeypatch):
    monkeypatch.setenv(STORAGE_ROOT_ENV, "/mnt/shared/library")
    cfg = load_config(_write(tmp_path))
    assert cfg.storage_root == Path("/mnt/shared/library")
    assert cfg.artifacts_root == Path("/mnt/shared/library/digitized/studio")
    assert cfg.project("stu").scans_root == Path("/mnt/shared/library/raw-scans")


def test_placeholder_without_a_root_is_a_config_error(tmp_path):
    with pytest.raises(ValueError, match="no storage root"):
        load_config(_write(tmp_path, studio_extra=""))


def test_the_services_always_need_a_root(tmp_path):
    with pytest.raises(ValueError, match="need the shared storage root"):
        load_config(_write(tmp_path, studio_extra="", artifacts="/out", scans="/scans"))


def test_the_services_need_paths_under_the_root(tmp_path):
    with pytest.raises(ValueError, match="outside it: /scans"):
        load_config(_write(tmp_path, scans="/scans"))


# ---------------------------------------------------------------- routes
def test_every_digitalization_stage_defaults_to_its_service(tmp_path):
    routes = load_config(_write(tmp_path)).orchestration.routes
    assert routes == DEFAULT_ROUTES
    assert {s: r.backend for s, r in routes.items()} == {
        "clean": "service", "analyze": "service", "formulas": "service", "render": "service",
        "gate": "local", "publish": "local"}
    assert routes["clean"].task == "evilflowers_scan.clean"
    assert routes["analyze"].part("hocr").queue == "evilflowers_ocr_worker.standalone"
    assert routes["analyze"].part("layout").interactive_queue == \
        "evilflowers_analyzer.standalone.interactive"
    assert routes["render"].part("book").task == "evilflowers_render.book"
    assert (routes["clean"].part("marks").backend, routes["analyze"].part("title_page").backend
            ) == ("local", "local")


def test_a_stage_table_overrides_single_fields(tmp_path):
    routes = load_config(_write(tmp_path, extra="""
[orchestration.stages.clean]
batch = 8
queue = "scan.other"
[orchestration.stages.analyze.hocr]
queue = "ocr.other"
[orchestration.stages.render]
task = "render.other"
""")).orchestration.routes
    assert (routes["clean"].task, routes["clean"].queue, routes["clean"].interactive_queue,
            routes["clean"].batch) == ("evilflowers_scan.clean", "scan.other", "scan.other", 8)
    assert routes["analyze"].part("hocr").queue == "ocr.other"
    assert routes["analyze"].part("layout").queue == "evilflowers_analyzer.standalone"
    assert routes["render"].part("book").task == "render.other"
    assert routes["render"].part("prepare").backend == "local"


@pytest.mark.parametrize("extra, message", [
    ('[orchestration.stages.clean]\nbackend = "local"\n', "in-process stages are gone"),
    ('[orchestration.stages.analyze]\ntask = "x"\n', "configure them as subtables"),
    ('[orchestration.stages.analyze.ocr]\ntask = "x"\n', "unknown part"),
    ('[orchestration.stages.paint]\ntask = "x"\n', "unknown stage"),
])
def test_bad_routes_are_refused(tmp_path, extra, message):
    with pytest.raises(ValueError, match=message):
        load_config(_write(tmp_path, extra=extra))


def test_retired_project_keys_are_refused(tmp_path):
    project = 'layout_detectors = ["heuristic"]\n[projects.stu.pipeline.vlm]\nurl = "x"\n'
    with pytest.raises(ValueError, match="layout_detectors, pipeline.vlm .*#6"):
        load_config(_write(tmp_path, project=project))


# ---------------------------------------------------------------- [decider] / [marks]
def test_decider_from_the_table_then_the_kit_env(tmp_path, monkeypatch):
    path = _write(tmp_path, extra='[decider]\nurl = "http://d"\ntimeout = 5\n')
    cfg = load_config(path)
    assert (cfg.decider.url, cfg.decider.vision_url, cfg.decider.timeout) == ("http://d", "", 5)
    monkeypatch.setenv("DECIDER_VISION_URL", "http://v")
    monkeypatch.setenv("DECIDER_TIMEOUT", "30")
    cfg = load_config(path)
    assert (cfg.decider.url, cfg.decider.vision_url, cfg.decider.timeout) == (
        "http://d", "http://v", 30.0)
    monkeypatch.setenv("DECIDER_ENABLED", "false")
    assert (load_config(path).decider.url, load_config(path).decider.vision_url) == ("", "")


def test_marks_table_and_unknown_keys(tmp_path):
    cfg = load_config(_write(tmp_path, extra="[marks]\nmin_leaves = 5\nhide_above = 0.9\n"))
    assert (cfg.marks.min_leaves, cfg.marks.hash_distance, cfg.marks.hide_above) == (5, 6, 0.9)
    with pytest.raises(ValueError, match=r"\[marks\]: unknown keys"):
        load_config(_write(tmp_path, extra="[marks]\nleaves = 5\n"))


# ---------------------------------------------------------------- shipped configs
@pytest.mark.parametrize("name", ["studio.k8s.toml", "studio.dev.toml"])
def test_shipped_configs_load(monkeypatch, name):
    from evilflowers_books_digitalizer.config import PROJECT_ROOT

    monkeypatch.setenv(STORAGE_ROOT_ENV, "/srv/library")
    cfg = load_config(PROJECT_ROOT / "configs" / name)
    assert cfg.artifacts_root == Path("/srv/library/digitized/studio")
    assert all(p.scans_root == Path("/srv/library/raw-scans") for p in cfg.projects.values())
    assert all(r.backend == "service" for s, r in cfg.orchestration.routes.items()
               if s not in ("gate", "publish"))
