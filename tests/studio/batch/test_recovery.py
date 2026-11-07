"""Stale-run recovery: orphaned dispatches, lost messages, dead workers — and what is left alone."""

from __future__ import annotations

import base64
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from collections.abc import Iterator
from typing import Any

import pytest
from batch_harness import Studio

from evilflowers_books_digitalizer.db.models import Book, Run, utcnow
from evilflowers_books_digitalizer.studio.domain import BookStatus, RunKind, RunStatus
from evilflowers_books_digitalizer.studio.workers import context, recovery, runs, tasks
from evilflowers_books_digitalizer.studio.workers.app import BEAT_SCHEDULE, ROUTES
from evilflowers_books_digitalizer.studio.workers.stages.orchestrate import WorkPlan, orchestrate

LATER = timedelta(hours=2)


class Probe:
    def __init__(self, ids: set[int] | None = None) -> None:
        self.ids = ids

    def run_ids(self) -> set[int] | None:
        return self.ids


def sweep(*, probe: Probe | None = None, now: datetime | None = None,
          dispatch: Any = None, enqueue: Any = None) -> recovery.SweepReport:
    return recovery.sweep(context.current(), probe or Probe(set()),
                          dispatch=dispatch or tasks._send,
                          enqueue=enqueue or (lambda run_id: pytest.fail("unexpected enqueue")),
                          now=now or utcnow() + LATER)


def orphan(studio: Studio, kind: RunKind = RunKind.FULL) -> int:
    """A run orchestrate started whose worker died before sending the canvas."""
    run_id = studio.new_run(kind)
    assert orchestrate(context.current(), run_id) is not None
    return run_id


def set_run(studio: Studio, run_id: int, **values: Any) -> None:
    s = studio.session()
    run = s.get(Run, run_id)
    for k, v in values.items():
        setattr(run, k, v)
    s.commit()


# ---------------------------------------------------------------- never dispatched
def test_sweeper_sends_the_canvas_of_an_orphaned_run(discovered: Studio) -> None:
    run_id = orphan(discovered)
    report = sweep()
    assert report.redispatched == [run_id] and not report.failed
    run = discovered.session().get(Run, run_id)
    assert run.status == RunStatus.SUCCEEDED, run.error           # eager canvas ran to the end
    assert run.dispatched_at is not None
    assert any("never sent" in e["message"] for e in run.log or [])


def test_orphan_within_the_grace_period_is_left_alone(discovered: Studio) -> None:
    run_id = orphan(discovered)
    assert sweep(now=utcnow()).redispatched == []
    assert discovered.session().get(Run, run_id).status == RunStatus.RUNNING


def test_orphan_is_dispatched_once_however_many_sweeps(discovered: Studio) -> None:
    run_id = orphan(discovered)
    sent: list[WorkPlan] = []
    for _ in range(3):
        sweep(dispatch=lambda ctx, plan: sent.append(plan))
    assert [p.run_id for p in sent] == [run_id]
    assert sent[0].stages == ("clean", "analyze", "formulas", "render", "gate")


def test_redelivered_studio_run_sends_the_canvas_orchestrate_committed(
        discovered: Studio) -> None:
    run_id = orphan(discovered)
    out = tasks.run(run_id)                                        # the redelivered message
    assert out["stages"][0] == "clean"
    assert discovered.session().get(Run, run_id).status == RunStatus.SUCCEEDED
    # and a further copy finds it dispatched: no second canvas
    set_run(discovered, run_id, status=RunStatus.RUNNING)
    assert tasks.run(run_id) == {"run_id": run_id, "skipped": True}


def test_dispatch_claim_is_exclusive(discovered: Studio) -> None:
    run_id = orphan(discovered)
    ctx = context.current()
    assert runs.claim_dispatch(ctx, run_id) is True
    assert runs.claim_dispatch(ctx, run_id) is False


# ---------------------------------------------------------------- lost while running
def test_running_run_without_heartbeat_or_messages_fails_and_errors_the_book(
        discovered: Studio) -> None:
    run_id = orphan(discovered)
    assert runs.claim_dispatch(context.current(), run_id)         # canvas "sent", then lost
    set_run(discovered, run_id, stage="analyze")
    report = sweep()
    assert report.failed == [run_id]
    run = discovered.session().get(Run, run_id)
    assert run.status == RunStatus.FAILED and "run lost" in (run.error or "")
    assert "stage analyze" in (run.error or "")
    book = discovered.session().query(Book).one()
    assert book.status == BookStatus.ERROR and "run lost" in (book.error or "")


def test_render_run_lost_leaves_the_book_status_alone(discovered: Studio) -> None:
    assert discovered.execute(RunKind.FULL).status == RunStatus.SUCCEEDED
    before = discovered.session().query(Book).one().status
    run_id = orphan(discovered, RunKind.RENDER)
    runs.claim_dispatch(context.current(), run_id)
    set_run(discovered, run_id, stage="render")
    assert sweep().failed == [run_id]
    assert discovered.session().query(Book).one().status == before


def test_long_render_with_a_fresh_heartbeat_is_not_touched(discovered: Studio) -> None:
    run_id = orphan(discovered)
    runs.claim_dispatch(context.current(), run_id)
    set_run(discovered, run_id, stage="render", started_at=utcnow() - timedelta(hours=9))
    runs.touch(context.current(), run_id)                          # the task's heartbeat
    assert sweep(now=utcnow() + timedelta(minutes=5)).failed == []
    assert discovered.session().get(Run, run_id).status == RunStatus.RUNNING


