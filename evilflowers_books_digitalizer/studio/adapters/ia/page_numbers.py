"""``<id>_page_numbers.json``: printed page numbers per *access* leaf (format-version 2).

IA's derive writes this file (archive-hocr-tools ``hocr-page-numbers``) and
BookReader reads ``leafNum`` + ``pageNumber`` from it. Excluded leaves
(``addToAccessFormats=false``) are left out, as at IA. Unnumbered leaves carry
``pageNumber: ""``. Our numbers come from operators/scandata, not OCR guesses,
so ``confidence`` is always 100 and ``ocr_value`` is empty.

Nothing in our toolchain consumes the file, so the field set follows the files
archive.org serves; the round-trip test is the only machine check.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.studio.metadata import Leaf

FORMAT_VERSION = "2"


def to_json(item_id: str, leaves: Sequence[Leaf]) -> dict[str, Any]:
    return {
        "identifier": item_id,
        "format-version": FORMAT_VERSION,
        "confidence": 100,
        "pages": [
            {"leafNum": leaf.leaf_num, "pageNumber": leaf.page_number or "",
             "confidence": 100, "pageProb": 100, "ocr_value": []}
            for leaf in leaves if leaf.in_access
        ],
    }


def to_bytes(item_id: str, leaves: Sequence[Leaf]) -> bytes:
    return (json.dumps(to_json(item_id, leaves), ensure_ascii=False, indent=2) + "\n").encode()


def write_page_numbers(item_id: str, leaves: Sequence[Leaf], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(to_bytes(item_id, leaves))
    return path


def from_json(data: dict[str, Any]) -> dict[int, str | None]:
    """leafNum -> printed number (None when unnumbered)."""
    if str(data.get("format-version", FORMAT_VERSION)) != FORMAT_VERSION:
        raise ValueError(f"unsupported page_numbers format-version {data.get('format-version')!r}")
    return {int(p["leafNum"]): (str(p.get("pageNumber") or "") or None) for p in data.get("pages", [])}


def read_page_numbers(path: Path) -> dict[int, str | None]:
    return from_json(json.loads(path.read_text(encoding="utf-8")))
