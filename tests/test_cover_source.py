"""OPAC cover fetch (mocked) + cover-step source selection / fallback."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from evilflowers_books_digitalizer.covers import opac
from evilflowers_books_digitalizer.covers.renderer import CoverRenderer
from evilflowers_books_digitalizer.pipeline.base import BookContext
from evilflowers_books_digitalizer.pipeline.steps.cover import GenerateCover


def _png_bytes(size):
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_opac_cover_real_image(tmp_path, monkeypatch):
    monkeypatch.setattr(opac.urllib.request, "urlopen", lambda *a, **k: _Resp(_png_bytes((170, 240))))
    out = opac.fetch_opac_cover("9788022726566", tmp_path / "c.jpg", min_px=80)
    assert out is not None and out.exists()
    with Image.open(out) as im:
        assert im.format == "JPEG" and im.size == (170, 240)


def test_fetch_opac_cover_rejects_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(opac.urllib.request, "urlopen", lambda *a, **k: _Resp(_png_bytes((1, 1))))
    assert opac.fetch_opac_cover("9788022726566", tmp_path / "c.jpg", min_px=80) is None


def test_fetch_opac_cover_handles_error(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(opac.urllib.request, "urlopen", boom)
    assert opac.fetch_opac_cover("123", tmp_path / "c.jpg") is None


def test_fetch_opac_cover_no_isbn(tmp_path):
    assert opac.fetch_opac_cover("", tmp_path / "c.jpg") is None


def _ctx(tmp_path, isbn=None):
    return BookContext(
        source="fad",
        book_id="CVI_OPACID_FA_0415128269",
        work_dir=tmp_path / "work",
        output_dir=tmp_path / "out",
        metadata={"title": "T", "faculty": "FAD", **({"isbn": isbn} if isbn else {})},
    )


def test_cover_step_uses_opac_when_available(tmp_path, monkeypatch):
    import evilflowers_books_digitalizer.pipeline.steps.cover as cov

    def fake_fetch(isbn, dest, **k):
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (170, 240), "white").save(dest, "JPEG")
        return dest

    monkeypatch.setattr(cov, "fetch_opac_cover", fake_fetch)
    ctx = GenerateCover(CoverRenderer(), source="opac_then_generated").run(_ctx(tmp_path, "0415128269"))
    assert ctx.metadata["cover_source"] == "opac"
    assert ctx.artifacts["cover"].exists()


def test_cover_step_falls_back_to_generated(tmp_path, monkeypatch):
    import evilflowers_books_digitalizer.pipeline.steps.cover as cov

    monkeypatch.setattr(cov, "fetch_opac_cover", lambda *a, **k: None)  # OPAC miss
    ctx = GenerateCover(CoverRenderer(), source="opac_then_generated").run(_ctx(tmp_path, "0415128269"))
    assert ctx.metadata["cover_source"] == "generated"
    assert ctx.artifacts["cover"].exists()


def test_cover_step_generated_only_skips_opac(tmp_path, monkeypatch):
    import evilflowers_books_digitalizer.pipeline.steps.cover as cov

    def boom(*a, **k):
        raise AssertionError("generated source must not call OPAC")

    monkeypatch.setattr(cov, "fetch_opac_cover", boom)
    ctx = GenerateCover(CoverRenderer(), source="generated").run(_ctx(tmp_path, "0415128269"))
    assert ctx.metadata["cover_source"] == "generated"


def test_cover_step_opac_only_no_isbn_no_cover(tmp_path):
    ctx = GenerateCover(CoverRenderer(), source="opac").run(_ctx(tmp_path))  # no isbn
    assert "cover" not in ctx.artifacts


def test_unknown_source_raises():
    with pytest.raises(ValueError):
        GenerateCover(CoverRenderer(), source="nope")
