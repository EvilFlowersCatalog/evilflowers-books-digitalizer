"""Build the digitalization pipeline from ``configs/pipeline.toml``.

The TOML config is the contract between the experimentation notebooks (which
write it) and batch processing (which consumes it) — tuning the pipeline does
not require code changes.

One engine: **ScanTailor → (DocRes) → Tesseract hOCR → MRC render → enrich →
(metadata/cover) → finalize → PDF/A → (catalog manifest)**. The render step
emits one PDF per profile (distribution + archival); see
:mod:`~evilflowers_books_digitalizer.pipeline.profiles`. The legacy OpenCV +
OCRmyPDF engine was removed — it had been frozen out of production since the
ScanTailor engine landed.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.cache import LocalCache
from evilflowers_books_digitalizer.config import PROJECT_ROOT
from evilflowers_books_digitalizer.covers.renderer import CoverRenderer
from evilflowers_books_digitalizer.metadata.catalog import MetadataCatalog
from evilflowers_books_digitalizer.pipeline.base import Pipeline, PipelineStep
from evilflowers_books_digitalizer.pipeline.profiles import profiles_from_config
from evilflowers_books_digitalizer.pipeline.steps import (
    AttachMetadata,
    DetectLanguage,
    DocResEnhance,
    DownloadBook,
    EnrichPdfMetadata,
    EnsurePdfA,
    FinalizePdf,
    GenerateCover,
    OcrPages,
    RenderPdf,
    ScanTailorScans,
    WriteCatalogManifest,
)
from evilflowers_books_digitalizer.sources.base import AbstractBookSource

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "pipeline.toml"


def build_pipeline(
    source: AbstractBookSource,
    cache: LocalCache,
    config_path: Path | None = None,
    jobs: int | None = None,
    *,
    config: dict[str, Any] | None = None,
    catalog: MetadataCatalog | None = None,
) -> Pipeline:
    """Standard digitalization pipeline with settings from ``pipeline.toml``.

    ``config`` (a pre-parsed dict) takes precedence over ``config_path`` so the
    batch worker can parse the TOML once and reuse it. ``catalog`` enables the
    metadata + cover tail when the ``[metadata]`` block is on.
    """
    if config is None:
        path = config_path or DEFAULT_CONFIG_PATH
        with path.open("rb") as fh:
            config = tomllib.load(fh)

    st = config.get("scantailor", {})
    docres = config.get("docres", {})
    meta_cfg = config.get("metadata", {})
    cover_cfg = config.get("cover", {})
    finalize_cfg = config.get("finalize", {})
    render_cfg = config.get("render", {})
    catalog_cfg = config.get("catalog", {})
    output_dpi = st.get("output_dpi") or st.get("dpi", 300)
    language = config.get("ocr", {}).get("language", "auto")

    steps: list[PipelineStep] = [DownloadBook(source, cache)]

    # Metadata is attached *before* rendering so RenderPdf can bake title/authors
    # into each PDF's XMP — that keeps the archival PDF/A valid (rewriting XMP
    # with pikepdf afterwards breaks conformance).
    if catalog is not None and meta_cfg.get("enabled", False):
        steps.append(AttachMetadata(catalog, faculty_map=meta_cfg.get("faculty_names")))

    steps.append(
        ScanTailorScans(
            binary=st.get("binary", "scantailor-deviant-cli"),
            color_mode=st.get("color_mode", "mixed"),
            margins_mm=st.get("margins_mm", 8.0),
            dewarping=st.get("dewarping", "auto"),
            normalize_illumination=st.get("normalize_illumination", True),
            despeckle=st.get("despeckle", "cautious"),
            dpi=st.get("dpi", 300),
            output_dpi=st.get("output_dpi"),
        )
    )
    if docres.get("enabled", False):
        steps.append(_build_docres(docres))
    if language == "auto":
        steps.append(DetectLanguage())
    steps.append(OcrPages(language=None if language == "auto" else language, dpi=output_dpi))
    steps.append(
        RenderPdf(
            profiles=profiles_from_config(config),
            dpi=output_dpi,
            layout=render_cfg.get("layout", "flat"),
        )
    )
    steps.append(EnrichPdfMetadata())

    if cover_cfg.get("enabled", False):
        renderer = CoverRenderer.from_config(cover_cfg)
        steps.append(
            GenerateCover(
                renderer,
                source=cover_cfg.get("source", "opac_then_generated"),
                min_px=cover_cfg.get("min_px", 80),
            )
        )
    if finalize_cfg.get("enabled", True):
        steps.append(
            FinalizePdf(
                bookmarks=finalize_cfg.get("bookmarks", True),
                page_labels=finalize_cfg.get("page_labels", True),
                linearize=finalize_cfg.get("linearize", True),
            )
        )
    steps.append(EnsurePdfA(validate=render_cfg.get("validate_pdfa", False)))

    if catalog_cfg.get("enabled", False) and catalog_cfg.get("catalog"):
        steps.append(
            WriteCatalogManifest(
                catalog_cfg["catalog"],
                default_language=catalog_cfg.get("default_language", "slk"),
                relation=catalog_cfg.get("relation", "open-access"),
                entry_config=catalog_cfg.get("entry_config"),
                author_name_order=catalog_cfg.get("author_name_order", "given_first"),
            )
        )
    return Pipeline(steps)


def _build_docres(docres: dict[str, Any]) -> DocResEnhance:
    """DocRes appearance enhancer, with env overrides for the Docker image."""
    return DocResEnhance(
        repo=docres.get(
            "repo",
            os.environ.get("EVILFLOWERS_DOCRES_REPO", "~/.local/share/evilflowers-tools/DocRes"),
        ),
        python=docres.get(
            "python",
            os.environ.get(
                "EVILFLOWERS_DOCRES_PYTHON",
                "~/.local/share/evilflowers-tools/venv-docres/bin/python",
            ),
        ),
        task=docres.get("task", "appearance"),
    )
