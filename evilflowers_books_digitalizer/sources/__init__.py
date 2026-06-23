"""Book sources: where raw TIFF frames are read from.

``build_source`` selects a backend from the ``[source]`` block of
``pipeline.toml`` so deployments switch between the local mount (production)
and WebDAV (development) without code changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.sources.base import AbstractBookSource
from evilflowers_books_digitalizer.sources.filesystem import FilesystemBookSource
from evilflowers_books_digitalizer.sources.webdav import BookSource

__all__ = [
    "AbstractBookSource",
    "BookSource",
    "FilesystemBookSource",
    "build_source",
]


def build_source(source_config: dict[str, Any], key: str) -> AbstractBookSource:
    """Construct the configured source backend for one faculty ``key``.

    ``source_config`` is the ``[source]`` table from ``pipeline.toml``::

        [source]
        backend = "filesystem"            # or "webdav"
        root = "/mnt/digital-library/raw-scans"
        stage_mode = "symlink"            # symlink (zero-copy) | copy | hardlink
        [source.paths]                    # faculty key -> path under root
        fad = "fad/FA-skeny"

    WebDAV credentials still come from ``credentials.toml`` (loaded lazily so
    the filesystem backend needs no secrets).
    """
    backend = source_config.get("backend", "filesystem")
    if backend == "filesystem":
        root = Path(source_config["root"]).expanduser()
        rel = source_config.get("paths", {}).get(key, key)
        return FilesystemBookSource(
            key,
            root / rel,
            stage_mode=source_config.get("stage_mode", "symlink"),
        )
    if backend == "webdav":
        from evilflowers_books_digitalizer.config import load_settings

        return BookSource(load_settings().sources[key])
    raise ValueError(f"unknown source backend: {backend!r}")
