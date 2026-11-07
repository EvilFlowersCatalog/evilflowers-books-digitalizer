"""Celery tasks end to end (eager) over SQLite, the in-memory bus and the shared fakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from batch_harness import PROJECT, Studio, make_studio
from PIL import Image

from evilflowers_books_digitalizer.db.models import Book, Note, Page, Publication, Region, Run
from evilflowers_books_digitalizer.studio.domain import (
    DOCLAYNET,
    BBox,
    BookStatus,
    PageOverride,
    PageStatus,
    RegionKind,
    RegionProposal,
    RegionSource,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.events import EventType
from evilflowers_books_digitalizer.studio.ports import PortError
from evilflowers_books_digitalizer.studio.testing import fakes
from evilflowers_books_digitalizer.studio.workers import tasks


def _pages(studio: Studio) -> list[Page]:
    return studio.session().query(Page).order_by(Page.index).all()


# ---------------------------------------------------------------- discover
def test_discover_upserts_books_once(discovered: Studio) -> None:
    book = discovered.book()
    assert book.status == BookStatus.DISCOVERED and book.item_id == "stu-fei-book1"
    assert book.n_frames == 2 and book.item_metadata["identifier"] == "stu-fei-book1"
    tasks.discover.delay(PROJECT, "fei")          # idempotent: no second row, no new event
    assert discovered.session().query(Book).count() == 1
    assert len(discovered.events(EventType.BOOK_DISCOVERED)) == 1
    finished = discovered.events(EventType.IMPORT_FINISHED)
    assert finished[-1].data == {"collection": "fei", "books": 1, "new": 0}


# ---------------------------------------------------------------- full run
def test_full_run_reaches_gate_with_regions(discovered: Studio) -> None:
    run = discovered.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert run.stage == "gate" and run.done == run.total
    book = discovered.book()
    assert book.status == BookStatus.APPROVED          # fakes: 92% OCR, nothing disputed
    assert book.quality["passed"] is True and book.quality_score is not None
    pages = _pages(discovered)
    assert [p.index for p in pages] == [0, 1, 2, 3]
    assert [p.hand_side for p in pages] == ["LEFT", "RIGHT", "LEFT", "RIGHT"]
    assert all(p.status == PageStatus.ANALYZED and p.version == 1 for p in pages)
    assert all(p.crop_box and len(p.crop_box) == 4 for p in pages)
    assert all(Path(p.dzi_path or "").exists() and Path(p.preview_path or "").exists()
               for p in pages)
    regions = discovered.session().query(Region).all()
    assert len(regions) == 4 and {r.kind for r in regions} == {RegionKind.TEXT}
    store = discovered.adapters.artifacts
    for kind in ("hocr", "text", "images", "regions_json", "pdf", "pdfa", "epub",
                 "meta_xml", "scandata_xml", "page_numbers", "files_xml"):
        assert store.exists(book.item_id or "", kind), kind
        assert book.artifacts[kind] == str(store.path(book.item_id or "", kind))
    assert book.item_metadata["title"] == "Testovacia kniha"
    assert book.item_metadata["imagecount"] == 4
    types = {e.type for e in discovered.bus.published}
    assert {EventType.RUN_STARTED, EventType.RUN_PROGRESS, EventType.RUN_LOG,
            EventType.PAGE_UPDATED, EventType.REGIONS_UPDATED, EventType.BOOK_READY,
            EventType.RUN_FINISHED} <= types


def test_full_run_renders_access_leaves_only_and_ia_hocr(discovered: Studio) -> None:
    discovered.execute(RunKind.FULL)
    s = discovered.session()
    page = s.query(Page).filter_by(index=2).one()
    page.add_to_access = False
    s.commit()
    run = discovered.execute(RunKind.RENDER)
    assert run.status == RunStatus.SUCCEEDED, run.error
    request = discovered.adapters.renderer.requests[-1]          # type: ignore[attr-defined]
    assert [p.name for p in request.pages] == ["0000.tif", "0001.tif", "0003.tif"]
    hocr = Path(discovered.book().artifacts["hocr"]).read_text()
    assert [f"page_{i:06d}" in hocr for i in range(4)] == [True] * 4
    assert discovered.book().status == BookStatus.APPROVED       # render runs do not gate


def test_gate_flags_disputed_regions(tmp_path: Path) -> None:
    stamp = RegionProposal(RegionKind.STAMP, BBox(10, 160, 60, 200), RegionSource.HEURISTIC, 0.9)
    studio = make_studio(tmp_path, layout=fakes.FakeLayout(extra=[stamp]))
    tasks.discover.delay(PROJECT)
    studio.execute(RunKind.FULL)
    book = studio.book()
    assert book.status == BookStatus.REVIEW
    assert any("disputed" in f for f in book.quality["flags"])
    assert all(p.status == PageStatus.DISPUTED for p in _pages(studio))
    review = studio.events(EventType.BOOK_REVIEW_NEEDED)
    assert review and review[0].data["flags"] == book.quality["flags"]


def test_auto_publish_after_gate(tmp_path: Path) -> None:
    studio = make_studio(tmp_path, auto_publish="gate")
    tasks.discover.delay(PROJECT)
    studio.execute(RunKind.FULL)
    book = studio.book()
    assert book.status == BookStatus.PUBLISHED
    kinds = [(r.kind, r.status) for r in studio.session().query(Run).order_by(Run.id)]
    assert kinds == [("full", "succeeded"), ("publish", "succeeded")]
    assert studio.events(EventType.BOOK_PUBLISHED)[0].data["version"] == 1


# ---------------------------------------------------------------- rerun
def test_rerun_bumps_version_and_honours_override(discovered: Studio) -> None:
    discovered.execute(RunKind.FULL)
    s = discovered.session()
    page = s.query(Page).filter_by(index=1).one()
    page.override = PageOverride(despeckle="off", crop=BBox(200, 0, 390, 300)).as_dict()
    page.regions.append(Region(kind="stamp", x0=0, y0=0, x1=40, y1=40, source="operator",
                               status="confirmed", action="remove", author="op"))
    page.regions.append(Region(kind="picture", x0=50, y0=50, x1=120, y1=120, source="operator",
                               status="confirmed", action="as_image", author="op"))
    s.commit()
    cleaner: fakes.FakePageCleaner = discovered.adapters.cleaner  # type: ignore[assignment]
    run = discovered.execute(RunKind.RERUN, pages=[1], interactive=True)
    assert run.status == RunStatus.SUCCEEDED, run.error

    _, leaves, overrides = cleaner.calls[-1]
    assert leaves == [1] and overrides[1].despeckle == "off" and overrides[1].crop.x0 == 200
    pages = _pages(discovered)
    assert [p.version for p in pages] == [1, 2, 1, 1]
    kept = [r for r in pages[1].regions if r.source == "operator"]
    assert len(kept) == 2 and all(r.status == "confirmed" for r in kept)
    with Image.open(pages[1].clean_path or "") as im:
        assert im.getpixel((5, 5)) == 255                         # removed: erased in the page
        assert im.getpixel((60, 60)) != 255                       # as_image: kept in the page
    ocr_input = discovered.adapters.ocr.images[-1]                # type: ignore[attr-defined]
    assert ocr_input.parent.name == "ocr"
    with Image.open(ocr_input) as im:
        assert im.getpixel((60, 60)) == 255                       # ... but hidden from OCR
    assert discovered.book().status == BookStatus.APPROVED        # reruns leave the book alone
    updated = [e for e in discovered.events(EventType.PAGE_UPDATED) if e.data["index"] == 1]
    assert updated[-1].data["version"] == 2


def test_rerun_keeps_rejected_regions_rejected(tmp_path: Path) -> None:
    stamp = RegionProposal(RegionKind.STAMP, BBox(10, 160, 60, 200), RegionSource.HEURISTIC, 0.9)
    studio = make_studio(tmp_path, layout=fakes.FakeLayout(extra=[stamp]))
    tasks.discover.delay(PROJECT)
    studio.execute(RunKind.FULL)
    s = studio.session()
    for r in s.query(Region).filter_by(kind="stamp"):
        r.status = "rejected"
    s.commit()
    studio.execute(RunKind.RERUN, pages=[0])
    stamps = studio.session().query(Region).filter_by(kind="stamp").join(Page).filter(
        Page.index == 0).all()
    assert [r.status for r in stamps] == ["rejected"]


def test_rerun_moves_notes_on_replaced_proposals_to_the_page(discovered: Studio) -> None:
    """A re-analysis replaces machine proposals; the operator's note must survive."""
    discovered.execute(RunKind.FULL)
    s = discovered.session()
    page = s.query(Page).filter_by(index=0).one()
    proposal = next(r for r in page.regions if r.status == "proposed")
    s.add(Note(book_pk=page.book_pk, page_pk=page.id, region_pk=proposal.id, text="smudge?"))
    s.commit()
    page_id, proposal_id = page.id, proposal.id

    discovered.execute(RunKind.RERUN, pages=[0])

    s = discovered.session()
    note = s.query(Note).filter_by(text="smudge?").one()
    assert (note.region_pk, note.page_pk) == (None, page_id)
    assert s.get(Region, proposal_id) is None                      # the proposal was replaced


