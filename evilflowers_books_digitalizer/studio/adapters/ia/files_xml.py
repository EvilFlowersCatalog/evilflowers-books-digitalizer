"""``<id>_files.xml``: the item's file manifest (name, source, format, checksums, size).

IA uses it to know which files are originals and which it may regenerate; we
also use it locally as an integrity manifest for the output tree, so every
entry carries md5 *and* sha1 like archive.org's.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from evilflowers_books_digitalizer.studio.adapters.ia import _xml
from evilflowers_books_digitalizer.studio.adapters.ia.naming import FileSource

_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class FileEntry:
    name: str
    source: FileSource
    format: str
    md5: str
    sha1: str
    size: int


def describe(path: Path, *, name: str, source: FileSource, fmt: str) -> FileEntry:
    """Checksum a file in one streaming pass (page zips can be gigabytes)."""
    md5, sha1, size = hashlib.md5(usedforsecurity=False), hashlib.sha1(usedforsecurity=False), 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            md5.update(chunk)
            sha1.update(chunk)
            size += len(chunk)
    return FileEntry(name, source, fmt, md5.hexdigest(), sha1.hexdigest(), size)


def describe_bytes(data: bytes, *, name: str, source: FileSource, fmt: str) -> FileEntry:
    return FileEntry(name, source, fmt, hashlib.md5(data, usedforsecurity=False).hexdigest(),
                     hashlib.sha1(data, usedforsecurity=False).hexdigest(), len(data))


def to_xml(entries: Iterable[FileEntry]) -> bytes:
    root = ET.Element("files")
    for e in entries:
        el = _xml.sub(root, "file", name=e.name, source=e.source.value)
        _xml.sub(el, "format", e.format)
        _xml.sub(el, "md5", e.md5)
        _xml.sub(el, "sha1", e.sha1)
        _xml.sub(el, "size", e.size)
    return _xml.to_bytes(root)


def write_files_xml(entries: Iterable[FileEntry], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(to_xml(entries))
    return path


def from_xml(data: bytes | Path) -> list[FileEntry]:
    root = _xml.parse(data)
    if root.tag != "files":
        raise ValueError(f"not an IA files.xml (root <{root.tag}>)")
    return [_entry(el) for el in root.findall("file")]


def _entry(el: ET.Element) -> FileEntry:
    source = el.get("source", FileSource.ORIGINAL.value)
    return FileEntry(
        name=el.get("name", ""),
        source=FileSource(source) if source in FileSource else FileSource.ORIGINAL,
        format=_xml.text(el, "format") or "",
        md5=_xml.text(el, "md5") or "",
        sha1=_xml.text(el, "sha1") or "",
        size=int(_xml.text(el, "size") or 0),
    )
