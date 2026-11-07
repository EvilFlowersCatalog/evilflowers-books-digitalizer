"""``<id>_scandata.xml``: ``list[Leaf]`` <-> IA's per-leaf capture document.

Shape (Scribe3 / archive-pdf-tools)::

    <book>
      <bookData>
        <bookId>…</bookId><leafCount>N</leafCount><dpi>300</dpi>
        <pageNumData><assertion><leafNum>5</leafNum><pageNum>1</pageNum></assertion>…</pageNumData>
      </bookData>
      <pageData>
        <page leafNum="0">
          <pageType title="…" level="1">Chapter</pageType>
          <addToAccessFormats>true</addToAccessFormats> <handSide>RIGHT</handSide>
          <rotateDegree>0</rotateDegree> <cropBox><x/><y/><w/><h/></cropBox> …
        </page>
      </pageData>
    </book>

``bookData`` comes first, as in every scandata.xml archive.org serves; parsers
look elements up by name, so the order is cosmetic.

Constraints that come from archive-pdf-tools (``internetarchivepdf.scandata``),
which reads these files with xmltodict and string comparisons:

* booleans are the literal strings ``true``/``false``;
* every page carries ``<pageType>`` (``scandata_xml_get_toc`` indexes it);
* ``pageType@title``/``@level`` only when there is a TOC title (its toc reader
  references an unbound ``title`` otherwise);
* ``<pageNumber>`` is omitted for unnumbered leaves so ``.get()`` yields None.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from evilflowers_books_digitalizer.studio.adapters.ia import _xml
from evilflowers_books_digitalizer.studio.metadata import HandSide, Leaf, PageType


@dataclass(frozen=True, slots=True)
class Scandata:
    book_id: str
    leaves: list[Leaf]
    dpi: int | None = None


def check_contiguous(leaves: Sequence[Leaf]) -> None:
    """Leaf numbers must be 0..n-1 in order: they name images and hOCR pages."""
    nums = [leaf.leaf_num for leaf in leaves]
    if nums != list(range(len(nums))):
        raise ValueError(f"leaves must be numbered 0..{len(nums) - 1} in order, got {nums[:10]}…")


# ---------------------------------------------------------------- writing
def to_xml(book_id: str, leaves: Sequence[Leaf], *, dpi: int | None = None) -> bytes:
    check_contiguous(leaves)
    root = ET.Element("book")
    _book_data(root, book_id, leaves, dpi if dpi is not None else _common_ppi(leaves))
    page_data = _xml.sub(root, "pageData")
    for leaf in leaves:
        _page(page_data, leaf)
    return _xml.to_bytes(root)


def write_scandata(book_id: str, leaves: Sequence[Leaf], path: Path, *,
                   dpi: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(to_xml(book_id, leaves, dpi=dpi))
    return path


def _common_ppi(leaves: Sequence[Leaf]) -> int | None:
    """The book's dpi: the most common leaf ppi (IA keeps one value per book)."""
    values = [leaf.ppi for leaf in leaves if leaf.ppi]
    return max(set(values), key=values.count) if values else None


def _book_data(root: ET.Element, book_id: str, leaves: Sequence[Leaf], dpi: int | None) -> None:
    book = _xml.sub(root, "bookData")
    _xml.sub(book, "bookId", book_id)
    _xml.sub(book, "leafCount", len(leaves))
    if dpi:
        _xml.sub(book, "dpi", dpi)
    numbered = [leaf for leaf in leaves if leaf.page_number]
    if numbered:
        assertions = _xml.sub(book, "pageNumData")
        for leaf in numbered:
            a = _xml.sub(assertions, "assertion")
            _xml.sub(a, "leafNum", leaf.leaf_num)
            _xml.sub(a, "pageNum", leaf.page_number)


