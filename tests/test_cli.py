"""E + check + publish-book through the real argument parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evilflowers_books_digitalizer.runner as runner_mod
from evilflowers_books_digitalizer import cli, preflight
from evilflowers_books_digitalizer.catalog.publisher import CatalogSettings
from evilflowers_books_digitalizer.pipeline.layout import distribution_pdf


def _config(tmp_path: Path, *, catalog_enabled: bool = True, extra: str = "") -> Path:
    scans = tmp_path / "raw-scans" / "svf" / "SVF-skeny"
    (scans / "B1").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "configs" / "pipeline.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""
[paths]
cache_dir = "../cache"
output_dir = "../output"
[source]
backend = "filesystem"
root = "{tmp_path / 'raw-scans'}"
[source.paths]
svf = "svf/SVF-skeny"
[metadata]
enabled = false
[render]
layout = "split"
[orchestration]
max_parallel_books = 4
min_free_gb = 0
sources = ["svf"]
[catalog]
enabled = {str(catalog_enabled).lower()}
base_url = "https://x/api/v1"
by_source = true
catalog = ""
{extra}
""", encoding="utf-8")
    return path


def _fake_process(statuses: dict[str, str], seen: list[str]):
    def process_book(source, book_id, **kwargs):
        seen.append(book_id)
        status = statuses.get(book_id, "ok")
        row = {"source": source, "book_id": book_id, "status": status, "minutes": 0.1}
        if status == "error":
            row["error"] = "RuntimeError: boom"
        return row
    return process_book


def _produce(tmp_path: Path, book_id: str) -> None:
    pdf = distribution_pdf(tmp_path / "output", "svf", f"svf_{book_id}", "split")
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.7")


# -- E: several ids, in order; exit codes -----------------------------------------


def test_run_book_runs_every_id_in_turn(tmp_path, monkeypatch, capsys):
    seen: list[str] = []
    monkeypatch.setattr(runner_mod, "process_book", _fake_process({}, seen))
    config = _config(tmp_path)
    code = cli.main(["run-book", "svf", "B3", "B1", "B2", "--config", str(config)])
    assert code == 0
    assert seen == ["B3", "B1", "B2"]  # sequential, in argument order
    rows = json.loads(capsys.readouterr().out)
    assert [r["book_id"] for r in rows] == ["B3", "B1", "B2"]
    report = (tmp_path / "output" / "batch_report_svf.jsonl").read_text().splitlines()
    assert len(report) == 3


def test_run_book_exits_1_if_any_book_errored(tmp_path, monkeypatch, capsys):
    seen: list[str] = []
    monkeypatch.setattr(runner_mod, "process_book", _fake_process({"B2": "error"}, seen))
    code = cli.main(["run-book", "svf", "B1", "B2", "B3", "--config", str(_config(tmp_path))])
    assert code == 1
    assert seen == ["B1", "B2", "B3"]  # an error does not stop the rest


def test_run_book_publish_publishes_each_success(tmp_path, monkeypatch, capsys, fake_catalog):
    seen: list[str] = []
    monkeypatch.setattr(runner_mod, "process_book",
                        _fake_process({"B2": "error", "B3": "skipped"}, seen))
    monkeypatch.setattr(CatalogSettings, "client", lambda self: fake_catalog)
    monkeypatch.setenv("EVILFLOWERS_API_KEY", "k")
    _produce(tmp_path, "B1")
    _produce(tmp_path, "B3")  # finished in an earlier run: still published

    code = cli.main(["run-book", "svf", "B1", "B2", "B3", "--publish",
                     "--config", str(_config(tmp_path))])
    rows = {r["book_id"]: r for r in json.loads(capsys.readouterr().out)}
    assert code == 1  # B2 failed to digitize
    assert rows["B1"]["publish"]["status"] == "published"
    assert "publish" not in rows["B2"]
    assert rows["B3"]["publish"]["status"] == "published"
    assert len(fake_catalog.in_catalog("svf")) == 2


def test_run_book_publish_error_sets_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner_mod, "process_book", _fake_process({}, []))
    monkeypatch.delenv("EVILFLOWERS_API_KEY", raising=False)
    _produce(tmp_path, "B1")
    code = cli.main(["run-book", "svf", "B1", "--publish", "--config", str(_config(tmp_path))])
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "ok" and row["publish"]["status"] == "error"
    assert code == 1


def test_run_source_exit_codes(tmp_path, monkeypatch, capsys):
    config = str(_config(tmp_path))
    monkeypatch.setattr(runner_mod, "process_book", _fake_process({}, []))
    monkeypatch.setattr(runner_mod, "ProcessPoolExecutor", _InlineExecutor)
    assert cli.main(["run-source", "svf", "--config", config]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": 1}  # same shape as before

    monkeypatch.setattr(runner_mod, "process_book", _fake_process({"B1": "error"}, []))
    assert cli.main(["run-source", "svf", "--config", config]) == 1


def test_run_source_publish(tmp_path, monkeypatch, capsys, fake_catalog):
    monkeypatch.setattr(runner_mod, "process_book", _fake_process({}, []))
    monkeypatch.setattr(runner_mod, "ProcessPoolExecutor", _InlineExecutor)
    monkeypatch.setattr(CatalogSettings, "client", lambda self: fake_catalog)
    monkeypatch.setenv("EVILFLOWERS_API_KEY", "k")
    _produce(tmp_path, "B1")
    code = cli.main(["run-source", "svf", "--publish", "--config", str(_config(tmp_path))])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"digitize": {"ok": 1},
                                                   "publish": {"published": 1}}


