"""High-level cover renderer: metadata + config -> a saved cover image."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image

from evilflowers_books_digitalizer.covers.fonts import BUNDLED
from evilflowers_books_digitalizer.covers.palette import resolve_palette
from evilflowers_books_digitalizer.covers.templates import CoverSpec, get_template
from evilflowers_books_digitalizer.metadata.models import BookMetadata

logger = logging.getLogger(__name__)

DEFAULT_SIZE = (1200, 1697)  # ~ISO 1:√2 book proportion


class CoverRenderer:
    """Render stylish book covers from catalog metadata.

    Driven by the ``[cover]`` config block: template name, canvas size, output
    format/quality, font roles, and per-faculty palette overrides.
    """

    def __init__(
        self,
        template: str = "banner",
        size: tuple[int, int] = DEFAULT_SIZE,
        fmt: str = "JPEG",
        quality: int = 88,
        fonts: dict[str, str] | None = None,
        palette_overrides: dict[str, dict[str, str]] | None = None,
    ):
        self.template = get_template(template)
        self.size = size
        self.fmt = fmt.upper()
        self.quality = quality
        self.fonts = {**BUNDLED, **(fonts or {})}
        self.palette_overrides = palette_overrides or {}

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "CoverRenderer":
        size = cfg.get("size")
        return cls(
            template=cfg.get("template", "banner"),
            size=tuple(size) if size else DEFAULT_SIZE,
            fmt=cfg.get("format", "JPEG"),
            quality=cfg.get("quality", 88),
            fonts=cfg.get("fonts"),
            palette_overrides=cfg.get("palette"),
        )

    def render(self, metadata: BookMetadata | dict) -> Image.Image:
        meta = metadata if isinstance(metadata, BookMetadata) else BookMetadata(**metadata)
        spec = CoverSpec(
            title=meta.title,
            subtitle=meta.subtitle,
            authors=meta.author_line,
            year=meta.year,
            publisher=meta.publisher,
            isbn=meta.isbn,
            faculty=meta.faculty or "",
        )
        palette = resolve_palette(meta.faculty, self.palette_overrides)
        return self.template.render(spec, palette, self.size, self.fonts)

    @property
    def suffix(self) -> str:
        return ".png" if self.fmt == "PNG" else ".jpg"

    def render_to_file(self, metadata: BookMetadata | dict, path: Path) -> Path:
        img = self.render(metadata)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.fmt == "JPEG":
            img.save(path, "JPEG", quality=self.quality, optimize=True)
        else:
            img.save(path, self.fmt)
        return path
