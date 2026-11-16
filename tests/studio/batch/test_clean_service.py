"""The clean stage on scan-service, end to end (evilflowers-books-digitalizer#4, #6).

Real Celery (eager) with ``link`` / ``link_error``: the ledger sends ``evilflowers_scan.clean``,
the reference implementation (:mod:`studio.testing.standalone`, registered by the harness)
stands in for evilflowers-scan-service on the synthetic scan tree, its ``job_done`` callback
applies the outcome, and the run goes on (marks, analyze, formulas, render, gate).
docs/diagrams/sequence-digitalizer-clean-service.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from batch_harness import CLEAN as TASK
from batch_harness import PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Book, Job, Run, utcnow
from evilflowers_books_digitalizer.studio.config import SERVICE_BATCH, _orchestration
from evilflowers_books_digitalizer.studio.domain import (
    BookStatus,
    JobStatus,
    PageStatus,
    RegionAction,
    RegionKind,
    RegionSource,
    RegionStatus,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.orchestration import contract, ledger
from evilflowers_books_digitalizer.studio.testing import fakes
from evilflowers_books_digitalizer.studio.workers import context, tasks

QUEUE, INTERACTIVE = "evilflowers_scan.standalone", "evilflowers_scan.standalone.interactive"


def _studio(tmp_path: Path, **route: object) -> Studio:
    studio = make_studio(tmp_path, orchestration=_orchestration(
        {"stages": {"clean": dict(route)}} if route else {}))
    tasks.discover.delay(PROJECT)
    return studio


def _clean_jobs(studio: Studio, run_id: int) -> list[Job]:
    return (studio.session().query(Job).filter_by(run_pk=run_id, stage="clean")
            .order_by(Job.seq).all())


def _attempt_dirs(studio: Studio) -> set[Path]:
    return {d for p in studio.book().pages
            for d in [contract.attempt_dir(p.clean_path)] if d is not None}


# ---------------------------------------------------------------- end to end
def test_full_run_cleans_on_the_service(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert studio.book().status == BookStatus.APPROVED

    job, marks = _clean_jobs(studio, run.id)              # 4 leaves, batch 32: one job + tail
    assert (contract.part(marks), marks.backend) == ("marks", "local")
    assert (job.backend, job.task, job.queue, job.status) == ("service", TASK, QUEUE, "applied")
    assert job.params["pages"] == [0, 1, 2, 3]
    pages = studio.book().pages
    assert {p.status for p in pages} != {PageStatus.PENDING}
    for page in pages:                                    # promoted: rows point into the attempt
        assert contract.attempt_dir(page.clean_path) == tmp_path / job.params["output_dir"]
        assert Path(page.clean_path).is_file() and Path(page.dzi_path).is_file()
        assert page.preview_path and Path(page.preview_path).suffix == ".webp"
    stages = [j.stage for j in studio.session().query(Job).filter_by(run_pk=run.id)]
    assert set(stages) >= {"clean", "analyze", "render", "gate"}


def _mark(category: str, band: str, bbox: list[int], p: float = 0.9) -> dict:
    return {"bbox": bbox, "category": category, "p": p, "band": band, "model": "signals",
            "signals": {"position": "margin"}, "reason": None}


def test_scan_marks_become_disputed_stamp_proposals_at_analyze(tmp_path: Path) -> None:
    """scan-service's artefact candidates (evilflowers-scan-service#2) wait on the page and
    enter the analyze fusion: a stamp or sticker always needs the operator's consent; a mark
    the decider kept as content, or a category with no region kind, is not proposed."""
    studio = _studio(tmp_path)
    studio.engines.marks[1] = [_mark("stamp", "hide", [10, 10, 60, 60]),
                               _mark("sticker", "keep", [100, 100, 150, 150]),
                               _mark("edge", "hide", [0, 0, 5, 200])]
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    page = next(p for p in studio.book().pages if p.index == 1)
    assert [m["category"] for m in page.marks or []] == ["stamp", "sticker", "edge"]
    scan = [r for r in page.regions if r.source == RegionSource.SCAN]
    assert [(r.kind, r.x0, r.y0, r.x1, r.y1, r.status, r.action) for r in scan] == [
        (RegionKind.STAMP, 10, 10, 60, 60, RegionStatus.DISPUTED, RegionAction.REMOVE)]
    assert all(not p.regions or p.index == 1 or
               RegionSource.SCAN not in {r.source for r in p.regions}
               for p in studio.book().pages)


def test_a_reclean_replaces_the_marks_of_the_page(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    studio.engines.marks[0] = [_mark("stamp", "review", [10, 10, 60, 60])]
    studio.execute(RunKind.FULL)
    assert next(p for p in studio.book().pages if p.index == 0).marks
    studio.engines.marks.clear()                          # this time scan-service finds none
    assert studio.execute(RunKind.RERUN, pages=[0]).status == RunStatus.SUCCEEDED
    page = next(p for p in studio.book().pages if p.index == 0)
    assert page.marks == []
    assert RegionSource.SCAN not in {r.source for r in page.regions}


def test_operator_rerun_is_one_interactive_job_per_leaf(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    studio.execute(RunKind.FULL)
    rerun = studio.execute(RunKind.RERUN, pages=[1, 3], interactive=True)
    assert rerun.status == RunStatus.SUCCEEDED, rerun.error
    jobs = [j for j in _clean_jobs(studio, rerun.id) if contract.part(j) is None]
    assert [(j.queue, j.params["pages"]) for j in jobs] == [(INTERACTIVE, [1]), (INTERACTIVE, [3])]


def test_a_partly_superseded_attempt_stays_until_no_page_points_into_it(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    studio.execute(RunKind.FULL)
    (first,) = _attempt_dirs(studio)

    studio.execute(RunKind.RERUN, pages=[1], interactive=True)
    assert first.is_dir()                                 # leaves 0, 2, 3 still live there
    assert len(_attempt_dirs(studio)) == 2

    studio.execute(RunKind.RERUN, pages=[0, 2, 3], interactive=True)
    assert not first.exists()                             # the last page moved off it
    assert all(d.is_dir() for d in _attempt_dirs(studio))
    assert all(Path(p.clean_path).is_file() for p in studio.book().pages)


class _Broken(fakes.FakePageCleaner):
    def clean(self, plans, out_dir, overrides=None):  # type: ignore[no-untyped-def]
        (out_dir / "partial.tif").write_bytes(b"half a page")
        raise RuntimeError("scantailor exited 3")


def test_a_failing_service_fails_the_run_through_the_errback_and_leaves_no_files(
        tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    run = studio.execute(RunKind.FULL)                   # plans the leaves, cleans them once
    studio.engines.cleaner = _Broken(dpi=300)
    failed = studio.execute(RunKind.RERUN, pages=[2], interactive=True)
    assert failed.status == RunStatus.FAILED
    assert "scantailor exited 3" in (failed.error or "")  # read back from the failed outcome
    (job,) = _clean_jobs(studio, failed.id)
    assert job.status == JobStatus.FAILED
    assert not (tmp_path / job.params["output_dir"]).exists()
    assert _clean_jobs(studio, run.id)[0].status == JobStatus.APPLIED   # the old pages stand


# ---------------------------------------------------------------- without a callback
def test_reconcile_discards_a_lost_attempt_before_sending_it_again(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    run_id = studio.new_run(RunKind.FULL)
    assert ledger.start_run(context.current(), run_id) == "started"

    class Silent:                                         # the message is lost on the way
        sent: list[ledger.Dispatch] = []

        def send(self, d: ledger.Dispatch) -> None:
            self.sent.append(d)

        def start_run(self, run_id: int) -> None: ...
        def revoke(self, task_ids: list[str]) -> None: ...

    transport = Silent()
    ctx = context.current()
    ledger.pump(ctx, transport, ctx.admission)  # type: ignore[arg-type]
    (d,) = transport.sent
    half = tmp_path / d.kwargs["output_dir"]
    half.mkdir(parents=True)
    (half / "0000.tif").write_bytes(b"from a worker that died")

    result = ledger.reconcile(ctx, transport, utcnow() + timedelta(hours=2))  # type: ignore[arg-type]
    assert result["resent"] == 1
    assert not half.exists()
    job = studio.session().get(Job, d.job_id)
    assert (job.status, job.budget) == (JobStatus.PLANNED, 1)
    assert studio.session().get(Run, run_id).status == RunStatus.RUNNING


# ---------------------------------------------------------------- configuration
def test_batch_sizes_per_lane() -> None:
    route = _orchestration({}).routes["clean"]
    assert (route.task, route.queue, route.interactive_queue) == (TASK, QUEUE, INTERACTIVE)
    assert route.batch_size(interactive=False) == SERVICE_BATCH
    assert route.batch_size(interactive=True) == 1
    tuned = _orchestration({"stages": {"clean": {"batch": 4, "interactive_batch": 2}}}
                           ).routes["clean"]
    assert tuned.batch_size(interactive=False) == 4
    assert tuned.batch_size(interactive=True) == 2


def test_a_bulk_book_is_split_into_service_batches(tmp_path: Path) -> None:
    studio = _studio(tmp_path, batch=3)
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert [j.params.get("pages") for j in _clean_jobs(studio, run.id)] == [[0, 1, 2], [3], None]
    assert studio.session().query(Book).one().status == BookStatus.APPROVED
