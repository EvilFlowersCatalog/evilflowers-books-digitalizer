"""Build the production pipeline from ``configs/pipeline.toml``.

The TOML config is the contract between the experimentation notebooks (which
write it, see notebook 03) and batch processing (which consumes it) — settings
changes don't require code changes.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from evilflowers_books_digitalizer.cache import LocalCache
from evilflowers_books_digitalizer.config import PROJECT_ROOT
from evilflowers_books_digitalizer.pipeline.base import Pipeline
from evilflowers_books_digitalizer.pipeline.steps import (
    AssemblePdf,
    DetectLanguage,
    DocResEnhance,
    DownloadBook,
    EnrichPdfMetadata,
    FinalizePdf,
    MrcPdf,
    OcrPdf,
    PreprocessScans,
    ScanTailorScans,
)
from evilflowers_books_digitalizer.webdav import BookSource

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "pipeline.toml"


def build_pipeline(
    source: BookSource,
    cache: LocalCache,
    config_path: Path | None = None,
    jobs: int | None = None,
) -> Pipeline:
    """Standard digitalization pipeline with settings from ``pipeline.toml``."""
    path = config_path or DEFAULT_CONFIG_PATH
    with path.open("rb") as fh:
        config = tomllib.load(fh)

    engine = config.get("pipeline", {}).get("engine", "legacy")
    if engine == "scantailor_mrc":
        return _build_scantailor_mrc(source, cache, config)
    if engine == "legacy":
        return _build_legacy(source, cache, config, jobs)
    raise ValueError(f"unknown pipeline engine: {engine!r}")


def _build_legacy(
    source: BookSource, cache: LocalCache, config: dict, jobs: int | None
) -> Pipeline:
    """OpenCV preprocess -> img2pdf -> OCRmyPDF (the original pipeline)."""
    preprocess = config.get("preprocess", {})
    # OCRmyPDF knobs live in [legacy_ocr]; the OCR language is shared in [ocr]
    ocr = dict(config.get("legacy_ocr", {}))
    ocr["language"] = config.get("ocr", {}).get("language", "auto")

    steps = [
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
    return Pipeline(steps)


def _build_scantailor_mrc(source: BookSource, cache: LocalCache, config: dict) -> Pipeline:
    """ScanTailor cleanup [-> DocRes] -> Tesseract hOCR -> MRC PDF (notebook 06)."""
    st = config.get("scantailor", {})
    docres = config.get("docres", {})
    mrc = dict(config.get("mrc", {}))
    finalize = config.get("finalize", {})
    language = config.get("ocr", {}).get("language", "auto")

    steps: list = [
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
    return Pipeline(steps)
