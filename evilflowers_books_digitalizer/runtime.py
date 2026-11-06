"""Runtime configuration resolved from ``pipeline.toml``.

This is the single entry point the batch worker, runner and CLI use to turn the
TOML config into ready-to-use objects: resolved cache/output dirs, the selected
source backend, and the (cached) metadata catalog.

Which file is read (first match wins):

1. an explicit path (``--config``),
2. the ``EVILFLOWERS_DIGITALIZER_CONFIG`` environment variable,
3. ``configs/pipeline.toml`` in a source checkout.

Relative paths in the TOML (``[paths]`` entries, ``[metadata].excel_path`` and
``[source].root``) resolve against the **directory of the config file**, never against the package
location, so an installed package (Docker image, k8s Job) behaves the same as a
checkout. Absolute paths are used as given.

Unlike :func:`config.load_settings` (which needs ``credentials.toml`` for the
WebDAV backend), this loader works credential-free for the production
filesystem backend.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.config import PROJECT_ROOT
from evilflowers_books_digitalizer.metadata.catalog import MetadataCatalog
from evilflowers_books_digitalizer.pipeline.layout import Layout, layout_from_config

logger = logging.getLogger(__name__)

#: Environment variable naming the config file (overridden by ``--config``).
CONFIG_ENV = "EVILFLOWERS_DIGITALIZER_CONFIG"

#: Fallback for a source checkout; meaningless inside an installed package.
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "pipeline.toml"


def resolve_config_path(config_path: Path | str | None = None) -> Path:
    """The config file to read: ``config_path`` > ``$EVILFLOWERS_DIGITALIZER_CONFIG`` > default."""
    if config_path:
        return Path(config_path).expanduser()
    env_value = os.environ.get(CONFIG_ENV, "").strip()
    if env_value:
        return Path(env_value).expanduser()
    return DEFAULT_CONFIG_PATH


def resolve_path(value: Path | str, base_dir: Path) -> Path:
    """``value`` as an absolute path; relative values resolve against ``base_dir``."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path


@dataclass
class RuntimeConfig:
    """Everything the runtime needs, resolved from one TOML file."""

    config: dict[str, Any]
    config_path: Path
    cache_dir: Path
    output_dir: Path

    @property
    def config_dir(self) -> Path:
        """Directory relative config paths resolve against.

        ``absolute()`` rather than ``resolve()``: a k8s ConfigMap file is a
        symlink into a hidden ``..data`` directory, and following it would move
        the base away from the mount point.
        """
        return self.config_path.absolute().parent

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
    def catalog(self) -> dict[str, Any]:
        """The ``[catalog]`` block — EvilFlowers Catalog import settings."""
        return self.config.get("catalog", {})

    @property
    def enrich(self) -> dict[str, Any]:
        """The ``[enrich]`` block — OCR metadata extraction + ISBN lookup."""
        return self.config.get("enrich", {})

    @property
    def layout(self) -> Layout:
        """Output-tree layout (``[render].layout``)."""
        return layout_from_config(self.config)

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
    """Read and resolve the config (see the module docstring for precedence)."""
    path = resolve_config_path(config_path)
    with path.open("rb") as fh:
        config = tomllib.load(fh)
    base_dir = path.absolute().parent

    paths = config.get("paths", {})
    for section, key in (("metadata", "excel_path"), ("source", "root")):
        table = config.get(section)
        if isinstance(table, dict) and table.get(key):
            table[key] = str(resolve_path(table[key], base_dir))

    return RuntimeConfig(
        config=config,
        config_path=path,
        cache_dir=resolve_path(paths.get("cache_dir", ".cache"), base_dir),
        output_dir=resolve_path(paths.get("output_dir", "output"), base_dir),
    )


@lru_cache(maxsize=4)
def _load_catalog_cached(
    excel_path: str, sheet: Any, key_field: str, columns_items: tuple[tuple[str, str], ...]
) -> MetadataCatalog:
    return MetadataCatalog.from_excel(
        excel_path, sheet=sheet, columns=dict(columns_items), key_field=key_field
    )


def build_catalog(
    metadata_config: dict[str, Any], *, base_dir: Path | None = None
) -> MetadataCatalog | None:
    """Build the catalog from the ``[metadata]`` block, or ``None`` if disabled.

    :func:`load_runtime` has already made ``excel_path`` absolute; ``base_dir``
    only matters for a hand-built config dict (default: the working directory).

    Cached by (path, sheet, key_field, columns) so repeated book runs in one
    process don't re-parse the spreadsheet.
    """
    if not metadata_config.get("enabled", False):
        return None
    excel = metadata_config.get("excel_path")
    if not excel:
        logger.warning("[metadata] enabled but no excel_path set — skipping catalog")
        return None
    excel_path = resolve_path(excel, base_dir or Path.cwd())
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
