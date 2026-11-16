"""Book-level IA text documents ``render/prepare`` writes besides render-service's media.

* ``<id>_hocr.html``  — every leaf, ``page_%06d``; excluded / empty leaves as empty pages;
* ``<id>_djvu.txt``   — plain text of the access leaves (IA's derivative name);
* ``<id>_studio/regions.json`` — region decisions per leaf (training, IA export).

The page package (``<id>_jp2.zip``) is render-service's, with the PDFs and the EPUB.
Each file is written to a temporary name and renamed, so a reader never sees half
a file and a crashed prepare leaves the previous version intact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.studio.hocr import empty_page, hocr_document, relabel_page


@dataclass(frozen=True, slots=True)
class LeafFiles:
    """What the render stage knows about one leaf (a detached copy of the row)."""

    leaf: int
    in_access: bool
    image: Path | None
    hocr: Path | None
    width: int
    height: int
    text: str
    regions: list[dict[str, Any]] = field(default_factory=list)


def _atomic_write(path: Path, data: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)
    return path


#: an ocr_page with its size (``bbox`` in its title); an empty page from ocr-service has none
_SIZED_PAGE = re.compile(r"""class=['"]ocr_page['"][^>]*?title=(['"])(?:(?!\1).)*\bbbox\b""")


def leaf_hocr(leaf: LeafFiles) -> str:
    page = leaf.hocr.read_text(encoding="utf-8") if leaf.hocr and leaf.hocr.exists() else ""
    if not page.strip() or ("ocr_page" in page and not _SIZED_PAGE.search(page)):
        return empty_page(leaf.leaf, leaf.width, leaf.height)    # blank, or a page with no size
    return relabel_page(page, leaf.leaf)


def write_hocr(leaves: Sequence[LeafFiles], path: Path, *, access_only: bool, title: str) -> Path:
    """IA hOCR (all leaves, excluded ones empty) or the access-only hOCR for renderers."""
    if access_only:
        pages = [leaf_hocr(lf) for lf in leaves if lf.in_access]
    else:
        pages = [leaf_hocr(lf) if lf.in_access else empty_page(lf.leaf, lf.width, lf.height)
                 for lf in leaves]
    return _atomic_write(path, hocr_document(pages, title=title))


def write_text(leaves: Sequence[LeafFiles], path: Path) -> Path:
    return _atomic_write(path, "\f".join(lf.text for lf in leaves if lf.in_access) + "\n")


def write_regions(leaves: Sequence[LeafFiles], path: Path) -> Path:
    data = {"leaves": [{"leaf": lf.leaf, "regions": lf.regions} for lf in leaves]}
    return _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1))
