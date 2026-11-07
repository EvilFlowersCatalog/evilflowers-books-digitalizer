"""Book-level IA files the render stage writes besides the Renderer's outputs.

* ``<id>_hocr.html``  — every leaf, ``page_%06d``; excluded / empty leaves as empty pages;
* ``<id>_djvu.txt``   — plain text of the access leaves (IA's derivative name);
* ``<id>_jp2.zip``    — the cleaned page images of all leaves (IA "original" images);
* ``<id>_studio/regions.json`` — region decisions per leaf (training, IA export).

Each file is written to a temporary name and renamed, so a reader never sees half
a file and a crashed render leaves the previous version intact.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from evilflowers_books_digitalizer.studio.adapters.ia.images import build_jp2_zip
from evilflowers_books_digitalizer.studio.adapters.ocr.hocr import (
    empty_page,
    hocr_document,
    relabel_page,
)

Image.MAX_IMAGE_PIXELS = None


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


def leaf_hocr(leaf: LeafFiles) -> str:
    page = leaf.hocr.read_text(encoding="utf-8") if leaf.hocr and leaf.hocr.exists() else ""
    return relabel_page(page, leaf.leaf) if page.strip() else \
        empty_page(leaf.leaf, leaf.width, leaf.height)


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


def write_jp2_zip(leaves: Sequence[LeafFiles], path: Path, item_id: str,
                  cache_dir: Path | None = None) -> Path | None:
    """Cleaned leaves as JPEG2000 in IA's ``<id>_jp2/<id>_%04d.jp2`` layout.

    Delegates to the IA adapter's packer (one encoder for render and export).
    An IA page package must hold *every* leaf, so with any leaf uncleaned this
    writes nothing and returns None instead of shipping a short package.
    ``cache_dir`` keeps the encoded pages, so a re-render re-encodes only the
    leaves whose cleaned image changed.
    """
    ordered = sorted(leaves, key=lambda lf: lf.leaf)
    images = [lf.image for lf in ordered if lf.image is not None and lf.image.exists()]
    if len(images) != len(ordered):
        return None
    return build_jp2_zip(item_id, images, path, cache_dir=cache_dir)