def _page(parent: ET.Element, leaf: Leaf) -> None:
    page = _xml.sub(parent, "page", leafNum=str(leaf.leaf_num))
    attrs = {"title": leaf.toc_title} if leaf.toc_title else {}
    if leaf.toc_title and leaf.toc_level is not None:   # absent @level reads as 1 in archive-pdf-tools
        attrs["level"] = str(leaf.toc_level)
    _xml.sub(page, "pageType", leaf.page_type.value, **attrs)
    _xml.sub(page, "addToAccessFormats", _xml.bool_text(leaf.add_to_access))
    _optional(page, "origWidth", leaf.orig_width)
    _optional(page, "origHeight", leaf.orig_height)
    if leaf.crop_box is not None:
        crop = _xml.sub(page, "cropBox")
        for tag, value in zip("xywh", leaf.crop_box, strict=True):
            _xml.sub(crop, tag, value)
    _xml.sub(page, "bookStart", _xml.bool_text(leaf.book_start))
    _optional(page, "handSide", leaf.hand_side.value if leaf.hand_side else None)
    _xml.sub(page, "rotateDegree", leaf.rotate_degree)
    _optional(page, "skewAngle", leaf.skew_angle)
    _optional(page, "pageNumber", leaf.page_number)
    _optional(page, "ppi", leaf.ppi)
    _optional(page, "origFileName", leaf.orig_file_name)


def _optional(parent: ET.Element, tag: str, value: object | None) -> None:
    if value is not None and value != "":
        _xml.sub(parent, tag, value)


# ---------------------------------------------------------------- reading
def from_xml(data: bytes | Path) -> Scandata:
    root = _xml.parse(data)
    if root.tag != "book":
        raise ValueError(f"not an IA scandata.xml (root <{root.tag}>)")
    book = root.find("bookData")
    assertions = _assertions(book)
    pages = root.findall("pageData/page")
    leaves = [_leaf(page, assertions) for page in pages]
    leaves.sort(key=lambda leaf: leaf.leaf_num)
    dpi = _xml.text(book, "dpi")
    return Scandata(book_id=_xml.text(book, "bookId") or "", leaves=leaves,
                    dpi=int(float(dpi)) if dpi else None)


def read_scandata(path: Path) -> Scandata:
    return from_xml(path)


def _assertions(book: ET.Element | None) -> dict[int, str]:
    if book is None:
        return {}
    out: dict[int, str] = {}
    for a in book.findall("pageNumData/assertion"):
        leaf, num = _xml.text(a, "leafNum"), _xml.text(a, "pageNum")
        if leaf is not None and num is not None:
            out[int(leaf)] = num
    return out


def _leaf(page: ET.Element, assertions: dict[int, str]) -> Leaf:
    leaf_num = int(page.get("leafNum", "-1"))
    page_type_el = page.find("pageType")
    level = page_type_el.get("level") if page_type_el is not None else None
    return Leaf(
        leaf_num=leaf_num,
        page_type=_page_type(_xml.text(page, "pageType")),
        hand_side=_hand_side(_xml.text(page, "handSide")),
        add_to_access=_xml.as_bool(_xml.text(page, "addToAccessFormats"), True),
        page_number=_xml.text(page, "pageNumber") or assertions.get(leaf_num),
        crop_box=_crop_box(page.find("cropBox")),
        rotate_degree=int(float(_xml.text(page, "rotateDegree") or 0)),
        skew_angle=_float(_xml.text(page, "skewAngle")),
        orig_file_name=_xml.text(page, "origFileName"),
        orig_width=_int(_xml.text(page, "origWidth")),
        orig_height=_int(_xml.text(page, "origHeight")),
        ppi=_int(_xml.text(page, "ppi")),
        book_start=_xml.as_bool(_xml.text(page, "bookStart"), False),
        toc_title=page_type_el.get("title") if page_type_el is not None else None,
        toc_level=int(level) if level else None,
    )


def _page_type(value: str | None) -> PageType:
    """IA spelling, matched case-insensitively; unknown types read as Normal."""
    if not value:
        return PageType.NORMAL
    by_lower = {pt.value.lower(): pt for pt in PageType}
    return by_lower.get(value.lower(), PageType.NORMAL)


def _hand_side(value: str | None) -> HandSide | None:
    return HandSide(value.upper()) if value and value.upper() in HandSide.__members__ else None


def _crop_box(el: ET.Element | None) -> tuple[int, int, int, int] | None:
    if el is None:
        return None
    values = [_xml.text(el, tag) for tag in "xywh"]
    if any(v is None for v in values):
        return None
    x, y, w, h = (int(float(v)) for v in values if v is not None)
    return x, y, w, h


def _int(value: str | None) -> int | None:
    return int(float(value)) if value else None


def _float(value: str | None) -> float | None:
    return float(value) if value else None
