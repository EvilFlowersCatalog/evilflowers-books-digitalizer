"""Cover / thumbnail generation from catalog metadata (Pillow, template-driven)."""

from evilflowers_books_digitalizer.covers.palette import Palette, resolve_palette
from evilflowers_books_digitalizer.covers.renderer import CoverRenderer
from evilflowers_books_digitalizer.covers.templates import CoverSpec, CoverTemplate, get_template

__all__ = [
    "CoverRenderer",
    "CoverSpec",
    "CoverTemplate",
    "Palette",
    "get_template",
    "resolve_palette",
]
