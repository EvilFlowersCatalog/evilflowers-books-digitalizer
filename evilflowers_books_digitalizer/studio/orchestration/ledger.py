"""The job ledger: plan a run's stages as jobs, admit them, settle their results.

Flow (docs/diagrams/sequence-digitalizer-full-run)::

    start_run ─▶ plan the first stage ─▶ pump: admit + claim ─▶ Transport.send
        ▲                                                         │ link / link_error
        └── next stage / finish ◀── settle (job_done) ◀───────────┘
                                    fail (job_failed)
    reconcile (beat): settle an outcome whose callback was lost, re-send a lost job
                      while its budget lasts, fail the run once it is spent

Every function opens its own unit of work and returns what must happen *after* the
commit (messages to send). Nothing is sent from inside a transaction: an eager test
run executes the message inline, and a real worker must never read uncommitted rows.

Concurrency: settling locks the run row (``SELECT … FOR UPDATE``), so the last two
jobs of a stage finishing at once plan the next stage exactly once. The pump takes a
transaction-scoped advisory lock on PostgreSQL, so two pumps never double-admit.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select, text

from evilflowers_books_digitalizer.db.models import Job, Run, utcnow
from evilflowers_books_digitalizer.studio.config import PORTAL_QUEUE, StageRoute
from evilflowers_books_digitalizer.studio.domain import JobStatus, Lane, RunStatus
from evilflowers_books_digitalizer.studio.orchestration import contract
from evilflowers_books_digitalizer.studio.orchestration.scheduling import Admission, Capacity
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow, WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages import common
from evilflowers_books_digitalizer.studio.workers.stages.orchestrate import (
    WorkPlan,
    blocked_by,
    orchestrate,
    work_plan,
)

logger = logging.getLogger(__name__)

#: stages that fan out one job per leaf chunk / per leaf; the rest are one job per run
CHUNKED, PER_PAGE = "clean", "analyze"
OPEN = [str(s) for s in JobStatus if s.open]
IN_FLIGHT = [str(JobStatus.DISPATCHED), str(JobStatus.STARTED)]
ACTIVE_RUNS = [str(RunStatus.QUEUED), str(RunStatus.RUNNING)]
#: an arbitrary constant: the pump's PostgreSQL advisory lock
PUMP_LOCK = 0x6469_6769  # "digi"


@dataclass(frozen=True, slots=True)
class Dispatch:
    """One message to send: a claimed job (its token and task id are already committed)."""

    job_id: int
    run_id: int
    stage: str
    lane: str
    task: str
    queue: str
    task_id: str
    kwargs: dict[str, Any]


class Transport(Protocol):
    def send(self, dispatch: Dispatch) -> None: ...
    def start_run(self, run_id: int) -> None: ...
    def revoke(self, task_ids: list[str]) -> None: ...


@dataclass
class Settled:
    """What a callback changed; ``follow_ups`` are runs to start after the commit."""

    applied: bool = False
    run_finished: bool = False
    follow_ups: list[int] = field(default_factory=list)
    revoke: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- planning
def _units(stage: str, plan: WorkPlan) -> list[dict[str, Any]]:
    if stage == CHUNKED:
        return [{"pages": chunk} for chunk in plan.chunks()]
    if stage == PER_PAGE:
        return [{"page": index} for index in plan.pages]
    return [{}]


def _plan_from(uow: Uow, ctx: WorkerContext, run: Run, start: int) -> bool:
    """Plan the first stage at or after ``start`` that has work; False = nothing left."""
    plan = work_plan(ctx, run)
    for stage in plan.stages[start:]:
        units = _units(stage, plan)
        if not units:
            continue
        route = ctx.config.orchestration.route(stage)
        interactive = bool(run.interactive)
        lane = Lane.INTERACTIVE if interactive else Lane.BULK
        for seq, params in enumerate(units):
            uow.session.add(Job(run_pk=run.id, stage=stage, seq=seq, lane=str(lane),
                                backend=route.backend, task=route.task,
                                queue=route.interactive_queue if interactive else route.queue,
                                params=params, status=str(JobStatus.PLANNED),
                                budget=ctx.config.orchestration.budget))
        if route.backend == "service":
            runs.enter_stage(uow, run, stage, len(plan.pages) if stage in (CHUNKED, PER_PAGE)
                             else 1)
        runs.log(uow, run, f"stage {stage}: {len(units)} job(s) on {route.backend} "
                           f"{route.task}")
        return True
    return False


def start_run(ctx: WorkerContext, run_id: int) -> str:
    """``digitalizer.run``: claim a queued run and plan its first stage.

    Returns ``started`` / ``held`` (an earlier run of the book must finish first; the
    pump starts it then) / ``skipped`` (not queued: cancelled, or already started).
    """
    if runs.is_active(ctx, run_id) and blocked_by(ctx, run_id) is not None:
        with ctx.uow() as uow:
            run = runs.load_run(uow, run_id)
            run.scheduled_at = None          # the pump starts it once the blocker is gone
        return "held"
    started = orchestrate(ctx, run_id)       # QUEUED -> RUNNING, leaves planned (row lock)
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id, lock=True)
        if run.status != RunStatus.RUNNING or run.jobs:
            return "started" if started else "skipped"
        if not _plan_from(uow, ctx, run, 0):
            runs.finish_in(uow, run)
    return "started"


# ---------------------------------------------------------------- admission (the pump)
def _try_pump_lock(uow: Uow) -> bool:
    if uow.session.get_bind().dialect.name != "postgresql":
        return True
    return bool(uow.session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                    {"k": PUMP_LOCK}).scalar())


def _revoke_orphans(uow: Uow) -> list[str]:
    """Open jobs of runs that ended (cancelled, failed) are revoked; their task ids returned."""
    orphans = uow.session.scalars(
        select(Job).join(Run).where(Job.status.in_(OPEN), Run.status.notin_(ACTIVE_RUNS))).all()
    now = utcnow()
    for job in orphans:
        job.status, job.finished_at = str(JobStatus.REVOKED), now
    return [j.task_id for j in orphans if j.task_id]


def _kwargs(ctx: WorkerContext, job: Job, run: Run, token: str) -> dict[str, Any]:
    """Local tasks get their job id + token; services get the standalone contract."""
    if job.backend == "local":
        return {"job_id": job.id, "token": token, **(job.params or {})}
    storage = contract.Storage(ctx.config.storage_root)
    item_dir = ctx.adapters(runs.project_of(run.book)).artifacts.item_dir(common.item_id(run.book))
    if job.stage == CHUNKED:
        wanted = set(job.params.get("pages") or [])
        inputs = contract.clean_inputs([p for p in run.book.pages if p.index in wanted], storage)
    else:
        raise contract.PortError(f"stage {job.stage} has no service contract yet")
    message = contract.message(job, run, token, storage, Path(item_dir), inputs)
    job.params = {**(job.params or {}), "output_dir": message["output_dir"],
                  "outcome": message["outcome"]}
    return message


def _admit(job: Job, cap: Capacity, inflight: Counter[str], per_queue: Counter[str]) -> bool:
    lane = Lane(job.lane)
    if lane == Lane.BULK and job.stage in cap.held_stages:
        return False
    if inflight[job.lane] >= cap.lanes.get(lane, 0):
        return False
    limit = cap.queues.get(job.queue)
    return limit is None or per_queue[job.queue] < limit


def pump(ctx: WorkerContext, transport: Transport, admission: Admission,
         now: datetime | None = None) -> dict[str, Any]:
    """Admit planned jobs within the current capacity and start due queued runs."""
    cap = admission.capacity(now)
    sent: list[Dispatch] = []
    starts: list[int] = []
    revoke: list[str] = []
    moment = now or utcnow()
    with ctx.uow() as uow:
        if not _try_pump_lock(uow):
            return {"skipped": "another pump holds the lock"}
        revoke = _revoke_orphans(uow)
        open_jobs = uow.session.scalars(
            select(Job).join(Run).where(Job.status.in_(OPEN), Run.status.in_(ACTIVE_RUNS))
            .order_by(Job.id)).all()
        inflight = Counter(j.lane for j in open_jobs if j.status in IN_FLIGHT)
        per_queue = Counter(j.queue for j in open_jobs if j.status in IN_FLIGHT)
        planned = sorted((j for j in open_jobs if j.status == JobStatus.PLANNED),
                         key=lambda j: (j.lane != Lane.INTERACTIVE, j.run_pk, j.seq))
        for job in planned:
            if not _admit(job, cap, inflight, per_queue):
                continue
            try:
                sent.append(_claim(ctx, job, moment))
            except contract.PortError as exc:
                job.status, job.error, job.finished_at = str(JobStatus.FAILED), str(exc), moment
                runs.fail_in(uow, job.run, f"job {job.id} ({job.stage}): {exc}")
                continue
            inflight[job.lane] += 1
            per_queue[job.queue] += 1
        starts = _due_starts(ctx, uow, moment)
    _deliver(ctx, transport, sent)
    for run_id in starts:
        transport.start_run(run_id)
    if revoke:
        transport.revoke(revoke)
    return {"capacity": cap.as_dict(), "sent": len(sent), "started_runs": starts,
            "revoked": len(revoke)}


def _claim(ctx: WorkerContext, job: Job, now: datetime) -> Dispatch:
    route: StageRoute = ctx.config.orchestration.route(job.stage)
    token = secrets.token_urlsafe(24)
    job.attempt += 1
    job.token, job.task_id = token, uuid.uuid4().hex
    kwargs = _kwargs(ctx, job, job.run, token)
    job.status, job.dispatched_at = str(JobStatus.DISPATCHED), now
    job.started_at = job.heartbeat_at = None
    job.deadline_at = now + timedelta(seconds=route.deadline_seconds)
    return Dispatch(job.id, job.run_pk, job.stage, job.lane, job.task, job.queue, job.task_id,
                    kwargs)


def _deliver(ctx: WorkerContext, transport: Transport, sent: list[Dispatch]) -> None:
    """Send claimed jobs; a broker error puts the job back to ``planned`` for the next pump."""
    for d in sent:
        try:
            transport.send(d)
        except Exception as exc:  # noqa: BLE001 - any broker error means "not sent"
            # eager mode runs the task inside send(): a task error surfaces here *after* its
            # errback failed the job, so only a job still waiting for its message is reset
            with ctx.uow() as uow:
                job = uow.session.get(Job, d.job_id)
                if job is None or job.task_id != d.task_id \
                        or job.status != JobStatus.DISPATCHED or job.started_at is not None:
                    continue
                job.status, job.token, job.task_id = str(JobStatus.PLANNED), None, None
                job.attempt -= 1
            logger.warning("job %s not sent, back to planned: %s", d.job_id, exc,
                           extra={"job_id": d.job_id, "run_id": d.run_id, "stage": d.stage})


def _due_starts(ctx: WorkerContext, uow: Uow, now: datetime) -> list[int]:
    """Queued runs whose start message is lost or that were held and are now unblocked."""
    grace = timedelta(seconds=ctx.config.orchestration.start_grace_seconds)
    queued = uow.session.scalars(select(Run).where(Run.status == str(RunStatus.QUEUED))
                                 .order_by(Run.id)).all()
    due: list[int] = []
    for run in queued:
        sent = run.scheduled_at
        if sent is not None and now - sent < grace:
            continue
        if sent is None and now - run.created_at < grace and run.task_id is not None:
            continue                     # just enqueued by the API: its message is in flight
        if blocked_by(ctx, run.id) is not None:
            continue
        run.scheduled_at = now
        due.append(run.id)
    return due


# ---------------------------------------------------------------- local job lifecycle
@dataclass(frozen=True, slots=True)
class LocalJob:
    id: int
    run_id: int
    stage: str
    params: dict[str, Any]


def begin(ctx: WorkerContext, job_id: int, token: str) -> LocalJob | None:
    """A local task starts: its token must be the current attempt's, its job open, its run active."""
    with ctx.uow() as uow:
        job = uow.session.get(Job, job_id, with_for_update=True)
        if job is None or job.token != token or not JobStatus(job.status).open \
                or job.run.status not in ACTIVE_RUNS:
            return None
        now = utcnow()
        job.status = str(JobStatus.STARTED)
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        return LocalJob(job.id, job.run_pk, job.stage, dict(job.params or {}))


