"""Reference implementations of the standalone service tasks, for tests and as a spec.

Each function does what its service task must do, with the digitalizer's own ports:

* :func:`scan_clean` — ``evilflowers_scan.clean`` (evilflowers-scan-service#1);
* :func:`scan_erase` — ``evilflowers_scan.erase`` (evilflowers-scan-service#1);
* :func:`ocr_hocr` — ``evilflowers_ocr_worker.standalone.hocr`` (evilflowers-ocr-service#7);
* :func:`analyzer_layout` — ``evilflowers_analyzer.standalone.layout``
  (evilflowers-analyzer-service#8);
* :func:`equation_formulas` — ``evilflowers_equation.standalone.formulas``
  (evilflowers-equation-service#8);
* :func:`render_book` — ``evilflowers_render.book`` (evilflowers-render-service#1).

They are the executable form of the contract in
:mod:`evilflowers_books_digitalizer.studio.orchestration.contract`:

* every path in the message and in the outcome is relative to the storage root
  (the service's ``STORAGE_PATH``);
* the frames are only read; everything is written under ``output_dir``;
* the outcome file is written **last** and atomically (temp file + rename), so its
  presence means the attempt's files are complete (``reconcile`` relies on that);
* ``run_id`` and ``token`` are echoed unchanged;
* every message is first checked as the kit's ``evilflowers_worker.contract.StandaloneJob``
  validates it (:func:`accept`), so a message a real service would refuse fails here too.

Not shipped to a service: the engines are the test doubles of :mod:`.engines`. The service
ports the behaviour, not the module.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from evilflowers_books_digitalizer.studio.adapters.ia.images import build_jp2_zip
from evilflowers_books_digitalizer.studio.adapters.ia.naming import ia_name
from evilflowers_books_digitalizer.studio.domain import BBox, PageOverride
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.orchestration.contract import (
    CleanOutcome,
    FormulasOutcome,
    HocrOutcome,
    LayoutOutcome,
    RenderOutcome,
)
from evilflowers_books_digitalizer.studio.ports import LeafPlan
from evilflowers_books_digitalizer.studio.testing.engines import (
    FormulaJob,
    FormulaRecognizer,
    ImageOps,
    LayoutDetector,
    OcrEngine,
    PageCleaner,
    Renderer,
    RenderRequest,
)

PREVIEW_PX = 1600


class StandaloneJob(BaseModel):
    """A copy of the fields of ``evilflowers_worker.contract.StandaloneJob`` (evilflowers-worker
    @ e530f68): the kit is not a dependency of the digitalizer, so its rules are mirrored here.
    Unknown fields are ignored, as in the kit."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    run_id: str = Field(min_length=1)
    job: str = Field(min_length=1)
    token: str = Field(min_length=1)
    inputs: list[str] = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    outcome: str = Field(min_length=1)


def accept(storage_root: Path, message: dict[str, Any]) -> StandaloneJob:
    """Validate ``message`` as the kit does before any work: the fields, and every input a
    relative path that stays under the storage root (``StandaloneJob.input_paths``). Also
    checks what the kit cannot: the input files exist."""
    job = StandaloneJob.model_validate(message)
    root = storage_root.resolve()
    for rel in job.inputs:
        path = (root / rel).resolve()
        if Path(rel).is_absolute() or not path.is_relative_to(root):
            raise ValueError(f"input escapes the storage root: {rel!r}")
        if not path.is_file():
            raise FileNotFoundError(f"input {rel!r} does not exist")
    return job


def write_outcome(path: Path, outcome: dict[str, Any]) -> None:
    """Atomically, as ``evilflowers_worker.outcome`` does: a reader sees all of it or nothing."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(outcome), encoding="utf-8")
    os.replace(tmp, path)


def scan_clean(storage_root: Path, cleaner: PageCleaner, image_ops: ImageOps, *,
               marks_of: Callable[[int], list[dict[str, Any]]] | None = None,
               **message: Any) -> dict[str, Any]:
    """One ``evilflowers_scan.clean`` job: the leaves of ``message`` -> cleaned pages + outcome.

    ``marks_of(leaf)`` stands in for the service's artefact check (``leaves[].marks``,
    evilflowers-scan-service#2): stamp / sticker candidates in cleaned-page pixels."""
    root = storage_root.resolve()
    accept(root, message)
    out = root / message["output_dir"]
    out.mkdir(parents=True, exist_ok=True)

    def rel(p: Path) -> str:
        return p.resolve().relative_to(root).as_posix()

    plans = [LeafPlan(leaf["leaf"], root / leaf["frame"], leaf.get("side"),
                      BBox.from_xywh(leaf["crop_box"]), leaf.get("rotate_degree") or 0,
                      leaf.get("hand_side"))
             for leaf in message["leaves"]]
    overrides = {leaf["leaf"]: PageOverride.from_dict(leaf["override"])
                 for leaf in message["leaves"] if leaf.get("override")}
    try:
        cleaned = cleaner.clean(plans, out, overrides)
        leaves = []
        for page in cleaned:
            dzi = image_ops.previews(page.image, out, max_px=PREVIEW_PX)
            webp = out / "leaves" / f"{page.image.stem}.webp"
            leaves.append({"leaf": page.leaf_num, "image": rel(page.image), "width": page.width,
                           "height": page.height, "dpi": page.dpi, "skew_deg": page.skew_deg,
                           "preview": rel(webp) if webp.exists() else None, "dzi": rel(dzi),
                           "marks": marks_of(page.leaf_num) if marks_of else []})
    except Exception as exc:
        write_outcome(root / message["outcome"], {"run_id": message["run_id"],
                                                  "token": message["token"], "status": "failed",
                                                  "error": f"{type(exc).__name__}: {exc}"})
        raise
    outcome = {"run_id": message["run_id"], "token": message["token"], "status": "done",
               "error": None, "leaves": leaves}
    CleanOutcome.model_validate(outcome)          # the reference never writes an invalid outcome
    write_outcome(root / message["outcome"], outcome)
    return {"job": message["job"], "leaves": len(leaves)}


