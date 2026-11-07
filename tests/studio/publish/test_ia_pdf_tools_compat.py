"""Our scandata.xml must read correctly through archive-pdf-tools (recode_pdf --scandata)."""

from __future__ import annotations

from pathlib import Path

from internetarchivepdf import scandata as ia_scandata

from evilflowers_books_digitalizer.studio.adapters.ia.scandata import write_scandata
from evilflowers_books_digitalizer.studio.metadata import HandSide, Leaf, PageType


def leaves() -> list[Leaf]:
    return [
        Leaf(0, PageType.COLOR_CARD, add_to_access=False, ppi=300),
        Leaf(1, PageType.COVER, HandSide.RIGHT, ppi=300),
        Leaf(2, PageType.CHAPTER, HandSide.LEFT, page_number="1", toc_title="Vektory", toc_level=2,
             ppi=300),
        Leaf(3, PageType.WHITE_CARD, add_to_access=False, ppi=300),
        Leaf(4, PageType.NORMAL, HandSide.RIGHT, page_number="2", ppi=600),
    ]


def test_skip_pages_are_the_leaves_excluded_from_access(tmp_path: Path):
    path = write_scandata("stu-fei-x", leaves(), tmp_path / "stu-fei-x_scandata.xml")
    skip = ia_scandata.scandata_xml_get_skip_pages(str(path))
    assert skip == [leaf.leaf_num for leaf in leaves() if not leaf.add_to_access] == [0, 3]


def test_page_numbers_dpi_and_toc(tmp_path: Path):
    path = str(write_scandata("stu-fei-x", leaves(), tmp_path / "s.xml"))
    assert ia_scandata.scandata_xml_get_page_numbers(path) == [None, "1", "2"]
    assert ia_scandata.scandata_xml_get_dpi_per_page(path) == ["300", "300", "600"]
    assert ia_scandata.scandata_xml_get_document_dpi(path) == 300
    toc = ia_scandata.scandata_xml_get_toc(path)
    assert toc == [{"title": "Vektory", "level": 2, "label": None, "leaf": "2",
                    "accessible-page": 1}]
