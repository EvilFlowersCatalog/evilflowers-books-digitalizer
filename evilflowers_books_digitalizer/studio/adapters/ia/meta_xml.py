"""``<id>_meta.xml``: :class:`ItemMetadata` <-> IA's flat ``<metadata>`` document.

IA stores a repeatable field as the same element repeated (``<creator>`` twice),
never as a delimited string, so lists map to repeated elements both ways. Field
naming and repeatability are already decided by :meth:`ItemMetadata.to_ia` /
:meth:`ItemMetadata.from_ia`; this module only adds the XML shape.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from evilflowers_books_digitalizer.studio.adapters.ia import _xml
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata


def to_xml(meta: ItemMetadata) -> bytes:
    """Serialize; raises ValueError for a custom field that is not a valid XML name."""
    root = ET.Element("metadata")
    for key, value in meta.to_ia().items():
        if not _xml.is_xml_name(key):
            raise ValueError(f"meta.xml field {key!r} is not a valid XML element name")
        for item in value if isinstance(value, list) else [value]:
            _xml.sub(root, key, item)
    return _xml.to_bytes(root)


def from_xml(data: bytes | Path) -> ItemMetadata:
    """Parse; every element becomes a list so :meth:`ItemMetadata.from_ia` decides arity."""
    root = _xml.parse(data)
    if root.tag != "metadata":
        raise ValueError(f"not an IA meta.xml (root <{root.tag}>)")
    fields: dict[str, list[str]] = {}
    for child in root:
        fields.setdefault(child.tag, []).append((child.text or "").strip())
    return ItemMetadata.from_ia(fields)


def write_meta_xml(meta: ItemMetadata, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(to_xml(meta))
    return path


def read_meta_xml(path: Path) -> ItemMetadata:
    return from_xml(path)
