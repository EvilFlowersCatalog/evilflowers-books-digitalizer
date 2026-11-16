"""The clean stage on a service, end to end (evilflowers-books-digitalizer#4).

Real Celery (eager) with ``link`` / ``link_error``: the ledger sends ``evilflowers_scan.clean``,
the reference implementation (:mod:`studio.testing.standalone`) stands in for
evilflowers-scan-service on the synthetic scan tree, its ``job_done`` callback applies the
outcome, and the rest of the run (analyze, formulas, render, gate) continues in-process.
docs/diagrams/sequence-digitalizer-clean-service.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from batch_harness import PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Book, Job, Run, utcnow
from evilflowers_books_digitalizer.studio.config import (
    SERVICE_BATCH,
    OrchestrationConfig,
    StageRoute,
    _orchestration,
)
from evilflowers_books_digitalizer.studio.domain import (
    BookStatus,
    JobStatus,
    PageStatus,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.orchestration import contract, ledger
from evilflowers_books_digitalizer.studio.testing import fakes, standalone
from evilflowers_books_digitalizer.studio.workers import context, tasks
from evilflowers_books_digitalizer.studio.workers.app import celery

TASK = "evilflowers_scan.clean"
QUEUE, INTERACTIVE = "evilflowers_scan.standalone", "evilflowers_scan.standalone.interactive"


@pytest.fixture(autouse=True)
def _scan_service() -> Iterator[None]:
    """Register the stand-in service task on the eager app for this module only."""

    @celery.task(name=TASK)
    def scan_clean(**message):  # type: ignore[no-untyped-def]
        ctx = context.current()
        adapters = ctx.adapters(PROJECT)
        assert ctx.config.storage_root is not None
        return standalone.scan_clean(ctx.config.storage_root, adapters.cleaner,
                                     adapters.image_ops, **message)

    yield
    celery.tasks.pop(TASK, None)


def _studio(tmp_path: Path, **route: object) -> Studio:
    routes = dict(OrchestrationConfig().routes)
    routes["clean"] = StageRoute("service", TASK, QUEUE, INTERACTIVE, 3600,
                                 **({"batch": SERVICE_BATCH} | route))  # type: ignore[arg-type]
    studio = make_studio(tmp_path, orchestration=OrchestrationConfig(routes=routes),
                         storage_root=tmp_path)
    tasks.discover.delay(PROJECT)
    return studio


def _clean_jobs(studio: Studio, run_id: int) -> list[Job]:
    return (studio.session().query(Job).filter_by(run_pk=run_id, stage="clean")
            .order_by(Job.seq).all())


def _attempt_dirs(studio: Studio) -> set[Path]:
    return {d for p in studio.book().pages
            for d in [contract.attempt_dir(p.clean_path)] if d is not None}


# ---------------------------------------------------------------- end to end
def test_full_run_cleans_on_the_service_and_finishes_in_process(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert studio.book().status == BookStatus.APPROVED

    (job,) = _clean_jobs(studio, run.id)                  # 4 leaves, batch 32: one job
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


def test_operator_rerun_is_one_interactive_job_per_leaf(tmp_path: Path) -> None:
    studio = _studio(tmp_path)
    studio.execute(RunKind.FULL)
    rerun = studio.execute(RunKind.RERUN, pages=[1, 3], interactive=True)
    assert rerun.status == RunStatus.SUCCEEDED, rerun.error
    jobs = _clean_jobs(studio, rerun.id)
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
    studio.adapters.cleaner = _Broken(dpi=300)
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
    routes = _orchestration({"stages": {"clean": {"backend": "service", "task": TASK,
                                                  "queue": QUEUE}}}).routes
    assert (routes["clean"].batch, routes["clean"].interactive_batch) == (SERVICE_BATCH, 1)
    local = OrchestrationConfig().routes["clean"]
    assert local.batch_size(interactive=False, project_default=8) == 8   # [pipeline] chunk_size
    assert local.batch_size(interactive=True, project_default=8) == 1
    tuned = _orchestration({"stages": {"clean": {"backend": "service", "task": TASK,
                                                 "queue": QUEUE, "batch": 4,
                                                 "interactive_batch": 2}}}).routes["clean"]
    assert tuned.batch_size(interactive=False, project_default=8) == 4
    assert tuned.batch_size(interactive=True, project_default=8) == 2


def test_a_bulk_book_is_split_into_service_batches(tmp_path: Path) -> None:
    studio = _studio(tmp_path, batch=3)
    run = studio.execute(RunKind.FULL)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert [j.params["pages"] for j in _clean_jobs(studio, run.id)] == [[0, 1, 2], [3]]
    assert studio.session().query(Book).one().status == BookStatus.APPROVED