def beat(ctx: WorkerContext, job_id: int, token: str) -> None:
    with ctx.uow() as uow:
        job = uow.session.get(Job, job_id)
        if job is not None and job.token == token and job.status == JobStatus.STARTED:
            job.heartbeat_at = utcnow()


def record_error(ctx: WorkerContext, job_id: int, token: str, exc: BaseException) -> None:
    """A local task keeps its error on the job; ``job_failed`` (the errback) reads it."""
    try:
        with ctx.uow() as uow:
            job = uow.session.get(Job, job_id)
            if job is not None and job.token == token:
                job.error = runs.describe(exc)
    except Exception:  # noqa: BLE001 - never mask the original error
        logger.exception("could not record the error of job %s", job_id)


# ---------------------------------------------------------------- settling results
type Applier = Callable[[Uow, WorkerContext, Run, Job, contract.Outcome], None]

#: how a service outcome lands in the rows, per stage (studio.orchestration.appliers)
APPLIERS: dict[str, Applier] = {}


def _advance(uow: Uow, ctx: WorkerContext, run: Run, stage: str, settled: Settled) -> None:
    """After a job is applied: plan the next stage once the whole stage is applied."""
    same = [j for j in run.jobs if j.stage == stage]
    if any(JobStatus(j.status).open for j in same):
        return
    stages = work_plan(ctx, run).stages
    if _plan_from(uow, ctx, run, stages.index(stage) + 1):
        return
    runs.finish_in(uow, run)
    settled.run_finished = True
    for job in run.jobs:
        settled.follow_ups.extend(int(i) for i in (job.summary or {}).get("follow_ups", []))


