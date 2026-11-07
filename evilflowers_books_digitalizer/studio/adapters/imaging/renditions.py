"""Browser-friendly renditions of page layers, cached on disk.

Browsers can't show TIFF, and the originals are ~24 MB each, so the viewer
asks for a width and gets a JPEG/WebP back:

* ``original`` — the source frame (optionally cropped to the spread half);
* ``cleaned``  — the persisted ScanTailor preview;
* ``pdf`` / ``archival`` — the page rendered from that PDF with pdfium.

Cache key = path + mtime + width + crop/page, under ``<cache_dir>/viewer``.
"""

from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path

from PIL import Image

from evilflowers_books_digitalizer.studio.adapters.imaging.dzi import (
    TileSource,
    atomic_write,
    browser_mode,
)

Image.MAX_IMAGE_PIXELS = None  # 600 dpi pages / uncompressed spreads are large

_PDFIUM_LOCK = threading.Lock()   # pdfium is not thread-safe


class ImageCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _key(self, src: Path, *parts: object) -> Path:
        stat = src.stat()
        digest = hashlib.sha1(
            "|".join(map(str, (src, stat.st_mtime_ns, stat.st_size, *parts))).encode()
        ).hexdigest()
        return self.root / digest[:2] / f"{digest}.jpg"

    def _store(self, target: Path, image: Image.Image, quality: int = 85) -> Path:
        buf = io.BytesIO()
        browser_mode(image).save(buf, "JPEG", quality=quality, optimize=True)
        return atomic_write(target, buf.getvalue())

    def original(self, path: Path, width: int) -> Path:
        """A whole image file (spread, cleaned page) at most ``width`` px wide."""
        return self.region(TileSource(path), width)

    def region(self, source: TileSource, width: int) -> Path:
        """A rotated + cropped part of a file, exactly what the deep-zoom layer shows.

        Leaf crops are in the *rotated* frame (scandata semantics), so the static
        image goes through :meth:`TileSource.open` too: both views of a layer agree.
        """
        target = self._key(source.path, width, source.crop, source.rotate)
        if target.exists():
            return target
        with source.open() as im:
            im.thumbnail((width, width * 4), Image.Resampling.LANCZOS)
            return self._store(target, im)

    def resized(self, path: Path, width: int) -> Path:
        """Any Pillow-readable image at most ``width`` px wide (previews, covers)."""
        with Image.open(path) as im:
            if im.width <= width:
                return path
        target = self._key(path, width)
        if target.exists():
            return target
        with Image.open(path) as im:
            im.thumbnail((width, width * 4), Image.Resampling.LANCZOS)
            return self._store(target, im)

    def pdf_page(self, pdf: Path, index: int, width: int) -> Path:
        target = self._key(pdf, index, width)
        if target.exists():
            return target
        import pypdfium2 as pdfium

        with _PDFIUM_LOCK:
            doc = pdfium.PdfDocument(str(pdf))
            try:
                if not 0 <= index < len(doc):
                    raise IndexError(index)
                page = doc[index]
                scale = width / page.get_width()
                image = page.render(scale=scale).to_pil()
                page.close()
            finally:
                doc.close()
        return self._store(target, image, quality=88)


def page_count(pdf: Path) -> int:
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(pdf))
        try:
            return len(doc)
        finally:
            doc.close()