def test_run_with_a_message_in_the_broker_is_never_failed(discovered: Studio) -> None:
    """Silent heartbeat, but its tasks wait behind another book's backlog."""
    run_id = orphan(discovered)
    runs.claim_dispatch(context.current(), run_id)
    set_run(discovered, run_id, stage="analyze")
    assert sweep(probe=Probe({run_id}), now=utcnow() + timedelta(days=2)).failed == []
    assert discovered.session().get(Run, run_id).status == RunStatus.RUNNING


def test_nothing_happens_when_the_broker_cannot_be_inspected(discovered: Studio) -> None:
    run_id = orphan(discovered)
    report = sweep(probe=Probe(None))
    assert report.skipped and not report.redispatched
    assert discovered.session().get(Run, run_id).status == RunStatus.RUNNING


# ---------------------------------------------------------------- queued, message lost
def test_queued_run_without_a_message_is_enqueued_again(discovered: Studio) -> None:
    run_id = discovered.new_run(RunKind.FULL)
    sent: list[int] = []
    now = utcnow() + LATER
    assert sweep(now=now, enqueue=sent.append).requeued == [run_id]
    # the next sweep, minutes later: the re-enqueue refreshed the heartbeat
    assert sweep(now=utcnow() + timedelta(minutes=5), enqueue=sent.append).requeued == []
    assert sent == [run_id]
    assert discovered.session().get(Run, run_id).status == RunStatus.QUEUED


def test_fresh_queued_run_is_left_alone(discovered: Studio) -> None:
    discovered.new_run(RunKind.FULL)
    assert sweep(now=utcnow(), enqueue=lambda run_id: pytest.fail("enqueued")).requeued == []


# ---------------------------------------------------------------- heartbeat + wiring
def test_tasks_beat_while_their_body_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    beats: list[int] = []
    monkeypatch.setattr(runs, "touch", lambda ctx, run_id: beats.append(run_id))
    with tasks._heartbeat(object(), 7, every=0.01):  # type: ignore[arg-type]
        time.sleep(0.1)
    assert len(beats) >= 4 and set(beats) == {7}                    # start, ticks, end


def test_recover_task_does_nothing_on_a_broker_it_cannot_read(discovered: Studio) -> None:
    run_id = orphan(discovered)                    # eager app: memory:// has no Redis lists
    assert tasks.recover_runs.delay().get()["skipped"] == "broker not inspectable"
    assert discovered.session().get(Run, run_id).status == RunStatus.RUNNING


def test_sweeper_is_scheduled_on_the_io_queue() -> None:
    entry = BEAT_SCHEDULE["studio.recover_runs"]
    assert entry["task"] == tasks.recover_runs.name == "studio.recover_runs"
    assert ROUTES["studio.recover_runs"] == {"queue": "io"}


# ---------------------------------------------------------------- broker probe
def message(task: str, args: tuple[Any, ...], *, argsrepr: bool = True) -> dict[str, Any]:
    body = base64.b64encode(json.dumps([list(args), {}, {}]).encode()).decode()
    headers: dict[str, Any] = {"task": task, "id": "x"}
    if argsrepr:
        headers["argsrepr"] = repr(args)
    return {"body": body, "headers": headers, "properties": {"body_encoding": "base64"}}


def test_run_ids_are_read_from_queued_and_unacked_messages() -> None:
    queued = [json.dumps(message("studio.analyze", (7, 3))),
              json.dumps(message("studio.clean", (8, [0, 1]), argsrepr=False)),
              json.dumps(message("studio.discover", (9,))),          # not a run task
              "not json"]
    unacked = [json.dumps([message("studio.render", (11,)), "cpu", "cpu"])]
    assert recovery.run_ids_in(queued) == {7, 8}
    assert recovery.run_ids_in(unacked, unacked=True) == {11}


class FakeRedis:
    def __init__(self, lists: dict[str, list[str]], unacked: list[str]) -> None:
        self.lists, self.unacked = lists, unacked

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self.lists.get(key, [])

    def hvals(self, key: str) -> list[str]:
        assert key == "unacked"
        return self.unacked


class FakeChannel:
    sep = "\x06\x16"
    priority_steps = [0, 3, 6, 9]
    unacked_key = "unacked"

    def __init__(self, client: FakeRedis) -> None:
        self.client = client

    def _q_for_pri(self, queue: str, pri: int) -> str:
        return f"{queue}{self.sep}{pri}" if pri else queue


class FakeApp:
    def __init__(self, channel: FakeChannel | None) -> None:
        self.channel = channel

    @contextmanager
    def connection_for_read(self) -> Iterator[Any]:
        if self.channel is None:
            raise ConnectionError("redis down")

        class Conn:
            default_channel = self.channel

        yield Conn()


def test_redis_probe_scans_every_priority_list_and_the_unacked_hash() -> None:
    client = FakeRedis({"io": [json.dumps(message("studio.run", (1,)))],
                        "ai\x06\x163": [json.dumps(message("studio.analyze", (2, 0)))]},
                       [json.dumps([message("studio.render", (3,)), "cpu", "cpu"])])
    probe = recovery.RedisBrokerProbe(FakeApp(FakeChannel(client)), ("io", "ai", "cpu"))
    assert probe.run_ids() == {1, 2, 3}
    assert recovery.RedisBrokerProbe(FakeApp(None), ("io",)).run_ids() is None
