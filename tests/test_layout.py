"""Output-tree layout: flat (suffix) vs split (distribution/archival trees)."""

from __future__ import annotations

from pathlib import Path

from evilflowers_books_digitalizer.pipeline.layout import (
    book_dir,
    distribution_pdf,
    pdf_path,
)

ROOT = Path("/mnt/digital-library/content")


def test_flat_layout_puts_both_pdfs_by_the_sidecars():
    bd = book_dir(ROOT, "svf", "flat")
    assert bd == ROOT / "svf"
    assert pdf_path(bd, "svf", "svf_B", "distribution", "flat") == ROOT / "svf/svf_B.distribution.pdf"
    assert pdf_path(bd, "svf", "svf_B", "archival", "flat") == ROOT / "svf/svf_B.archival.pdf"


def test_split_layout_separates_trees_sidecars_with_distribution():
    bd = book_dir(ROOT, "svf", "split")
    assert bd == ROOT / "distribution/svf"  # sidecars + distribution PDF live here
    assert pdf_path(bd, "svf", "svf_B", "distribution", "split") == ROOT / "distribution/svf/svf_B.pdf"
    assert pdf_path(bd, "svf", "svf_B", "archival", "split") == ROOT / "archival/svf/svf_B.pdf"


def test_distribution_sentinel_matches_render_path():
    for layout in ("flat", "split"):
        bd = book_dir(ROOT, "fad", layout)
        assert distribution_pdf(ROOT, "fad", "fad_X", layout) == pdf_path(
            bd, "fad", "fad_X", "distribution", layout
        )
