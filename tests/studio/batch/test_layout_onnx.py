"""DocLayout-YOLO ONNX adapter: pre/post-processing without the model, plus one real run."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from evilflowers_books_digitalizer.studio import ports
from evilflowers_books_digitalizer.studio.adapters import registry
from evilflowers_books_digitalizer.studio.adapters.layout import OnnxLayout
from evilflowers_books_digitalizer.studio.adapters.layout.onnx import (
    DEFAULT_MODEL,
    DOCSTRUCTBENCH_CLASSES,
    Letterbox,
    batched_nms,
    decode,
    kind_for,
    letterbox,
    nms,
    parse_names,
    proposals,
)
from evilflowers_books_digitalizer.studio.config import ProjectConfig, StudioConfig
from evilflowers_books_digitalizer.studio.domain import BBox, RegionKind, RegionSource
from evilflowers_books_digitalizer.studio.ports import PortError

NC = len(DOCSTRUCTBENCH_CLASSES)


# ---------------------------------------------------------------- letterbox
def test_letterbox_fits_portrait_page_centred() -> None:
    img = np.zeros((2000, 1000), dtype=np.uint8)            # gray, portrait
    tensor, box = letterbox(img, 1024)
    assert tensor.shape == (1, 3, 1024, 1024) and tensor.dtype == np.float32
    assert box.scale == pytest.approx(0.512)
    assert (box.new_w, box.new_h, box.pad_x, box.pad_y) == (512, 1024, 256, 0)
    assert tensor.max() <= 1.0
    assert tensor[0, :, 0, 0] == pytest.approx([114 / 255] * 3)        # left pad is grey
    assert tensor[0, :, 512, 512] == pytest.approx([0, 0, 0])          # page is black


def test_letterbox_converts_bgr_to_rgb() -> None:
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[..., 0] = 255                                       # pure blue in BGR
    tensor, box = letterbox(img, 64)
    assert box.pad_x == box.pad_y == 0
    assert tensor[0, :, 32, 32] == pytest.approx([0, 0, 1])           # R, G, B


def test_letterbox_round_trip_to_page_coordinates() -> None:
    box = Letterbox.fit(2862, 4582, 1024)
    page = np.array([[100.0, 200.0, 2800.0, 4500.0]])
    scaled = page * box.scale + [box.pad_x, box.pad_y, box.pad_x, box.pad_y]
    assert box.to_page(scaled) == pytest.approx(page, abs=1e-6)
    # boxes spilling into the padding are clipped to the page
    assert box.to_page(np.array([[0.0, -5.0, 1024.0, 1100.0]])).tolist() == [
        [0.0, 0.0, 2862.0, 4582.0]]


# ---------------------------------------------------------------- decode + nms
def test_decode_accepts_nms_free_output() -> None:
    out = np.array([[[1, 2, 30, 40, 0.9, 0], [5, 5, 50, 20, 0.1, 8]]], dtype=np.float32)
    np.testing.assert_allclose(decode(out, NC), out[0])


def test_decode_accepts_raw_head() -> None:
    raw = np.zeros((1, 4 + NC, 2), dtype=np.float32)
    raw[0, :4, 0] = [50, 50, 20, 10]                 # cx, cy, w, h
    raw[0, 4 + 8, 0] = 0.8                           # isolate_formula
    raw[0, :4, 1] = [10, 10, 4, 4]
    raw[0, 4 + 1, 1] = 0.3
    dets = decode(raw, NC)
    np.testing.assert_allclose(dets, [[40, 45, 60, 55, 0.8, 8], [8, 8, 12, 12, 0.3, 1]],
                               rtol=1e-6)


def test_decode_rejects_unknown_layout() -> None:
    with pytest.raises(PortError, match="unexpected"):
        decode(np.zeros((1, 7, 3), dtype=np.float32), NC)


def test_nms_suppresses_overlaps_within_a_class_only() -> None:
    boxes = np.array([[0, 0, 10, 10], [1, 1, 10, 10], [50, 50, 60, 60]], dtype=np.float32)
    assert nms(boxes, np.array([0.5, 0.9, 0.7]), 0.5).tolist() == [1, 2]
    dets = np.array([[0, 0, 10, 10, 0.9, 3],       # figure
                     [1, 1, 10, 10, 0.8, 3],       # duplicate figure: dropped
                     [0, 0, 10, 10, 0.7, 4]],      # its caption on the same spot: kept
                    dtype=np.float32)
    assert batched_nms(dets, 0.5)[:, 4].tolist() == pytest.approx([0.9, 0.7])
    assert len(batched_nms(np.zeros((0, 6), dtype=np.float32), 0.5)) == 0


# ---------------------------------------------------------------- class mapping
def test_class_mapping_and_abandon_by_position() -> None:
    top, bottom = BBox(0, 10, 100, 50), BBox(0, 950, 100, 990)
    assert kind_for("abandon", top, 1000) == RegionKind.PAGE_HEADER
    assert kind_for("abandon", bottom, 1000) == RegionKind.PAGE_FOOTER
    assert kind_for("isolate_formula", top, 1000) == RegionKind.FORMULA
    assert kind_for("formula_caption", top, 1000) == RegionKind.CAPTION
    assert kind_for("plain text", top, 1000) == RegionKind.TEXT
    assert kind_for("mystery", top, 1000) == RegionKind.OTHER


def test_proposals_rescale_and_drop_degenerate_boxes() -> None:
    box = Letterbox.fit(2048, 2048, 1024)                   # scale 0.5, no padding
    dets = np.array([[10, 20, 110, 70, 0.876, 8],
                     [5, 5, 5, 40, 0.9, 1],                 # zero width
                     [0, 0, 50, 10, 0.6, 99]], dtype=np.float32)
    out = proposals(dets, box, DOCSTRUCTBENCH_CLASSES)
    assert [(p.kind, p.bbox.as_list(), p.source, p.confidence) for p in out] == [
        (RegionKind.FORMULA, [20, 40, 220, 140], RegionSource.YOLO, 0.876),
        (RegionKind.OTHER, [0, 0, 100, 20], RegionSource.YOLO, 0.6)]


def test_parse_names_from_ultralytics_metadata() -> None:
    meta = {"names": "{0: 'title', 1: 'plain text', 2: 'abandon'}"}
    assert parse_names(meta) == ("title", "plain text", "abandon")
    assert parse_names({}) is None and parse_names({"names": "not python"}) is None


# ---------------------------------------------------------------- adapter + registry
class _FakeSession:
    def __init__(self, output: np.ndarray) -> None:
        self.output = output
        self.seen: list[tuple[int, ...]] = []

    def get_inputs(self) -> list[Any]:
        return [type("I", (), {"name": "images"})()]

    def run(self, _: Any, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.seen.append(feeds["images"].shape)
        return [self.output]


def test_adapter_is_lazy_and_filters_by_confidence(tmp_path: Path) -> None:
    page = tmp_path / "0000.tif"
    Image.new("L", (1024, 1024), 255).save(page)
    layout = OnnxLayout(tmp_path / "missing.onnx", conf=0.3)
    assert isinstance(layout, ports.LayoutDetector) and layout.name == "doclayout"
    with pytest.raises(PortError, match="not found"):
        layout.detect(page)

    session = _FakeSession(np.array([[[10, 10, 500, 60, 0.95, 0],
                                      [10, 980, 60, 1010, 0.8, 2],
                                      [10, 100, 900, 400, 0.2, 1]]], dtype=np.float32))
    layout._session = session
    out = layout.detect(page)
    assert session.seen == [(1, 3, 1024, 1024)]
    assert [(p.kind, p.confidence) for p in out] == [(RegionKind.TITLE, 0.95),
                                                     (RegionKind.PAGE_FOOTER, 0.8)]


def _project(tmp_path: Path, **kw: Any) -> ProjectConfig:
    return ProjectConfig(slug="stu", name="STU", scans_root=tmp_path,
                         item_id="stu-{collection}-{book}", **kw)


def test_registry_builds_doclayout_without_loading_it(tmp_path: Path) -> None:
    project = _project(tmp_path, layout_detectors=("heuristic", "doclayout", "yolo"),
                       pipeline={"layout": {"model": "~/m.onnx", "conf": 0.4, "threads": 2}})
    cfg = StudioConfig("sqlite://", "memory://", tmp_path / "a", tmp_path / "c", {"stu": project})
    built = registry.build_adapters(cfg, project)
    assert [d.name for d in built.detectors] == ["heuristic", "doclayout", "doclayout"]
    onnx = built.detectors[1]
    assert isinstance(onnx, OnnxLayout)
    assert (onnx.model, onnx.conf, onnx.threads) == (Path.home() / "m.onnx", 0.4, 2)
    assert "onnxruntime" not in sys.modules or onnx._session is None

    bad = _project(tmp_path, layout_detectors=("doclayout",),
                   pipeline={"layout": {"weights": "x.pt"}})
    with pytest.raises(PortError, match="unknown"):
        registry.build_adapters(cfg, bad)
    assert OnnxLayout().model == DEFAULT_MODEL


# ---------------------------------------------------------------- the real model
@pytest.mark.integration
def test_real_model_finds_title_and_text(tmp_path: Path) -> None:
    pytest.importorskip("onnxruntime")
    if not DEFAULT_MODEL.is_file():
        pytest.skip(f"model not installed: {DEFAULT_MODEL}")
    from PIL import ImageDraw, ImageFont

    page = tmp_path / "page.tif"
    img = Image.new("L", (1700, 2400), 255)
    draw = ImageDraw.Draw(img)
    try:
        title = ImageFont.load_default(size=72)
        body = ImageFont.load_default(size=34)
    except TypeError:                                       # Pillow < 10.1
        title = body = ImageFont.load_default()
    draw.text((200, 250), "1.4 Krivkový integrál", font=title, fill=0)
    for i in range(14):
        draw.text((200, 450 + i * 50), "Elementárny vektor má v danom bode krivky smer jej "
                  "dotyčnice a veľkosť", font=body, fill=0)
    draw.text((830, 2250), "29", font=body, fill=0)
    img.save(page)

    layout = OnnxLayout(threads=2)
    started = time.perf_counter()
    out = layout.detect(page)
    assert time.perf_counter() - started < 60
    kinds = {p.kind for p in out}
    assert RegionKind.TEXT in kinds, out
    assert all(p.source == RegionSource.YOLO and 0 < p.confidence <= 1 for p in out)
    assert all(0 <= p.bbox.x0 < p.bbox.x1 <= 1700 and 0 <= p.bbox.y0 < p.bbox.y1 <= 2400
               for p in out)
    body_box = BBox(200, 450, 1500, 1150)
    assert any(p.kind == RegionKind.TEXT and p.bbox.iou(body_box) > 0.5 for p in out), out
