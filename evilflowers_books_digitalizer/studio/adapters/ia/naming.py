"""IA file names, ``_files.xml`` formats and sources for every artifact kind.

The ArtifactStore decides where a file lives on *our* disk; an IA item has its own
fixed naming (``<id>_meta.xml``, ``<id>_jp2.zip`` …). Keeping the table here means the
exporter, the importer and ``_files.xml`` agree on one spelling, and the store's
layout can change without breaking archive.org compatibility
(docs/design/studio-spec.md, "Output tree").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FileSource(StrEnum):
    """``<file source=…>`` in ``_files.xml`` (IA vocabulary)."""

    ORIGINAL = "original"
    DERIVATIVE = "derivative"
    METADATA = "metadata"


@dataclass(frozen=True, slots=True)
class IaFile:
    suffix: str             # appended to the identifier
    format: str             # IA ``<format>`` label
    source: FileSource


#: artifact kind (ports.IA_KINDS) -> IA naming
IA_FILES: dict[str, IaFile] = {
    "meta_xml": IaFile("_meta.xml", "Metadata", FileSource.ORIGINAL),
    "scandata_xml": IaFile("_scandata.xml", "Scandata", FileSource.ORIGINAL),
    "images": IaFile("_jp2.zip", "Single Page Processed JP2 ZIP", FileSource.ORIGINAL),
    "hocr": IaFile("_hocr.html", "hOCR", FileSource.DERIVATIVE),
    "page_numbers": IaFile("_page_numbers.json", "Page Numbers JSON", FileSource.DERIVATIVE),
    "pdf": IaFile(".pdf", "Text PDF", FileSource.DERIVATIVE),
    "pdfa": IaFile("_archival.pdf", "Archival PDF/A", FileSource.DERIVATIVE),
    "epub": IaFile(".epub", "EPUB", FileSource.DERIVATIVE),
    "text": IaFile("_djvu.txt", "DjVuTXT", FileSource.DERIVATIVE),
    "files_xml": IaFile("_files.xml", "Metadata", FileSource.METADATA),
}


def ia_name(item_id: str, kind: str) -> str:
    """The IA file name of ``kind`` for ``item_id`` (KeyError for studio-only kinds)."""
    return f"{item_id}{IA_FILES[kind].suffix}"


def kind_of(item_id: str, name: str) -> str | None:
    """Inverse of :func:`ia_name`: which kind a file of an IA item is, if any.

    Longest suffix first, so ``_archival.pdf`` is not mistaken for ``.pdf``.
    """
    for kind, spec in sorted(IA_FILES.items(), key=lambda kv: -len(kv[1].suffix)):
        if name == f"{item_id}{spec.suffix}":
            return kind
    return None


def leaf_image_name(item_id: str, leaf_num: int, ext: str = "jp2") -> str:
    """``<id>_jp2/<id>_0007.jp2`` — the member name IA expects inside ``_jp2.zip``."""
    return f"{item_id}_{ext}/{item_id}_{leaf_num:04d}.{ext}"
