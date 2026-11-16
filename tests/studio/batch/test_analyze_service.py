"""The analyze stage on services, end to end (evilflowers-books-digitalizer#4).

Each leaf batch is two jobs, ``hocr`` (evilflowers-ocr-service) and ``layout``
(evilflowers-analyzer-service), fused by the orchestrator when the second one is applied;
leaves with confirmed "remove" boxes then get an ``erase`` job (evilflowers-scan-service).
The reference tasks (:mod:`studio.testing.standalone`) stand in for the services, through
real Celery (eager) with ``link`` / ``link_error``.
docs/diagrams/sequence-digitalizer-analyze-service.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from batch_harness import PROJECT, Studio, make_studio
from PIL import Image

from evilflowers_books_digitalizer.db.models import Job, Region
from evilflowers_books_digitalizer.studio.config import (
    OrchestrationConfig,
    RoutePart,
    StageRoute,
    _orchestration,
)
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
from evilflowers_books_digitalizer.studio.testing import standalone
from evilflowers_books_digitalizer.studio.workers import context, tasks
from evilflowers_books_digitalizer.studio.workers.app import celery

HOCR, LAYOUT, ERASE = ("evilflowers_ocr_worker.standalone.hocr",
                       "evilflowers_analyzer.standalone.layout", "evilflowers_scan.erase")


class _BrokenDetector:
    name = "docling"

    def detect(self, image, *, ocr=None):  # type: ignore[no-untyped-def]
        raise MemoryError("docling ran out of memory")


@pytest.fixture(autouse=True)
def _services() -> Iterator[None]:
    """The three stand-in service tasks, on the eager app for this module only."""

    def _ctx():  # type: ignore[no-untyped-def]
        ctx = context.current()
        assert ctx.config.storage_root is not None
        return ctx, ctx.adapters(PROJECT)

    @celery.task(name=HOCR)
    def hocr(**message):  # type: ignore[no-untyped-def]
        ctx, a = _ctx()
        return standalone.ocr_hocr(ctx.config.storage_root, a.ocr, a.image_ops, **message)

    @celery.task(name=LAYOUT)
    def layout(**message):  # type: ignore[no-untyped-def]
        ctx, a = _ctx()
        return standalone.analyzer_layout(ctx.config.storage_root, a.detectors, a.image_ops,
                                          **message)

    @celery.task(name=ERASE)
    def erase(**message):  # type: ignore[no-untyped-def]
        ctx, a = _ctx()
        return standalone.scan_erase(ctx.config.storage_root, a.image_ops, **message)

    yield
    for name in (HOCR, LAYOUT, ERASE):
        celery.tasks.pop(name, None)


def _routes(service: bool) -> OrchestrationConfig:
    routes = dict(OrchestrationConfig().routes)
    if service:
        routes["analyze"] = StageRoute(
            "service", "", "", "", 1800, batch=32,
            parts={"hocr": RoutePart(HOCR, "evilflowers_ocr_worker.standalone",
                                     "evilflowers_ocr_worker.standalone.interactive"),
                   "layout": RoutePart(LAYOUT, "evilflowers_analyzer.standalone",
                                       "evilflowers_analyzer.standalone.interactive"),
                   "erase": RoutePart(ERASE, "evilflowers_scan.standalone",
                                      "evilflowers_scan.standalone.interactive")})
    return OrchestrationConfig(routes=routes)


def _studio(tmp_path: Path, *, service: bool = True) -> Studio:
    studio = make_studio(tmp_path, orchestration=_routes(service), storage_root=tmp_path)
    tasks.discover.delay(PROJECT)
    return studio


def _jobs(studio: Studio, run_id: int, stage: str = "analyze") -> list[Job]:
    return (studio.session().query(Job).filter_by(run_pk=run_id, stage=stage)
            .order_by(Job.seq, Job.id).all())


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
        "formulas", "render", "gate"}


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
    studio.adapters.detectors = [_BrokenDetector()]       # the analyzer fails mid-batch
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.FAILED
    assert "docling ran out of memory" in (run.error or "")   # from its failed outcome
    assert [(contract.part(j), j.status) for j in _jobs(studio, run.id)] == [
        ("hocr", "applied"), ("layout", "failed")]
    for job in _jobs(studio, run.id):
        assert not (tmp_path / job.params["output_dir"]).exists(), contract.part(job)


# ---------------------------------------------------------------- same result as in-process
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


def _after_decided_rerun(tmp_path: Path, service: bool) -> tuple[Studio, int]:
    studio = _studio(tmp_path, service=service)
    studio.execute(RunKind.FULL)
    _decide(studio, 1)
    rerun = studio.execute(RunKind.RERUN, pages=[1], interactive=True)
    assert rerun.status == RunStatus.SUCCEEDED, rerun.error
    return studio, rerun.id


def test_service_analyze_gives_the_in_process_result(tmp_path: Path) -> None:
    local, _ = _after_decided_rerun(tmp_path / "local", service=False)
    local_ocr_input = _pixels(local.adapters.ocr.images[-1])  # type: ignore[attr-defined]
    remote, run_id = _after_decided_rerun(tmp_path / "service", service=True)
    remote_ocr_input = _pixels(remote.adapters.ocr.images[-1])  # type: ignore[attr-defined]

    a = next(p for p in local.book().pages if p.index == 1)
    b = next(p for p in remote.book().pages if p.index == 1)
    assert np.array_equal(local_ocr_input, remote_ocr_input)   # both masks applied for OCR
    assert np.array_equal(_pixels(a.clean_path), _pixels(b.clean_path))   # stamp erased
    assert np.array_equal(_pixels(a.preview_path), _pixels(b.preview_path))
    assert (a.ocr_text, a.ocr_conf, a.status) == (b.ocr_text, b.ocr_conf, b.status)

    def regions(page):  # type: ignore[no-untyped-def]
        return sorted((r.kind, r.x0, r.y0, r.x1, r.y1, r.status, r.action,
                       r.source == RegionSource.OPERATOR) for r in page.regions)

    assert regions(a) == regions(b)

    parts = [contract.part(j) for j in _jobs(remote, run_id)]
    assert parts == ["hocr", "layout", "erase"]           # erase only after the fuse
    erase = _jobs(remote, run_id)[-1]
    assert erase.status == JobStatus.APPLIED and erase.params["pages"] == [1]
    assert contract.attempt_dir(b.clean_path) == (tmp_path / "service" /
                                                  erase.params["output_dir"]).resolve()
    stamp = next(r for r in b.regions if r.action == RegionAction.REMOVE)
    picture = next(r for r in b.regions if r.action == RegionAction.AS_IMAGE)
    page_pixels = _pixels(b.clean_path)
    assert (page_pixels[stamp.y0:stamp.y1, stamp.x0:stamp.x1] == 255).all()   # erased
    kept = page_pixels[picture.y0:picture.y1, picture.x0:picture.x1]
    assert not (kept == 255).all()                       # the picture stays on the page …
    assert (remote_ocr_input[picture.y0:picture.y1, picture.x0:picture.x1] == 255).all()  # … not in OCR


# ---------------------------------------------------------------- configuration
def test_a_split_stage_runs_entirely_on_services_or_not_at_all() -> None:
    with pytest.raises(ValueError, match=r"analyze\.layout\] with task and queue"):
        _orchestration({"stages": {"analyze": {"backend": "service",
                                               "hocr": {"task": HOCR, "queue": "q"}}}})
    route = _orchestration({"stages": {"analyze": {
        "backend": "service", "hocr": {"task": HOCR, "queue": "o"},
        "layout": {"task": LAYOUT, "queue": "l", "interactive_queue": "l.i"}}}}).routes["analyze"]
    assert (route.part("hocr").interactive_queue, route.part("layout").interactive_queue) == (
        "o", "l.i")
    assert route.part("erase").task == ERASE                 # on-demand part: scan-service default
    assert route.batch == 32 and route.interactive_batch == 1
