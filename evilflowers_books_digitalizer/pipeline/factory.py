"""Build the production pipeline from ``configs/pipeline.toml``.

The TOML config is the contract between the experimentation notebooks (which
write it, see notebook 03) and batch processing (which consumes it) — settings
changes don't require code changes.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from evilflowers_books_digitalizer.cache import LocalCache
from evilflowers_books_digitalizer.config import PROJECT_ROOT
from evilflowers_books_digitalizer.pipeline.base import Pipeline
from evilflowers_books_digitalizer.pipeline.steps import (
    AssemblePdf,
    DetectLanguage,
    DownloadBook,
    EnrichPdfMetadata,
    OcrPdf,
    PreprocessScans,
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

    preprocess = config.get("preprocess", {})
    ocr = dict(config.get("ocr", {}))

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