# ---------------------------------------------------------------- formulas
def test_formulas_fill_latex_and_reject_drawings(tmp_path: Path) -> None:
    formula = RegionProposal(RegionKind.FORMULA, BBox(10, 160, 150, 200), RegionSource.HEURISTIC,
                             0.9)
    studio = make_studio(tmp_path, layout=fakes.FakeLayout(extra=[formula]))
    studio.adapters.formulas = fakes.FakeFormulaRecognizer(default="\\[ E = mc^2 \\]")
    tasks.discover.delay(PROJECT)
    studio.execute(RunKind.FULL)
    texts = {r.text for r in studio.session().query(Region).filter_by(kind="formula")}
    assert texts == {"E = mc^2"}

    studio.adapters.formulas = fakes.FakeFormulaRecognizer(default="Obr. 3 \\alpha")
    studio.execute(RunKind.FULL)
    formulas = studio.session().query(Region).filter_by(kind="formula").all()
    assert all(r.text is None and "vlm rejected" in (r.reason or "") for r in formulas)
    book = studio.book()
    assert book.status == BookStatus.REVIEW
    assert book.quality["signals"]["vlm_rejected"] == 4


# ---------------------------------------------------------------- publish
def test_publish_records_publication_and_republishes(discovered: Studio) -> None:
    discovered.execute(RunKind.FULL)
    assert discovered.execute(RunKind.PUBLISH).status == RunStatus.SUCCEEDED
    assert discovered.execute(RunKind.PUBLISH).status == RunStatus.SUCCEEDED
    pubs = discovered.session().query(Publication).order_by(Publication.id).all()
    assert [p.version for p in pubs] == [1, 2]
    assert pubs[0].entry_id == pubs[1].entry_id == "entry-stu-fei-book1"
    assert set(pubs[1].files) == {"pdf", "pdfa", "epub"}
    assert pubs[1].files["pdf"]["acquisition"] == "acq-pdf"
    publisher: fakes.FakePublisher = discovered.adapters.publisher  # type: ignore[assignment]
    assert publisher.published == [("stu-fei-book1", None),
                                   ("stu-fei-book1", "entry-stu-fei-book1")]
    assert discovered.book().status == BookStatus.PUBLISHED


