"""Stale-run sweeper: find runs no task is working on any more and settle them.

A run is *active* (``queued``/``running``) while some Celery message for it exists:
waiting in a queue, or delivered to a worker and not yet acknowledged
(``acks_late``: a task is acked only when it is done, so a worker that dies
mid-task leaves its message in Redis' ``unacked`` hash until it is redelivered).
A run with no such message and a silent heartbeat is lost. The sweeper — the
``studio.recover_runs`` beat task (``app.BEAT_SCHEDULE``, every 5 min by default) —
handles three cases:

* **never dispatched** — ``running``, no stage entered, no ``dispatched_at``: the
  worker died between orchestrate's commit and sending the canvas. The plan is
  rebuilt from the rows and the canvas sent (claimed with a compare-and-set, so a
  redelivered ``studio.run`` and the sweeper never both send it);
* **queued, message lost** (e.g. Redis restarted without persistence): ``studio.run``
  is enqueued again — orchestrate starts a run only once (row lock + status check);
* **running, lost** — heartbeat silent for :attr:`RecoveryPolicy.stale_after` and
  nothing for the run in the broker: the run is failed with a clear error (a failed
  *full* run puts the book in ``error`` along ``domain.check_transition``).

Long renders are safe twice over: every task bumps ``runs.heartbeat_at`` while its
body runs (``tasks._heartbeat``), and a run with any message in the broker is never
touched, however old its heartbeat — work queued behind another book's backlog
included. When the broker cannot be inspected the sweep does nothing.
"""

from __future__ import annotations

import ast
import base64
import json
import logging
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select

from evilflowers_books_digitalizer.db.models import Run, utcnow
from evilflowers_books_digitalizer.studio.domain import RunStatus
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import WorkerContext
from evilflowers_books_digitalizer.studio.workers.stages.orchestrate import WorkPlan, work_plan

logger = logging.getLogger(__name__)

#: tasks whose first positional argument is a run id
RUN_TASKS = frozenset({"studio.run", "studio.clean", "studio.analyze", "studio.formulas",
                       "studio.render", "studio.gate", "studio.publish"})


class LostRun(RuntimeError):
    """The error recorded on a run the sweeper gave up on."""


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    #: a started run that has entered no stage and sent no canvas for this long was
    #: orphaned between orchestrate's commit and the dispatch
    dispatch_grace: timedelta = timedelta(minutes=5)
    #: heartbeat silence (with nothing for the run in the broker) after which a run is lost
    stale_after: timedelta = timedelta(minutes=30)

    @classmethod
    def from_env(cls) -> RecoveryPolicy:
        return cls(timedelta(seconds=float(os.environ.get(
                       "STUDIO_RECOVER_DISPATCH_GRACE_SECONDS", "300"))),
                   timedelta(seconds=float(os.environ.get(
                       "STUDIO_RECOVER_STALE_SECONDS", "1800"))))


class InFlightProbe(Protocol):
    def run_ids(self) -> set[int] | None:
        """Run ids that have a message in the broker (queued or unacked); None = unknown."""
        ...


@dataclass
class SweepReport:
    redispatched: list[int] = field(default_factory=list)
    requeued: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    skipped: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"redispatched": self.redispatched, "requeued": self.requeued,
                "failed": self.failed, "skipped": self.skipped}


@dataclass(frozen=True, slots=True)
class _Candidate:
    id: int
    status: str
    stage: str | None
    started: bool                   # heartbeat_at set: started after migration 0003
    dispatched: bool
    last_seen: datetime


def _candidates(ctx: WorkerContext) -> list[_Candidate]:
    with ctx.uow() as uow:
        rows = uow.session.scalars(
            select(Run).where(Run.status.in_([str(s) for s in runs.ACTIVE])).order_by(Run.id))
        return [_Candidate(r.id, r.status, r.stage, r.heartbeat_at is not None,
                           r.dispatched_at is not None,
                           r.heartbeat_at or r.started_at or r.created_at) for r in rows]


def _plan(ctx: WorkerContext, run_id: int) -> WorkPlan:
    with ctx.uow() as uow:
        return work_plan(ctx, runs.load_run(uow, run_id))


