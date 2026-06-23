"""Plain runner (sequential path) + monitor rendering."""

from __future__ import annotations

import json
import types

from rich.console import Console

import evilflowers_books_digitalizer.monitor as monitor_mod
import evilflowers_books_digitalizer.runner as runner_mod


def _fake_rt(tmp_path, **orch):
    return types.SimpleNamespace(
        source={"backend": "filesystem", "root": "/x", "paths": {"svf": "svf", "mtf": "mtf"}},
        orchestration={"ocr_jobs": 2, "max_parallel_books": 1, **orch},
        output_dir=tmp_path / "out",
        source_keys=["svf", "mtf"],
    )


def test_run_source_sequential_writes_report(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "load_runtime", lambda *a, **k: _fake_rt(tmp_path))
    monkeypatch.setattr(runner_mod, "build_source",
                        lambda *a, **k: types.SimpleNamespace(list_books=lambda: ["a", "b", "c"]))

    def fake_process(src, book_id, **k):
        if book_id == "b":
            return {"source": src, "book_id": book_id, "status": "error", "error": "x", "minutes": 0.1}
        return {"source": src, "book_id": book_id, "status": "ok", "n_pages": 10,
                "pdf_mb": 1.0, "minutes": 1.0}

    monkeypatch.setattr(runner_mod, "process_book", fake_process)
    res = runner_mod.run_source("svf", max_parallel=1)
    assert res["counts"] == {"ok": 2, "error": 1}
    report = tmp_path / "out" / "batch_report_svf.jsonl"
    assert report.exists()
    assert len(report.read_text().splitlines()) == 3


def test_run_source_isolates_worker_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "load_runtime", lambda *a, **k: _fake_rt(tmp_path))

    def boom(src, book_id, **k):
        raise RuntimeError("worker died")

    monkeypatch.setattr(runner_mod, "process_book", boom)
    res = runner_mod.run_source("svf", book_ids=["a"], max_parallel=1)
    assert res["counts"] == {"error": 1}
    assert "worker died" in res["rows"][0]["error"]


def test_run_corpus_aggregates(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "load_runtime", lambda *a, **k: _fake_rt(tmp_path))
    monkeypatch.setattr(runner_mod, "build_source",
                        lambda cfg, key: types.SimpleNamespace(list_books=lambda: [f"{key}1"]))
    monkeypatch.setattr(runner_mod, "process_book",
                        lambda src, b, **k: {"source": src, "book_id": b, "status": "ok",
                                             "n_pages": 5, "pdf_mb": 0.5, "minutes": 1.0})
    res = runner_mod.run_corpus(max_parallel=1)
    assert res["totals"] == {"ok": 2}
    assert set(res["sources"]) == {"svf", "mtf"}


def test_monitor_render(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "batch_report_svf.jsonl").write_text(
        json.dumps({"source": "svf", "book_id": "a", "status": "ok", "n_pages": 100,
                    "pdf_mb": 2.0, "minutes": 4.0}) + "\n")
    rt = types.SimpleNamespace(output_dir=out, source_keys=["svf", "mtf"], source={})
    console = Console(width=100, record=True)
    console.print(monitor_mod.render(rt, {"svf": 150, "mtf": 270}))
    text = console.export_text()
    assert "svf" in text and "TOTAL" in text and "100" in text


def test_monitor_bar():
    assert "100%" in monitor_mod._bar(10, 10)
    assert monitor_mod._bar(5, None) == "—"
