"""Adapter registry: a project's config names its adapters, this builds them.

Open/closed: a new metadata source or catalog is one entry in a table below (or a
:func:`register` call from a plugin), never an edit of the workers. Heavy adapters (the
catalog client) import their dependencies inside their factories, so building the registry
is cheap. OCR, layout, formulas and rendering are not adapters any more: they are the
services' (evilflowers-books-digitalizer#6).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.studio.config import ProjectConfig, StudioConfig
from evilflowers_books_digitalizer.studio.ports import (
    ArtifactStore,
    CatalogPublisher,
    LeafPlanner,
    MetadataProvider,
    PortError,
    ScanStore,
)

type Factory[T] = Callable[[StudioConfig, ProjectConfig], T]

#: module the publish agent provides; it must expose ``build_publisher(studio, project)``
CATALOG_MODULE = "evilflowers_books_digitalizer.studio.adapters.catalog"


@dataclass
class Adapters:
    """Everything the orchestrator needs for one project, one field per port."""

    scans: ScanStore
    artifacts: ArtifactStore
    planner: LeafPlanner
    metadata: MetadataProvider
    publisher: CatalogPublisher | None


def _settings(project: ProjectConfig, key: str) -> dict[str, Any]:
    return dict(project.pipeline.get(key, {}))


def output_dpi(project: ProjectConfig) -> int:
    """Resolution of cleaned pages (ScanTailor ``output_dpi``, falling back to ``dpi``)."""
    st = _settings(project, "scantailor")
    return int(st.get("output_dpi") or st.get("dpi") or 600)


# ---------------------------------------------------------------- metadata providers
def _excel(cfg: StudioConfig, project: ProjectConfig) -> MetadataProvider:
    from evilflowers_books_digitalizer.studio.adapters.metadata import ExcelMetadata

    path = project.metadata.get("excel_path")
    if not path:
        raise PortError(f"project {project.slug}: [metadata] excel_path is required for 'excel'")
    return ExcelMetadata(project, Path(path), columns=project.metadata.get("columns"),
                         key_field=project.metadata.get("key_field", "isbn"))


def _opac(cfg: StudioConfig, project: ProjectConfig) -> MetadataProvider:
    from evilflowers_books_digitalizer.studio.adapters.metadata import OpacMetadata

    return OpacMetadata(project, cache_dir=cfg.cache_dir / "isbn",
                        providers=project.metadata.get("opac_providers", ("stu_opac", "openlibrary")))


def _ocr_extract(cfg: StudioConfig, project: ProjectConfig) -> MetadataProvider:
    from evilflowers_books_digitalizer.studio.adapters.metadata import OcrExtractMetadata

    return OcrExtractMetadata(project)


# ---------------------------------------------------------------- catalog publishers
def _no_publisher(cfg: StudioConfig, project: ProjectConfig) -> CatalogPublisher | None:
    return None


def _evilflowers(cfg: StudioConfig, project: ProjectConfig) -> CatalogPublisher | None:
    try:
        module = importlib.import_module(CATALOG_MODULE)
    except ImportError as exc:
        raise PortError(f"publisher 'evilflowers' unavailable: {exc}") from exc
    # artifacts_root is the served root: external_url = files_base_url + path below it
    publisher: CatalogPublisher = module.build_publisher(project.publish,
                                                         artifacts_root=cfg.artifacts_root)
    return publisher


METADATA_PROVIDERS: dict[str, Factory[MetadataProvider]] = {
    "excel": _excel, "opac": _opac, "ocr": _ocr_extract, "ocr-extract": _ocr_extract}
PUBLISHERS: dict[str, Factory[CatalogPublisher | None]] = {
    "none": _no_publisher, "evilflowers": _evilflowers}

_TABLES: dict[str, dict[str, Any]] = {"metadata": METADATA_PROVIDERS, "publisher": PUBLISHERS}


def register(port: str, name: str, factory: Factory[Any]) -> None:
    """Add an adapter by name (``port`` = metadata | publisher)."""
    _TABLES[port][name] = factory


def _pick[T](table: dict[str, Factory[T]], port: str, name: str) -> Factory[T]:
    try:
        return table[name]
    except KeyError:
        raise PortError(f"unknown {port} adapter {name!r} (known: {', '.join(table)})") from None


# ---------------------------------------------------------------- the bundle
def build_adapters(cfg: StudioConfig, project: ProjectConfig) -> Adapters:
    from evilflowers_books_digitalizer.studio.adapters.artifacts import IaArtifactStore
    from evilflowers_books_digitalizer.studio.adapters.imaging import SpreadPlanner
    from evilflowers_books_digitalizer.studio.adapters.metadata import CompositeMetadata
    from evilflowers_books_digitalizer.studio.adapters.scans import FilesystemScanStore

    providers = project.metadata.get("providers", ["excel", "opac"] if
                                     project.metadata.get("excel_path") else ["opac"])
    return Adapters(
        scans=FilesystemScanStore(project.scans_root, project.book_pattern, project.frames_glob),
        artifacts=IaArtifactStore(cfg.artifacts_root, project.slug),
        planner=SpreadPlanner(split=bool(_settings(project, "scantailor").get("split", True))),
        metadata=CompositeMetadata(project, [_pick(METADATA_PROVIDERS, "metadata", n)(cfg, project)
                                             for n in providers]),
        publisher=_pick(PUBLISHERS, "publisher", project.publish.target)(cfg, project),
    )
