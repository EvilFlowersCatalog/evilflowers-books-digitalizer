"""Cover rendering smoke tests (the STU template, long titles, file output)."""

from __future__ import annotations

import pytest
from PIL import Image

from evilflowers_books_digitalizer.covers.logos import LogoLibrary, faculty_key
from evilflowers_books_digitalizer.covers.palette import resolve_palette
from evilflowers_books_digitalizer.covers.renderer import DEFAULT_SIZE, CoverRenderer
from evilflowers_books_digitalizer.metadata.models import BookMetadata

LONG = BookMetadata(
    book_id="x",
    title="Architektonická kompozícia a typológia obytných budov v meste",
    subtitle="Druhé prepracované vydanie",
    authors=["Mária Kováčová", "Ján Novák"],
    year=2014,
    publisher="Vydavateľstvo STU",
    isbn="978-80-227-5046-2",
    faculty="FAD",
)


def test_render_returns_image_of_expected_size():
    img = CoverRenderer(template="stu").render(LONG)
    assert isinstance(img, Image.Image)
    assert img.size == DEFAULT_SIZE


def test_long_title_fits_and_renders_diacritics(tmp_path):
    # rendering must not raise on a long Slovak title; output is a valid JPEG
    out = CoverRenderer(template="stu").render_to_file(LONG, tmp_path / "c.jpg")
    assert out.exists() and out.stat().st_size > 1000
    with Image.open(out) as im:
        im.verify()


def test_unknown_faculty_falls_back_to_default():
    meta = BookMetadata(book_id="x", title="T", faculty="ZZZ")
    assert CoverRenderer().render(meta).size == DEFAULT_SIZE


def test_png_format_changes_suffix():
    r = CoverRenderer(fmt="PNG")
    assert r.suffix == ".png"


def test_unknown_template_raises():
    with pytest.raises(ValueError):
        CoverRenderer(template="nope")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("FEI", "fei"),
        ("fad", "fad"),
        ("STU-MTF", "mtf"),
        ("Fakulta elektrotechniky a informatiky STU", "fei"),
        ("Fakulta architektúry a dizajnu STU", "fad"),
        ("Stavebná fakulta STU", "svf"),
        ("", "stu"),
        (None, "stu"),
        ("totally unknown", "stu"),
    ],
)
def test_faculty_key_normalisation(value, expected):
    assert faculty_key(value) == expected


def test_every_faculty_has_a_bundled_logo():
    lib = LogoLibrary()
    for key in ("stu", "fei", "fiit", "fchpt", "mtf", "sjf", "svf", "fad"):
        logo = lib.load(key)
        assert logo is not None and logo.mode == "RGBA"


def test_light_accent_is_darkened_for_legibility():
    # FCHPT gold is too light to read on the page; accent_ink must darken it.
    pal = resolve_palette("fchpt")
    r, g, b = pal.accent_ink
    assert 0.2126 * r + 0.7152 * g + 0.4126 * b <= 150


def test_palette_accent_override():
    pal = resolve_palette("fad", {"fad": {"accent": "#123456"}})
    assert pal.accent == (0x12, 0x34, 0x56)