class _InlineExecutor:
    """ProcessPoolExecutor stand-in: runs submissions inline (patched functions stay patched)."""

    def __init__(self, max_workers: int) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        future: Future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


def test_config_env_var_is_used_without_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner_mod, "process_book", _fake_process({}, []))
    monkeypatch.setenv("EVILFLOWERS_DIGITALIZER_CONFIG", str(_config(tmp_path)))
    assert cli.main(["run-book", "svf", "B1"]) == 0
    assert (tmp_path / "output" / "batch_report_svf.jsonl").exists()


# -- publish-book ----------------------------------------------------------------


def test_publish_book_dry_run_several_ids(tmp_path, capsys):
    _produce(tmp_path, "B1")
    _produce(tmp_path, "B2")
    code = cli.main(["publish-book", "svf", "B1", "B2", "--dry-run",
                     "--config", str(_config(tmp_path))])
    rows = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [(r["book_id"], r["status"], r["catalog"]) for r in rows] == [
        ("B1", "dry-run", "svf"), ("B2", "dry-run", "svf")]
    assert (tmp_path / "output" / "distribution" / "svf" / "svf_B1.entry.json").exists()


def test_publish_book_exits_1_on_error(tmp_path, capsys):
    _produce(tmp_path, "B1")
    code = cli.main(["publish-book", "svf", "B1", "MISSING", "--dry-run",
                     "--config", str(_config(tmp_path))])
    assert code == 1


# -- check -----------------------------------------------------------------------


def _all_tools(name: str) -> str | None:
    return f"/usr/bin/{name}"


def _langs(cmd: list[str]) -> str:
    return "List of available languages (3):\neng\nosd\nslk\n"


def test_check_passes_on_a_healthy_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(preflight.shutil, "which", _all_tools)
    monkeypatch.setattr(preflight, "_run_text", _langs)
    code = cli.main(["check", "--config", str(_config(tmp_path))])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "OK    source svf" in out and "1 book dirs" in out
    assert "OK    output_dir writable" in out and "OK    binary jbig2" in out
    assert not list((tmp_path / "output").glob(".preflight-*"))  # temp file removed


def test_check_fails_on_missing_binary_and_language(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(preflight.shutil, "which",
                        lambda name: None if name == "jbig2" else f"/usr/bin/{name}")
    monkeypatch.setattr(preflight, "_run_text", lambda cmd: "eng\nosd\n")
    code = cli.main(["check", "--config", str(_config(tmp_path))])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  binary jbig2" in out
    assert "FAIL  tesseract languages  missing slk" in out


def test_check_fails_on_unreadable_source_and_low_disk(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(preflight.shutil, "which", _all_tools)
    monkeypatch.setattr(preflight, "_run_text", _langs)
    config = _config(tmp_path, extra="")
    text = config.read_text().replace("min_free_gb = 0", "min_free_gb = 1000000000")
    text = text.replace('svf = "svf/SVF-skeny"', 'svf = "svf/missing"')
    config.write_text(text)
    code = cli.main(["check", "--config", str(config)])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  source svf" in out and "FAIL  free space" in out


def test_check_missing_config_fails(tmp_path, capsys):
    assert cli.main(["check", "--config", str(tmp_path / "nope.toml")]) == 1
    assert "FAIL  config" in capsys.readouterr().out


def test_check_catalog(tmp_path, monkeypatch, capsys, fake_catalog):
    monkeypatch.setattr(preflight.shutil, "which", _all_tools)
    monkeypatch.setattr(preflight, "_run_text", _langs)
    monkeypatch.setattr(CatalogSettings, "client", lambda self: fake_catalog)
    config = str(_config(tmp_path))

    assert cli.main(["check", "--catalog", "--config", config]) == 1
    assert "FAIL  catalog token  EVILFLOWERS_API_KEY is not set" in capsys.readouterr().out

    monkeypatch.setenv("EVILFLOWERS_API_KEY", "k")
    assert cli.main(["check", "--catalog", "--config", config]) == 0
    out = capsys.readouterr().out
    assert "OK    catalog ping" in out and "OK    catalog svf" in out

    fake_catalog.catalogs.pop("svf")
    assert cli.main(["check", "--catalog", "--config", config]) == 1
    assert "FAIL  catalog svf" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", [
    ["run-book", "svf", "B1", "--publish", "--jobs", "2", "--keep-cache"],
    ["run-source", "svf", "--limit", "3", "--publish"],
    ["publish-book", "svf", "A", "B", "--dry-run", "--force"],
    ["publish-catalog", "svf", "--limit", "1", "--force", "--dry-run"],
    ["publish-catalog"],
    ["check", "--catalog"],
    ["list", "svf", "--limit", "2"],
])
def test_contract_commands_parse(cmd):
    args = cli.build_parser().parse_args(cmd)
    assert callable(args.func)
