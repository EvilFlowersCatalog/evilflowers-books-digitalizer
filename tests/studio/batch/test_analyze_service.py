"""The analyze stage on services, end to end (evilflowers-books-digitalizer#4, #6).

Each leaf batch is two jobs, ``hocr`` (evilflowers-ocr-service) and ``layout``
(evilflowers-analyzer-service), fused by the orchestrator when the second one is applied;
leaves with confirmed "remove" boxes then get an ``erase`` job (evilflowers-scan-service).
The reference tasks (:mod:`studio.testing.standalone`, registered by the harness) stand in for
the services, through real Celery (eager) with ``link`` / ``link_error``.
docs/diagrams/sequence-digitalizer-analyze-service.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from batch_harness import ERASE, HOCR, LAYOUT, PROJECT, Studio, make_studio
from PIL import Image

from evilflowers_books_digitalizer.db.models import Job, Region
from evilflowers_books_digitalizer.studio.config import _orchestration
from evilflowers_books_digitalizer.studio.domain import (
    BookStatus,
    JobStatus,
    RegionAction,
    RegionKind,
    RegionSource,
    RegionStatus,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.orchestration import contract
from evilflowers_books_digitalizer.studio.workers import tasks


class _BrokenDetector:
    name = "docling"

    def detect(self, image, *, ocr=None):  # type: ignore[no-untyped-def]
        raise MemoryError("docling ran out of memory")


def _studio(tmp_path: Path) -> Studio:
    studio = make_studio(tmp_path)
    tasks.discover.delay(PROJECT)
    return studio


def _jobs(studio: Studio, run_id: int, stage: str = "analyze") -> list[Job]:
    return [j for j in studio.session().query(Job).filter_by(run_pk=run_id, stage=stage)
            .order_by(Job.seq, Job.id).all() if contract.part(j) != "title_page"]


# ---------------------------------------------------------------- end to end
def test_full_run_analyzes_each_batch_on_two_services_and_fuses_it(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert studio.book().status == BookStatus.APPROVED

    jobs = _jobs(studio, run.id)                          # 4 leaves, batch 32: one batch
    assert [(contract.part(j), j.task, j.status) for j in jobs] == [
        ("hocr", HOCR, "applied"), ("layout", LAYOUT, "applied")]
    assert {contract.job_key(j) for j in jobs} == {"analyze-hocr-0000-a1",
                                                   "analyze-layout-0000-a1"}
    hocr_dir = tmp_path / jobs[0].params["output_dir"]
    for page in studio.book().pages:
        assert page.ocr_text and page.ocr_conf == pytest.approx(92.0)
        assert contract.attempt_dir(page.hocr_path) == hocr_dir.resolve()
        machine = [r for r in page.regions if r.source != RegionSource.OPERATOR]
        assert machine and {r.source for r in machine} == {RegionSource.DOCLING}
    assert not (tmp_path / jobs[1].params["output_dir"]).exists()   # layout consumed
    assert {j.stage for j in studio.session().query(Job).filter_by(run_pk=run.id)} >= {
        "render", "gate"}                                 # no formula regions: formulas skipped


def test_an_operator_rerun_sends_one_leaf_per_part_to_the_interactive_queues(
        tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    studio.execute(RunKind.FULL)
    rerun = studio.execute(RunKind.RERUN, pages=[2], interactive=True)
    assert rerun.status == RunStatus.SUCCEEDED, rerun.error
    assert [(contract.part(j), j.queue, j.params["pages"]) for j in _jobs(studio, rerun.id)] == [
        ("hocr", "evilflowers_ocr_worker.standalone.interactive", [2]),
        ("layout", "evilflowers_analyzer.standalone.interactive", [2])]


def test_a_failing_half_fails_the_run_and_leaves_no_attempt_dirs(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    studio.engines.detectors = [_BrokenDetector()]        # the analyzer fails mid-batch
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.FAILED
    assert "docling ran out of memory" in (run.error or "")   # from its failed outcome
    assert [(contract.part(j), j.status) for j in _jobs(studio, run.id)] == [
        ("hocr", "applied"), ("layout", "failed")]
    for job in _jobs(studio, run.id):
        assert not (tmp_path / job.params["output_dir"]).exists(), contract.part(job)


# ---------------------------------------------------------------- operator decisions
def _decide(studio: Studio, leaf: int) -> None:
    """The operator confirms a stamp to remove and a picture to keep as an image."""
    s = studio.session()
    page = next(p for p in studio.book().pages if p.index == leaf)
    page = s.merge(page)
    mid = page.height // 2
    for kind, action, x0 in ((RegionKind.STAMP, RegionAction.REMOVE, 10),
                             (RegionKind.PICTURE, RegionAction.AS_IMAGE, 60)):
        s.add(Region(page_pk=page.id, kind=str(kind), x0=x0, y0=mid + 10, x1=x0 + 40,
                     y1=mid + 50, source=str(RegionSource.OPERATOR),
                     status=str(RegionStatus.CONFIRMED), action=str(action), confidence=1.0))
    s.commit()


def _pixels(path: str | Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("L"))


def test_masks_go_to_the_services_and_the_erase_to_scan_service(tmp_path: Path) -> None:
    """Remove boxes: masked for OCR and layout, then painted out of the page by
    ``evilflowers_scan.erase``; as-image boxes: masked for OCR only, the page keeps them."""
    studio = _studio(tmp_path)
    studio.execute(RunKind.FULL)
    _decide(studio, 1)
    rerun = studio.execute(RunKind.RERUN, pages=[1], interactive=True)
    assert rerun.status == RunStatus.SUCCEEDED, rerun.error
    ocr_input = _pixels(studio.engines.ocr.images[-1])

    parts = [contract.part(j) for j in _jobs(studio, rerun.id)]
    assert parts == ["hocr", "layout", "erase"]           # erase only after the fuse
    erase = _jobs(studio, rerun.id)[-1]
    assert (erase.task, erase.status, erase.params["pages"]) == (ERASE, JobStatus.APPLIED, [1])
    page = next(p for p in studio.book().pages if p.index == 1)
    assert contract.attempt_dir(page.clean_path) == (tmp_path / erase.params["output_dir"]).resolve()
    stamp = next(r for r in page.regions if r.action == RegionAction.REMOVE)
    picture = next(r for r in page.regions if r.action == RegionAction.AS_IMAGE)
    page_pixels = _pixels(page.clean_path)
    assert (page_pixels[stamp.y0:stamp.y1, stamp.x0:stamp.x1] == 255).all()   # erased
    kept = page_pixels[picture.y0:picture.y1, picture.x0:picture.x1]
    assert not (kept == 255).all()                       # the picture stays on the page …
    assert (ocr_input[picture.y0:picture.y1, picture.x0:picture.x1] == 255).all()  # … not in OCR
    assert (ocr_input[stamp.y0:stamp.y1, stamp.x0:stamp.x1] == 255).all()


# ---------------------------------------------------------------- configuration
def test_the_analyze_parts_are_overridden_one_by_one() -> None:
    route = _orchestration({"stages": {"analyze": {
        "hocr": {"queue": "o"},
        "layout": {"task": LAYOUT, "queue": "l", "interactive_queue": "l.i"}}}}).routes["analyze"]
    assert (route.part("hocr").task, route.part("hocr").interactive_queue) == (HOCR, "o")
    assert route.part("layout").interactive_queue == "l.i"
    assert route.part("erase").task == ERASE                 # on-demand part: scan-service default
    assert route.batch == 32 and route.interactive_batch == 1