def test_publish_of_unreviewed_book_fails(tmp_path: Path) -> None:
    stamp = RegionProposal(RegionKind.STAMP, BBox(10, 160, 60, 200), RegionSource.HEURISTIC, 0.9)
    studio = make_studio(tmp_path, layout=fakes.FakeLayout(extra=[stamp]))
    tasks.discover.delay(PROJECT)
    studio.execute(RunKind.FULL)
    with pytest.raises(ValueError, match="review to published"):
        studio.execute(RunKind.PUBLISH)
    failed = studio.session().query(Run).filter_by(kind="publish").one()
    assert failed.status == RunStatus.FAILED and "review" in (failed.error or "")
    assert studio.book().status == BookStatus.REVIEW
    assert studio.adapters.publisher.published == []             # type: ignore[union-attr]


# ---------------------------------------------------------------- failures / guards
def test_failed_task_marks_run_and_book(discovered: Studio) -> None:
    class Broken(fakes.FakePageCleaner):
        def clean(self, plans, out_dir, overrides=None):
            raise PortError("scantailor exploded")

    discovered.adapters.cleaner = Broken()
    with pytest.raises(PortError):
        discovered.execute(RunKind.FULL)
    run = discovered.session().query(Run).one()
    assert run.status == RunStatus.FAILED and "scantailor exploded" in (run.error or "")
    assert discovered.book().status == BookStatus.ERROR
    finished = discovered.events(EventType.RUN_FINISHED)
    assert finished[-1].data["status"] == "failed"