def sweep(ctx: WorkerContext, probe: InFlightProbe, *,
          dispatch: Callable[[WorkerContext, WorkPlan], None],
          enqueue: Callable[[int], None],
          policy: RecoveryPolicy = RecoveryPolicy(),
          now: datetime | None = None) -> SweepReport:
    """One pass over the active runs (see the module docstring for the rules)."""
    in_flight = probe.run_ids()
    if in_flight is None:
        logger.warning("recover_runs: broker not inspectable, nothing done")
        return SweepReport(skipped="broker not inspectable")
    now = now or utcnow()
    report = SweepReport()
    for run in _candidates(ctx):
        if run.id in in_flight:
            continue                                    # some task will still work on it
        age = now - run.last_seen
        if (run.status == RunStatus.RUNNING and run.started and not run.dispatched
                and run.stage is None):
            if age >= policy.dispatch_grace and runs.claim_dispatch(ctx, run.id):
                _redispatch(ctx, run.id, dispatch, report)
            continue
        if age < policy.stale_after:
            continue
        if run.status == RunStatus.QUEUED:
            _requeue(ctx, run.id, enqueue, report)
        else:
            _fail(ctx, run, now, report)
    if report.redispatched or report.requeued or report.failed:
        logger.warning("recover_runs: %s", report.as_dict())
    return report


def _redispatch(ctx: WorkerContext, run_id: int,
                dispatch: Callable[[WorkerContext, WorkPlan], None], report: SweepReport) -> None:
    with ctx.uow() as uow:
        runs.log(uow, runs.load_run(uow, run_id),
                 "recovery: the run's tasks were never sent (worker lost after start); "
                 "sending them now", "warning")
    try:
        dispatch(ctx, _plan(ctx, run_id))
    except Exception:  # noqa: BLE001 — dispatch already failed the run; keep sweeping
        logger.exception("recover_runs: re-dispatch of run %s failed", run_id)
        return
    report.redispatched.append(run_id)


def _requeue(ctx: WorkerContext, run_id: int, enqueue: Callable[[int], None],
             report: SweepReport) -> None:
    try:
        enqueue(run_id)
    except Exception as exc:  # noqa: BLE001 — any broker error: try again next sweep
        logger.warning("recover_runs: re-enqueue of run %s failed: %s", run_id, exc)
        return
    with ctx.uow() as uow:
        runs.log(uow, runs.load_run(uow, run_id),
                 "recovery: no task was queued for this run; enqueued it again", "warning")
    report.requeued.append(run_id)


def _fail(ctx: WorkerContext, run: _Candidate, now: datetime, report: SweepReport) -> None:
    minutes = int((now - run.last_seen).total_seconds() // 60)
    runs.fail(ctx, run.id, LostRun(
        f"run lost: no worker heartbeat for {minutes} min and no task queued or running "
        f"for it (stage {run.stage or 'none'}); a worker probably died — start the run again"))
    report.failed.append(run.id)


# ---------------------------------------------------------------- broker probe
def message_run_id(payload: dict[str, Any]) -> int | None:
    """Run id of one Celery (protocol 2) message, or None for other tasks."""
    headers = payload.get("headers") or {}
    if headers.get("task") not in RUN_TASKS:
        return None
    args = _message_args(payload)
    first = args[0] if args else None
    return first if isinstance(first, int) and not isinstance(first, bool) else None


def _message_args(payload: dict[str, Any]) -> Sequence[Any] | None:
    rep = (payload.get("headers") or {}).get("argsrepr")
    if isinstance(rep, str):
        try:
            value = ast.literal_eval(rep)
        except (ValueError, SyntaxError):
            value = None
        if isinstance(value, tuple | list):
            return value
    try:
        body: Any = payload.get("body")
        if (payload.get("properties") or {}).get("body_encoding") == "base64":
            body = base64.b64decode(body)
        decoded = json.loads(body)
    except (TypeError, ValueError):
        return None
    return decoded[0] if isinstance(decoded, list) and decoded and \
        isinstance(decoded[0], list) else None


def run_ids_in(raw_messages: Iterable[bytes | str], *, unacked: bool = False) -> set[int]:
    """Run ids in raw Redis entries (queue list items, or ``unacked`` hash values)."""
    out: set[int] = set()
    for raw in raw_messages:
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        payload = item[0] if unacked and isinstance(item, list) and item else item
        if isinstance(payload, dict) and (run_id := message_run_id(payload)) is not None:
            out.add(run_id)
    return out


class RedisBrokerProbe:
    """Reads the Redis broker directly: every queue (all priority lists) + ``unacked``."""

    def __init__(self, app: Any, queues: Sequence[str]) -> None:
        self.app = app
        self.queues = tuple(queues)

    def run_ids(self) -> set[int] | None:
        try:
            with self.app.connection_for_read() as conn:
                channel = conn.default_channel
                client = channel.client
                found: set[int] = set()
                for queue in self.queues:
                    keys = dict.fromkeys(channel._q_for_pri(queue, p)
                                         for p in channel.priority_steps)
                    for key in keys:
                        found |= run_ids_in(client.lrange(key, 0, -1))
                found |= run_ids_in(client.hvals(channel.unacked_key), unacked=True)
                return found
        except Exception as exc:  # noqa: BLE001 — not Redis, or unreachable: "unknown"
            logger.warning("recover_runs: cannot inspect the broker: %s", exc)
            return None
