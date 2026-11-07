"""Read an Internet Archive item directory back into ItemMetadata + leaves.

Used to import a book that already lives on archive.org (or a previous studio
export) without re-deriving its leaf decisions. Only ``_meta.xml`` is required;
an item without ``_scandata.xml`` yields no leaves (the studio then plans them
from the frames), and ``_page_numbers.json`` only fills printed numbers that
scandata does not carry.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path

from evilflowers_books_digitalizer.studio.adapters.ia import meta_xml, page_numbers, scandata
from evilflowers_books_digitalizer.studio.adapters.ia.naming import ia_name, kind_of
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata, Leaf
from evilflowers_books_digitalizer.studio.ports import PortError


@dataclass(frozen=True, slots=True)
class IaItem:
    metadata: ItemMetadata
    leaves: list[Leaf]
    dpi: int | None = None
    #: artifact kind -> file in the item directory (IA-named files only)
    files: dict[str, Path] = field(default_factory=dict)


def find_identifier(directory: Path) -> str:
    """The identifier is the prefix of the single ``*_meta.xml`` file."""
    metas = sorted(directory.glob("*_meta.xml"))
    if len(metas) != 1:
        raise PortError(f"{directory}: expected one *_meta.xml, found {len(metas)}")
    return metas[0].name.removesuffix("_meta.xml")


def read_item(directory: Path, item_id: str | None = None) -> IaItem:
    """Raises PortError for anything wrong with the item (foreign input is expected to be bad)."""
    item_id = item_id or find_identifier(directory)
    try:
        meta = meta_xml.read_meta_xml(directory / ia_name(item_id, "meta_xml"))
        leaves, dpi = _leaves(directory, item_id)
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        raise PortError(f"{directory}: not a readable IA item: {exc}") from exc
    files = {kind: p for p in sorted(directory.iterdir())
             if p.is_file() and (kind := kind_of(item_id, p.name)) is not None}
    return IaItem(meta, leaves, dpi, files)


def _leaves(directory: Path, item_id: str) -> tuple[list[Leaf], int | None]:
    sd_path = directory / ia_name(item_id, "scandata_xml")
    if not sd_path.exists():
        return [], None
    sd = scandata.read_scandata(sd_path)
    scandata.check_contiguous(sd.leaves)
    pn_path = directory / ia_name(item_id, "page_numbers")
    if pn_path.exists():
        numbers = page_numbers.read_page_numbers(pn_path)
        leaves = [leaf if leaf.page_number else replace(leaf, page_number=numbers.get(leaf.leaf_num))
                  for leaf in sd.leaves]
        return leaves, sd.dpi
    return sd.leaves, sd.dpi
