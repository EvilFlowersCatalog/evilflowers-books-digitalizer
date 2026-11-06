"""B: config discovery and path resolution independent of the package location."""

from __future__ import annotations

from pathlib import Path

import pytest

from evilflowers_books_digitalizer import runtime
from evilflowers_books_digitalizer.runtime import (
    CONFIG_ENV,
    build_catalog,
    load_runtime,
    resolve_config_path,
)


def _write_config(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pipeline.toml"
    path.write_text(body, encoding="utf-8")
    return path


RELATIVE = """
[paths]
cache_dir = "scratch/cache"
output_dir = "../out"

[metadata]
enabled = true
excel_path = "catalog.xlsx"

[source]
root = "raw-scans"
"""


def test_relative_paths_resolve_against_the_config_directory(tmp_path, monkeypatch):
    config = _write_config(tmp_path / "etc" / "digitalizer", RELATIVE)
    monkeypatch.chdir(tmp_path)  # the working directory must not matter
    rt = load_runtime(config)
    base = tmp_path / "etc" / "digitalizer"
    assert rt.cache_dir == base / "scratch" / "cache"
    assert rt.output_dir == base / ".." / "out"
    assert rt.metadata["excel_path"] == str(base / "catalog.xlsx")
    assert rt.source["root"] == str(base / "raw-scans")
    assert rt.config_dir == base


def test_paths_never_resolve_into_the_package(tmp_path):
    rt = load_runtime(_write_config(tmp_path, RELATIVE))
    package_dir = Path(runtime.__file__).resolve().parent
    for path in (rt.cache_dir, rt.output_dir, Path(rt.metadata["excel_path"])):
        assert package_dir not in path.resolve().parents


def test_absolute_paths_are_used_as_given(tmp_path):
    config = _write_config(tmp_path / "cfg", f"""
[paths]
cache_dir = "{tmp_path / 'abs-cache'}"
output_dir = "/data/output"
[metadata]
excel_path = "/data/configs/catalog.xlsx"
""")
    rt = load_runtime(config)
    assert rt.cache_dir == tmp_path / "abs-cache"
    assert rt.output_dir == Path("/data/output")
    assert rt.metadata["excel_path"] == "/data/configs/catalog.xlsx"


def test_config_dir_does_not_follow_a_configmap_symlink(tmp_path):
    # k8s mounts ConfigMap keys as <dir>/pipeline.toml -> ..data/pipeline.toml
    real = _write_config(tmp_path / "mount" / "..2026_11_06" , RELATIVE)
    link = tmp_path / "mount" / "pipeline.toml"
    link.symlink_to(real)
    rt = load_runtime(link)
    assert rt.cache_dir == tmp_path / "mount" / "scratch" / "cache"


def test_precedence_cli_over_env_over_default(tmp_path, monkeypatch):
    cli = tmp_path / "cli.toml"
    env = tmp_path / "env.toml"
    monkeypatch.setenv(CONFIG_ENV, str(env))
    assert resolve_config_path(cli) == cli
    assert resolve_config_path(None) == env
    monkeypatch.delenv(CONFIG_ENV)
    assert resolve_config_path(None) == runtime.DEFAULT_CONFIG_PATH
    monkeypatch.setenv(CONFIG_ENV, "  ")  # blank counts as unset
    assert resolve_config_path(None) == runtime.DEFAULT_CONFIG_PATH


def test_load_runtime_reads_the_env_config(tmp_path, monkeypatch):
    config = _write_config(tmp_path / "from-env", RELATIVE)
    monkeypatch.setenv(CONFIG_ENV, str(config))
    rt = load_runtime()
    assert rt.config_path == config
    assert rt.cache_dir == tmp_path / "from-env" / "scratch" / "cache"


def test_the_k8s_config_parses_and_ignores_unknown_keys(tmp_path):
    config = _write_config(tmp_path, K8S_CONFIG + "\n[future]\nsomething = 1\n")
    rt = load_runtime(config)
    assert rt.output_dir == Path("/data/output")
    assert rt.layout == "split"
    assert rt.enrich["head_pages"] == 8
    assert rt.catalog["by_source"] is True
    assert rt.source_keys == ["svf"]
    assert build_catalog(rt.metadata, base_dir=rt.config_dir) is None  # metadata disabled


def test_unknown_layout_is_rejected(tmp_path):
    rt = load_runtime(_write_config(tmp_path, '[render]\nlayout = "nested"\n'))
    with pytest.raises(ValueError):
        _ = rt.layout


# The config the Ansible role renders (task brief §3), verbatim.
K8S_CONFIG = """
[paths]
cache_dir = "/data/cache"
output_dir = "/data/output"

[source]
backend = "filesystem"
root = "/data/raw-scans"
stage_mode = "symlink"
[source.paths]
fad = "fad/FA-skeny"
fei = "fei/FEI-skeny"
mtf = "mtf/MTF-skeny"
sjf = "sjf/SJF-skeny"
svf = "svf/SVF-skeny"

[metadata]
enabled = false

[enrich]
enabled = true
head_pages = 8
tail_pages = 4
lookup = true
override = false

[cover]
enabled = true
source = "opac_then_generated"
template = "stu"

[catalog]
enabled = true
base_url = "https://dev.evilflowers.elvira.stuba.sk/api/v1"
by_source = true
catalog = ""
api_key_env = "EVILFLOWERS_API_KEY"
relation = "open-access"
default_language = "sk"
author_name_order = "given_first"
[catalog.entry_config]
evilflowers_ocr_enabled = false
evilflowers_share_enabled = true
evilflowers_metadata_fetch = true

[orchestration]
max_parallel_books = 1
ocr_jobs = 2
min_free_gb = 200
sources = ["svf"]

[scantailor]
binary = "scantailor-deviant-cli"
color_mode = "mixed"
margins_mm = 8.0
dewarping = "auto"
normalize_illumination = true
despeckle = "cautious"
dpi = 300
output_dpi = 600

[docres]
enabled = false

[render]
outputs = ["distribution", "archival"]
layout = "split"
validate_pdfa = false
[render.distribution]
bg_downsample = 3
[render.archival]
bg_downsample = 2
pdfa = "2b"

[ocr]
language = "auto"

[finalize]
enabled = true
bookmarks = true
page_labels = true
linearize = true
"""
