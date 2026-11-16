"""Deep Zoom pyramid math (pure) and lazily rendered, cached tiles."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from evilflowers_books_digitalizer.studio.adapters.imaging import dzi
from evilflowers_books_digitalizer.studio.adapters.imaging.dzi import (
    DeepZoomGrid,
    TileCache,
    TileSource,
)
from evilflowers_books_digitalizer.studio.adapters.imaging.renditions import ImageCache


def test_levels_halve_and_round_up():
    g = DeepZoomGrid(1000, 600)
    assert g.max_level == 10                       # 2**10 = 1024 >= 1000
    assert g.level_size(10) == (1000, 600)
    assert g.level_size(9) == (500, 300)
    assert g.level_size(8) == (250, 150)
    assert g.level_size(1) == (2, 2)               # ceil(1000/512), ceil(600/512)
    assert g.level_size(0) == (1, 1)


def test_tile_counts_and_overlap_at_edges():
    g = DeepZoomGrid(1000, 600, tile_size=254, overlap=1)
    assert g.tile_count(10) == (4, 3)
    assert g.tile_box(10, 0, 0) == (0, 0, 255, 255)            # first: overlap only on the right
    assert g.tile_box(10, 1, 1) == (253, 253, 509, 509)        # inner: overlap on both sides
    assert g.tile_box(10, 3, 2) == (761, 507, 1000, 600)       # last: clipped to the image
    assert g.tile_count(8) == (1, 1) and g.tile_box(8, 0, 0) == (0, 0, 250, 150)


def test_source_box_scales_to_full_resolution():
    g = DeepZoomGrid(1000, 600)
    assert g.scale(9) == 2
    assert g.source_box(9, 0, 0) == (0, 0, 510, 510)
    assert g.source_box(9, 1, 1) == (506, 506, 1000, 600)      # clamped


def test_out_of_range_is_rejected():
    g = DeepZoomGrid(1000, 600)
    with pytest.raises(ValueError):
        g.tile_box(10, 4, 0)
    with pytest.raises(ValueError):
        g.level_size(11)
    with pytest.raises(ValueError):
        DeepZoomGrid(0, 10)


def test_one_pixel_image_has_one_level():
    g = DeepZoomGrid(1, 1)
    assert g.max_level == 0 and g.tile_box(0, 0, 0) == (0, 0, 1, 1)


def test_descriptor_is_deep_zoom_xml():
    xml = DeepZoomGrid(300, 200).descriptor("png")
    assert 'TileSize="254"' in xml and 'Overlap="1"' in xml and 'Format="png"' in xml
    assert '<Size Width="300" Height="200"/>' in xml


def test_tiles_are_rendered_once_and_cached(tmp_path):
    src = tmp_path / "page.png"
    Image.new("L", (600, 400), 200).save(src)
    cache = TileCache(tmp_path / "dzi")
    source = TileSource(src)
    first = cache.tile(source, 10, 2, 1)
    with Image.open(first) as im:
        assert im.size == (600 - 507, 400 - 253)
    mtime = first.stat().st_mtime_ns
    assert cache.tile(source, 10, 2, 1) == first and first.stat().st_mtime_ns == mtime
    with Image.open(cache.tile(source, 0, 0, 0, "png")) as im:
        assert im.size == (1, 1)


def test_crop_and_rotation_define_the_tiled_image(tmp_path):
    src = tmp_path / "frame.png"
    Image.new("RGB", (400, 300), (10, 20, 30)).save(src)
    assert TileSource(src, crop=(200, 0, 400, 300)).size() == (200, 300)
    assert TileSource(src, rotate=90).size() == (300, 400)
    with TileSource(src, rotate=90).open() as im:
        assert im.size == (300, 400)


def test_a_thumbnail_burst_decodes_two_at_a_time_and_only_once(tmp_path, monkeypatch):
    """A book page asks for every leaf at once (#12): decodes are bounded, hits are free."""
    monkeypatch.setattr(dzi, "_RENDER_SLOTS", threading.BoundedSemaphore(2))
    sources = []
    for i in range(24):
        src = tmp_path / f"{i:04d}.png"
        Image.new("L", (400, 600), i).save(src)
        sources.append(src)
    active, peak, decodes, lock = 0, 0, 0, threading.Lock()
    real_open = TileSource.open

    def counting_open(self: TileSource) -> Image.Image:
        nonlocal active, peak, decodes
        with lock:
            active, decodes = active + 1, decodes + 1
            peak = max(peak, active)
        time.sleep(0.01)
        try:
            return real_open(self)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(TileSource, "open", counting_open)
    cache = ImageCache(tmp_path / "viewer")
    with ThreadPoolExecutor(16) as pool:
        first = list(pool.map(lambda p: cache.original(p, 240), sources))
    assert peak <= 2 and decodes == 24
    with ThreadPoolExecutor(16) as pool:
        again = list(pool.map(lambda p: cache.original(p, 240), sources))
    assert again == first and decodes == 24                 # a reload decodes nothing
