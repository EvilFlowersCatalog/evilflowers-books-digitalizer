"""The shared storage root: one setting, {storage_root} paths, env override, load-time checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from evilflowers_books_digitalizer.studio.config import STORAGE_ROOT_ENV, load_config

BASE = """
[studio]
database_url = "sqlite://"
{studio_extra}
artifacts_root = "{artifacts}"
cache_dir = "/scratch/cache"

[projects.stu]
scans_root = "{scans}"
"""

SERVICE_CLEAN = """
[orchestration.stages.clean]
backend = "service"
task = "evilflowers_scan.clean"
queue = "evilflowers_scan.standalone"
"""


def _write(tmp_path: Path, *, studio_extra: str = 'storage_root = "/data"',
           artifacts: str = "{storage_root}/digitized/studio",
           scans: str = "{storage_root}/raw-scans", extra: str = "") -> Path:
    path = tmp_path / "studio.toml"
    path.write_text(BASE.replace("{studio_extra}", studio_extra)
                    .replace("{artifacts}", artifacts).replace("{scans}", scans) + extra)
    return path


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(STORAGE_ROOT_ENV, raising=False)


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


def test_local_stages_need_no_root(tmp_path):
    cfg = load_config(_write(tmp_path, studio_extra="", artifacts="/out", scans="/scans"))
    assert cfg.storage_root is None


def test_service_stage_needs_a_root(tmp_path):
    with pytest.raises(ValueError, match="needs a storage root"):
        load_config(_write(tmp_path, studio_extra="", artifacts="/out", scans="/scans",
                           extra=SERVICE_CLEAN))


def test_service_stage_needs_paths_under_the_root(tmp_path):
    with pytest.raises(ValueError, match="outside it: /scans"):
        load_config(_write(tmp_path, scans="/scans", extra=SERVICE_CLEAN))


def test_shipped_k8s_config_loads(monkeypatch):
    from evilflowers_books_digitalizer.config import PROJECT_ROOT

    monkeypatch.setenv(STORAGE_ROOT_ENV, "/srv/library")
    cfg = load_config(PROJECT_ROOT / "configs" / "studio.k8s.toml")
    assert cfg.artifacts_root == Path("/srv/library/digitized/studio")
    assert cfg.project("stu").scans_root == Path("/srv/library/raw-scans")
