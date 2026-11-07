"""IaExporter (DB rows + artifacts -> IA item zip) and the importer (item dir -> metadata + leaves)."""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from evilflowers_books_digitalizer.db.models import Book, Page
from evilflowers_books_digitalizer.studio import ports
from evilflowers_books_digitalizer.studio.adapters.ia import (
    IaItemExporter as IaExporter,
)
from evilflowers_books_digitalizer.studio.adapters.ia import (
    apply_leaf,
    build_ia_exporter,
    leaf_from_page,
    leaves_from_pages,
    read_item,
)
from evilflowers_books_digitalizer.studio.adapters.ia.files_xml import from_xml as read_files_xml
from evilflowers_books_digitalizer.studio.adapters.ia.images import jp2_members
from evilflowers_books_digitalizer.studio.metadata import HandSide, ItemMetadata, Leaf, PageType
from evilflowers_books_digitalizer.studio.ports import PortError
from evilflowers_books_digitalizer.studio.testing.fakes import FakeArtifactStore, sqlite_session

ITEM = "stu-fei-e2e_fyzika_vektory"


def make_book(tmp_path: Path) -> tuple[Book, list[Page]]:
    session = sqlite_session()
    meta = ItemMetadata(ITEM, title="Fyzika: vektory", creator=["Červeň, Ivan"], date="2007",
                        language=["slo"], isbn=["9788022726634"], collection=["fei"], ppi=300)
    book = Book(source="fei", book_id="E2E_FYZIKA_VEKTORY", slug="fei_E2E", project="stu",
                item_id=ITEM, item_metadata=meta.as_dict())
    pages = []
    specs = [("Color Card", None, False, None), ("Cover", "RIGHT", True, None),
             ("Normal", "LEFT", True, "1"), ("Normal", "RIGHT", True, "2")]
    for i, (ptype, side, access, number) in enumerate(specs):
        img = tmp_path / "clean" / f"{i:04d}.tif"
        img.parent.mkdir(parents=True, exist_ok=True)
        Image.new("L" if i else "1", (64, 96), 255).save(img)
        pages.append(Page(index=i, page_type=ptype, hand_side=side, add_to_access=access,
                          page_number=number, crop_box=[0, 0, 64, 96], rotate_degree=0, dpi=300,
                          source_path=f"/scans/scan_{i // 2 + 1:04d}.tif", clean_path=str(img),
                          leaf_extra={"book_start": i == 2, "toc_title": "Úvod" if i == 2 else None,
                                      "toc_level": 1 if i == 2 else None}))
    book.pages = pages
    session.add(book)
    session.commit()
    return book, pages


def test_page_row_leaf_round_trip(tmp_path: Path):
    _, pages = make_book(tmp_path)
    leaf = leaf_from_page(pages[2])
    assert leaf == Leaf(2, PageType.NORMAL, HandSide.LEFT, True, "1", (0, 0, 64, 96), 0, None,
                        "scan_0002.tif", None, None, 300, True, "Úvod", 1)
    fresh = apply_leaf(Page(index=99, leaf_extra={"keep": 1}), leaf)
    assert leaf_from_page(fresh) == replace(leaf, orig_file_name=None)   # no source frame
    assert fresh.leaf_extra["keep"] == 1


