"""Colour palettes for generated covers, themed per faculty.

Each faculty gets a distinct, tasteful scheme; an unknown faculty falls back to
a neutral slate. Palettes are overridable from ``[cover.palette.<faculty>]`` in
``pipeline.toml`` so branding can be tuned without touching code.
"""

from __future__ import annotations

from dataclasses import dataclass

RGB = tuple[int, int, int]


def hex_to_rgb(value: str | RGB) -> RGB:
    if isinstance(value, (tuple, list)):
        return (int(value[0]), int(value[1]), int(value[2]))
    h = value.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def shade(color: RGB, factor: float) -> RGB:
    """Lighten (factor>1) or darken (factor<1) a colour, clamped to 0–255."""
    return tuple(max(0, min(255, round(c * factor))) for c in color)  # type: ignore[return-value]


@dataclass(frozen=True)
class Palette:
    """A cover colour scheme."""

    bg: RGB  # page background (light)
    accent: RGB  # banner / rules / author (saturated)
    text: RGB  # title / body on the light background (dark)
    banner_text: RGB = (255, 255, 255)  # text on the accent band
    muted: RGB = (120, 120, 120)  # footer / secondary lines

    @classmethod
    def from_config(cls, data: dict[str, str]) -> "Palette":
        base = dict(DEFAULT.__dict__)
        for key in ("bg", "accent", "text", "banner_text", "muted"):
            if key in data:
                base[key] = hex_to_rgb(data[key])
        return cls(**base)  # type: ignore[arg-type]


DEFAULT = Palette(bg=(244, 243, 239), accent=(51, 65, 85), text=(28, 30, 36))

#: Per-faculty defaults (lower-cased source key -> palette).
FACULTY_PALETTES: dict[str, Palette] = {
    "fad": Palette(bg=(245, 240, 234), accent=(168, 68, 42), text=(43, 30, 26)),  # clay
    "fei": Palette(bg=(238, 242, 248), accent=(31, 78, 140), text=(20, 30, 48)),  # blue
    "mtf": Palette(bg=(236, 244, 244), accent=(46, 110, 115), text=(22, 40, 41)),  # teal
    "sjf": Palette(bg=(245, 241, 234), accent=(180, 83, 9), text=(43, 33, 18)),  # amber
    "svf": Palette(bg=(238, 244, 239), accent=(47, 107, 60), text=(22, 41, 28)),  # green
}


def resolve_palette(
    faculty: str | None, overrides: dict[str, dict[str, str]] | None = None
) -> Palette:
    """Palette for a faculty key, applying any ``[cover.palette.*]`` overrides."""
    key = (faculty or "").lower()
    palette = FACULTY_PALETTES.get(key, DEFAULT)
    if overrides and key in overrides:
        palette = Palette.from_config({**_palette_to_hex(palette), **overrides[key]})
    return palette


def _palette_to_hex(p: Palette) -> dict[str, str]:
    return {k: "#%02x%02x%02x" % v for k, v in p.__dict__.items()}
