"""The job ledger without a worker in the loop: plan, admit, settle, fail, reconcile.

A :class:`RecordingTransport` stands in for Celery, so each step is driven by hand and
the scenarios eager end-to-end runs never reach (lost workers, stale attempts, broker
errors, preemption, admission limits) are asserted one by one. The canvas-era guards
of the stale-run sweeper are ported here (docs/diagrams/state-digitalizer-run-job).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from batch_harness import PROJECT, Studio, make_studio

from evilflowers_books_digitalizer.db.models import Book, Job, Run, utcnow
from evilflowers_books_digitalizer.studio.config import (
    BackpressureSource,
    OrchestrationConfig,
    SchedulingConfig,
    StageRoute,
)
from evilflowers_books_digitalizer.studio.domain import (
    BookStatus,
    JobStatus,
    Lane,
    PageStatus,
    RunKind,
    RunStatus,
)
from evilflowers_books_digitalizer.studio.orchestration import contract, ledger
from evilflowers_books_digitalizer.studio.orchestration.scheduling import (
    Admission,
    InMemoryControl,
    Mode,
    PeakWindow,
    RedisQueueDepth,
)
from evilflowers_books_digitalizer.studio.workers import context, tasks


@dataclass
class RecordingTransport:
    sent: list[ledger.Dispatch] = field(default_factory=list)
    started: list[int] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    fail_sends: bool = False

    def send(self, dispatch: ledger.Dispatch) -> None:
        if self.fail_sends:
            raise ConnectionError("broker gone")
        self.sent.append(dispatch)

    def start_run(self, run_id: int) -> None:
        self.started.append(run_id)

    def revoke(self, task_ids: list[str]) -> None:
        self.revoked.extend(task_ids)


@dataclass
class FixedBackpressure:
    over: dict[str, int] = field(default_factory=dict)

    def over_limit(self) -> dict[str, int]:
        return dict(self.over)


def _ctx() -> context.WorkerContext:
    return context.current()


def _jobs(studio: Studio, run_id: int) -> list[Job]:
    return studio.session().query(Job).filter_by(run_pk=run_id).order_by(Job.id).all()


def _pump(transport: RecordingTransport, admission: Admission | None = None,
          now: datetime | None = None) -> dict:
    ctx = _ctx()
    return ledger.pump(ctx, transport, admission or ctx.admission, now)  # type: ignore[arg-type]


def _started_full(studio: Studio) -> int:
    run_id = studio.new_run(RunKind.FULL)
    assert ledger.start_run(_ctx(), run_id) == "started"
    return run_id


# ---------------------------------------------------------------- planning
def test_full_run_plans_its_first_stage_once(discovered: Studio) -> None:
    run_id = _started_full(discovered)
    assert ledger.start_run(_ctx(), run_id) == "skipped"         # a duplicate digitalizer.run
    jobs = _jobs(discovered, run_id)                               # 2 spreads = 4 leaves, chunk 3
    assert [(j.stage, j.status, j.lane) for j in jobs] == [("clean", "planned", "bulk")] * 2
    assert [j.params for j in jobs] == [{"pages": [0, 1, 2]}, {"pages": [3]}]
    assert jobs[0].task == "digitalizer.local.clean"


def test_interactive_rerun_plans_on_the_interactive_lane_and_queue(discovered: Studio) -> None:
    discovered.execute(RunKind.FULL)
    run_id = discovered.new_run(RunKind.RERUN, pages=[1], interactive=True)
    ledger.start_run(_ctx(), run_id)
    job = _jobs(discovered, run_id)[0]
    assert (job.lane, job.queue, job.params) == ("interactive", "digitalizer.interactive",
                                                {"pages": [1]})


# ---------------------------------------------------------------- dispatch and settle
def test_pump_claims_with_a_fresh_token_and_settle_advances_the_stage(discovered: Studio) -> None:
    run_id = _started_full(discovered)
    transport = RecordingTransport()
    _pump(transport)
    first, second = transport.sent
    job = discovered.session().get(Job, first.job_id)
    assert job.status == JobStatus.DISPATCHED and job.attempt == 1
    assert first.kwargs == {"job_id": job.id, "token": job.token, "pages": [0, 1, 2]}
    assert first.task_id == job.task_id and first.queue == "digitalizer.cpu"

    for d in (first, second):
        tasks.clean.run(**d.kwargs)                                 # the worker, inline
    assert not ledger.settle(_ctx(), first.job_id, first.task_id, {}).run_finished
    assert [j.stage for j in _jobs(discovered, run_id)] == ["clean"] * 2   # stage still open
    assert ledger.settle(_ctx(), second.job_id, second.task_id, {}).applied
    stages = [j.stage for j in _jobs(discovered, run_id)]
    assert stages == ["clean"] * 2 + ["analyze"] * 4                # one analyze job per leaf


def test_a_stale_attempt_is_refused_by_token_and_by_task_id(discovered: Studio) -> None:
    _started_full(discovered)
    transport = RecordingTransport()
    _pump(transport)
    d = transport.sent[0]
    out = tasks.clean.run(job_id=d.job_id, token="not-the-token", pages=[0, 1])
    assert out["skipped"] is True                                   # the body never ran
    assert ledger.settle(_ctx(), d.job_id, "another-attempt", {}).applied is False
    assert ledger.fail(_ctx(), d.job_id, "another-attempt") == []
    assert discovered.session().get(Job, d.job_id).status == JobStatus.DISPATCHED


def test_failed_job_fails_the_run_and_revokes_its_siblings(discovered: Studio) -> None:
    discovered.execute(RunKind.FULL)
    run_id = discovered.new_run(RunKind.RERUN, pages=[0, 1])
    ledger.start_run(_ctx(), run_id)
    s = discovered.session()
    job = s.query(Job).filter_by(run_pk=run_id).one()
    job.params = {"pages": [0]}
    s.add(Job(run_pk=run_id, stage="clean", seq=1, lane="bulk", backend="local",
              task=job.task, queue=job.queue, params={"pages": [1]}, status="planned"))
    s.commit()
    transport = RecordingTransport()
    _pump(transport)
    first, second = transport.sent
    ledger.record_error(_ctx(), first.job_id, first.kwargs["token"], RuntimeError("scantailor"))
    revoke = ledger.fail(_ctx(), first.job_id, first.task_id)
    assert revoke == [second.task_id]
    run = discovered.session().get(Run, run_id)
    assert run.status == RunStatus.FAILED and "scantailor" in (run.error or "")
    assert [j.status for j in _jobs(discovered, run_id)] == ["failed", "revoked"]


def test_failed_local_task_marks_run_and_book(discovered: Studio) -> None:
    from evilflowers_books_digitalizer.studio.ports import PortError
    from evilflowers_books_digitalizer.studio.testing import fakes

    class Broken(fakes.FakePageCleaner):
        def clean(self, plans, out_dir, overrides=None):
            raise PortError("scantailor exploded")

    discovered.adapters.cleaner = Broken()
    run = discovered.execute(RunKind.FULL)                          # eager: errback fails it
    assert run.status == RunStatus.FAILED and "scantailor exploded" in (run.error or "")
    assert discovered.book().status == BookStatus.ERROR
    assert _jobs(discovered, run.id)[0].status == JobStatus.FAILED


# ---------------------------------------------------------------- broker trouble
def test_a_send_the_broker_refuses_is_retried_by_the_next_pump(discovered: Studio) -> None:
    run_id = _started_full(discovered)
    _pump(RecordingTransport(fail_sends=True))
    jobs = _jobs(discovered, run_id)
    assert all(j.status == JobStatus.PLANNED and j.attempt == 0 and j.token is None
               for j in jobs)
    assert discovered.session().get(Run, run_id).status == RunStatus.RUNNING
    transport = RecordingTransport()
    _pump(transport)
    assert len(transport.sent) == len(jobs)


def test_a_lost_start_message_is_sent_again_after_the_grace(discovered: Studio) -> None:
    run_id = discovered.new_run(RunKind.FULL)
    s = discovered.session()
    run = s.get(Run, run_id)
    run.task_id, run.created_at = "lost-message", utcnow() - timedelta(minutes=5)
    s.commit()
    transport = RecordingTransport()
    _pump(transport)
    _pump(transport)                                                # within the grace: once
    assert transport.started == [run_id]


def test_held_publish_is_started_by_the_pump_once_its_render_finished(discovered: Studio) -> None:
    render_id = discovered.new_run(RunKind.RENDER)
    publish_id = discovered.new_run(RunKind.PUBLISH)
    assert ledger.start_run(_ctx(), publish_id) == "held"
    transport = RecordingTransport()
    s = discovered.session()
    s.get(Run, render_id).task_id = "in-flight"                     # not lost, just queued
    s.commit()
    _pump(transport)
    assert publish_id not in transport.started                     # still blocked
    s = discovered.session()
    s.get(Run, render_id).status = RunStatus.SUCCEEDED
    s.commit()
    _pump(transport)
    assert transport.started == [publish_id]
    assert discovered.session().get(Run, publish_id).status == RunStatus.QUEUED


def test_publish_held_behind_a_render_publishes_after_it_end_to_end(discovered: Studio) -> None:
    """Eager: render and publish queued together; publish waits, then the pump starts it."""
    discovered.execute(RunKind.FULL)
    s = discovered.session()
    book = s.query(Book).filter_by(project=PROJECT).one()
    book.status = BookStatus.APPROVED
    s.commit()
    render_id = discovered.new_run(RunKind.RENDER)
    publish_id = discovered.new_run(RunKind.PUBLISH)
    assert tasks.run.delay(publish_id).get()["state"] == "held"
    tasks.run.delay(render_id)
    s = discovered.session()
    assert s.get(Run, render_id).status == RunStatus.SUCCEEDED
    assert s.get(Run, publish_id).status == RunStatus.SUCCEEDED
    assert discovered.book().status == BookStatus.PUBLISHED


# ---------------------------------------------------------------- reconcile
def _dispatched(studio: Studio) -> tuple[int, ledger.Dispatch]:
    """A full run whose single clean job (2 leaves, chunk 3 -> one chunk) is in flight."""
    studio.adapters.cleaner = _one_leaf_per_frame()
    run_id = _started_full(studio)
    transport = RecordingTransport()
    _pump(transport)
    (d,) = transport.sent
    return run_id, d


def _one_leaf_per_frame():
    from evilflowers_books_digitalizer.studio.testing import fakes

    class Whole(fakes.FakePageCleaner):
        def plan(self, frames):
            return [p for p in super().plan(frames) if p.side in (None, 0)]

    return Whole(dpi=300)


def test_a_silent_started_job_is_resent_with_a_new_token(discovered: Studio) -> None:
    """The worker died (or its pod was preempted) mid-job: no heartbeat, re-sent."""
    run_id, d = _dispatched(discovered)
    assert ledger.begin(_ctx(), d.job_id, d.kwargs["token"]) is not None
    later = utcnow() + timedelta(seconds=OrchestrationConfig().stale_after_seconds + 5)
    report = ledger.reconcile(_ctx(), RecordingTransport(), now=later)
    assert report["resent"] == 1
    job = discovered.session().get(Job, d.job_id)
    assert job.status == JobStatus.PLANNED and job.budget == 1 and job.token is None
    transport = RecordingTransport()
    _pump(transport)
    assert transport.sent[0].kwargs["token"] != d.kwargs["token"]
    assert tasks.clean.run(**d.kwargs)["skipped"] is True           # the old message, redelivered


def test_a_dispatched_job_never_picked_up_is_resent_after_its_deadline(discovered: Studio) -> None:
    """Preemption before the job started: the message waits for the visibility timeout."""
    _, d = _dispatched(discovered)
    route: StageRoute = _ctx().config.orchestration.route("clean")
    before = utcnow() + timedelta(seconds=route.deadline_seconds - 60)
    assert ledger.reconcile(_ctx(), RecordingTransport(), now=before)["resent"] == 0
    after = utcnow() + timedelta(seconds=route.deadline_seconds + 60)
    transport = RecordingTransport()
    assert ledger.reconcile(_ctx(), transport, now=after)["resent"] == 1
    assert transport.revoked == [d.task_id]          # the waiting message is dropped, not run


def test_a_long_job_with_a_fresh_heartbeat_is_left_alone(discovered: Studio) -> None:
    _, d = _dispatched(discovered)
    ledger.begin(_ctx(), d.job_id, d.kwargs["token"])
    soon = utcnow() + timedelta(seconds=30)
    assert ledger.reconcile(_ctx(), RecordingTransport(), now=soon) == \
        {"settled": 0, "resent": 0, "failed": 0, "follow_ups": []}


def test_a_spent_budget_fails_the_run_and_errors_the_book(discovered: Studio) -> None:
    run_id, d = _dispatched(discovered)
    s = discovered.session()
    s.get(Job, d.job_id).budget = 0
    s.commit()
    later = utcnow() + timedelta(days=1)
    assert ledger.reconcile(_ctx(), RecordingTransport(), now=later)["failed"] == 1
    run = discovered.session().get(Run, run_id)
    assert run.status == RunStatus.FAILED and "lost" in (run.error or "")
    assert discovered.book().status == BookStatus.ERROR


def test_cancelled_run_revokes_its_open_jobs(discovered: Studio) -> None:
    run_id, d = _dispatched(discovered)
    s = discovered.session()
    s.get(Run, run_id).status = RunStatus.CANCELLED
    s.commit()
    transport = RecordingTransport()
    _pump(transport)
    assert transport.revoked == [d.task_id] and transport.sent == []
    assert discovered.session().get(Job, d.job_id).status == JobStatus.REVOKED


# ---------------------------------------------------------------- admission
NOON_WEEKDAY = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)          # Wednesday 12:00 in Bratislava
NIGHT = datetime(2026, 11, 18, 23, 0, tzinfo=UTC)


def _policy(**kw) -> SchedulingConfig:
    base = dict(peak_hours=("mon-fri 07:00-19:00",), bulk_inflight=8, bulk_inflight_peak=1,
                bulk_inflight_contended=0, interactive_inflight=4)
    return SchedulingConfig(**{**base, **kw})


def test_peak_windows_are_local_time_and_wrap_days() -> None:
    w = PeakWindow.parse("mon-fri 07:00-19:00")
    admission = Admission(_policy())
    assert admission.is_peak(NOON_WEEKDAY) and not admission.is_peak(NIGHT)
    assert not admission.is_peak(datetime(2026, 11, 21, 11, 0, tzinfo=UTC))   # Saturday
    assert w.days == frozenset(range(5))
    assert PeakWindow.parse("22:00-06:00").contains(datetime(2026, 11, 18, 3, 0))
    with pytest.raises(ValueError):
        PeakWindow.parse("lunchtime")


def test_capacity_modes() -> None:
    control, pressure = InMemoryControl(), FixedBackpressure()
    admission = Admission(_policy(off_peak_stages=("render",)), control, pressure)
    assert admission.capacity(NIGHT).lanes[Lane.BULK] == 8
    peak = admission.capacity(NOON_WEEKDAY)
    assert (peak.mode, peak.lanes[Lane.BULK], peak.held_stages) == (Mode.PEAK, 1,
                                                                    frozenset({"render"}))
    pressure.over = {"catalog-extraction": 40}
    contended = admission.capacity(NIGHT)
    assert contended.mode == Mode.CONTENDED and contended.lanes[Lane.BULK] == 0
    assert contended.lanes[Lane.INTERACTIVE] == 4                   # operators never wait
    control.pause("maintenance")
    paused = admission.capacity(NIGHT)
    assert paused.mode == Mode.PAUSED and paused.reason == "maintenance"
    assert paused.lanes == {Lane.INTERACTIVE: 4, Lane.BULK: 0}


def test_at_peak_bulk_waits_while_an_operator_rerun_goes_first(discovered: Studio) -> None:
    discovered.execute(RunKind.FULL)
    bulk = discovered.new_run(RunKind.RERUN, pages=[0, 1])
    rerun = discovered.new_run(RunKind.RERUN, pages=[1], interactive=True)
    s = discovered.session()
    s.get(Run, bulk).pages = [0]
    s.commit()
    for run_id in (bulk, rerun):
        ledger.start_run(_ctx(), run_id)
    admission = Admission(_policy(bulk_inflight_peak=0))
    transport = RecordingTransport()
    _pump(transport, admission, now=NOON_WEEKDAY)
    assert [d.lane for d in transport.sent] == ["interactive"]
    _pump(transport, admission, now=NIGHT)
    assert [d.lane for d in transport.sent] == ["interactive", "bulk"]


def test_queue_caps_and_off_peak_stages(discovered: Studio) -> None:
    run_id = _started_full(discovered)
    s = discovered.session()
    s.add(Job(run_pk=run_id, stage="clean", seq=1, lane="bulk", backend="local",
              task="digitalizer.local.clean", queue="digitalizer.cpu", params={"pages": [1]},
              status="planned"))
    s.commit()
    transport = RecordingTransport()
    _pump(transport, Admission(_policy(queue_inflight={"digitalizer.cpu": 1})), now=NIGHT)
    assert len(transport.sent) == 1
    held = Admission(_policy(off_peak_stages=("clean",), bulk_inflight_peak=8))
    transport = RecordingTransport()
    _pump(transport, held, now=NOON_WEEKDAY)
    assert transport.sent == []


def test_redis_queue_depth_reports_only_sources_over_their_limit() -> None:
    class Fake:
        def llen(self, name: str) -> int:
            return {"evilflowers_analyzer": 30, "evilflowers_image": 2}.get(name, 0)

    probe = RedisQueueDepth([BackpressureSource("catalog", "redis://x/7",
                                                ("evilflowers_analyzer", "evilflowers_image"), 25),
                             BackpressureSource("quiet", "redis://x/7", ("other",), 25)],
                            client_for=lambda url: Fake())
    assert probe.over_limit() == {"catalog": 32}


# ---------------------------------------------------------------- a service-backed stage
def _service_studio(tmp_path: Path) -> Studio:
    routes = dict(OrchestrationConfig().routes)
    routes["clean"] = StageRoute("service", "evilflowers_scan.clean",
                                 "evilflowers_scan.standalone",
                                 "evilflowers_scan.standalone.interactive", 3600)
    studio = make_studio(tmp_path, orchestration=OrchestrationConfig(routes=routes),
                         storage_root=tmp_path)
    studio.adapters.cleaner = _one_leaf_per_frame()               # one clean job
    tasks.discover.delay(PROJECT)
    return studio


def _service_writes(tmp_path: Path, d: ledger.Dispatch, token: str | None = None) -> None:
    """What evilflowers-scan-service would leave: leaves, previews, tiles, then the outcome."""
    out = tmp_path / d.kwargs["output_dir"]
    out.mkdir(parents=True)
    leaves = []
    for leaf in d.kwargs["leaves"]:
        image = out / f"{leaf['leaf']:04d}.png"
        from PIL import Image

        Image.new("L", (120, 200), 255).save(image)
        (out / f"{leaf['leaf']:04d}.dzi").write_text("<Image/>")
        leaves.append({"leaf": leaf["leaf"], "image": f"{d.kwargs['output_dir']}/{image.name}",
                       "width": 120, "height": 200, "dpi": 300,
                       "dzi": f"{d.kwargs['output_dir']}/{leaf['leaf']:04d}.dzi"})
    (tmp_path / d.kwargs["outcome"]).write_text(json.dumps(
        {"run_id": d.kwargs["run_id"], "token": token or d.kwargs["token"], "status": "done",
         "leaves": leaves}))


def test_service_stage_gets_the_standalone_contract_and_its_outcome_is_applied(
        tmp_path: Path) -> None:
    studio = _service_studio(tmp_path)
    run_id = _started_full(studio)
    transport = RecordingTransport()
    _pump(transport)
    (d,) = transport.sent
    assert (d.task, d.queue) == ("evilflowers_scan.clean", "evilflowers_scan.standalone")
    assert d.kwargs["run_id"] == str(run_id) and d.kwargs["job"] == "clean-0000-a1"
    assert d.kwargs["output_dir"].endswith(f"jobs/{d.job_id}-a1")
    assert d.kwargs["leaves"][0]["frame"].startswith("scans/fei/BOOK1/")   # relative paths
    _service_writes(tmp_path, d)
    assert ledger.settle(_ctx(), d.job_id, d.task_id, {"status": "done"}).applied
    pages = studio.session().query(Book).one().pages
    assert {p.status for p in pages} == {PageStatus.CLEANED}
    assert all(p.clean_path and f"jobs/{d.job_id}-a1" in p.clean_path for p in pages)


def test_service_outcome_of_another_attempt_fails_the_job(tmp_path: Path) -> None:
    studio = _service_studio(tmp_path)
    run_id = _started_full(studio)
    transport = RecordingTransport()
    _pump(transport)
    (d,) = transport.sent
    _service_writes(tmp_path, d, token="forged")
    assert not ledger.settle(_ctx(), d.job_id, d.task_id, {}).applied
    run = studio.session().get(Run, run_id)
    assert run.status == RunStatus.FAILED and "another attempt" in (run.error or "")


def test_reconcile_applies_an_outcome_whose_callback_was_lost(tmp_path: Path) -> None:
    studio = _service_studio(tmp_path)
    _started_full(studio)
    transport = RecordingTransport()
    _pump(transport)
    _service_writes(tmp_path, transport.sent[0])
    assert ledger.reconcile(_ctx(), RecordingTransport())["settled"] == 1
    assert studio.session().get(Job, transport.sent[0].job_id).status == JobStatus.APPLIED


def test_service_stage_without_storage_root_fails_the_run_at_dispatch(tmp_path: Path) -> None:
    routes = dict(OrchestrationConfig().routes)
    routes["clean"] = StageRoute("service", "evilflowers_scan.clean", "q", "q", 60)
    studio = make_studio(tmp_path, orchestration=OrchestrationConfig(routes=routes))
    tasks.discover.delay(PROJECT)
    run_id = _started_full(studio)
    _pump(RecordingTransport())
    run = studio.session().get(Run, run_id)
    assert run.status == RunStatus.FAILED and "storage_root" in (run.error or "")


def test_contract_paths_never_escape_the_storage(tmp_path: Path) -> None:
    storage = contract.Storage(tmp_path)
    with pytest.raises(contract.OutcomeError):
        storage.abs("../etc/passwd")
    with pytest.raises(contract.PortError):
        storage.rel("/elsewhere/file")
