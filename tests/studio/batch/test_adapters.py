"""Batch adapters in isolation (no external tools, no network, no model weights)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from evilflowers_books_digitalizer.studio import ports
from evilflowers_books_digitalizer.studio.adapters.artifacts import IaArtifactStore
from evilflowers_books_digitalizer.studio.adapters.formula import NoFormula, VlmSidecar
from evilflowers_books_digitalizer.studio.adapters.imaging import PillowImageOps, ScanTailorCleaner
from evilflowers_books_digitalizer.studio.adapters.imaging.scantailor import cut_leaf
from evilflowers_books_digitalizer.studio.adapters.layout import HeuristicLayout, OnnxLayout
from evilflowers_books_digitalizer.studio.adapters.metadata import (
    CompositeMetadata,
    ExcelMetadata,
    OcrExtractMetadata,
    OpacMetadata,
)
from evilflowers_books_digitalizer.studio.adapters.ocr import TesseractOcr
from evilflowers_books_digitalizer.studio.adapters.ocr.hocr import (
    empty_page,
    hocr_document,
    page_text,
    relabel_page,
)
from evilflowers_books_digitalizer.studio.adapters.render import StudioRenderer, stage_pages
from evilflowers_books_digitalizer.studio.adapters.scans import FilesystemScanStore
from evilflowers_books_digitalizer.studio.config import ProjectConfig, StudioConfig
from evilflowers_books_digitalizer.studio.domain import BBox, PageOverride, RegionKind
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import FormulaJob, LeafPlan, PortError, ScannedBook
from evilflowers_books_digitalizer.studio.testing import fakes
from evilflowers_books_digitalizer.studio.adapters import registry


def _project(tmp_path: Path, **kw: Any) -> ProjectConfig:
    return ProjectConfig(slug="stu", name="STU", scans_root=tmp_path, item_id="stu-{collection}-{book}",
                         **kw)


def test_adapters_satisfy_ports(tmp_path: Path) -> None:
    project = _project(tmp_path)
    pairs = [
        (FilesystemScanStore(tmp_path), ports.ScanStore),
        (IaArtifactStore(tmp_path, "stu"), ports.ArtifactStore),
        (ScanTailorCleaner(), ports.PageCleaner),
        (PillowImageOps(), ports.ImageOps),
        (TesseractOcr(), ports.OcrEngine),
        (HeuristicLayout(), ports.LayoutDetector),
        (OnnxLayout(tmp_path / "m.onnx"), ports.LayoutDetector),
        (NoFormula(), ports.FormulaRecognizer),
        (StudioRenderer(), ports.Renderer),
        (CompositeMetadata(project, []), ports.MetadataProvider),
        (OcrExtractMetadata(project), ports.MetadataProvider),
    ]
    for impl, port in pairs:
        assert isinstance(impl, port), (type(impl).__name__, port.__name__)


# ---------------------------------------------------------------- scans
def test_scan_store_captures_collection_and_book(tmp_path: Path) -> None:
    fakes.make_frames(tmp_path / "fei" / "FEI-skeny" / "B1" / "stream_pages_tif", 2)
    fakes.make_frames(tmp_path / "fchpt" / "FCHPT-skeny" / "B2", 1)     # fallback *.tif
    (tmp_path / "fei" / "FEI-skeny" / "EMPTY").mkdir()
    store = FilesystemScanStore(tmp_path, "{collection}/*-skeny/{book}", "stream_pages_tif/*.tif")
    assert store.collections() == ["fchpt", "fei"]
    books = list(store.books("fei"))
    assert [(b.collection, b.external_id, len(b.frames)) for b in books] == [("fei", "B1", 2)]
    assert store.book("fchpt", "B2").frames[0].name == "scan_0001.tif"
    with pytest.raises(PortError):
        store.book("fei", "NOPE")


# ---------------------------------------------------------------- artifacts
def test_ia_artifact_store_layout(tmp_path: Path) -> None:
    store = IaArtifactStore(tmp_path, "stu")
    item = "stu-fei-b1"
    names = {k: store.path(item, k).relative_to(tmp_path / "stu" / item).as_posix()
             for k in ports.IA_KINDS}
    assert names == {
        "meta_xml": f"{item}_meta.xml", "scandata_xml": f"{item}_scandata.xml",
        "images": f"{item}_jp2.zip", "hocr": f"{item}_hocr.html",
        "page_numbers": f"{item}_page_numbers.json", "pdf": f"{item}.pdf",
        "pdfa": f"{item}_archival.pdf", "epub": f"{item}.epub", "text": f"{item}_djvu.txt",
        "files_xml": f"{item}_files.xml"}
    assert store.path(item, "dzi", leaf=3).as_posix().endswith(f"{item}_studio/dzi/0003.dzi")
    assert store.path(item, "leaf_webp", leaf=3).name == "0003.webp"
    assert store.path(item, "regions_json").as_posix().endswith(f"{item}_studio/regions.json")
    assert not store.exists(item, "pdf") and not (tmp_path / "stu").exists()
    with pytest.raises(PortError):
        store.path(item, "dzi")
    with pytest.raises(PortError):
        store.path(item, "bogus")


# ---------------------------------------------------------------- imaging
def test_erase_keeps_mode_and_previews_write_dzi(tmp_path: Path) -> None:
    page = tmp_path / "0002.tif"
    Image.new("1", (600, 400), 0).save(page)
    ops = PillowImageOps()
    out = ops.erase(page, [BBox(10, 10, 50, 30)], tmp_path / "erased.tif")
    with Image.open(out) as im:
        assert im.mode == "1" and im.getpixel((20, 20)) == 255 and im.getpixel((60, 60)) == 0
    dzi = ops.previews(page, tmp_path / "item_studio", max_px=200)
    assert dzi == tmp_path / "item_studio" / "dzi" / "0002.dzi"
    assert 'Width="600" Height="400"' in dzi.read_text()
    levels = sorted(int(p.name) for p in (dzi.parent / "0002_files").iterdir())
    assert levels == list(range(11))                      # ceil(log2(600)) = 10
    assert (dzi.parent / "0002_files" / "10" / "2_1.jpg").exists()
    with Image.open(tmp_path / "item_studio" / "leaves" / "0002.webp") as im:
        assert max(im.size) == 200


def test_cleaner_plans_spreads_and_single_pages(tmp_path: Path) -> None:
    spreads = fakes.make_frames(tmp_path / "spreads", 2)
    plans = ScanTailorCleaner().plan(spreads)
    assert [(p.leaf_num, p.side, p.hand_side) for p in plans] == [
        (0, 0, "LEFT"), (1, 1, "RIGHT"), (2, 0, "LEFT"), (3, 1, "RIGHT")]
    assert plans[0].crop.x1 == plans[1].crop.x0 and plans[1].crop.x1 == 400

    singles = fakes.make_frames(tmp_path / "singles", 3, spread=False, size=(300, 400))
    plans = ScanTailorCleaner().plan(singles)
    assert [(p.leaf_num, p.side, p.hand_side) for p in plans] == [
        (0, None, "RIGHT"), (1, None, "LEFT"), (2, None, "RIGHT")]
    assert plans[0].crop == BBox(0, 0, 300, 400)
    assert ScanTailorCleaner(split=False).plan(spreads)[1].frame == spreads[1]


def test_cut_leaf_rotates_crops_and_deskews(tmp_path: Path) -> None:
    frames = fakes.make_frames(tmp_path, 1)

    frame = np.zeros((300, 400, 3), np.uint8)
    frame[:, 200:] = 255
    plan = LeafPlan(0, frames[0], 1, BBox(200, 0, 400, 300), 0, "RIGHT")
    assert cut_leaf(frame, plan, None).shape[:2] == (300, 200)
    assert cut_leaf(frame, plan, PageOverride(crop=BBox(10, 20, 110, 220))).shape[:2] == (200, 100)
    rotated = LeafPlan(0, frames[0], None, BBox(0, 0, 300, 400), 90)
    assert cut_leaf(frame, rotated, None).shape[:2] == (400, 300)      # crop after rotation
    with pytest.raises(PortError):
        cut_leaf(frame, plan, PageOverride(crop=BBox(900, 900, 950, 950)))


def test_cleaner_reads_pipeline_settings() -> None:
    cleaner = ScanTailorCleaner.from_settings({"color_mode": "color_grayscale", "dpi": 300,
                                               "output_dpi": 600, "unknown": 1})
    assert (cleaner.color_mode, cleaner.output_dpi) == ("color_grayscale", 600)
    assert cleaner._group_key(PageOverride(despeckle="off")) == ("color_grayscale", "off")


# ---------------------------------------------------------------- hOCR
def test_hocr_relabel_and_document(tmp_path: Path) -> None:
    page = fakes.FakeOcr(text="Ahoj svet").recognize(
        fakes.make_frames(tmp_path, 1)[0], language="slk", dpi=300).hocr
    text, conf = page_text(page)
    assert text == "Ahoj svet" and conf == 92.0
    relabelled = relabel_page(page.replace("class='ocr_line'", "class='ocr_line' id='line_1_4'"), 5)
    assert "id=\"page_000005\"" in relabelled and "line_000005_4" in relabelled
    assert "ppageno 5" in relabelled
    doc = hocr_document([relabelled, empty_page(6, 10, 20)], title="T")
    from evilflowers_books_digitalizer.reflow.model import parse_pages

    pages = parse_pages(doc)
    assert len(pages) == 2 and pages[1].width == 10 and not pages[1].pars


# ---------------------------------------------------------------- layout
def _page_with_stamp(path: Path) -> Path:
    im = Image.new("RGB", (800, 1000), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((500, 700, 700, 900), fill=(70, 60, 200))   # saturated violet: a library stamp
    im.save(path)
    return path


def _prose_ocr(width: int, height: int, *, conf: int) -> ports.OcrPage:
    """A paragraph of six full-width lines of plausible Slovak words."""
    lines = []
    for row in range(6):
        y0 = 200 + row * 40
        words = "".join(
            f"<span class='ocrx_word' title='bbox {100 + i * 70} {y0} {160 + i * 70} {y0 + 25}; "
            f"x_wconf {conf}'>slovo</span> " for i in range(9))
        lines.append(f"<span class='ocr_line' title='bbox 100 {y0} 720 {y0 + 25}; x_size 25'>"
                     f"{words}</span>")
    hocr = (f"<div class='ocr_page' title='bbox 0 0 {width} {height}'>"
            f"<p class='ocr_par' title='bbox 100 200 720 445'>{''.join(lines)}</p></div>")
    return ports.OcrPage(hocr, "slovo " * 54, float(conf))


def test_heuristic_layout_proposes_stamps_and_text(tmp_path: Path) -> None:
    image = _page_with_stamp(tmp_path / "0000.tif")
    layout = HeuristicLayout()
    stamps = layout.detect(image)
    assert [p.kind for p in stamps] == [RegionKind.STAMP]
    assert stamps[0].bbox.x0 >= 495 and stamps[0].bbox.x1 <= 705

    found = layout.detect(image, ocr=_prose_ocr(800, 1000, conf=91))
    text = [p for p in found if p.kind == RegionKind.TEXT]
    assert text and text[0].confidence == pytest.approx(0.91) and "slovo" in (text[0].text or "")


# DocLayout-YOLO (ONNX): tests/studio/batch/test_layout_onnx.py


# ---------------------------------------------------------------- formulas
def test_vlm_sidecar_translates_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evilflowers_books_digitalizer.reflow import VlmSettings, vlm

    seen: dict[str, Any] = {}

    def fake_run_jobs(jobs: list[vlm.Job], cache: Path, settings: VlmSettings) -> dict[str, str]:
        seen["jobs"], seen["cache"] = jobs, cache
        return {jobs[0].key: "x^2"}

    monkeypatch.setattr(vlm, "run_jobs", fake_run_jobs)
    image = tmp_path / "book" / "clean" / "0007.tif"
    sidecar = VlmSidecar(VlmSettings(python=tmp_path / "py", model=tmp_path / "m"))
    out = sidecar.recognize([FormulaJob("region-1", image, BBox(1, 2, 3, 4), "formula"),
                             FormulaJob("region-2", image, BBox(5, 6, 7, 8), "ocr")])
    assert out == {"region-1": "x^2"}
    assert seen["cache"] == tmp_path / "book" / "vlm_cache.json"
    assert seen["jobs"][0].key == "p0007_formula_1_2_3_4"
    assert NoFormula().recognize([FormulaJob("k", image, BBox(0, 0, 1, 1), "formula")]) == {}


# ---------------------------------------------------------------- metadata
ISBN = "9788022735049"                         # checksum-valid
BOOK = ScannedBook("fei", ISBN, Path("/x"), ())


def test_ocr_extract_and_opac_metadata(tmp_path: Path) -> None:
    project = _project(tmp_path)
    colophon = "Vydala STU v Bratislave\nISBN 978-80-227-3504-9\n© 2011"
    meta = OcrExtractMetadata(project).lookup(BOOK, ocr_text="Titul\fobsah\f" + colophon)
    assert meta.identifier == f"stu-fei-{ISBN}"
    assert meta.isbn == [ISBN] and meta.date == "2011"

    class Enricher:
        def lookup(self, isbn: str) -> dict[str, Any]:
            return {"title": "Fyzika", "authors": ["Novák, Ján"], "publisher": "STU",
                    "place": "Bratislava", "year": 2011}

    opac = OpacMetadata(project, enricher=Enricher()).lookup(BOOK)
    assert (opac.title, opac.creator, opac.publisher, opac.date) == (
        "Fyzika", ["Novák, Ján"], "Bratislava : STU", "2011")


def test_excel_metadata_and_composite_priority(tmp_path: Path) -> None:
    import pandas as pd

    xlsx = tmp_path / "catalog.xlsx"
    pd.DataFrame([{"Názov": "Z Excelu", "ISBN": ISBN, "Autor": "Kováč, P.",
                   "Jazyk": "slk"}]).to_excel(xlsx, index=False)
    project = _project(tmp_path, language="slk")
    excel = ExcelMetadata(project, xlsx,
                          columns={"title": "Názov", "isbn": "ISBN", "authors": "Autor",
                                   "language": "Jazyk"})
    found = excel.lookup(BOOK)
    assert (found.title, found.creator, found.language) == ("Z Excelu", ["Kováč, P."], ["slo"])
    assert excel.lookup(ScannedBook("fei", "UNKNOWN", Path("/x"), ())).title == ""

    class Failing:
        def lookup(self, book: ScannedBook, *, ocr_text: str | None = None) -> ItemMetadata:
            raise RuntimeError("OPAC down")

    composite = CompositeMetadata(project, [excel, Failing(), fakes.FakeMetadataProvider()])
    merged = composite.lookup(BOOK)
    assert merged.identifier == f"stu-fei-{ISBN}"
    assert merged.title == "Z Excelu" and merged.date == "2007"      # gaps filled, order kept
    assert CompositeMetadata(project, []).lookup(BOOK).language == ["slo"]


# ---------------------------------------------------------------- render
def test_stage_pages_isolates_access_leaves(tmp_path: Path) -> None:
    pages = [tmp_path / f"{i:04d}.tif" for i in range(3)]
    for p in pages:
        Image.new("L", (10, 10)).save(p)
    staged = stage_pages([pages[0], pages[2]], tmp_path / "stage")
    assert [p.name for p in staged] == ["0000.tif", "0001.tif"]
    assert sorted(p.name for p in (tmp_path / "stage").iterdir()) == ["0000.tif", "0001.tif"]
    with pytest.raises(PortError):
        stage_pages([], tmp_path / "empty")


# ---------------------------------------------------------------- registry
def test_registry_builds_adapters_by_name(tmp_path: Path) -> None:
    project = _project(tmp_path, layout_detectors=("heuristic",), formula_recognizer="none")
    cfg = StudioConfig("sqlite://", "memory://", tmp_path / "a", tmp_path / "c", {"stu": project})
    built = registry.build_adapters(cfg, project)
    assert [d.name for d in built.detectors] == ["heuristic"]
    assert isinstance(built.formulas, NoFormula) and built.publisher is None
    assert isinstance(built.artifacts, IaArtifactStore)

    with pytest.raises(PortError, match="unknown layout adapter"):
        registry.build_adapters(cfg, _project(tmp_path, layout_detectors=("magic",)))
    lazy = registry.build_adapters(cfg, _project(tmp_path, layout_detectors=("yolo",)))
    assert [d.name for d in lazy.detectors] == ["doclayout"]     # model checked on detect

    registry.register("layout", "fake", lambda c, p: fakes.FakeLayout())
    try:
        custom = registry.build_adapters(cfg, _project(tmp_path, layout_detectors=("fake",)))
        assert custom.detectors[0].name == "fake"
    finally:
        del registry.LAYOUT_DETECTORS["fake"]


def test_registry_builds_the_evilflowers_publisher(tmp_path: Path) -> None:
    """The registry and the catalog package agree on build_publisher's signature."""
    from evilflowers_books_digitalizer.studio.adapters.catalog import EvilFlowersPublisher
    from evilflowers_books_digitalizer.studio.config import PublishConfig

    project = _project(tmp_path, publish=PublishConfig(target="evilflowers", base_url="http://x"))
    cfg = StudioConfig("sqlite://", "memory://", tmp_path / "a", tmp_path / "c", {"stu": project})
    publisher = registry.build_adapters(cfg, project).publisher
    assert isinstance(publisher, EvilFlowersPublisher)
    assert publisher.artifacts_root == tmp_path / "a"
