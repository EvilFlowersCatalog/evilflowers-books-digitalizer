"""Binary endpoints: page image layers, Deep Zoom, artifact downloads, the IA export zip.

Every file served here comes from a path recorded in the database (or derived
from the book's identifier by the ArtifactStore); request input only selects a
*kind* or a tile number, never a path, so there is no traversal surface.
Renditions are cached on disk (``adapters.imaging.renditions.ImageCache``, ``dzi.TileCache``)
and served with ETags; ``Cache-Control: no-cache`` makes the browser revalidate
so a re-run page shows up without a version in the URL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response
from fastapi.responses import FileResponse
from PIL import Image

from evilflowers_books_digitalizer.studio.api.deps import State
from evilflowers_books_digitalizer.studio.adapters.imaging.dzi import MEDIA_TYPES, TileSource
from evilflowers_books_digitalizer.studio.adapters.imaging.renditions import ImageCache
from evilflowers_books_digitalizer.studio.application import books, export, pages
from evilflowers_books_digitalizer.studio.application.errors import NotFound
from evilflowers_books_digitalizer.studio.application.pages import PageMedia

router = APIRouter()

type Layer = Literal["original", "spread", "clean", "pdf"]
type DziLayer = Literal["original", "spread", "clean"]
Width = Annotated[int, Query(ge=32, le=4000)]
_TILE = re.compile(r"(\d+)_(\d+)\.(jpg|jpeg|png|webp)")
_REVALIDATE = {"Cache-Control": "no-cache"}


# ---------------------------------------------------------------- image layers
@router.get("/pages/{page_id}/image/{layer}")
def page_image(state: State, page_id: int, layer: Layer, w: Width = 1200) -> FileResponse:
    media = pages.page_media(state.services, page_id)
    path = _render(state.images, media, layer, w)
    return FileResponse(path, media_type="image/jpeg", headers=_REVALIDATE)


def _render(images: ImageCache, media: PageMedia, layer: Layer, width: int) -> Path:
    match layer:
        case "original":     # the same rotate + crop as the deep-zoom layer
            return images.region(_tile_source(media, "original"), width)
        case "spread":
            return images.original(_need(media.frame, "original frame"), width)
        case "clean":
            return images.original(_need(media.clean, "cleaned page"), width)
        case "pdf":
            if media.access_index is None:
                raise NotFound(f"page {media.page_id} is excluded from access formats")
            try:
                return images.pdf_page(_need(media.pdf, "PDF"), media.access_index, width)
            except IndexError:
                raise NotFound(f"the PDF has no page {media.access_index + 1}") from None


def _need[T](value: T | None, what: str) -> T:
    if value is None:
        raise NotFound(f"no {what} for this page yet")
    return value


# ---------------------------------------------------------------- deep zoom
@router.get("/pages/{page_id}/dzi")
# OpenSeadragon derives the tile folder from the descriptor URL and keeps its query
# string (?v=, ?layer=) for the tiles only when the URL has an extension:
# ".../dzi.xml?v=2" -> ".../dzi_files/<level>/<col>_<row>.jpg?v=2"
@router.get("/pages/{page_id}/dzi.xml", include_in_schema=False)
def dzi_descriptor(state: State, page_id: int, layer: DziLayer = "clean") -> Response:
    media = pages.page_media(state.services, page_id)
    if layer == "clean" and media.dzi is not None:
        return FileResponse(media.dzi, media_type="application/xml", headers=_REVALIDATE)
    return Response(_tile_source(media, layer).grid().descriptor(), media_type="application/xml",
                    headers=_REVALIDATE)


@router.get("/pages/{page_id}/dzi_files/{level}/{tile}")
def dzi_tile(state: State, page_id: int, level: int, tile: str, layer: DziLayer = "clean"
             ) -> FileResponse:
    match = _TILE.fullmatch(tile)
    if match is None:
        raise NotFound(f"not a tile name: {tile!r}")
    col, row, ext = int(match[1]), int(match[2]), match[3]
    media = pages.page_media(state.services, page_id)
    if layer == "clean" and media.dzi is not None:
        path = media.dzi.with_name(f"{media.dzi.stem}_files") / str(level) / f"{col}_{row}.{ext}"
        if not path.is_file():
            raise NotFound(f"no tile {level}/{col}_{row}.{ext}")
    else:
        try:
            path = state.tiles.tile(_tile_source(media, layer), level, col, row, ext)
        except ValueError as exc:
            raise NotFound(str(exc)) from None
    return FileResponse(path, media_type=MEDIA_TYPES[ext], headers=_REVALIDATE)


def _tile_source(media: PageMedia, layer: DziLayer) -> TileSource:
    if layer == "clean":
        return TileSource(_need(media.clean, "cleaned page"))
    frame = _need(media.frame, "original frame")
    if layer == "spread":
        return TileSource(frame)
    if media.crop is not None:
        return TileSource(frame, media.crop, media.rotate)
    if media.half is not None:
        with Image.open(frame) as im:
            height = im.height
        return TileSource(frame, (media.half[0], 0, media.half[1], height))
    return TileSource(frame, rotate=media.rotate)


# ---------------------------------------------------------------- files
@router.get("/books/{book_id}/files/{kind}")
def book_file(state: State, book_id: int, kind: str) -> FileResponse:
    path = books.artifact_file(state.services, book_id, kind)
    return FileResponse(path, filename=path.name)


@router.get("/books/{book_id}/export/ia")
def export_ia(state: State, book_id: int) -> FileResponse:
    out_dir = state.services.config.cache_dir / "exports"
    path = export.export_ia(state.services, state.ia_exporter, book_id, out_dir)
    return FileResponse(path, media_type="application/zip", filename=path.name)
