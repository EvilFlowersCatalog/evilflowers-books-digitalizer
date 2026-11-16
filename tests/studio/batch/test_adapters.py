"""The orchestrator's adapters: scans, artifacts, leaf planning, hOCR, metadata, decider, registry."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from evilflowers_books_digitalizer.studio import ports
from evilflowers_books_digitalizer.studio.adapters import registry
from evilflowers_books_digitalizer.studio.adapters.artifacts import IaArtifactStore
from evilflowers_books_digitalizer.studio.adapters.decider import HttpDecider
from evilflowers_books_digitalizer.studio.adapters.imaging import SpreadPlanner
from evilflowers_books_digitalizer.studio.adapters.imaging.crops import crop_png
from evilflowers_books_digitalizer.studio.adapters.metadata import (
    CompositeMetadata,
    ExcelMetadata,
    OcrExtractMetadata,
    OpacMetadata,
)
from evilflowers_books_digitalizer.studio.adapters.scans import FilesystemScanStore
from evilflowers_books_digitalizer.studio.config import ProjectConfig, StudioConfig
from evilflowers_books_digitalizer.studio.domain import BBox
from evilflowers_books_digitalizer.studio.hocr import (
    empty_page,
    hocr_document,
    parse_pages,
    relabel_page,
)
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import PortError, ScannedBook
from evilflowers_books_digitalizer.studio.testing import fakes


def _project(tmp_path: Path, **kw: Any) -> ProjectConfig:
    return ProjectConfig(slug="stu", name="STU", scans_root=tmp_path, item_id="stu-{collection}-{book}",
                         **kw)


def test_adapters_satisfy_ports(tmp_path: Path) -> None:
    project = _project(tmp_path)
    pairs = [
        (FilesystemScanStore(tmp_path), ports.ScanStore),
        (IaArtifactStore(tmp_path, "stu"), ports.ArtifactStore),
        (SpreadPlanner(), ports.LeafPlanner),
        (HttpDecider("http://decider"), ports.Decider),
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


# ---------------------------------------------------------------- leaf planning
def test_planner_plans_spreads_and_single_pages(tmp_path: Path) -> None:
    spreads = fakes.make_frames(tmp_path / "spreads", 2)
    plans = SpreadPlanner().plan(spreads)
    assert [(p.leaf_num, p.side, p.hand_side) for p in plans] == [
        (0, 0, "LEFT"), (1, 1, "RIGHT"), (2, 0, "LEFT"), (3, 1, "RIGHT")]
    assert plans[0].crop.x1 == plans[1].crop.x0 and plans[1].crop.x1 == 400

    singles = fakes.make_frames(tmp_path / "singles", 3, spread=False, size=(300, 400))
    plans = SpreadPlanner().plan(singles)
    assert [(p.leaf_num, p.side, p.hand_side) for p in plans] == [
        (0, None, "RIGHT"), (1, None, "LEFT"), (2, None, "RIGHT")]
    assert plans[0].crop == BBox(0, 0, 300, 400)
    assert SpreadPlanner(split=False).plan(spreads)[1].frame == spreads[1]


def test_planner_skips_unreadable_frames(tmp_path: Path) -> None:
    frames = fakes.make_frames(tmp_path, 1)
    junk = tmp_path / "junk.tif"
    junk.write_bytes(b"not an image")
    assert [p.frame for p in SpreadPlanner().plan([junk, *frames])] == [frames[0], frames[0]]


def test_crop_png_pads_and_bounds(tmp_path: Path) -> None:
    page = tmp_path / "p.tif"
    Image.new("L", (2000, 3000), 255).save(page)
    with Image.open(__import__("io").BytesIO(crop_png(page, (100, 100, 300, 200)))) as im:
        assert im.size == (300, 150)                       # 200x100 + a quarter on each side
    with Image.open(__import__("io").BytesIO(crop_png(page, (0, 0, 2000, 3000)))) as im:
        assert max(im.size) == 448


# ---------------------------------------------------------------- hOCR
def test_hocr_relabel_document_and_parse(tmp_path: Path) -> None:
    page = fakes.FakeOcr(text="Ahoj svet").recognize(
        fakes.make_frames(tmp_path, 1)[0], language="slk", dpi=300).hocr
    (parsed,) = parse_pages(page)
    assert [ln.text for ln in parsed.lines] == ["Ahoj svet"] and parsed.lines[0].size == 20
    relabelled = relabel_page(page.replace("class='ocr_line'", "class='ocr_line' id='line_1_4'"), 5)
    assert "id=\"page_000005\"" in relabelled and "line_000005_4" in relabelled
    assert "ppageno 5" in relabelled
    doc = hocr_document([relabelled, empty_page(6, 10, 20)], title="T")
    pages = parse_pages(doc)
    assert len(pages) == 2 and pages[1].width == 10 and not pages[1].pars
    assert parse_pages("") == []


# ---------------------------------------------------------------- decider
def _decider(handler: Any, **kw: Any) -> HttpDecider:
    return HttpDecider("http://decider/v1/systemone", client=httpx.Client(
        transport=httpx.MockTransport(handler)), **kw)


def test_decider_sends_the_keyed_question_and_reads_the_choice() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "laya-multilingual@0.4.34",
            "answers": {"q": {"choice": "b", "probabilities": {"a": 0.2, "b": 0.8},
                              "confidence": 0.8}}})

    d = _decider(handler, model="multilingual").choose(
        "q", "Which?", {"a": "A", "b": "B"}, state={"body": "text"}, images=[b"\x89PNG"])
    assert (d.choice, d.p, d.model) == ("b", 0.8, "laya-multilingual@0.4.34")
    assert seen["model"] == "multilingual" and seen["state"] == {"body": "text"}
    assert seen["questions"] == {"q": {"type": "choice", "instructions": "Which?",
                                       "criteria": {"a": "A", "b": "B"}}}
    assert base64.b64decode(seen["images"][0]) == b"\x89PNG"


@pytest.mark.parametrize("response", [
    httpx.Response(503, text="loading"),
    httpx.Response(200, json={"answers": {"q": {"choice": "zzz"}}}),
    httpx.Response(200, text="not json"),
])
def test_decider_failures_are_port_errors(response: httpx.Response) -> None:
    with pytest.raises(PortError):
        _decider(lambda r: response).choose("q", "?", {"a": "A"})


def test_decider_unreachable_is_a_port_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(PortError, match="ConnectError"):
        _decider(refuse).choose("q", "?", {"a": "A"})


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


# ---------------------------------------------------------------- registry
def test_registry_builds_adapters_by_name(tmp_path: Path) -> None:
    project = _project(tmp_path, metadata={"providers": ["ocr"]},
                       pipeline={"scantailor": {"split": False}})
    cfg = StudioConfig("sqlite://", "memory://", tmp_path / "a", tmp_path / "c", {"stu": project})
    built = registry.build_adapters(cfg, project)
    assert isinstance(built.planner, SpreadPlanner) and built.planner.split is False
    assert built.publisher is None and isinstance(built.artifacts, IaArtifactStore)
    with pytest.raises(PortError, match="unknown metadata adapter"):
        registry.build_adapters(cfg, _project(tmp_path, metadata={"providers": ["magic"]}))

    registry.register("metadata", "fake", lambda c, p: fakes.FakeMetadataProvider())
    try:
        custom = registry.build_adapters(cfg, _project(tmp_path, metadata={"providers": ["fake"]}))
        assert custom.metadata.lookup(BOOK).title == "Testovacia kniha"
    finally:
        del registry.METADATA_PROVIDERS["fake"]


def test_registry_builds_the_evilflowers_publisher(tmp_path: Path) -> None:
    """The registry and the catalog package agree on build_publisher's signature."""
    from evilflowers_books_digitalizer.studio.adapters.catalog import EvilFlowersPublisher
    from evilflowers_books_digitalizer.studio.config import PublishConfig

    project = _project(tmp_path, publish=PublishConfig(target="evilflowers", base_url="http://x"))
    cfg = StudioConfig("sqlite://", "memory://", tmp_path / "a", tmp_path / "c", {"stu": project})
    publisher = registry.build_adapters(cfg, project).publisher
    assert isinstance(publisher, EvilFlowersPublisher)
    assert publisher.artifacts_root == tmp_path / "a"
