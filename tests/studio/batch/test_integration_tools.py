"""Real ScanTailor + Tesseract on one synthetic scan (opt-in: ``-m integration``)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evilflowers_books_digitalizer.studio.adapters.imaging import ScanTailorCleaner
from evilflowers_books_digitalizer.studio.adapters.layout import HeuristicLayout
from evilflowers_books_digitalizer.studio.adapters.ocr import TesseractOcr
from evilflowers_books_digitalizer.studio.domain import PageOverride

SCAN = Path("/private/tmp/claude-501/-Users-jdubec-Projects-EvilFlowers-evilflowers-books-digitalizer/"
            "6dab462b-be87-4faf-8b44-afb98fe19ba7/scratchpad/e2e/scans/fei/E2E_FYZIKA_VEKTORY/"
            "scan_0001.tif")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("scantailor-deviant-cli") is None, reason="no ScanTailor"),
    pytest.mark.skipif(shutil.which("tesseract") is None, reason="no tesseract"),
    pytest.mark.skipif(not SCAN.exists(), reason="synthetic scans not generated"),
]


def test_scantailor_and_tesseract_on_a_real_frame(tmp_path: Path) -> None:
    cleaner = ScanTailorCleaner(dpi=300, output_dpi=300)
    plans = cleaner.plan([SCAN])
    assert plans and [p.leaf_num for p in plans] == list(range(len(plans)))
    overrides = {plans[-1].leaf_num: PageOverride(despeckle="off")}   # second ScanTailor group
    pages = cleaner.clean(plans, tmp_path / "clean", overrides)
    assert [p.leaf_num for p in pages] == [p.leaf_num for p in plans]
    assert all(p.image.exists() and p.width > 100 and p.height > 100 for p in pages)
    assert sorted(f.name for f in (tmp_path / "clean").iterdir()) == \
        [f"{p.leaf_num:04d}.tif" for p in pages]                       # staging cleaned up

    ocr = TesseractOcr().recognize(pages[0].image, language="slk+eng", dpi=pages[0].dpi)
    assert "ocr_page" in ocr.hocr and ocr.text.strip() and 0 < ocr.mean_conf <= 100
    proposals = HeuristicLayout().detect(pages[0].image, ocr=ocr)
    assert proposals


@pytest.mark.skipif(shutil.which("pandoc") is None or shutil.which("jpegoptim") is None,
                    reason="no pandoc / jpegoptim")
def test_renderer_produces_ia_named_outputs(tmp_path: Path) -> None:
    from evilflowers_books_digitalizer.studio.adapters.ocr.hocr import hocr_document, relabel_page
    from evilflowers_books_digitalizer.studio.adapters.render import StudioRenderer
    from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
    from evilflowers_books_digitalizer.studio.ports import RenderRequest

    cleaner = ScanTailorCleaner(dpi=300, output_dpi=300)
    pages = cleaner.clean(cleaner.plan([SCAN]), tmp_path / "clean")
    ocr = [TesseractOcr().recognize(p.image, language="slk", dpi=300) for p in pages]
    hocr = tmp_path / "access.html"
    hocr.write_text(hocr_document([relabel_page(o.hocr, p.leaf_num) for o, p in zip(ocr, pages)]))
    out = tmp_path / "item"
    out.mkdir()
    produced = StudioRenderer(dpi=300).render(RenderRequest(
        "stu-fei-e2e", [p.image for p in pages], hocr,
        ItemMetadata("stu-fei-e2e", title="Vektory", language=["slo"]), ["pdf", "pdfa", "epub"],
        {"out_dir": out}))
    assert {k: v.name for k, v in produced.items()} == {
        "pdf": "stu-fei-e2e.pdf", "pdfa": "stu-fei-e2e_archival.pdf", "epub": "stu-fei-e2e.epub"}
    assert all(p.stat().st_size > 1000 for p in produced.values())
    assert sorted(p.name for p in out.iterdir()) == sorted(p.name for p in produced.values())
