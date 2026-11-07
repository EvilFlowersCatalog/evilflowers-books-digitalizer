"""ImageOps on Pillow: restoration erasures and browser renditions of a cleaned page.

File naming follows the IA artifact store: given ``<id>_studio`` as ``out_dir`` and a
cleaned page named ``<leaf>.tif``, previews land in ``leaves/<leaf>.webp`` and
``dzi/<leaf>.dzi`` — the same paths :class:`IaArtifactStore` reports for
``leaf_webp`` / ``dzi``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw

from evilflowers_books_digitalizer.studio.adapters.imaging.dzi import write_dzi
from evilflowers_books_digitalizer.studio.domain import BBox

Image.MAX_IMAGE_PIXELS = None


def _white(mode: str) -> int | tuple[int, ...]:
    """Paper white in the image's own mode (bitonal pages must stay bitonal)."""
    match mode:
        case "1":
            return 1
        case "L" | "P" | "I" | "F":
            return 255
        case "I;16":
            return 65535
        case "RGBA":
            return (255, 255, 255, 255)
        case "LA":
            return (255, 255)
        case _:
            return (255, 255, 255)


class PillowImageOps:
    def __init__(self, webp_quality: int = 80, tile: int = 254) -> None:
        self.webp_quality = webp_quality
        self.tile = tile

    def erase(self, image: Path, boxes: Sequence[BBox], out: Path) -> Path:
        """Paint ``boxes`` paper-white (stamps, stickers, scribbles); ``out`` may equal ``image``."""
        with Image.open(image) as im:
            im.load()
            dpi = im.info.get("dpi")
            page = im.copy()
        draw = ImageDraw.Draw(page)
        for b in boxes:
            draw.rectangle((b.x0, b.y0, b.x1 - 1, b.y1 - 1), fill=_white(page.mode))
        out.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, object] = {"dpi": dpi} if dpi else {}
        if out.suffix.lower() in (".tif", ".tiff"):
            kwargs["compression"] = "group4" if page.mode == "1" else "tiff_deflate"
        page.save(out, **kwargs)
        return out

    def previews(self, image: Path, out_dir: Path, *, max_px: int) -> Path:
        """``leaves/<stem>.webp`` (``max_px`` bound) + ``dzi/<stem>.dzi``; returns the .dzi."""
        with Image.open(image) as im:
            im.load()
            page = im.copy()
        webp = out_dir / "leaves" / f"{image.stem}.webp"
        webp.parent.mkdir(parents=True, exist_ok=True)
        small = page.convert("L") if page.mode in ("1", "L", "I;16") else page.convert("RGB")
        small.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        small.save(webp, "WEBP", quality=self.webp_quality, method=4)
        return write_dzi(page, out_dir / "dzi" / f"{image.stem}.dzi", tile=self.tile)
