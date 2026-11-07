"""IA documents round-trip: _meta.xml, _scandata.xml, _page_numbers.json, _files.xml."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from evilflowers_books_digitalizer.studio.adapters.ia import files_xml, meta_xml, page_numbers, scandata
from evilflowers_books_digitalizer.studio.adapters.ia.naming import FileSource, ia_name, kind_of
from evilflowers_books_digitalizer.studio.metadata import HandSide, ItemMetadata, Leaf, PageType


def sample_meta() -> ItemMetadata:
    return ItemMetadata(
        identifier="stu-fei-9788022726634", title="Vektory a matice — úvod",
        creator=["Červeň, Ivan, 1950-", "Novák, Ján"], publisher="Bratislava : STU",
        date="2007", language=["slo", "eng"], subject=["fyzika", "vektory"],
        isbn=["9788022726634"], oclc_id=["123"], collection=["fei", "stu"], ppi=300,
        imagecount=6, page_progression="lr", scandate="20261107120000",
        extra={"evilflowers_faculty": ["fei"], "evilflowers_tags": ["a", "b"]},
    )


def sample_leaves() -> list[Leaf]:
    return [
        Leaf(0, PageType.COLOR_CARD, add_to_access=False, ppi=300, orig_file_name="scan_0001.tif"),
        Leaf(1, PageType.COVER, HandSide.RIGHT, crop_box=(10, 20, 900, 1300), ppi=300,
             orig_width=2000, orig_height=1400),
        Leaf(2, PageType.TITLE, HandSide.LEFT, book_start=True, rotate_degree=-90, skew_angle=0.35,
             ppi=300),
        Leaf(3, PageType.CHAPTER, HandSide.RIGHT, page_number="1", toc_title="Úvod", toc_level=1,
             ppi=300),
        Leaf(4, PageType.DELETE, add_to_access=False, ppi=300),
        Leaf(5, PageType.NORMAL, HandSide.LEFT, page_number="xii", ppi=600),
        Leaf(6, PageType.SUBCHAPTER, HandSide.RIGHT, toc_title="Bez úrovne", ppi=300),
    ]


# ---------------------------------------------------------------- meta.xml
def test_meta_xml_round_trip_with_repeats_and_custom_fields():
    meta = sample_meta()
    data = meta_xml.to_xml(meta)
    root = ET.fromstring(data)
    assert root.tag == "metadata"
    assert [e.text for e in root.findall("creator")] == ["Červeň, Ivan, 1950-", "Novák, Ján"]
    assert root.find("oclc-id") is not None and root.find("page-progression") is not None
    assert meta_xml.from_xml(data) == meta


def test_meta_xml_file_round_trip(tmp_path: Path):
    path = meta_xml.write_meta_xml(sample_meta(), tmp_path / "x_meta.xml")
    assert path.read_bytes().startswith(b"<?xml")
    assert meta_xml.read_meta_xml(path) == sample_meta()


def test_meta_xml_rejects_invalid_custom_field_names():
    with pytest.raises(ValueError, match="not a valid XML"):
        meta_xml.to_xml(ItemMetadata("abcde-1", extra={"1bad name": ["x"]}))


def test_meta_xml_rejects_other_documents():
    with pytest.raises(ValueError):
        meta_xml.from_xml(b"<book/>")


# ---------------------------------------------------------------- scandata.xml
def test_scandata_round_trip_keeps_every_leaf_field():
    leaves = sample_leaves()
    sd = scandata.from_xml(scandata.to_xml("stu-fei-x", leaves))
    assert sd.book_id == "stu-fei-x" and sd.dpi == 300      # most common ppi
    assert sd.leaves == leaves


def test_scandata_shape():
    root = ET.fromstring(scandata.to_xml("stu-fei-x", sample_leaves()))
    assert root.tag == "book"
    assert root.findtext("bookData/leafCount") == "7"
    assertions = root.findall("bookData/pageNumData/assertion")
    assert [(a.findtext("leafNum"), a.findtext("pageNum")) for a in assertions] == [("3", "1"), ("5", "xii")]
    pages = root.findall("pageData/page")
    assert [p.get("leafNum") for p in pages] == [str(i) for i in range(7)]
    assert pages[6].find("pageType").get("level") is None     # type: ignore[union-attr]
    assert pages[0].findtext("addToAccessFormats") == "false"
    assert pages[1].find("cropBox/w").text == "900"           # type: ignore[union-attr]
    chapter = pages[3].find("pageType")
    assert chapter is not None and chapter.get("title") == "Úvod" and chapter.get("level") == "1"
    assert pages[2].find("pageType").get("title") is None     # type: ignore[union-attr]
    assert pages[2].find("pageNumber") is None                # unnumbered: omitted


def test_scandata_page_number_falls_back_to_assertions():
    data = (b"<book><bookData><bookId>x</bookId><pageNumData><assertion><leafNum>1</leafNum>"
            b"<pageNum>7</pageNum></assertion></pageNumData></bookData><pageData>"
            b"<page leafNum='0'><pageType>Cover</pageType></page>"
            b"<page leafNum='1'><pageType>normal</pageType><handSide>left</handSide></page>"
            b"</pageData></book>")
    sd = scandata.from_xml(data)
    assert sd.leaves[1].page_number == "7" and sd.leaves[1].hand_side == HandSide.LEFT
    assert sd.leaves[1].page_type == PageType.NORMAL and sd.leaves[0].add_to_access


def test_scandata_requires_contiguous_leaves():
    with pytest.raises(ValueError, match="0..1"):
        scandata.to_xml("x", [Leaf(0), Leaf(2)])


# ---------------------------------------------------------------- page_numbers.json
def test_page_numbers_cover_access_leaves_only():
    leaves = sample_leaves()
    doc = page_numbers.to_json("stu-fei-x", leaves)
    assert doc["format-version"] == "2"
    assert [p["leafNum"] for p in doc["pages"]] == [1, 2, 3, 5, 6]
    assert page_numbers.from_json(doc) == {1: None, 2: None, 3: "1", 5: "xii", 6: None}


def test_page_numbers_file_round_trip(tmp_path: Path):
    path = page_numbers.write_page_numbers("x", sample_leaves(), tmp_path / "x_page_numbers.json")
    assert page_numbers.read_page_numbers(path)[3] == "1"
    with pytest.raises(ValueError):
        page_numbers.from_json({"format-version": "1", "pages": []})


# ---------------------------------------------------------------- files.xml + naming
def test_files_xml_round_trip(tmp_path: Path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.7 hello")
    entry = files_xml.describe(f, name="x.pdf", source=FileSource.DERIVATIVE, fmt="Text PDF")
    assert entry.size == 14 and len(entry.md5) == 32 and len(entry.sha1) == 40
    same = files_xml.describe_bytes(b"%PDF-1.7 hello", name="x.pdf", source=FileSource.DERIVATIVE,
                                    fmt="Text PDF")
    assert same == entry
    assert files_xml.from_xml(files_xml.to_xml([entry])) == [entry]


def test_ia_names_are_reversible():
    assert ia_name("abc12", "images") == "abc12_jp2.zip"
    assert kind_of("abc12", "abc12_archival.pdf") == "pdfa"
    assert kind_of("abc12", "abc12.pdf") == "pdf"
    assert kind_of("abc12", "other.pdf") is None
