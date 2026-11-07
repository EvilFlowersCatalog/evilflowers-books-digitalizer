"""``<id>_jp2.zip``: the cleaned page images of *all* leaves as JPEG 2000.

IA's derive starts from this package (``<id>_jp2/<id>_%04d.jp2``, leaf order),
so excluded leaves are included too: exclusion lives in scandata, not here.

Encoding uses Pillow's OpenJPEG plugin. Pages are stored with
``ZIP_STORED`` because JP2 is already compressed. When Pillow was built without
OpenJPEG there is no fallback format: IA would not derive from anything else
under this name, so :func:`build_jp2_zip` raises :class:`PortError` telling the
operator to install a Pillow with ``jpg_2000`` support (the official wheels have it).
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, features

from evilflowers_books_digitalizer.studio.adapters.ia.naming import leaf_image_name
from evilflowers_books_digitalizer.studio.ports import PortError


@dataclass(frozen=True, slots=True)
class Jp2Options:
    #: compression ratio per quality layer (IA's own derive uses ~10–20 for page images)
    rates: tuple[float, ...] = (16.0,)
    irreversible: bool = True


def jp2_supported() -> bool:
    return bool(features.check("jpg_2000"))


def encode_jp2(image: Path, opts: Jp2Options = Jp2Options()) -> bytes:
    """One page as JP2 bytes; bitonal/palette pages are widened to 8-bit first
    because OpenJPEG has no 1-bit or palette mode."""
    with Image.open(image) as im:
        if im.mode not in ("L", "RGB"):
            im = im.convert("RGB" if im.mode in ("P", "RGBA", "CMYK") else "L")
        buf = io.BytesIO()
        im.save(buf, "JPEG2000", quality_mode="rates", quality_layers=list(opts.rates),
                irreversible=opts.irreversible)
        return buf.getvalue()


def cache_key(image: Path, opts: Jp2Options) -> str:
    """What makes an encoded page stale: the source file (size + mtime) and the options."""
    st = image.stat()
    spec = f"{image.resolve()}|{st.st_size}|{st.st_mtime_ns}|{opts.rates}|{opts.irreversible}"
    return hashlib.sha256(spec.encode()).hexdigest()[:20]


def cached_jp2(image: Path, leaf_num: int, cache_dir: Path, opts: Jp2Options) -> bytes:
    """``encode_jp2`` through a per-leaf cache: ``<cache_dir>/<leaf>-<key>.jp2``.

    An unchanged page (same file, size and mtime — a re-cleaned page is a new file)
    is read back instead of re-encoded; the leaf's older entries are dropped when
    it is encoded again, so the cache holds one file per leaf.
    """
    prefix = f"{leaf_num:04d}-"
    entry = cache_dir / f"{prefix}{cache_key(image, opts)}.jp2"
    if entry.is_file():
        return entry.read_bytes()
    data = encode_jp2(image, opts)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for stale in cache_dir.glob(f"{prefix}*.jp2"):
        stale.unlink(missing_ok=True)
    tmp = entry.with_name(entry.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(entry)
    return data


def build_jp2_zip(item_id: str, images: Sequence[Path], out: Path,
                  opts: Jp2Options = Jp2Options(), *, cache_dir: Path | None = None) -> Path:
    """Write the package; ``images[i]`` is leaf ``i``.

    With ``cache_dir`` (scratch, e.g. the book's studio work dir) each page is
    encoded once per version of its image (:func:`cached_jp2`); member names and
    order are the same either way.
    """
    if not jp2_supported():
        raise PortError("Pillow has no JPEG 2000 codec (features.check('jpg_2000') is False); "
                        "install a Pillow wheel built with OpenJPEG to build <id>_jp2.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
        for leaf_num, image in enumerate(images):
            data = (encode_jp2(image, opts) if cache_dir is None
                    else cached_jp2(image, leaf_num, cache_dir, opts))
            zf.writestr(leaf_image_name(item_id, leaf_num), data)
    tmp.replace(out)
    return out


def jp2_members(package: Path) -> list[str]:
    with zipfile.ZipFile(package) as zf:
        return sorted(n for n in zf.namelist() if n.endswith(".jp2"))
