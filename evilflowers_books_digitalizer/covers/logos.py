"""Faculty logo lookup for covers.

The bundled ``assets/logos/<key>.png`` files are the official STU marks — the
black, transparent *vertical lockup* (``ncv`` variant: the dot grid + "STU"
wordmark + faculty initials + the spelled-out faculty/university name). They are
monochrome on a transparent ground, which is exactly what a neutral light cover
wants: no coloured box to fight the page.

A book's ``faculty`` field is messy — it may be a key (``"FEI"``), the source
folder upper-cased, or a full human name (``"Fakulta elektrotechniky a
informatiky STU"``). :func:`faculty_key` collapses all of those to a logo key,
falling back to the plain ``stu`` mark when nothing matches.
"""

from __future__ import annotations

from functools import lru_cache

from PIL import Image

from evilflowers_books_digitalizer.covers.palette import RGB
from evilflowers_books_digitalizer.covers.resources import ResourceResolver

#: Known faculty logo keys (a bundled ``logos/<key>.png`` exists for each).
FACULTY_KEYS = ("stu", "fei", "fiit", "fchpt", "mtf", "sjf", "svf", "fad")

#: Substrings (Slovak, lower-cased) that disambiguate a human faculty name.
_NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("architekt", "fad"),
    ("dizajn", "fad"),
    ("elektrotech", "fei"),
    ("informatik", "fiit"),  # FIIT — checked before FEI's "a informatiky" via word order below
    ("chemic", "fchpt"),
    ("chemick", "fchpt"),
    ("potravin", "fchpt"),
    ("materiál", "mtf"),
    ("materialov", "mtf"),
    ("strojníc", "sjf"),
    ("strojnic", "sjf"),
    ("stavebn", "svf"),
)


def faculty_key(faculty: str | None) -> str:
    """Normalise any faculty string to a logo/palette key (default ``stu``)."""
    text = (faculty or "").strip().lower()
    if not text:
        return "stu"
    # A specific faculty abbreviation wins (e.g. "fei", "stu-fei", "fei stu").
    # The generic "stu" token must NOT short-circuit here: every faculty name
    # ends with "STU", so it is only the fallback when nothing else matches.
    tokens = {t for t in text.replace("-", " ").replace("_", " ").split()}
    for key in FACULTY_KEYS:
        if key != "stu" and key in tokens:
            return key
    # FIIT vs FEI: "fakulta informatiky a informačných technológií" -> fiit,
    # but FEI is "elektrotechniky a informatiky" — prefer the electro hint.
    if "elektrotech" in text:
        return "fei"
    for hint, key in _NAME_HINTS:
        if hint in text:
            return key
    return "stu"


class LogoLibrary:
    """Resolve and cache faculty logos, with optional per-faculty overrides."""

    def __init__(
        self,
        resolver: ResourceResolver | None = None,
        overrides: dict[str, str] | None = None,
    ):
        self.resolver = resolver or ResourceResolver()
        # faculty key -> explicit filename/path (from [cover.logos])
        self.overrides = {k.lower(): v for k, v in (overrides or {}).items()}

    def path_for(self, faculty: str | None):
        key = faculty_key(faculty)
        name = self.overrides.get(key) or self.overrides.get("default") or f"{key}.png"
        path = self.resolver.find("logos", name)
        if path is None and name != "stu.png":
            path = self.resolver.find("logos", "stu.png")  # last-resort default mark
        return path

    @lru_cache(maxsize=32)
    def _load(self, path_str: str, tint: RGB | None) -> Image.Image:
        logo = Image.open(path_str).convert("RGBA")
        return _tint(logo, tint) if tint else logo

    def load(self, faculty: str | None, tint: RGB | None = None) -> Image.Image | None:
        """The faculty logo as RGBA (optionally re-inked to ``tint``)."""
        path = self.path_for(faculty)
        if path is None:
            return None
        return self._load(str(path), tint).copy()


def _tint(logo: Image.Image, color: RGB) -> Image.Image:
    """Recolour an opaque-alpha logo to ``color``, keeping its alpha shape."""
    solid = Image.new("RGBA", logo.size, (*color, 0))
    solid.putalpha(logo.getchannel("A"))
    return solid
