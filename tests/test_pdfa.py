"""PDF/A part-identifier patching (lossless PDF/A-3b -> 2b re-declaration)."""

from __future__ import annotations

import pikepdf
import pytest

from evilflowers_books_digitalizer.pipeline.pdfa import to_pdfa2b


def _make_pdf(path, *, part="3", embed=False) -> None:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    with pdf.open_metadata() as meta:
        meta["pdfaid:part"] = part
        meta["pdfaid:conformance"] = "B"
    if embed:
        pdf.Root.Names = pikepdf.Dictionary(
            EmbeddedFiles=pikepdf.Dictionary(Names=pikepdf.Array())
        )
    pdf.save(path)


def _part(path) -> str | None:
    with pikepdf.open(path) as pdf:
        return pdf.open_metadata().get("pdfaid:part")


def test_redeclares_part_3_as_2(tmp_path):
    pdf = tmp_path / "a.pdf"
    _make_pdf(pdf, part="3")
    assert to_pdfa2b(pdf) is True
    assert _part(pdf) == "2"


def test_keeps_part_3_when_files_embedded(tmp_path):
    pdf = tmp_path / "b.pdf"
    _make_pdf(pdf, part="3", embed=True)
    assert to_pdfa2b(pdf) is False
    assert _part(pdf) == "3"  # genuine PDF/A-3 feature -> left alone


@pytest.mark.parametrize("verapdf", ["definitely-not-installed-xyz"])
def test_validate_missing_verapdf_is_none(tmp_path, verapdf):
    from evilflowers_books_digitalizer.pipeline.pdfa import validate_pdfa

    pdf = tmp_path / "c.pdf"
    _make_pdf(pdf)
    assert validate_pdfa(pdf, "2b", verapdf=verapdf) is None  # fail-safe, never raises
