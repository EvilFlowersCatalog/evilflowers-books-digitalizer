"""Fixed bookmark heuristic: merge wrapped headings, reject OCR noise."""

from __future__ import annotations

from evilflowers_books_digitalizer.pipeline.hocr import HocrLine
from evilflowers_books_digitalizer.pipeline.steps.finalize import FinalizePdf

BODY = 30.0
HEAD = 40.0  # > BODY * 1.18


def _body_lines(n: int, page: int = 0) -> list[HocrLine]:
    return [HocrLine(page, f"body sentence number {i}", BODY, (0, 100 + i, 500, 120 + i))
            for i in range(n)]


def test_wrapped_heading_becomes_single_bookmark():
    lines = _body_lines(12)
    # a two-line heading that wraps — the old code emitted two bookmarks
    lines += [
        HocrLine(3, "2 DRUHY A KATEGÓRIE", HEAD, (0, 50, 500, 90)),
        HocrLine(3, "INŽINIERSKYCH SIETÍ", HEAD, (0, 92, 500, 132)),
    ]
    headings = FinalizePdf()._detect_headings(lines)
    assert headings == [(3, "2 DRUHY A KATEGÓRIE INŽINIERSKYCH SIETÍ")]


def test_ocr_noise_is_rejected():
    lines = _body_lines(12)
    lines += [
        HocrLine(5, "| || |_|", HEAD, (0, 50, 500, 90)),       # table ruling
        HocrLine(6, "Aja: PI", HEAD, (0, 50, 200, 90)),        # short OCR garbage
        HocrLine(7, "ÚVOD", HEAD, (0, 50, 300, 90)),           # real heading
    ]
    headings = FinalizePdf()._detect_headings(lines)
    assert headings == [(7, "ÚVOD")]


def test_no_headings_without_enough_body():
    lines = _body_lines(5)  # < 10 body lines -> can't estimate body size
    lines.append(HocrLine(1, "NADPIS", HEAD, (0, 50, 300, 90)))
    assert FinalizePdf()._detect_headings(lines) == []
