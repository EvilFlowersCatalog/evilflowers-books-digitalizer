"""Book sources: where raw TIFF frames are read from.

``build_source`` builds the backend from the ``[source]`` block of
``pipeline.toml``. The scans are a filesystem mount (the gateway's ``raw-scans``
export); there is no remote backend anymore.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.sources.base import AbstractBookSource
from evilflowers_books_digitalizer.sources.filesystem import FilesystemBookSource

__all__ = [
    "AbstractBookSource",
    "FilesystemBookSource",
    "build_source",
]


def build_source(source_config: dict[str, Any], key: str) -> AbstractBookSource:
    """Construct the source for one faculty ``key``.

    ``source_config`` is the ``[source]`` table from ``pipeline.toml``::

        [source]
        backend = "filesystem"            # the only backend
        root = "/mnt/digital-library/raw-scans"
        stage_mode = "symlink"            # symlink (zero-copy) | copy | hardlink
        [source.paths]                    # faculty key -> path under root
        fad = "fad/FA-skeny"
    """
    backend = source_config.get("backend", "filesystem")
    if backend != "filesystem":
        raise ValueError(f"unknown source backend: {backend!r} (only 'filesystem' exists)")
    root = Path(source_config["root"]).expanduser()
    rel = source_config.get("paths", {}).get(key, key)
    return FilesystemBookSource(key, root / rel,
                                stage_mode=source_config.get("stage_mode", "symlink"))
