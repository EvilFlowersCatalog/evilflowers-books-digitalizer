"""Render stage outputs: region decisions steer the EPUB, JP2 pages are encoded once."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Any

import pytest
from batch_harness import PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Book, Page
from evilflowers_books_digitalizer.reflow.edition import RegionDecision
from evilflowers_books_digitalizer.studio.adapters import render as render_adapter
from evilflowers_books_digitalizer.studio.adapters.ia import images
from evilflowers_books_digitalizer.studio.adapters.ia.naming import leaf_image_name
from evilflowers_books_digitalizer.studio.adapters.render import StudioRenderer, region_decisions
from evilflowers_books_digitalizer.studio.domain import (
    BBox,
    RegionKind,
    RegionProposal,
    RegionSource,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import PortError, RenderRequest
from evilflowers_books_digitalizer.studio.testing import fakes
from evilflowers_books_digitalizer.studio.workers import tasks


def region(kind: str, status: str, action: str, text: str | None = None,
           bbox: tuple[int, int, int, int] = (10, 20, 110, 60)) -> dict[str, Any]:
    return {"id": 1, "kind": kind, "bbox": list(bbox), "status": status, "action": action,
            "source": "yolo", "text": text}


# ---------------------------------------------------------------- EPUB: region decisions
def test_region_decisions_keep_what_the_edition_must_honour() -> None:
    page = [
        region("stamp", "confirmed", "remove"),
        region("formula", "proposed", "reocr", "a+b"),
        region("formula", "confirmed", "reocr", "  "),             # no LaTeX: a crop
        region("picture", "proposed", "as_image"),
        region("section_header", "confirmed", "keep"),
        region("text", "proposed", "keep"),                         # a detector's guess: ignored
        region("stamp", "disputed", "remove"),                      # undecided: ignored
        region("stamp", "rejected", "remove"),                      # "not a stamp": ignored
        region("formula", "confirmed", "reocr", "x", bbox=(5, 5, 5, 9)),   # empty box
    ]
    decisions = region_decisions([page, []], 2)
    assert decisions == [[
        RegionDecision("stamp", (10, 20, 110, 60), "remove", None),
        RegionDecision("formula", (10, 20, 110, 60), "reocr", "a+b"),
        RegionDecision("formula", (10, 20, 110, 60), "reocr", None),
        RegionDecision("picture", (10, 20, 110, 60), "as_image", None),
        RegionDecision("section_header", (10, 20, 110, 60), "keep", None),
    ], []]
    assert region_decisions(None, 2) is None                      # no rows: heuristics only
    with pytest.raises(PortError):
        region_decisions([page], 2)


def test_studio_renderer_hands_the_decisions_to_the_edition(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_edition(pages: list[Path], hocr: Path, out: Path, *args: Any, **kwargs: Any) -> None:
        seen.update(pages=len(pages), decisions=kwargs.get("decisions"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"epub")

    monkeypatch.setattr(render_adapter, "build_edition", fake_edition)
    pages = fakes.make_frames(tmp_path / "pages", 2, spread=False)
    hocr = tmp_path / "book.hocr"
    hocr.write_text("<html/>")
    produced = StudioRenderer().render(RenderRequest(
        "item", pages, hocr, ItemMetadata(identifier="item"), ["epub"],
        {"out_dir": tmp_path / "out",
         "regions": [[region("formula", "confirmed", "reocr", "x^2")], []]}))
    assert produced["epub"].read_bytes() == b"epub"
    assert seen == {"pages": 2, "decisions": [
        [RegionDecision("formula", (10, 20, 110, 60), "reocr", "x^2")], []]}


def test_render_stage_passes_the_region_rows_per_access_page(tmp_path: Path) -> None:
    formula = RegionProposal(RegionKind.FORMULA, BBox(10, 160, 150, 200),
                             RegionSource.HEURISTIC, 0.9)
    studio = make_studio(tmp_path, layout=fakes.FakeLayout(extra=[formula]))
    tasks.discover.delay(PROJECT)
    s = studio.session()
    book = s.query(Book).filter_by(project=PROJECT).one()
    assert studio.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    s.expire_all()
    pages = s.query(Page).filter_by(book_pk=book.id).order_by(Page.index).all()
    s.get(Page, pages[0].id).add_to_access = False                 # one leaf out of the edition
    s.commit()
    assert studio.execute(RunKind.RENDER).status == RunStatus.SUCCEEDED

    renderer: fakes.FakeRenderer = studio.adapters.renderer  # type: ignore[assignment]
    req = renderer.requests[-1]
    rows = req.extra["regions"]
    assert isinstance(rows, list) and len(rows) == len(req.pages) == len(pages) - 1
    decisions = region_decisions(rows, len(req.pages))
    assert decisions is not None
    # the formulas stage stored the recognizer's LaTeX on the proposed formula region
    assert all(any(d.kind == "formula" and d.latex == "x^2" for d in page) for page in decisions)


# ---------------------------------------------------------------- JP2 cache
@pytest.fixture
def encodes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Count encodes; the bytes depend on the file content, like a real encoder."""
    calls: list[Path] = []

    def fake_encode(image: Path, opts: images.Jp2Options = images.Jp2Options()) -> bytes:
        calls.append(image)
        data = image.read_bytes()
        return f"jp2:{image.name}:{len(data)}:{hash(data)}".encode()

    monkeypatch.setattr(images, "jp2_supported", lambda: True)
    monkeypatch.setattr(images, "encode_jp2", fake_encode)
    return calls


def _rewrite(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data + b"\0")                                 # new size
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


def test_jp2_cache_reencodes_only_changed_leaves(tmp_path: Path, encodes: list[Path]) -> None:
    pages = fakes.make_frames(tmp_path / "pages", 3, spread=False)
    cache, out = tmp_path / "cache", tmp_path / "item" / "item_jp2.zip"
    names = [leaf_image_name("item", i) for i in range(3)]

    images.build_jp2_zip("item", pages, out, cache_dir=cache)
    assert len(encodes) == 3
    with zipfile.ZipFile(out) as zf:
        first = {n: zf.read(n) for n in zf.namelist()}
    assert list(first) == names

    images.build_jp2_zip("item", pages, out, cache_dir=cache)
    assert len(encodes) == 3                                         # nothing re-encoded
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == names and {n: zf.read(n) for n in names} == first

    _rewrite(pages[1])
    images.build_jp2_zip("item", pages, out, cache_dir=cache)
    assert encodes[3:] == [pages[1]]                                 # only the changed leaf
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == names
        assert zf.read(names[0]) == first[names[0]] and zf.read(names[1]) != first[names[1]]
    assert len(list(cache.glob("0001-*.jp2"))) == 1                  # the stale entry is gone


def test_render_runs_reencode_only_the_pages_that_changed(
        discovered: Studio, encodes: list[Path]) -> None:
    assert discovered.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    s = discovered.session()
    leaves = s.query(Page).order_by(Page.index).all()
    assert len(encodes) == len(leaves)

    assert discovered.execute(RunKind.RENDER).status == RunStatus.SUCCEEDED
    assert len(encodes) == len(leaves)                               # unchanged book: 0 encodes

    changed = Path(leaves[-1].clean_path or "")
    _rewrite(changed)
    assert discovered.execute(RunKind.RENDER).status == RunStatus.SUCCEEDED
    assert encodes[len(leaves):] == [changed]
    book = discovered.session().query(Book).one()
    package = Path(book.artifacts["images"])
    with zipfile.ZipFile(package) as zf:
        assert zf.namelist() == [leaf_image_name(book.item_id or "", i)
                                 for i in range(len(leaves))]