def _apply_service(uow: Uow, ctx: WorkerContext, run: Run, job: Job) -> None:
    model = contract.OUTCOMES.get(job.stage)
    applier = APPLIERS.get(job.stage)
    if model is None or applier is None:
        raise contract.OutcomeError(f"stage {job.stage} has no service applier")
    storage = contract.Storage(ctx.config.storage_root)
    outcome = contract.read_outcome(storage, job, model)
    if outcome.status == "failed":
        raise contract.OutcomeError(outcome.error or f"job {job.id}: the service reported failure")
    if outcome.status == "done":
        applier(uow, ctx, run, job, outcome)


def settle(ctx: WorkerContext, job_id: int, task_id: str | None,
           summary: dict[str, Any] | None) -> Settled:
    """``digitalizer.job_done``: apply the attempt's result if it is still the current one."""
    settled = Settled()
    with ctx.uow() as uow:
        job = uow.session.get(Job, job_id)
        if job is None or (task_id is not None and job.task_id != task_id) \
                or not JobStatus(job.status).open:
            return settled               # a stale attempt, a duplicate callback, a revoked job
        run = runs.load_run(uow, job.run_pk, lock=True)
        uow.session.refresh(job)
        if not JobStatus(job.status).open:
            return settled
        if run.status not in ACTIVE_RUNS:
            job.status, job.finished_at = str(JobStatus.REVOKED), utcnow()
            return settled
        if (summary or {}).get("skipped"):
            return settled               # the task refused this attempt (it is not current)
        try:
            if job.backend == "service":
                _apply_service(uow, ctx, run, job)
        except contract.OutcomeError as exc:
            settled.revoke = _fail_job(uow, run, job, str(exc))
            return settled
        job.status, job.finished_at = str(JobStatus.APPLIED), utcnow()
        job.summary = dict(summary or {})
        settled.applied = True
        _advance(uow, ctx, run, job.stage, settled)
    return settled


