"""Build the production pipeline from ``configs/pipeline.toml``.

The TOML config is the contract between the experimentation notebooks (which
write it, see notebook 03) and batch processing (which consumes it) — settings
changes don't require code changes.
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
from evilflowers_books_digitalizer.pipeline.steps import (
    AssemblePdf,
    AttachMetadata,
    DetectLanguage,
    DocResEnhance,
    DownloadBook,
    EnrichPdfMetadata,
    FinalizePdf,
    GenerateCover,
    MrcPdf,
    OcrPdf,
    PreprocessScans,
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

    engine = config.get("pipeline", {}).get("engine", "legacy")
    if engine == "scantailor_mrc":
        steps = _build_scantailor_mrc(source, cache, config)
    elif engine == "legacy":
        steps = _build_legacy(source, cache, config, jobs)
    else:
        raise ValueError(f"unknown pipeline engine: {engine!r}")

    steps = _append_metadata_and_cover(steps, config, catalog)
    return Pipeline(steps)


def _append_metadata_and_cover(
    steps: list[PipelineStep], config: dict, catalog: MetadataCatalog | None
) -> list[PipelineStep]:
    """Insert AttachMetadata (before enrich) and GenerateCover (after enrich).

    Keeps the engine builders focused on imaging; the enrichment tail is shared.
    """
    meta_cfg = config.get("metadata", {})
    cover_cfg = config.get("cover", {})
    catalog_cfg = config.get("catalog", {})

    if catalog is not None and meta_cfg.get("enabled", False):
        attach = AttachMetadata(catalog, faculty_map=meta_cfg.get("faculty_names"))
        enrich_idx = next(
            (i for i, s in enumerate(steps) if s.name == "enrich"), len(steps)
        )
        steps.insert(enrich_idx, attach)

    if cover_cfg.get("enabled", False):
        renderer = CoverRenderer.from_config(cover_cfg)
        cover = GenerateCover(
            renderer,
            source=cover_cfg.get("source", "opac_then_generated"),
            min_px=cover_cfg.get("min_px", 80),
        )
        # after enrich if present, else append
        enrich_idx = next(
            (i for i, s in enumerate(steps) if s.name == "enrich"), len(steps) - 1
        )
        steps.insert(enrich_idx + 1, cover)

    # Catalog manifest goes last — it needs the final PDF and the cover.
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
    return steps


def _build_legacy(
    source: AbstractBookSource, cache: LocalCache, config: dict, jobs: int | None
) -> list[PipelineStep]:
    """OpenCV preprocess -> img2pdf -> OCRmyPDF (the original pipeline)."""
    preprocess = config.get("preprocess", {})
    # OCRmyPDF knobs live in [legacy_ocr]; the OCR language is shared in [ocr]
    ocr = dict(config.get("legacy_ocr", {}))
    ocr["language"] = config.get("ocr", {}).get("language", "auto")

    steps: list[PipelineStep] = [
        DownloadBook(source, cache),
        PreprocessScans(
            split=preprocess.get("split", True),
            do_deskew=preprocess.get("deskew", True),
            whiten=preprocess.get("whiten", True),
            color_mode=preprocess.get("color_mode", "keep"),
        ),
    ]
    # language = "auto" -> detect per book; otherwise pass the value through
    if ocr.get("language") == "auto":
        ocr["language"] = None
        steps.append(DetectLanguage())
    steps += [AssemblePdf(), OcrPdf(jobs=jobs, **ocr), EnrichPdfMetadata()]
    return steps


def _build_scantailor_mrc(
    source: AbstractBookSource, cache: LocalCache, config: dict
) -> list[PipelineStep]:
    """ScanTailor cleanup [-> DocRes] -> Tesseract hOCR -> MRC PDF (notebook 06)."""
    st = config.get("scantailor", {})
    docres = config.get("docres", {})
    mrc = dict(config.get("mrc", {}))
    finalize = config.get("finalize", {})
    language = config.get("ocr", {}).get("language", "auto")

    steps: list[PipelineStep] = [
        DownloadBook(source, cache),
        ScanTailorScans(
            binary=st.get("binary", "scantailor-deviant-cli"),
            color_mode=st.get("color_mode", "mixed"),
            margins_mm=st.get("margins_mm", 8.0),
            dewarping=st.get("dewarping", "auto"),
            normalize_illumination=st.get("normalize_illumination", True),
            despeckle=st.get("despeckle", "cautious"),
            dpi=st.get("dpi", 300),
            output_dpi=st.get("output_dpi"),
        ),
    ]
    if docres.get("enabled", False):
        # env overrides let the Docker image point at its baked-in DocRes
        steps.append(
            DocResEnhance(
                repo=docres.get(
                    "repo",
                    os.environ.get(
                        "EVILFLOWERS_DOCRES_REPO", "~/.local/share/evilflowers-tools/DocRes"
                    ),
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
        )
    if language == "auto":
        language = None
        steps.append(DetectLanguage())
    steps += [
        MrcPdf(
            language=language,
            # pages carry ScanTailor's output dpi (mask/page geometry must match)
            dpi=st.get("output_dpi") or st.get("dpi", 300),
            mask_compression=mrc.get("mask_compression", "jbig2"),
            jpeg2000_encoder=mrc.get("jpeg2000_encoder", "pillow"),
            bg_downsample=mrc.get("bg_downsample"),
        ),
        EnrichPdfMetadata(),
    ]
    if finalize.get("enabled", True):
        steps.append(
            FinalizePdf(
                bookmarks=finalize.get("bookmarks", True),
                page_labels=finalize.get("page_labels", True),
                linearize=finalize.get("linearize", True),
            )
        )
    return steps
