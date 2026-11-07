"""Catalog publishers (``ports.CatalogPublisher``), chosen by ``publish.target``.

``build_publisher`` is the one place a shell (worker, CLI) turns project config
into a publisher, so adding a target means one new ``case`` here.
"""

from __future__ import annotations

from pathlib import Path

from evilflowers_books_digitalizer.studio.adapters.catalog.client import (
    AcquisitionDeleter,
    StudioCatalogApi,
    TransportDeleter,
    delete_acquisition,
)
from evilflowers_books_digitalizer.studio.adapters.catalog.null import NullPublisher
from evilflowers_books_digitalizer.studio.adapters.catalog.publisher import (
    EvilFlowersPublisher,
    PublishConflict,
    PublishError,
)
from evilflowers_books_digitalizer.studio.config import PublishConfig
from evilflowers_books_digitalizer.studio.ports import CatalogPublisher, PortError


def build_publisher(config: PublishConfig, *, artifacts_root: Path | None = None,
                    client: StudioCatalogApi | None = None) -> CatalogPublisher:
    """``artifacts_root`` = the served root of the output tree (``external_url`` paths,
    kept for when the catalog supports them; publishing refuses ``files_base_url`` today)."""
    match config.target:
        case "none" | "":
            return NullPublisher()
        case "evilflowers":
            return EvilFlowersPublisher(config, artifacts_root=artifacts_root, client=client)
        case other:
            raise PortError(f"unknown publish target {other!r} (known: none, evilflowers)")


__all__ = [
    "AcquisitionDeleter",
    "EvilFlowersPublisher",
    "NullPublisher",
    "PublishConflict",
    "PublishError",
    "StudioCatalogApi",
    "TransportDeleter",
    "build_publisher",
    "delete_acquisition",
]