def _fail_job(uow: Uow, run: Run, job: Job, error: str) -> list[str]:
    """Fail a job and its run; revoke the run's other open jobs. Returns task ids to revoke."""
    now = utcnow()
    job.status, job.error, job.finished_at = str(JobStatus.FAILED), error[:4000], now
    revoke = []
    for other in run.jobs:
        if other.id != job.id and JobStatus(other.status).open:
            other.status, other.finished_at = str(JobStatus.REVOKED), now
            if other.task_id:
                revoke.append(other.task_id)
    runs.fail_in(uow, run, f"{job.stage} job {job.seq} failed: {error}")
    return revoke


def fail(ctx: WorkerContext, job_id: int, task_id: str | None) -> list[str]:
    """``digitalizer.job_failed`` (errback): the current attempt failed -> its run fails."""
    with ctx.uow() as uow:
        job = uow.session.get(Job, job_id)
        if job is None or (task_id is not None and job.task_id != task_id) \
                or not JobStatus(job.status).open:
            return []
        run = runs.load_run(uow, job.run_pk, lock=True)
        error = job.error or "the task failed"
        if job.backend == "service" and not job.error:
            error = _service_error(ctx, job) or error
        return _fail_job(uow, run, job, error)


def _service_error(ctx: WorkerContext, job: Job) -> str | None:
    model = contract.OUTCOMES.get(job.stage, contract.Outcome)
    try:
        outcome = contract.read_outcome(contract.Storage(ctx.config.storage_root), job, model)
    except contract.OutcomeError:
        return None
    return outcome.error


