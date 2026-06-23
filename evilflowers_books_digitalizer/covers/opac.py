"""Fetch real book-cover thumbnails by ISBN from obalkyknih.cz.

obalkyknih.cz is the Czech/Slovak union cover database that the STU OPAC itself
sources its cover images from. Its ``/api/cover?isbn=`` endpoint returns the
cover image directly (≈170×240, portrait — the same ~0.71 aspect as our
generated covers, so a catalog grid stays visually consistent). When a book has
no cover, the endpoint returns a tiny placeholder, which we reject by a minimum
size gate so the pipeline falls back to a generated cover.

Best-effort and fail-safe: any error / placeholder / missing ISBN yields
``None`` and the caller generates a cover instead.
"""

from __future__ import annotations

import io
import logging
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

OBALKYKNIH_COVER = "https://obalkyknih.cz/api/cover?isbn={isbn}"
_USER_AGENT = "EvilFlowersBooksDigitalizer/0.1 (library digitization)"


def fetch_opac_cover(
    isbn: str,
    dest: Path,
    *,
    min_px: int = 80,
    quality: int = 88,
    timeout: float = 12.0,
) -> Path | None:
    """Download the cover for ``isbn`` to ``dest`` (JPEG). ``None`` if unavailable.

    A response smaller than ``min_px`` in either dimension is treated as a
    "no cover" placeholder and rejected.
    """
    if not isbn:
        return None
    url = OBALKYKNIH_COVER.format(isbn=isbn.strip())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https host
            data = resp.read()
        image = Image.open(io.BytesIO(data))
        image.load()
    except (urllib.error.URLError, TimeoutError, OSError, UnidentifiedImageError) as exc:
        logger.debug("opac cover %s failed: %s", isbn, exc)
        return None

    if image.size[0] < min_px or image.size[1] < min_px:
        logger.debug("opac cover %s is a placeholder (%s)", isbn, image.size)
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(dest, "JPEG", quality=quality, optimize=True)
    return dest
