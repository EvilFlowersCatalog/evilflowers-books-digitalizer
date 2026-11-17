"""A small PNG of one region of a cleaned page, with context: what a judge or a labeller sees."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def crop_png(image: Path, bbox: tuple[int, int, int, int], max_side: int = 448) -> bytes:
    """``bbox`` (page px) padded by a quarter of its size (at least 8 px), as scan-service crops
    a mark for decider-vision, scaled to at most ``max_side`` (decider-vision: 448 px)."""
    x0, y0, x1, y1 = bbox
    pad_x, pad_y = max(8, (x1 - x0) // 4), max(8, (y1 - y0) // 4)
    with Image.open(image) as im:
        w, h = im.size
        region = im.crop((max(0, x0 - pad_x), max(0, y0 - pad_y),
                          min(w, x1 + pad_x), min(h, y1 + pad_y))).convert("RGB")
    region.thumbnail((max_side, max_side))
    out = io.BytesIO()
    region.save(out, "PNG")
    return out.getvalue()


def fit_image(content: bytes, max_side: int) -> tuple[bytes, str, tuple[int, int], tuple[int, int]]:
    """An image for a vision model (#16): unchanged when its long edge fits ``max_side``, else
    scaled down and re-encoded as PNG. Returns (bytes, media type, size sent, original size).
    Raises ``OSError`` / ``ValueError`` on bytes PIL cannot read."""
    with Image.open(io.BytesIO(content)) as im:
        original = im.size
        fmt = (im.format or "PNG").upper()
        if max(original) <= max_side and fmt in ("PNG", "JPEG", "WEBP", "GIF"):
            return content, f"image/{fmt.lower()}", original, original
        small = im.convert("RGB") if im.mode not in ("RGB", "L") else im.copy()
    small.thumbnail((max_side, max_side))
    out = io.BytesIO()
    small.save(out, "PNG")
    return out.getvalue(), "image/png", small.size, original
