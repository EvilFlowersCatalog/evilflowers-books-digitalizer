"""PageCleaner on ScanTailor (``scantailor-deviant-cli``) + our own spread detection.

Two phases, matching the port:

* :meth:`ScanTailorCleaner.plan` runs ``imaging.analyze_spread`` once per frame and
  fixes every leaf's frame, side and crop *before* any cleaning, so leaf numbers are
  stable across chunks and re-runs (overrides are keyed by them).
* :meth:`ScanTailorCleaner.clean` cuts each planned leaf out of its frame (rotation
  first, then the crop — IA scandata order), applies the operator override, and
  hands the cut leaves to the existing ``ScanTailorScans`` step with splitting off.
  ScanTailor's flags are per invocation, so leaves are grouped by their effective
  ``(color_mode, despeckle)`` and each group gets its own run. Every call works in
  a private staging directory and writes only ``<leaf>.tif`` for its own leaves,
  so parallel chunks of one book never collide.
"""

from __future__ import annotations

import inspect
import logging
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from evilflowers_books_digitalizer.imaging import analyze_spread, load_bgr
from evilflowers_books_digitalizer.pipeline.base import BookContext
from evilflowers_books_digitalizer.pipeline.steps.scantailor import ScanTailorScans
from evilflowers_books_digitalizer.studio.domain import BBox, PageOverride
from evilflowers_books_digitalizer.studio.metadata import HandSide
from evilflowers_books_digitalizer.studio.ports import CleanedPage, LeafPlan, PortError

logger = logging.getLogger(__name__)

type GroupKey = tuple[str, str]


def single_page_side(leaf_num: int) -> HandSide:
    """Hand side of a one-page frame: leaf 0 is a recto, then they alternate (lr books)."""
    return HandSide.RIGHT if leaf_num % 2 == 0 else HandSide.LEFT