# ---------------------------------------------------------------- reconcile (beat)
def reconcile(ctx: WorkerContext, transport: Transport, now: datetime | None = None
              ) -> dict[str, Any]:
    """Settle what callbacks missed: lost outcomes, dead workers, jobs past their deadline."""
    moment = now or utcnow()
    stale = timedelta(seconds=ctx.config.orchestration.stale_after_seconds)
    to_settle: list[tuple[int, str | None]] = []
    resent, failed = 0, 0
    revoke: list[str] = []
    with ctx.uow() as uow:
        revoke = _revoke_orphans(uow)
        flying = uow.session.scalars(select(Job).join(Run).where(
            Job.status.in_(IN_FLIGHT), Run.status.in_(ACTIVE_RUNS))).all()
        for job in flying:
            if job.backend == "service" and _has_outcome(ctx, job):
                to_settle.append((job.id, job.task_id))
                continue
            if not _lost(job, moment, stale):
                continue
            if job.budget > 0:
                job.budget -= 1
                job.status, job.token = str(JobStatus.PLANNED), None
                job.error = f"lost in attempt {job.attempt} (no result by {moment:%H:%M:%S})"
                runs.log(uow, job.run, f"{job.stage} job {job.seq} lost; re-sending "
                                       f"({job.budget} left)", "warning")
                resent += 1
            else:
                revoke += _fail_job(uow, job.run, job, "lost: no result and no re-sends left")
                failed += 1
    settled = [settle(ctx, job_id, task_id, {}) for job_id, task_id in to_settle]
    if revoke:
        transport.revoke(revoke)
    return {"settled": len(settled), "resent": resent, "failed": failed,
            "follow_ups": [r for s in settled for r in s.follow_ups]}


def _lost(job: Job, now: datetime, stale: timedelta) -> bool:
    """Local: started and silent for ``stale``; anything: past its deadline."""
    if job.backend == "local" and job.status == JobStatus.STARTED:
        return job.heartbeat_at is not None and now - job.heartbeat_at > stale
    return job.deadline_at is not None and now > job.deadline_at


def _has_outcome(ctx: WorkerContext, job: Job) -> bool:
    rel = (job.params or {}).get("outcome")
    if not rel or ctx.config.storage_root is None:
        return False
    try:
        return contract.Storage(ctx.config.storage_root).abs(rel).is_file()
    except contract.OutcomeError:
        return False


# ---------------------------------------------------------------- reading (API, metrics)
def counts(ctx: WorkerContext) -> dict[str, int]:
    """Open jobs per ``status/lane`` (the scheduler endpoint and the metrics)."""
    with ctx.uow() as uow:
        rows = uow.session.execute(
            select(Job.status, Job.lane).join(Run).where(Job.status.in_(OPEN),
                                                         Run.status.in_(ACTIVE_RUNS))).all()
    return dict(Counter(f"{status}/{lane}" for status, lane in rows))


__all__ = ["APPLIERS", "Dispatch", "LocalJob", "PORTAL_QUEUE", "Settled", "Transport", "beat",
           "begin", "counts", "fail", "pump", "reconcile", "record_error", "settle", "start_run"]
