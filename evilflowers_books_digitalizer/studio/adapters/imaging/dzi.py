"""Deep Zoom (DZI) for OpenSeadragon: pyramid math, batch writer and lazy tile cache.

One module owns the pyramid, so the tiles batch pre-renders (:func:`write_dzi`,
``pages.dzi_path``) and the ones the API cuts on demand (:class:`TileCache`, for
frames and crops) can never disagree on sizes, overlap or the descriptor.

Pyramid convention (Microsoft Deep Zoom, as OpenSeadragon reads it): level
``max_level = ceil(log2(max(w, h)))`` is full resolution, each level below
halves it (rounding up), level 0 is 1x1. Tiles are ``tile_size`` px plus
``overlap`` px shared with each neighbour.

Pillow alone: pyvips would be faster but is a native dependency the image does
not ship; one page at 600 dpi is a few hundred tiles, about a second.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # 600 dpi pages and uncompressed spreads are large

TILE_SIZE = 254
OVERLAP = 1
DZI_NS = "http://schemas.microsoft.com/deepzoom/2008"
FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
MEDIA_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

type Box = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class DeepZoomGrid:
    width: int
    height: int
    tile_size: int = TILE_SIZE
    overlap: int = OVERLAP

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError(f"empty image {self.width}x{self.height}")

    @property
    def max_level(self) -> int:
        return math.ceil(math.log2(max(self.width, self.height)))

    def scale(self, level: int) -> int:
        """Full-resolution pixels per level pixel (a power of two)."""
        self._check_level(level)
        return 2 ** (self.max_level - level)

    def level_size(self, level: int) -> tuple[int, int]:
        s = self.scale(level)
        return max(1, math.ceil(self.width / s)), max(1, math.ceil(self.height / s))

    def tile_count(self, level: int) -> tuple[int, int]:
        w, h = self.level_size(level)
        return math.ceil(w / self.tile_size), math.ceil(h / self.tile_size)

    def tile_box(self, level: int, col: int, row: int) -> Box:
        """The tile's pixels at ``level`` (x0, y0, x1, y1), overlap included."""
        cols, rows = self.tile_count(level)
        if not (0 <= col < cols and 0 <= row < rows):
            raise ValueError(f"tile {col},{row} outside level {level} ({cols}x{rows})")
        w, h = self.level_size(level)
        (x0, x1), (y0, y1) = self._span(col, w), self._span(row, h)
        return x0, y0, x1, y1

    def source_box(self, level: int, col: int, row: int) -> Box:
        """The same tile in full-resolution pixels (clamped to the image)."""
        x0, y0, x1, y1 = self.tile_box(level, col, row)
        s = self.scale(level)
        return x0 * s, y0 * s, min(self.width, x1 * s), min(self.height, y1 * s)

    def descriptor(self, fmt: str = "jpg") -> str:
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Image xmlns="{DZI_NS}" Format="{fmt}" '
                f'Overlap="{self.overlap}" TileSize="{self.tile_size}">'
                f'<Size Width="{self.width}" Height="{self.height}"/></Image>\n')

    def _span(self, index: int, extent: int) -> tuple[int, int]:
        start = index * self.tile_size - (self.overlap if index else 0)
        end = min(extent, (index + 1) * self.tile_size + self.overlap)
        return start, end

    def _check_level(self, level: int) -> None:
        if not 0 <= level <= self.max_level:
            raise ValueError(f"level {level} outside 0..{self.max_level}")


@dataclass(frozen=True, slots=True)
class TileSource:
    """A region of an image file to tile: a cleaned page, or a leaf's crop of its frame."""

    path: Path
    crop: Box | None = None          # x0, y0, x1, y1 in the (rotated) file
    rotate: int = 0                  # clockwise degrees, applied before the crop

    def size(self) -> tuple[int, int]:
        if self.crop is not None:
            return self.crop[2] - self.crop[0], self.crop[3] - self.crop[1]
        with Image.open(self.path) as im:
            w, h = im.size
        return (h, w) if self.rotate % 180 else (w, h)

    def grid(self) -> DeepZoomGrid:
        return DeepZoomGrid(*self.size())

    def cache_key(self) -> str:
        stat = self.path.stat()
        raw = f"{self.path}|{stat.st_mtime_ns}|{stat.st_size}|{self.crop}|{self.rotate}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def open(self) -> Image.Image:
        im: Image.Image = Image.open(self.path)
        if self.rotate % 360:
            im = im.rotate(-self.rotate, expand=True)
        return im.crop(self.crop) if self.crop is not None else im


def browser_mode(im: Image.Image) -> Image.Image:
    return im.convert("L") if im.mode in ("1", "L", "I;16", "I", "LA") else im.convert("RGB")


def atomic_write(target: Path, data: bytes) -> Path:
    """Write ``data`` to ``target`` so a concurrent reader never sees half a file.

    The API serves renditions from a thread pool; a temp file *unique per writer*
    (not ``target.tmp``) keeps two requests for the same key from writing into one
    file. Losing the final rename to the other writer is fine: the target holds
    the same bytes either way.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        if not target.exists():
            raise
    return target


def encode(image: Image.Image, fmt: str, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    browser_mode(image).save(buf, FORMATS[fmt], quality=quality)
    return buf.getvalue()


class TileCache:
    """Renders tiles on demand and keeps them under ``root/<source key>/<level>/``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def tile(self, source: TileSource, level: int, col: int, row: int, fmt: str = "jpg") -> Path:
        grid = source.grid()
        target = self.root / source.cache_key() / str(level) / f"{col}_{row}.{fmt}"
        if target.exists():
            return target
        box = grid.tile_box(level, col, row)   # validates before any pixel work
        with source.open() as im:
            tile = im.crop(grid.source_box(level, col, row)).resize(
                (box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS)
        return atomic_write(target, encode(tile, fmt))


def write_dzi(image: Image.Image, dzi: Path, *, tile: int = TILE_SIZE, overlap: int = OVERLAP,
              fmt: str = "jpg", quality: int = 85) -> Path:
    """Write ``dzi`` and its ``<stem>_files`` pyramid; returns ``dzi``.

    Levels are built by halving the previous one (not resampling the full page
    each time), which keeps the cost close to one full-size pass; halving with
    rounding up yields exactly :meth:`DeepZoomGrid.level_size`.
    """
    base = browser_mode(image)
    grid = DeepZoomGrid(base.width, base.height, tile, overlap)
    files = dzi.with_name(f"{dzi.stem}_files")
    if files.exists():
        shutil.rmtree(files)   # a re-run may have changed the page size
    level_img = base
    for level in range(grid.max_level, -1, -1):
        out = files / str(level)
        out.mkdir(parents=True, exist_ok=True)
        cols, rows = grid.tile_count(level)
        for col in range(cols):
            for row in range(rows):
                level_img.crop(grid.tile_box(level, col, row)).save(
                    out / f"{col}_{row}.{fmt}", quality=quality)
        level_img = level_img.resize(grid.level_size(level - 1), Image.Resampling.LANCZOS) \
            if level else level_img
    dzi.parent.mkdir(parents=True, exist_ok=True)
    dzi.write_text(grid.descriptor(fmt))
    return dzi