def test_render_refuses_leaves_whose_ocr_was_evicted(discovered: Studio) -> None:
    discovered.execute(RunKind.FULL)
    pdf = Path(discovered.book().artifacts["pdf"])
    before = pdf.read_bytes()
    Path(_pages(discovered)[1].hocr_path or "").unlink()          # cache_dir is scratch
    with pytest.raises(PortError, match="OCR"):
        discovered.execute(RunKind.RENDER)
    assert pdf.read_bytes() == before                             # previous outputs intact
    assert discovered.book().status == BookStatus.APPROVED        # a failed render is not ERROR


def test_inactive_runs_are_skipped(discovered: Studio) -> None:
    run_id = discovered.new_run(RunKind.FULL)
    s = discovered.session()
    s.get(Run, run_id).status = RunStatus.CANCELLED
    s.commit()
    assert tasks.run.delay(run_id).get()["skipped"] is True
    assert tasks.clean.delay(run_id, [0]).get()["skipped"] is True
    assert discovered.session().query(Page).count() == 0


def test_publish_waits_for_an_earlier_render_of_the_same_book(discovered: Studio) -> None:
    """Approval enqueues render then publish with no ordering: publish must hold."""
    from evilflowers_books_digitalizer.studio.workers import context
    from evilflowers_books_digitalizer.studio.workers.stages.orchestrate import blocked_by

    render_id = discovered.new_run(RunKind.RENDER)
    publish_id = discovered.new_run(RunKind.PUBLISH)
    ctx = context.current()
    assert blocked_by(ctx, publish_id) == render_id
    assert blocked_by(ctx, render_id) is None                  # only earlier runs count
    s = discovered.session()
    s.get(Run, render_id).status = RunStatus.SUCCEEDED
    s.commit()
    assert blocked_by(ctx, publish_id) is None


# ---------------------------------------------------------------- training export
def test_export_training_writes_valid_coco(discovered: Studio) -> None:
    discovered.execute(RunKind.FULL)
    s = discovered.session()
    regions = s.query(Region).all()
    regions[0].status = "confirmed"                                  # a text block
    page = s.get(Page, regions[0].page_pk)
    page.regions.append(Region(kind="stamp", x0=0, y0=0, x1=20, y1=20, source="operator",
                               status="confirmed", action="remove"))
    page.regions.append(Region(kind="formula", x0=30, y0=30, x1=90, y1=60, source="operator",
                               status="confirmed", action="reocr"))
    s.commit()
    path = Path(tasks.export_training.delay(PROJECT).get())
    coco = json.loads(path.read_text())
    assert [c["name"] for c in coco["categories"]] == list(DOCLAYNET.values())
    assert len(coco["images"]) == 1 and len(coco["annotations"]) == 2   # stamp excluded
    names = {c["id"]: c["name"] for c in coco["categories"]}
    assert sorted(names[a["category_id"]] for a in coco["annotations"]) == ["Formula", "Text"]
    image = coco["images"][0]
    assert (path.parent / image["file_name"]).exists()
    for ann in coco["annotations"]:
        x, y, w, h = ann["bbox"]
        assert ann["image_id"] == image["id"] and w > 0 and h > 0 and ann["area"] == w * h
        assert x + w <= image["width"] and y + h <= image["height"]
    assert (path.parent / "crops" / "Formula").is_dir()