def test_export_zip_is_an_ia_item_and_imports_back(tmp_path: Path):
    book, pages = make_book(tmp_path)
    store = FakeArtifactStore(tmp_path / "artifacts")
    store.path(ITEM, "pdf").write_bytes(b"%PDF-1.7 distribution")
    store.path(ITEM, "hocr").write_text("<html></html>", encoding="utf-8")

    out = IaExporter(store).export_zip(book, pages, tmp_path / "export.zip")

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        zf.extractall(tmp_path / "unzipped")
    assert {f"{ITEM}/{ITEM}{s}" for s in ("_meta.xml", "_scandata.xml", "_page_numbers.json",
                                          "_files.xml", "_jp2.zip", ".pdf", "_hocr.html")} == names

    item_dir = tmp_path / "unzipped" / ITEM
    assert jp2_members(item_dir / f"{ITEM}_jp2.zip") == [
        f"{ITEM}_jp2/{ITEM}_{i:04d}.jp2" for i in range(4)]

    entries = {e.name: e for e in read_files_xml(item_dir / f"{ITEM}_files.xml")}
    assert set(entries) == {n.split("/", 1)[1] for n in names} - {f"{ITEM}_files.xml"}
    pdf = entries[f"{ITEM}.pdf"]
    assert pdf.source == "derivative" and pdf.format == "Text PDF"
    assert pdf.md5 == hashlib.md5(b"%PDF-1.7 distribution").hexdigest()
    assert entries[f"{ITEM}_meta.xml"].source == "original"

    item = read_item(item_dir)
    assert item.metadata.identifier == ITEM and item.metadata.creator == ["Červeň, Ivan"]
    assert item.metadata.imagecount == 4
    assert item.leaves == leaves_from_pages(pages)
    assert item.dpi == 300
    assert set(item.files) == {"meta_xml", "scandata_xml", "page_numbers", "files_xml", "images",
                               "pdf", "hocr"}


def test_write_item_puts_ia_documents_into_the_artifact_tree(tmp_path: Path):
    book, pages = make_book(tmp_path)
    store = FakeArtifactStore(tmp_path / "artifacts")
    store.path(ITEM, "epub").write_bytes(b"PK epub")
    written = IaExporter(store).write_item(book, pages)
    assert set(written) == {"meta_xml", "scandata_xml", "page_numbers", "files_xml"}
    names = [e.name for e in read_files_xml(written["files_xml"])]
    assert names == [f"{ITEM}_meta.xml", f"{ITEM}_scandata.xml", f"{ITEM}_page_numbers.json",
                     f"{ITEM}.epub"]


def test_images_are_skipped_until_every_leaf_is_cleaned(tmp_path: Path):
    book, pages = make_book(tmp_path)
    pages[1].clean_path = None
    assert IaExporter(FakeArtifactStore(tmp_path / "a")).ensure_images(book, pages) is None


def test_broken_ia_item_is_a_port_error(tmp_path: Path):
    (tmp_path / "abcde_meta.xml").write_bytes(b"<metadata><identifier>abcde</identifier></metadata>")
    (tmp_path / "abcde_scandata.xml").write_bytes(b"<book><pageData><page leafNum='3'/></pageData>")
    with pytest.raises(PortError, match="not a readable IA item"):
        read_item(tmp_path)


def test_book_without_item_id_cannot_be_exported(tmp_path: Path):
    book, pages = make_book(tmp_path)
    book.item_id = None
    with pytest.raises(ValueError, match="item_id"):
        IaExporter(FakeArtifactStore(tmp_path / "a")).export_zip(book, pages, tmp_path / "x.zip")


def test_plain_data_export_item_implements_the_port(tmp_path: Path):
    """The API path: no store, no rows — metadata, leaves, files and leaf images only."""
    book, pages = make_book(tmp_path)
    exporter = build_ia_exporter(cast(Any, None))
    assert isinstance(exporter, ports.IaExporter)
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    meta = ItemMetadata.from_dict(book.item_metadata)
    images = {p.index: Path(p.clean_path) for p in pages if p.clean_path}

    out = exporter.export_item(ITEM, meta, leaves_from_pages(pages), {"pdf": pdf}, images,
                               tmp_path / "ia.zip")

    with zipfile.ZipFile(out) as zf:
        zf.extractall(tmp_path / "plain")
    item = read_item(tmp_path / "plain" / ITEM)
    assert item.leaves == leaves_from_pages(pages)
    assert set(item.files) == {"meta_xml", "scandata_xml", "page_numbers", "files_xml",
                               "images", "pdf"}
