"""Font loading and text-fitting helpers for cover rendering.

Fonts are bundled (DejaVu Serif/Sans — full Slovak diacritic coverage,
deterministic across the dev Mac and the Linux VM). The bundled set is the
default; ``[cover.fonts]`` can point at other TTFs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import ImageDraw, ImageFont

FONTS_DIR = Path(__file__).parent / "assets" / "fonts"

#: Logical font role -> bundled filename.
BUNDLED = {
    "serif": "DejaVuSerif.ttf",
    "serif_bold": "DejaVuSerif-Bold.ttf",
    "serif_italic": "DejaVuSerif-Italic.ttf",
    "sans": "DejaVuSans.ttf",
    "sans_bold": "DejaVuSans-Bold.ttf",
}


def resolve_font_path(role_or_path: str) -> str:
    """A bundled role name, a bundled filename, or an explicit path -> a path."""
    if role_or_path in BUNDLED:
        return str(FONTS_DIR / BUNDLED[role_or_path])
    candidate = FONTS_DIR / role_or_path
    if candidate.exists():
        return str(candidate)
    return role_or_path  # assume an absolute/relative path to a TTF


@lru_cache(maxsize=256)
def load_font(role_or_path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(resolve_font_path(role_or_path), size)


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """Greedy word-wrap to ``max_width`` (breaks over-long single words)."""
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words, line = paragraph.split(), ""
        for word in words:
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=font) <= max_width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
    return lines or [""]


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    role: str,
    *,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int = 14,
    line_spacing: float = 1.12,
) -> tuple[list[str], ImageFont.FreeTypeFont, int]:
    """Largest font size at which ``text`` wraps within the given box.

    Returns ``(lines, font, line_height)``. Shrinks from ``max_size`` until the
    wrapped block fits both ``max_width`` and ``max_height`` (or ``min_size``).
    """
    for size in range(max_size, min_size - 1, -2):
        font = load_font(role, size)
        lines = _wrap(draw, text, font, max_width)
        line_height = round(size * line_spacing)
        if len(lines) * line_height <= max_height:
            return lines, font, line_height
    font = load_font(role, min_size)
    return _wrap(draw, text, font, max_width), font, round(min_size * line_spacing)


def draw_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    *,
    top: int,
    line_height: int,
    color: tuple[int, int, int],
    center_x: int,
    align: str = "center",
) -> int:
    """Draw wrapped lines; return the y just below the block."""
    y = top
    for line in lines:
        if align == "center":
            anchor, x = "ma", center_x
        else:
            anchor, x = "la", center_x
        draw.text((x, y), line, font=font, fill=color, anchor=anchor)
        y += line_height
    return y
