"""Runtime configuration resolved from ``configs/pipeline.toml``.

This is the single entry point the batch worker, CLI and Prefect flows use to
turn the TOML config into ready-to-use objects: resolved cache/output dirs, the
selected source backend, and the (cached) metadata catalog. Paths in the TOML
are resolved relative to the project root.

Unlike :func:`config.load_settings` (which needs ``credentials.toml`` for the
WebDAV backend), this loader works credential-free for the production
filesystem backend.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.config import PROJECT_ROOT
from evilflowers_books_digitalizer.metadata.catalog import MetadataCatalog

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "pipeline.toml"


def _resolve(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


@dataclass
class RuntimeConfig:
    """Everything the runtime needs, resolved from one TOML file."""

    config: dict[str, Any]
    config_path: Path
    cache_dir: Path
    output_dir: Path

    @property
    def source(self) -> dict[str, Any]:
        return self.config.get("source", {})

    @property
    def metadata(self) -> dict[str, Any]:
        return self.config.get("metadata", {})

    @property
    def cover(self) -> dict[str, Any]:
        return self.config.get("cover", {})

    @property
    def orchestration(self) -> dict[str, Any]:
        return self.config.get("orchestration", {})

    @property
    def source_keys(self) -> list[str]:
        """Faculty keys to process, from [orchestration].sources or [source.paths]."""
        configured = self.orchestration.get("sources")
        if configured:
            return list(configured)
        return list(self.source.get("paths", {}).keys())

    def faculty_names(self) -> dict[str, str]:
        return self.metadata.get("faculty_names", {})


def load_runtime(config_path: Path | str | None = None) -> RuntimeConfig:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with path.open("rb") as fh:
        config = tomllib.load(fh)
    paths = config.get("paths", {})
    return RuntimeConfig(
        config=config,
        config_path=path,
        cache_dir=_resolve(paths.get("cache_dir", ".cache")),
        output_dir=_resolve(paths.get("output_dir", "output")),
    )


@lru_cache(maxsize=4)
def _load_catalog_cached(
    excel_path: str, sheet: Any, key_field: str, columns_items: tuple
) -> MetadataCatalog:
    return MetadataCatalog.from_excel(
        excel_path, sheet=sheet, columns=dict(columns_items), key_field=key_field
    )


def build_catalog(metadata_config: dict[str, Any]) -> MetadataCatalog | None:
    """Build the catalog from the ``[metadata]`` block, or ``None`` if disabled.

    Cached by (path, sheet, key_field, columns) so repeated book runs in one
    process don't re-parse the spreadsheet.
    """
    if not metadata_config.get("enabled", False):
        return None
    excel = metadata_config.get("excel_path")
    if not excel:
        logger.warning("[metadata] enabled but no excel_path set — skipping catalog")
        return None
    excel_path = _resolve(excel)
    if not excel_path.exists():
        logger.warning("catalog file %s not found — books will use stub titles", excel_path)
        return MetadataCatalog([])  # empty -> every book gets a stub
    columns = metadata_config.get("columns", {})
    return _load_catalog_cached(
        str(excel_path),
        metadata_config.get("sheet", 0),
        metadata_config.get("key_field", "isbn"),
        tuple(sorted(columns.items())),
    )
