"""Batch report loading, dedup, and summary."""

from __future__ import annotations

import json

from evilflowers_books_digitalizer.reporting import (
    latest_per_book,
    load_reports,
    summarize_by_source,
    summarize_reports,
)


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_load_reports_reads_all_files(tmp_path):
    _write(tmp_path / "batch_report_svf.jsonl", [{"source": "svf", "book_id": "a", "status": "ok"}])
    _write(tmp_path / "batch_report_fad.jsonl", [{"source": "fad", "book_id": "b", "status": "ok"}])
    (tmp_path / "batch_report_bad.jsonl").write_text("not json\n{}\n")
    rows = load_reports(tmp_path)
    assert len(rows) == 3  # 2 valid + the empty {} ; bad line skipped


def test_latest_per_book_keeps_newest(tmp_path):
    rows = [
        {"source": "svf", "book_id": "a", "status": "error"},
        {"source": "svf", "book_id": "a", "status": "ok"},  # re-run succeeded
        {"source": "svf", "book_id": "b", "status": "ok"},
    ]
    latest = latest_per_book(rows)
    assert len(latest) == 2
    a = next(r for r in latest if r["book_id"] == "a")
    assert a["status"] == "ok"


def test_summarize_reports():
    rows = [
        {"source": "svf", "book_id": "a", "status": "ok", "n_pages": 100, "pdf_mb": 2.0,
         "ocr_chars": 5000, "minutes": 4.0},
        {"source": "svf", "book_id": "b", "status": "ok", "n_pages": 50, "pdf_mb": 1.0,
         "ocr_chars": 2000, "minutes": 2.0},
        {"source": "svf", "book_id": "c", "status": "error", "minutes": 0.5},
        {"source": "svf", "book_id": "d", "status": "skipped"},
    ]
    s = summarize_reports(rows)
    assert s["books"] == 4
    assert s["by_status"] == {"ok": 2, "error": 1, "skipped": 1}
    assert s["produced"] == 2
    assert s["pages"] == 150
    assert s["pdf_mb"] == 3.0
    assert s["mb_per_page"] == 0.02
    assert s["pages_per_min"] == round(150 / 6.5, 1)


def test_summarize_by_source():
    rows = [
        {"source": "svf", "book_id": "a", "status": "ok", "n_pages": 10, "pdf_mb": 1.0, "minutes": 1},
        {"source": "fad", "book_id": "b", "status": "ok", "n_pages": 20, "pdf_mb": 2.0, "minutes": 2},
    ]
    by = summarize_by_source(rows)
    assert set(by) == {"svf", "fad"}
    assert by["fad"]["pages"] == 20


def test_summarize_empty():
    s = summarize_reports([])
    assert s["books"] == 0 and s["mb_per_page"] is None and s["pages_per_min"] is None
