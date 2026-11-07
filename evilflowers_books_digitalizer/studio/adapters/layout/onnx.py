"""LayoutDetector on DocLayout-YOLO (DocStructBench classes) exported to ONNX, CPU inference.

Enabled per project with ``layout_detectors = ["heuristic", "doclayout"]``; settings
live in ``[projects.<p>.pipeline.layout]`` (``model``, ``conf``, ``iou``, ``imgsz``,
``threads``). Only the ONNX graph is ever loaded — never the PyTorch checkpoint
(a pickle) and never ``ultralytics`` / ``doclayout_yolo``.

The exported model is YOLOv10 (NMS-free): its output is ``[1, 300, 6]`` rows of
``x1, y1, x2, y2, score, class`` in letterboxed input pixels. :func:`decode` also
accepts a raw YOLOv8-style head ``[1, 4 + nc, anchors]`` (``cx, cy, w, h`` +
class scores) so a different export can be dropped in via ``model``; a per-class
NMS runs either way (it is a no-op on the NMS-free output, a necessity on the raw one).

``onnxruntime`` is imported when the session is first needed, so importing this
module (the API process, the test suite) costs nothing.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from evilflowers_books_digitalizer.studio.adapters.layout.heuristic import read_page_image
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    RegionKind,
    RegionProposal,
    RegionSource,
)
from evilflowers_books_digitalizer.studio.ports import OcrPage, PortError

#: where ``tools/`` installs the model (see docs/studio.md, "Layout detectors")
DEFAULT_MODEL = (Path.home() / ".local/share/evilflowers-tools/models/doclayout-yolo"
                 / "doclayout_yolo_docstructbench_imgsz1024.onnx")

#: DocStructBench class order (the ONNX ``names`` metadata, verified 2026-11-07)
DOCSTRUCTBENCH_CLASSES: tuple[str, ...] = (
    "title", "plain text", "abandon", "figure", "figure_caption", "table", "table_caption",
    "table_footnote", "isolate_formula", "formula_caption",
)

#: DocStructBench class -> region kind. "abandon" (running heads, folios, footnote
#: rules) is page furniture: header or footer by position, see :func:`kind_for`.
DOCSTRUCTBENCH: dict[str, RegionKind] = {
    "title": RegionKind.TITLE, "plain text": RegionKind.TEXT, "abandon": RegionKind.PAGE_HEADER,
    "figure": RegionKind.PICTURE, "figure_caption": RegionKind.CAPTION, "table": RegionKind.TABLE,
    "table_caption": RegionKind.CAPTION, "table_footnote": RegionKind.FOOTNOTE,
    "isolate_formula": RegionKind.FORMULA, "formula_caption": RegionKind.CAPTION,
}

PAD_VALUE = 114     # ultralytics letterbox grey


# ---------------------------------------------------------------- pre/post-processing
@dataclass(frozen=True, slots=True)
class Letterbox:
    """How a page was fitted into the square model input: ``input = page * scale + pad``."""

    scale: float
    pad_x: int          # left / top padding in input pixels (page centred)
    pad_y: int
    width: int          # original page size
    height: int
    new_w: int          # resized page size inside the input
    new_h: int

    @classmethod
    def fit(cls, width: int, height: int, size: int) -> Letterbox:
        scale = min(size / width, size / height)
        new_w, new_h = min(size, round(width * scale)), min(size, round(height * scale))
        return cls(scale, (size - new_w) // 2, (size - new_h) // 2, width, height, new_w, new_h)

    def to_page(self, boxes: np.ndarray) -> np.ndarray:
        """Input-pixel ``xyxy`` -> page-pixel ``xyxy``, clipped to the page."""
        out = boxes.astype(np.float64, copy=True)
        out[:, [0, 2]] = ((out[:, [0, 2]] - self.pad_x) / self.scale).clip(0, self.width)
        out[:, [1, 3]] = ((out[:, [1, 3]] - self.pad_y) / self.scale).clip(0, self.height)
        return out


def letterbox(img: np.ndarray, size: int) -> tuple[np.ndarray, Letterbox]:
    """8-bit gray/BGR page -> ``(1, 3, size, size)`` float32 RGB in 0..1, plus the mapping."""
    if img.ndim == 2:
        rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    else:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    box = Letterbox.fit(w, h, size)
    resized = cv2.resize(rgb, (box.new_w, box.new_h), interpolation=cv2.INTER_AREA
                         if box.scale < 1 else cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), PAD_VALUE, dtype=np.uint8)
    canvas[box.pad_y:box.pad_y + box.new_h, box.pad_x:box.pad_x + box.new_w] = resized
    tensor = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return np.ascontiguousarray(tensor), box


def decode(output: np.ndarray, nc: int) -> np.ndarray:
    """Model output -> ``(N, 6)`` rows of ``x1, y1, x2, y2, score, class`` (input pixels).

    Accepts the NMS-free YOLOv10 layout ``(1, N, 6)`` and the raw YOLOv8 head
    ``(1, 4 + nc, N)`` (``cx, cy, w, h`` then one score per class).
    """
    out = output[0] if output.ndim == 3 else output
    if out.ndim == 2 and out.shape[1] == 6 and out.shape[0] != 4 + nc:
        return out.astype(np.float32, copy=False)
    if out.ndim == 2 and out.shape[0] == 4 + nc:
        raw = out.T
        cx, cy, w, h = raw[:, 0], raw[:, 1], raw[:, 2], raw[:, 3]
        scores = raw[:, 4:]
        cls = scores.argmax(axis=1)
        return np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2,
                         scores[np.arange(len(cls)), cls], cls], axis=1).astype(np.float32)
    raise PortError(f"unexpected layout model output shape {output.shape} (nc={nc})")


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> np.ndarray:
    """Greedy non-maximum suppression; returns kept indices, best score first."""
    order = scores.argsort()[::-1]
    x0, y0, x1, y1 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0, x1 - x0) * np.maximum(0, y1 - y0)
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        iw = np.maximum(0, np.minimum(x1[i], x1[rest]) - np.maximum(x0[i], x0[rest]))
        ih = np.maximum(0, np.minimum(y1[i], y1[rest]) - np.maximum(y0[i], y0[rest]))
        inter = iw * ih
        union = areas[i] + areas[rest] - inter
        overlap = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
        order = rest[overlap < iou]
    return np.asarray(keep, dtype=np.int64)


def batched_nms(dets: np.ndarray, iou: float) -> np.ndarray:
    """Per-class NMS over ``(N, 6)`` detections (figure vs caption overlaps survive)."""
    if not len(dets):
        return dets
    offset = dets[:, 5:6] * (float(dets[:, :4].max()) + 1.0)
    keep = nms(dets[:, :4] + offset, dets[:, 4], iou)
    return dets[keep]


def kind_for(name: str, bbox: BBox, page_height: int) -> RegionKind:
    """Region kind of a DocStructBench class; "abandon" is a header above mid-page."""
    kind = DOCSTRUCTBENCH.get(name, RegionKind.OTHER)
    if kind == RegionKind.PAGE_HEADER and (bbox.y0 + bbox.y1) / 2 > page_height / 2:
        return RegionKind.PAGE_FOOTER
    return kind


def proposals(dets: np.ndarray, box: Letterbox, names: Sequence[str]) -> list[RegionProposal]:
    """Filtered input-space detections -> page-space proposals (zero-area boxes dropped)."""
    out: list[RegionProposal] = []
    for row, page_xyxy in zip(dets, box.to_page(dets[:, :4]), strict=True):
        cls = int(row[5])
        name = names[cls] if 0 <= cls < len(names) else ""
        try:
            bbox = BBox.of(page_xyxy)
        except ValueError:
            continue
        out.append(RegionProposal(kind_for(name, bbox, box.height), bbox, RegionSource.YOLO,
                                  round(float(row[4]), 3)))
    return out


# ---------------------------------------------------------------- the adapter
def parse_names(meta: dict[str, str]) -> tuple[str, ...] | None:
    """Class names from ultralytics ``names`` metadata (``{0: 'title', ...}``), if present."""
    raw = meta.get("names")
    if not raw:
        return None
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(parsed, dict):
        return None
    return tuple(str(parsed[i]) for i in sorted(parsed))


class OnnxLayout:
    """DocLayout-YOLO via onnxruntime (CPU). The session is created on first ``detect``."""

    name = "doclayout"

    def __init__(self, model: Path | str = DEFAULT_MODEL, *, conf: float = 0.25,
                 iou: float = 0.5, imgsz: int = 1024, threads: int = 0,
                 names: Sequence[str] = DOCSTRUCTBENCH_CLASSES) -> None:
        self.model = Path(model).expanduser()
        self.conf = conf
        self.iou = iou
        self.imgsz = imgsz
        self.threads = threads          # 0 = onnxruntime default (all physical cores)
        self.names: tuple[str, ...] = tuple(names)
        self._session: Any = None

    def _get_session(self) -> Any:
        if self._session is None:
            if not self.model.is_file():
                raise PortError(f"DocLayout-YOLO ONNX model not found: {self.model}")
            try:
                import onnxruntime as ort
            except ImportError as exc:
                raise PortError("onnxruntime is not installed on this worker") from exc
            opts = ort.SessionOptions()
            if self.threads:
                opts.intra_op_num_threads = self.threads
            session = ort.InferenceSession(str(self.model), sess_options=opts,
                                           providers=["CPUExecutionProvider"])
            names = parse_names(session.get_modelmeta().custom_metadata_map)
            if names:
                self.names = names
            self._session = session
        return self._session

    def detect(self, image: Path, *, ocr: OcrPage | None = None) -> list[RegionProposal]:
        session = self._get_session()
        tensor, box = letterbox(read_page_image(image), self.imgsz)
        (output,) = session.run(None, {session.get_inputs()[0].name: tensor})[:1]
        dets = decode(np.asarray(output), len(self.names))
        dets = dets[dets[:, 4] >= self.conf]
        return proposals(batched_nms(dets, self.iou), box, self.names)
