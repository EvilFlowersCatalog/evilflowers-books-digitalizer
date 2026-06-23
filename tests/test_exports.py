"""Stats exports: CSV / JSON / HTML."""

from __future__ import annotations

import csv
import json

import pytest

from evilflowers_books_digitalizer.exports import (
    default_path,
    export_report,
    write_csv,
    write_html,
    write_json,
)

ROWS = [
    {"source": "svf", "book_id": "a", "status": "ok", "title": "Book A", "n_pages": 100,
     "pdf_mb": 2.0, "minutes": 4.0, "ocr_chars": 5000, "language": "slk+eng"},
    {"source": "svf", "book_id": "b", "status": "error", "error": "boom", "minutes": 0.5},
    {"source": "mtf", "book_id": "c", "status": "ok", "title": "Kniha C", "n_pages": 50,
     "pdf_mb": 1.0, "minutes": 2.0, "ocr_chars": 2000, "language": "slk"},
]


def test_default_path(tmp_path):
    assert default_path(tmp_path, "csv") == tmp_path / "stats.csv"
    assert default_path(tmp_path, "html", "svf") == tmp_path / "stats_svf.html"


def test_write_csv_has_header_and_rows(tmp_path):
    out = write_csv(ROWS, tmp_path / "s.csv")
    parsed = list(csv.DictReader(out.open()))
    assert len(parsed) == 3
    assert parsed[0]["source"] == "mtf"  # sorted by (source, book_id)
    assert {"title", "status", "n_pages", "error"} <= set(parsed[0].keys())


def test_write_json_summary(tmp_path):
    out = write_json(ROWS, tmp_path / "s.json")
    payload = json.loads(out.read_text())
    assert payload["overall"]["books"] == 3
    assert payload["overall"]["by_status"]["ok"] == 2
    assert set(payload["by_source"]) == {"svf", "mtf"}


def test_write_html_is_self_contained(tmp_path):
    out = write_html(ROWS, tmp_path / "s.html", totals={"svf": 4, "mtf": 2},
                     publish={"books": 2, "by_status": {"published": 2}})
    text = out.read_text()
    assert text.startswith("<!doctype html>")
    assert "http://" not in text and "https://" not in text  # no external assets
    assert "svf" in text and "mtf" in text and "TOTAL" in text
    assert "published" in text


def test_export_report_dispatch_and_unknown(tmp_path):
    assert export_report(ROWS, "csv", tmp_path / "x.csv").exists()
    with pytest.raises(ValueError):
        export_report(ROWS, "pdf", tmp_path / "x.pdf")
