"""Colour for generated covers — a neutral page, themed by the faculty logo.

The cover is deliberately quiet: a warm off-white page, near-black ink for the
title and author (maximum legibility), and a single faculty accent used only for
a hairline rule. The accent of each faculty is sampled from its official colour
logo (``*-nfv``), so the cover's one spot of colour matches its STU mark:

    FAD green · FEI blue · FIIT cyan · FCHPT gold · MTF red · SvF orange ·
    STU/SjF bordeaux (SjF's mark is monochrome, so it inherits the STU wine).

Any of these can be overridden per faculty from ``[cover.palette.<key>]`` in
``pipeline.toml`` without touching code.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

RGB = tuple[int, int, int]


def hex_to_rgb(value: str | RGB) -> RGB:
    if isinstance(value, (tuple, list)):
        return (int(value[0]), int(value[1]), int(value[2]))
    h = value.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def shade(color: RGB, factor: float) -> RGB:
    """Lighten (factor>1) or darken (factor<1) a colour, clamped to 0–255."""
    return tuple(max(0, min(255, round(c * factor))) for c in color)  # type: ignore[return-value]


def _luminance(color: RGB) -> float:
    r, g, b = color
    return 0.2126 * r + 0.7152 * g + 0.4126 * b


@dataclass(frozen=True)
class Palette:
    """A cover colour scheme: a light page, dark ink, one accent."""

    bg: RGB  # page background (light, neutral)
    accent: RGB  # the single spot of faculty colour (hairline rule)
    text: RGB  # title (near-black, on the light page)
    muted: RGB = (118, 116, 112)  # author / footer (secondary ink)

    @property
    def accent_ink(self) -> RGB:
        """Accent darkened until it reads on the light page (e.g. FCHPT gold)."""
        accent = self.accent
        while _luminance(accent) > 150 and accent != (0, 0, 0):
            accent = shade(accent, 0.82)
        return accent

    @classmethod
    def from_config(cls, data: dict[str, str], base: "Palette | None" = None) -> "Palette":
        fields = dict((base or DEFAULT).__dict__)
        for key in ("bg", "accent", "text", "muted"):
            if key in data:
                fields[key] = hex_to_rgb(data[key])
        return cls(**fields)


#: Neutral page + ink shared by every faculty (only the accent changes).
_PAGE: RGB = (247, 246, 242)
_INK: RGB = (28, 28, 30)

DEFAULT = Palette(bg=_PAGE, accent=(142, 7, 51), text=_INK)  # STU bordeaux

#: Per-faculty accent (sampled from the official colour logo).
FACULTY_PALETTES: dict[str, Palette] = {
    "stu": replace(DEFAULT, accent=(142, 7, 51)),  # bordeaux
    "fad": replace(DEFAULT, accent=(0, 150, 70)),  # green
    "fei": replace(DEFAULT, accent=(19, 76, 149)),  # blue
    "fiit": replace(DEFAULT, accent=(29, 124, 170)),  # cyan-blue
    "fchpt": replace(DEFAULT, accent=(214, 178, 0)),  # gold
    "mtf": replace(DEFAULT, accent=(178, 22, 30)),  # red
    "sjf": replace(DEFAULT, accent=(142, 7, 51)),  # monochrome mark -> STU wine
    "svf": replace(DEFAULT, accent=(196, 108, 26)),  # orange
}


def resolve_palette(
    faculty: str | None, overrides: dict[str, dict[str, str]] | None = None
) -> Palette:
    """Palette for a faculty key, applying any ``[cover.palette.*]`` overrides."""
    from evilflowers_books_digitalizer.covers.logos import faculty_key

    key = faculty_key(faculty)
    palette = FACULTY_PALETTES.get(key, DEFAULT)
    if overrides and key in overrides:
        palette = Palette.from_config(overrides[key], base=palette)
    return palette
