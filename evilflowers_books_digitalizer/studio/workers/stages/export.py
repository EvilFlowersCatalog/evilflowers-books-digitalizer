"""``studio.export_training``: confirmed regions -> COCO JSON with DocLayNet labels + crops.

Only operator-*confirmed* regions are ground truth, and only kinds that DocLayNet
defines (``domain.DOCLAYNET``) are exported — stamps, stickers and handwriting are
restoration labels, not layout classes. The result drops straight into
DocLayout-YOLO / DocLayNet fine-tuning::

    <artifacts_root>/<project>/_training/
        coco.json                        # images, annotations (bbox = x, y, w, h), categories
        images/<item_id>_<leaf>.png      # the cleaned pages the boxes refer to
        crops/<DocLayNet label>/<region id>.png
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select

from evilflowers_books_digitalizer.db.models import Book, Page, Region
from evilflowers_books_digitalizer.studio.domain import DOCLAYNET, RegionKind, RegionStatus
from evilflowers_books_digitalizer.studio.events import EventType
from evilflowers_books_digitalizer.studio.workers.context import WorkerContext

Image.MAX_IMAGE_PIXELS = None

#: DocLayNet categories, 1-based ids in DOCLAYNET order
CATEGORIES = [{"id": i, "name": name, "supercategory": "layout"}
              for i, name in enumerate(DOCLAYNET.values(), start=1)]
_CATEGORY_ID = {kind: i for i, kind in enumerate(DOCLAYNET, start=1)}


@dataclass(frozen=True, slots=True)
class Sample:
    page_id: int
    item_id: str
    leaf: int
    image: Path
    width: int
    height: int
    regions: list[tuple[int, RegionKind, tuple[int, int, int, int]]]


def _samples(ctx: WorkerContext, project: str) -> list[Sample]:
    kinds = [str(k) for k in DOCLAYNET]
    with ctx.uow() as uow:
        rows = uow.session.execute(
            select(Region, Page, Book).join(Page, Region.page_pk == Page.id)
            .join(Book, Page.book_pk == Book.id)
            .where(Book.project == project, Region.status == RegionStatus.CONFIRMED,
                   Region.kind.in_(kinds), Page.clean_path.is_not(None))
            .order_by(Book.id, Page.index, Region.id)).all()
        by_page: dict[int, Sample] = {}
        for region, page, book in rows:
            sample = by_page.setdefault(page.id, Sample(
                page.id, book.item_id or book.slug, page.index, Path(page.clean_path or ""),
                page.width or 0, page.height or 0, []))
            sample.regions.append((region.id, RegionKind(region.kind),
                                   (region.x0, region.y0, region.x1, region.y1)))
        return list(by_page.values())


def _annotation(region_id: int, image_id: int, kind: RegionKind,
                box: tuple[int, int, int, int]) -> dict[str, Any]:
    x0, y0, x1, y1 = box
    return {"id": region_id, "image_id": image_id, "category_id": _CATEGORY_ID[kind],
            "bbox": [x0, y0, x1 - x0, y1 - y0], "area": (x1 - x0) * (y1 - y0), "iscrowd": 0,
            "segmentation": [[x0, y0, x1, y0, x1, y1, x0, y1]]}


def _write_sample(sample: Sample, out: Path) -> dict[str, Any]:
    name = f"images/{sample.item_id}_{sample.leaf:04d}.png"
    with Image.open(sample.image) as im:
        page = im.convert("L") if im.mode in ("1", "L", "I;16") else im.convert("RGB")
    page.save(out / name)
    for region_id, kind, box in sample.regions:
        crop_dir = out / "crops" / DOCLAYNET[kind]
        crop_dir.mkdir(parents=True, exist_ok=True)
        page.crop(box).save(crop_dir / f"{region_id}.png")
    return {"id": sample.page_id, "file_name": name, "width": page.width, "height": page.height,
            "doc_name": sample.item_id, "page_no": sample.leaf}


def export_training(ctx: WorkerContext, project: str) -> Path:
    ctx.project(project)   # unknown project -> KeyError before touching the disk
    out = ctx.config.artifacts_root / project / "_training"
    if out.exists():
        shutil.rmtree(out)
    (out / "images").mkdir(parents=True)
    samples = _samples(ctx, project)
    images = [_write_sample(s, out) for s in samples]
    annotations = [_annotation(rid, s.page_id, kind, box)
                   for s in samples for rid, kind, box in s.regions]
    coco = {"info": {"description": f"EvilFlowers studio export ({project})",
                     "label_set": "DocLayNet"},
            "images": images, "annotations": annotations, "categories": CATEGORIES}
    path = out / "coco.json"
    path.write_text(json.dumps(coco, ensure_ascii=False, indent=1), encoding="utf-8")
    with ctx.uow() as uow:
        uow.emit(EventType.RUN_LOG, project, None, run_id=None, level="info",
                 message=f"training export: {len(images)} pages, {len(annotations)} regions -> "
                         f"{path}")
    return path
