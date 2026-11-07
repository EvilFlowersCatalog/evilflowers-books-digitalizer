"""Small XML helpers shared by the IA readers and writers.

Writing uses the stdlib ElementTree. Reading parses files that may come from
archive.org or another library (untrusted): the stdlib parser does not resolve
external entities, and the bundled expat (>= 2.4.1, Python 3.14) refuses
billion-laughs expansion, so no extra dependency (``defusedxml`` is only a
transitive one) is needed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

#: XML 1.0 element name (ASCII subset — IA field names are ASCII)
_XML_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")


def is_xml_name(name: str) -> bool:
    return bool(_XML_NAME.match(name)) and not name.lower().startswith("xml")


def sub(parent: ET.Element, tag: str, text: object | None = None, **attrs: str) -> ET.Element:
    """Append ``<tag attrs>text</tag>``; ``None`` text leaves the element empty."""
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = str(text)
    return el


def to_bytes(root: ET.Element) -> bytes:
    """UTF-8 document with declaration, indented (diff-friendly, like IA's files)."""
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def write(root: ET.Element, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(to_bytes(root))
    return path


def parse(data: bytes | Path) -> ET.Element:
    """Root element of an XML document given as bytes or a path."""
    raw = data.read_bytes() if isinstance(data, Path) else data
    return ET.fromstring(raw)


def text(el: ET.Element | None, tag: str) -> str | None:
    """Stripped text of the child ``tag``; None when absent or empty."""
    if el is None:
        return None
    child = el.find(tag)
    if child is None or child.text is None or not child.text.strip():
        return None
    return child.text.strip()


def as_bool(value: str | None, default: bool) -> bool:
    return default if value is None else value.strip().lower() == "true"


def bool_text(value: bool) -> str:
    """IA spells booleans ``true``/``false`` (archive-pdf-tools compares strings)."""
    return "true" if value else "false"