# ---------------------------------------------------------------- analyze
def _masked(image_ops: ImageOps, image: Path, mask: list[list[int]], out: Path) -> Path:
    """The page as the service may read it: ``mask`` painted white on a PRIVATE copy."""
    if not mask:
        return image
    return image_ops.erase(image, [BBox(*b) for b in mask], out / "masked" / image.name)


def _failing(message: dict[str, Any], root: Path, exc: Exception) -> None:
    write_outcome(root / message["outcome"], {"run_id": message["run_id"],
                                              "token": message["token"], "status": "failed",
                                              "error": f"{type(exc).__name__}: {exc}"})


def ocr_hocr(storage_root: Path, ocr: OcrEngine, image_ops: ImageOps,
             **message: Any) -> dict[str, Any]:
    """``evilflowers_ocr_worker.standalone.hocr`` (evilflowers-ocr-service#7)."""
    root = storage_root.resolve()
    accept(root, message)
    out = root / message["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    leaves = []
    try:
        for leaf in message["leaves"]:
            image = _masked(image_ops, root / leaf["image"], leaf.get("mask") or [], out)
            page = ocr.recognize(image, language=leaf["language"], dpi=leaf["dpi"])
            hocr = out / f"{leaf['leaf']:04d}_hocr.html"
            hocr.write_text(page.hocr, encoding="utf-8")
            leaves.append({"leaf": leaf["leaf"], "hocr": hocr.relative_to(root).as_posix(),
                           "text": page.text, "mean_conf": page.mean_conf})
    except Exception as exc:
        _failing(message, root, exc)
        raise
    outcome = {"run_id": message["run_id"], "token": message["token"], "status": "done",
               "error": None, "leaves": leaves}
    HocrOutcome.model_validate(outcome)
    write_outcome(root / message["outcome"], outcome)
    return {"job": message["job"], "leaves": len(leaves)}


def analyzer_layout(storage_root: Path, detectors: list[LayoutDetector], image_ops: ImageOps,
                    **message: Any) -> dict[str, Any]:
    """``evilflowers_analyzer.standalone.layout`` (evilflowers-analyzer-service#8): regions as
    fractions of the page, origin top-left, in ``regions.json``."""
    root = storage_root.resolve()
    accept(root, message)
    out = root / message["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    try:
        for leaf in message["leaves"]:
            image = _masked(image_ops, root / leaf["image"], leaf.get("mask") or [], out)
            with Image.open(image) as im:
                w, h = im.size
            found = [p for d in detectors for p in d.detect(image, ocr=None)]
            rows += [{"leaf": leaf["leaf"], "kind": str(p.kind), "seq": i,
                      "bbox": [p.bbox.x0 / w, p.bbox.y0 / h, p.bbox.x1 / w, p.bbox.y1 / h],
                      "confidence": p.confidence} for i, p in enumerate(found)]
    except Exception as exc:
        _failing(message, root, exc)
        raise
    regions = out / "regions.json"
    regions.write_text(json.dumps(rows), encoding="utf-8")
    outcome = {"run_id": message["run_id"], "token": message["token"], "status": "done",
               "error": None, "regions": regions.relative_to(root).as_posix(),
               "leaves": [leaf["leaf"] for leaf in message["leaves"]]}
    LayoutOutcome.model_validate(outcome)
    write_outcome(root / message["outcome"], outcome)
    return {"job": message["job"], "regions": len(rows)}


def equation_formulas(storage_root: Path, recognizer: FormulaRecognizer,
                      **message: Any) -> dict[str, Any]:
    """``evilflowers_equation.standalone.formulas`` (evilflowers-equation-service#8): crops of
    the cleaned pages -> LaTeX / text in ``formulas.json``. A region without an answer is a
    row with ``text: null`` (or no row); the orchestrator judges every answer itself."""
    root = storage_root.resolve()
    accept(root, message)
    out = root / message["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    try:
        jobs = []
        for r in message["regions"]:
            image = root / r["image"]
            with Image.open(image) as im:
                w, h = im.size
            x0, y0, x1, y1 = r["bbox"]
            jobs.append(FormulaJob(r["key"], image, BBox.of((x0 * w, y0 * h, x1 * w, y1 * h)),
                                   r["task"]))
        answers = recognizer.recognize(jobs) if jobs else {}
    except Exception as exc:
        _failing(message, root, exc)
        raise
    # a fake's stand-in for the service's own checks (render SSIM, onnx cross-check)
    doubt = getattr(recognizer, "dispute", None)
    rows = [{"key": j.key, "text": answers.get(j.key), "model": "reference",
             **({"disputed": True, "reason": doubt} if doubt and answers.get(j.key) else {})}
            for j in jobs]
    formulas = out / "formulas.json"
    formulas.write_text(json.dumps(rows), encoding="utf-8")
    outcome = {"run_id": message["run_id"], "token": message["token"], "status": "done",
               "error": None, "formulas": formulas.relative_to(root).as_posix()}
    FormulasOutcome.model_validate(outcome)
    write_outcome(root / message["outcome"], outcome)
    return {"job": message["job"], "answered": sum(v is not None for v in answers.values())}


def render_book(storage_root: Path, renderer: Renderer, **message: Any) -> dict[str, Any]:
    """``evilflowers_render.book`` (evilflowers-render-service#1): the request -> media
    (distribution PDF, PDF/A-2b master, EPUB, IA JP2 package) in ``output_dir``. Only media:
    the text documents and the IA XML stay with the orchestrator."""
    root = storage_root.resolve()
    accept(root, message)
    out = root / message["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    request = json.loads((root / message["request"]).read_text(encoding="utf-8"))
    item = request["item_id"]
    targets = {fmt: out / ia_name(item, fmt) for fmt in request["formats"]}
    try:
        produced = renderer.render(RenderRequest(
            item, [root / p for p in request["pages"]], root / request["hocr"],
            ItemMetadata.from_dict(request["metadata"]), request["formats"],
            {"out_dir": out, "targets": targets, "dpi": request["dpi"],
             "regions": request["regions"]}))
        files = {}
        for fmt, path in produced.items():
            final = targets[fmt]
            if path.resolve() != final.resolve():       # a renderer that ignores targets
                path.replace(final)
            files[fmt] = final
        if request.get("jp2"):
            files["images"] = build_jp2_zip(item, [root / p for p in request["jp2"]],
                                            out / ia_name(item, "images"))
    except Exception as exc:
        _failing(message, root, exc)
        raise
    outcome = {"run_id": message["run_id"], "token": message["token"], "status": "done",
               "error": None, "pdfa_valid": None,
               "files": {k: {"path": p.relative_to(root).as_posix(), "size": p.stat().st_size,
                             "md5": hashlib.md5(p.read_bytes(), usedforsecurity=False).hexdigest()}
                         for k, p in files.items()}}
    RenderOutcome.model_validate(outcome)
    write_outcome(root / message["outcome"], outcome)
    return {"job": message["job"], "files": sorted(files)}


def scan_erase(storage_root: Path, image_ops: ImageOps, **message: Any) -> dict[str, Any]:
    """``evilflowers_scan.erase``: ``boxes`` painted white on the page -> a new page + previews."""
    root = storage_root.resolve()
    accept(root, message)
    out = root / message["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    leaves = []
    try:
        for leaf in message["leaves"]:
            source = root / leaf["image"]
            image = image_ops.erase(source, [BBox(*b) for b in leaf["boxes"]],
                                    out / source.name)
            with Image.open(image) as im:
                w, h = im.size
            dzi = image_ops.previews(image, out, max_px=PREVIEW_PX)
            webp = out / "leaves" / f"{image.stem}.webp"
            leaves.append({"leaf": leaf["leaf"], "image": image.relative_to(root).as_posix(),
                           "width": w, "height": h, "dpi": leaf["dpi"],
                           "preview": webp.relative_to(root).as_posix() if webp.exists() else None,
                           "dzi": dzi.resolve().relative_to(root).as_posix()})
    except Exception as exc:
        _failing(message, root, exc)
        raise
    outcome = {"run_id": message["run_id"], "token": message["token"], "status": "done",
               "error": None, "leaves": leaves}
    CleanOutcome.model_validate(outcome)
    write_outcome(root / message["outcome"], outcome)
    return {"job": message["job"], "leaves": len(leaves)}
