"""Adapter registry: a project's config names its adapters, this builds them.

Open/closed: a new detector, recognizer, metadata source or catalog is one entry
in a table below (or a :func:`register` call from a plugin), never an edit of
the workers. Heavy adapters (YOLO, VLM, the catalog client) import their
dependencies inside their factories, so building the registry is cheap.
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
    FormulaRecognizer,
    ImageOps,
    LayoutDetector,
    MetadataProvider,
    OcrEngine,
    PageCleaner,
    PortError,
    Renderer,
    ScanStore,
)

type Factory[T] = Callable[[StudioConfig, ProjectConfig], T]

#: module the publish agent provides; it must expose ``build_publisher(studio, project)``
CATALOG_MODULE = "evilflowers_books_digitalizer.studio.adapters.catalog"


@dataclass
class Adapters:
    """Everything a worker needs for one project, one field per port."""

    scans: ScanStore
    artifacts: ArtifactStore
    cleaner: PageCleaner
    image_ops: ImageOps
    ocr: OcrEngine
    detectors: list[LayoutDetector]
    formulas: FormulaRecognizer
    renderer: Renderer
    metadata: MetadataProvider
    publisher: CatalogPublisher | None


def _settings(project: ProjectConfig, key: str) -> dict[str, Any]:
    return dict(project.pipeline.get(key, {}))


def output_dpi(project: ProjectConfig) -> int:
    """Resolution of cleaned pages (ScanTailor ``output_dpi``, falling back to ``dpi``)."""
    st = _settings(project, "scantailor")
    return int(st.get("output_dpi") or st.get("dpi") or 600)


# ---------------------------------------------------------------- layout detectors
def _heuristic(cfg: StudioConfig, project: ProjectConfig) -> LayoutDetector:
    from evilflowers_books_digitalizer.studio.adapters.layout import HeuristicLayout

    return HeuristicLayout(**_settings(project, "heuristic"))


def _doclayout(cfg: StudioConfig, project: ProjectConfig) -> LayoutDetector:
    """DocLayout-YOLO (ONNX, CPU); ``[pipeline.layout]`` model / conf / iou / imgsz / threads.

    The model file is checked on the first ``detect``, not here: building adapters
    must work on hosts (the API) that never run layout detection.
    """
    from evilflowers_books_digitalizer.studio.adapters.layout import OnnxLayout

    settings = _settings(project, "layout")
    known = {"model", "conf", "iou", "imgsz", "threads"}
    if unknown := set(settings) - known:
        raise PortError(f"project {project.slug}: unknown [pipeline.layout] keys "
                        f"{sorted(unknown)} (known: {sorted(known)})")
    return OnnxLayout(**settings)


# ---------------------------------------------------------------- formula recognizers
def _no_formula(cfg: StudioConfig, project: ProjectConfig) -> FormulaRecognizer:
    from evilflowers_books_digitalizer.studio.adapters.formula import NoFormula

    return NoFormula()


def _paddle_vl(cfg: StudioConfig, project: ProjectConfig) -> FormulaRecognizer:
    from evilflowers_books_digitalizer.reflow import VlmSettings
    from evilflowers_books_digitalizer.studio.adapters.formula import VlmSidecar

    s = _settings(project, "vlm")
    if not s.get("python") or not s.get("model"):
        raise PortError(f"project {project.slug}: [pipeline.vlm] python and model are required")
    return VlmSidecar(VlmSettings(python=Path(s["python"]), model=Path(s["model"]),
                                  device=s.get("device", "auto"),
                                  timeout_s=int(s.get("timeout_s", 6 * 3600))))


def _paddle_vl_http(cfg: StudioConfig, project: ProjectConfig) -> FormulaRecognizer:
    from evilflowers_books_digitalizer.studio.adapters.formula.http import (
        HttpFormulaRecognizer,
        HttpVlmSettings,
    )

    return HttpFormulaRecognizer(HttpVlmSettings.from_mapping(_settings(project, "vlm")))


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


LAYOUT_DETECTORS: dict[str, Factory[LayoutDetector]] = {
    "heuristic": _heuristic, "doclayout": _doclayout, "yolo": _doclayout}
FORMULA_RECOGNIZERS: dict[str, Factory[FormulaRecognizer]] = {
    "none": _no_formula, "paddleocr-vl": _paddle_vl, "paddleocr-vl-http": _paddle_vl_http}
METADATA_PROVIDERS: dict[str, Factory[MetadataProvider]] = {
    "excel": _excel, "opac": _opac, "ocr": _ocr_extract, "ocr-extract": _ocr_extract}
PUBLISHERS: dict[str, Factory[CatalogPublisher | None]] = {
    "none": _no_publisher, "evilflowers": _evilflowers}

_TABLES: dict[str, dict[str, Any]] = {
    "layout": LAYOUT_DETECTORS, "formula": FORMULA_RECOGNIZERS,
    "metadata": METADATA_PROVIDERS, "publisher": PUBLISHERS,
}


def register(port: str, name: str, factory: Factory[Any]) -> None:
    """Add an adapter by name (``port`` = layout | formula | metadata | publisher)."""
    _TABLES[port][name] = factory


def _pick[T](table: dict[str, Factory[T]], port: str, name: str) -> Factory[T]:
    try:
        return table[name]
    except KeyError:
        raise PortError(f"unknown {port} adapter {name!r} (known: {', '.join(table)})") from None


# ---------------------------------------------------------------- the bundle
def build_adapters(cfg: StudioConfig, project: ProjectConfig) -> Adapters:
    from evilflowers_books_digitalizer.reflow import ReflowSettings
    from evilflowers_books_digitalizer.studio.adapters.artifacts import IaArtifactStore
    from evilflowers_books_digitalizer.studio.adapters.imaging import (
        PillowImageOps,
        ScanTailorCleaner,
    )
    from evilflowers_books_digitalizer.studio.adapters.metadata import CompositeMetadata
    from evilflowers_books_digitalizer.studio.adapters.ocr import TesseractOcr
    from evilflowers_books_digitalizer.studio.adapters.render import StudioRenderer
    from evilflowers_books_digitalizer.studio.adapters.scans import FilesystemScanStore

    providers = project.metadata.get("providers", ["excel", "opac"] if
                                     project.metadata.get("excel_path") else ["opac"])
    reflow = _settings(project, "reflow")
    return Adapters(
        scans=FilesystemScanStore(project.scans_root, project.book_pattern, project.frames_glob),
        artifacts=IaArtifactStore(cfg.artifacts_root, project.slug),
        cleaner=ScanTailorCleaner.from_settings(_settings(project, "scantailor")),
        image_ops=PillowImageOps(),
        ocr=TesseractOcr(**_settings(project, "tesseract")),
        detectors=[_pick(LAYOUT_DETECTORS, "layout", n)(cfg, project)
                   for n in project.layout_detectors],
        formulas=_pick(FORMULA_RECOGNIZERS, "formula", project.formula_recognizer)(cfg, project),
        renderer=StudioRenderer(project.pipeline, dpi=output_dpi(project),
                                reflow=ReflowSettings(pandoc=reflow.get("pandoc", "pandoc"),
                                                      hunspell_dict=reflow.get("hunspell_dict"))),
        metadata=CompositeMetadata(project, [_pick(METADATA_PROVIDERS, "metadata", n)(cfg, project)
                                             for n in providers]),
        publisher=_pick(PUBLISHERS, "publisher", project.publish.target)(cfg, project),
    )
