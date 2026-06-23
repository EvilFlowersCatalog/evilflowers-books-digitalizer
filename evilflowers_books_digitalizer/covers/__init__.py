"""Book covers: real OPAC thumbnails by ISBN, or styled covers from metadata."""

from evilflowers_books_digitalizer.covers.logos import LogoLibrary, faculty_key
from evilflowers_books_digitalizer.covers.opac import fetch_opac_cover
from evilflowers_books_digitalizer.covers.palette import Palette, resolve_palette
from evilflowers_books_digitalizer.covers.renderer import CoverRenderer
from evilflowers_books_digitalizer.covers.resources import ResourceResolver
from evilflowers_books_digitalizer.covers.templates import CoverSpec, CoverTemplate, get_template

__all__ = [
    "CoverRenderer",
    "CoverSpec",
    "CoverTemplate",
    "LogoLibrary",
    "Palette",
    "ResourceResolver",
    "faculty_key",
    "fetch_opac_cover",
    "get_template",
    "resolve_palette",
]
