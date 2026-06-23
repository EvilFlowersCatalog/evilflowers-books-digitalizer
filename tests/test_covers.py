"""Cover rendering smoke tests (both templates, long titles, file output)."""

from __future__ import annotations

import pytest
from PIL import Image

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


@pytest.mark.parametrize("template", ["banner", "minimal"])
def test_render_returns_image_of_expected_size(template):
    img = CoverRenderer(template=template).render(LONG)
    assert isinstance(img, Image.Image)
    assert img.size == DEFAULT_SIZE


def test_long_title_fits_and_renders_diacritics(tmp_path):
    # rendering must not raise on a long Slovak title; output is a valid JPEG
    out = CoverRenderer(template="banner").render_to_file(LONG, tmp_path / "c.jpg")
    assert out.exists() and out.stat().st_size > 1000
    with Image.open(out) as im:
        im.verify()


def test_unknown_faculty_falls_back_to_default_palette():
    meta = BookMetadata(book_id="x", title="T", faculty="ZZZ")
    assert CoverRenderer().render(meta).size == DEFAULT_SIZE


def test_png_format_changes_suffix():
    r = CoverRenderer(fmt="PNG")
    assert r.suffix == ".png"


def test_unknown_template_raises():
    with pytest.raises(ValueError):
        CoverRenderer(template="nope")
