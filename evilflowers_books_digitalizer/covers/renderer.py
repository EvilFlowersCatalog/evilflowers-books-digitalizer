"""High-level cover renderer: metadata + config -> a saved cover image."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image

from evilflowers_books_digitalizer.covers.fonts import BUNDLED, resolve_font_path
from evilflowers_books_digitalizer.covers.logos import LogoLibrary
from evilflowers_books_digitalizer.covers.palette import resolve_palette
from evilflowers_books_digitalizer.covers.resources import ResourceResolver
from evilflowers_books_digitalizer.covers.templates import CoverSpec, get_template
from evilflowers_books_digitalizer.metadata.models import BookMetadata

logger = logging.getLogger(__name__)

DEFAULT_SIZE = (1200, 1697)  # ~ISO 1:√2 book proportion


class CoverRenderer:
    """Render neutral, STU-branded book covers from catalog metadata.

    Driven by the ``[cover]`` config block: template name, canvas size, output
    format/quality, an optional ``assets_dir`` for static files, font roles,
    per-faculty logo overrides, and per-faculty palette (accent) overrides.
    """

    def __init__(
        self,
        template: str = "stu",
        size: tuple[int, int] = DEFAULT_SIZE,
        fmt: str = "JPEG",
        quality: int = 90,
        fonts: dict[str, str] | None = None,
        palette_overrides: dict[str, dict[str, str]] | None = None,
        assets_dir: str | list[str] | None = None,
        logos: dict[str, str] | None = None,
    ):
        self.template = get_template(template)
        self.size = size
        self.fmt = fmt.upper()
        self.quality = quality
        self.palette_overrides = palette_overrides or {}
        self.resolver = ResourceResolver(assets_dir)
        self.logos = LogoLibrary(self.resolver, overrides=logos)
        self.fonts = self._resolve_fonts(fonts or {})

    def _resolve_fonts(self, overrides: dict[str, str]) -> dict[str, str]:
        """Merge bundled roles with config, resolving each to a real path."""
        merged = {**BUNDLED, **overrides}
        resolved: dict[str, str] = {}
        for role, name in merged.items():
            path = self.resolver.find("fonts", name)
            resolved[role] = str(path) if path else resolve_font_path(name)
        return resolved

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "CoverRenderer":
        size = cfg.get("size")
        return cls(
            template=cfg.get("template", "stu"),
            size=tuple(size) if size else DEFAULT_SIZE,
            fmt=cfg.get("format", "JPEG"),
            quality=cfg.get("quality", 90),
            fonts=cfg.get("fonts"),
            palette_overrides=cfg.get("palette"),
            assets_dir=cfg.get("assets_dir"),
            logos=cfg.get("logos"),
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
        logo = self.logos.load(meta.faculty)
        return self.template.render(spec, palette, self.size, self.fonts, logo)

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