def rotate(img: np.ndarray, degrees: int) -> np.ndarray:
    """IA ``rotateDegree`` (clockwise, multiples of 90) applied to a frame."""
    return np.ascontiguousarray(np.rot90(img, k=-(degrees // 90) % 4)) if degrees % 360 else img


def deskew(img: np.ndarray, degrees: float) -> np.ndarray:
    """Operator deskew: rotate about the centre, filling uncovered corners with white."""
    h, w = img.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return cv2.warpAffine(img, matrix, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


class ScanTailorCleaner:
    def __init__(self, *, binary: str = "scantailor-deviant-cli", color_mode: str = "mixed",
                 despeckle: str = "cautious", margins_mm: float = 8.0, dewarping: str = "auto",
                 normalize_illumination: bool = True, dpi: int = 300,
                 output_dpi: int | None = None, split: bool = True) -> None:
        self.binary = binary
        self.color_mode = color_mode
        self.despeckle = despeckle
        self.margins_mm = margins_mm
        self.dewarping = dewarping
        self.normalize_illumination = normalize_illumination
        self.dpi = dpi
        self.output_dpi = output_dpi or dpi
        self.split = split

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> ScanTailorCleaner:
        """Build from a ``[pipeline.scantailor]`` table, ignoring keys we do not know."""
        known = set(inspect.signature(cls).parameters)
        return cls(**{k: v for k, v in settings.items() if k in known})

    # ------------------------------------------------------------------ plan
    def plan(self, frames: Sequence[Path]) -> list[LeafPlan]:
        plans: list[LeafPlan] = []
        for frame in frames:
            try:
                img = load_bgr(frame)
            except ValueError:
                logger.warning("skipping unreadable frame %s", frame)
                continue
            plans.extend(self._plan_frame(frame, img, len(plans)))
        return plans

    def _plan_frame(self, frame: Path, img: np.ndarray, first: int) -> list[LeafPlan]:
        h, w = img.shape[:2]
        layout = analyze_spread(img) if self.split else None
        if layout is not None and layout.gutter_x is not None:
            gx = int(layout.gutter_x)
            return [LeafPlan(first, frame, 0, BBox(0, 0, gx, h), 0, HandSide.LEFT.value),
                    LeafPlan(first + 1, frame, 1, BBox(gx, 0, w, h), 0, HandSide.RIGHT.value)]
        return [LeafPlan(first, frame, None, BBox(0, 0, w, h), 0, single_page_side(first).value)]

    # ------------------------------------------------------------------ clean
    def clean(self, plans: Sequence[LeafPlan], out_dir: Path,
              overrides: dict[int, PageOverride] | None = None) -> list[CleanedPage]:
        out_dir = out_dir.resolve()   # Leptonica/ScanTailor choke on macOS /tmp symlinks
        out_dir.mkdir(parents=True, exist_ok=True)
        overrides = overrides or {}
        groups: dict[GroupKey, list[LeafPlan]] = {}
        for plan in sorted(plans, key=lambda p: p.leaf_num):
            groups.setdefault(self._group_key(overrides.get(plan.leaf_num)), []).append(plan)
        pages: list[CleanedPage] = []
        for key, group in groups.items():
            pages += self._clean_group(key, group, out_dir, overrides)
        return sorted(pages, key=lambda p: p.leaf_num)

    def _group_key(self, override: PageOverride | None) -> GroupKey:
        o = override or PageOverride()
        return (o.color_mode or self.color_mode, o.despeckle or self.despeckle)

    def _clean_group(self, key: GroupKey, plans: list[LeafPlan], out_dir: Path,
                     overrides: dict[int, PageOverride]) -> list[CleanedPage]:
        stage = Path(tempfile.mkdtemp(prefix=".st-", dir=out_dir))
        try:
            inputs = self._stage_leaves(plans, stage / "in", overrides)
            ctx = BookContext(source="studio", book_id=stage.name, work_dir=stage,
                              output_dir=stage, tiffs=inputs)
            self._step(*key).run(ctx)
            if len(ctx.tiffs) != len(plans):
                raise PortError(f"ScanTailor returned {len(ctx.tiffs)} pages for {len(plans)} leaves")
            return [self._collect(plan, src, out_dir, overrides.get(plan.leaf_num))
                    for plan, src in zip(plans, ctx.tiffs, strict=True)]
        except RuntimeError as exc:   # ScanTailorScans signals tool failures as RuntimeError
            raise PortError(str(exc)) from exc
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _step(self, color_mode: str, despeckle: str) -> ScanTailorScans:
        return ScanTailorScans(binary=self.binary, color_mode=color_mode, margins_mm=self.margins_mm,
                               dewarping=self.dewarping,
                               normalize_illumination=self.normalize_illumination,
                               despeckle=despeckle, dpi=self.dpi, output_dpi=self.output_dpi,
                               split=False)

    def _stage_leaves(self, plans: list[LeafPlan], stage: Path,
                      overrides: dict[int, PageOverride]) -> list[Path]:
        """Cut every leaf out of its frame into ``stage/<leaf>.tif`` (name order = leaf order)."""
        stage.mkdir(parents=True)
        cache: tuple[Path, np.ndarray] | None = None
        out: list[Path] = []
        for plan in plans:
            if cache is None or cache[0] != plan.frame:   # spreads: two leaves, one read
                cache = (plan.frame, load_bgr(plan.frame))
            leaf = cut_leaf(cache[1], plan, overrides.get(plan.leaf_num))
            dst = stage / f"{plan.leaf_num:04d}.tif"
            cv2.imwrite(str(dst), leaf)
            out.append(dst)
        return out

    def _collect(self, plan: LeafPlan, src: Path, out_dir: Path,
                 override: PageOverride | None) -> CleanedPage:
        dst = out_dir / f"{plan.leaf_num:04d}.tif"
        shutil.move(src, dst)
        with Image.open(dst) as im:
            width, height = im.size
        skew = override.deskew_deg if override and override.deskew_deg is not None else 0.0
        return CleanedPage(plan.leaf_num, dst, width, height, self.output_dpi, skew)


def cut_leaf(frame: np.ndarray, plan: LeafPlan, override: PageOverride | None) -> np.ndarray:
    """Rotation, then the crop (override wins), then the operator's deskew."""
    img = rotate(frame, plan.rotate_degree)
    crop = override.crop if override and override.crop else plan.crop
    h, w = img.shape[:2]
    leaf = img[max(0, crop.y0):min(h, crop.y1), max(0, crop.x0):min(w, crop.x1)]
    if leaf.size == 0:
        raise PortError(f"leaf {plan.leaf_num}: crop {crop} is outside the {w}x{h} frame")
    if override and override.deskew_deg:
        leaf = deskew(leaf, override.deskew_deg)
    return leaf
