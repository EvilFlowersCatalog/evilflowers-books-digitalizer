"""Admission: how many jobs may be in flight now, per lane and per queue.

Book digitalization is background work. It runs at full width off-peak, narrows during
peak hours, and stops admitting new bulk jobs while another system's queues are deep
(the catalog's extraction backlog, say) or while an operator paused it. Operator
re-runs (the interactive lane) are admitted in every mode except a hard pause.

Admission only decides what is *sent*. A job that is already running is never
interrupted, so how fast the digitalizer yields equals its longest running job: keep
page chunks small, and keep whole-book stages (``off_peak_stages``) for off-peak.

CPU priority between pods is not decided here: that is Kubernetes (requests, limits,
a low ``PriorityClass`` for the bulk workers; deploy/k8s). Queue depth on another
Redis db measures the other system's *backlog*, not its CPU use; a worker busy on one
long job reads as an empty queue. That is why the probe is a port with a stated
meaning ("the other system has waiting work"), not a load metric.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from evilflowers_books_digitalizer.studio.config import BackpressureSource, SchedulingConfig
from evilflowers_books_digitalizer.studio.domain import Lane

logger = logging.getLogger(__name__)

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WINDOW = re.compile(r"^\s*(?:(?P<d1>[a-z]{3})(?:-(?P<d2>[a-z]{3}))?\s+)?"
                     r"(?P<t1>\d{1,2}:\d{2})-(?P<t2>\d{1,2}:\d{2})\s*$")


class Mode(StrEnum):
    NORMAL = "normal"           # off-peak: full width
    PEAK = "peak"               # peak hours: narrow
    CONTENDED = "contended"     # another system's queues are deep: bulk held (or narrow)
    PAUSED = "paused"           # an operator paused it: nothing new is sent


@dataclass(frozen=True, slots=True)
class PeakWindow:
    days: frozenset[int]        # 0 = Monday
    start: time
    end: time

    @classmethod
    def parse(cls, spec: str) -> PeakWindow:
        m = _WINDOW.match(spec.lower())
        if not m:
            raise ValueError(f"peak window {spec!r}: expected 'mon-fri 07:00-19:00' or '07:00-19:00'")
        first = _DAYS.index(m["d1"]) if m["d1"] else 0
        last = _DAYS.index(m["d2"]) if m["d2"] else (first if m["d1"] else 6)
        days = frozenset(range(first, last + 1)) if first <= last else \
            frozenset([*range(first, 7), *range(0, last + 1)])
        return cls(days, time.fromisoformat(m["t1"].zfill(5)), time.fromisoformat(m["t2"].zfill(5)))

    def contains(self, local: datetime) -> bool:
        if local.weekday() not in self.days:
            return False
        t = local.time()
        return self.start <= t < self.end if self.start <= self.end else \
            (t >= self.start or t < self.end)


class Backpressure(Protocol):
    """Waiting work of another system: ``{source: depth}`` of the sources over their limit."""

    def over_limit(self) -> dict[str, int]: ...


class ControlStore(Protocol):
    """The operator's pause switch, shared by every orchestrator process."""

    def paused(self) -> str | None: ...           # the reason, or None
    def pause(self, reason: str) -> None: ...
    def resume(self) -> None: ...


@dataclass
class InMemoryControl:
    reason: str | None = None

    def paused(self) -> str | None:
        return self.reason

    def pause(self, reason: str) -> None:
        self.reason = reason or "paused"

    def resume(self) -> None:
        self.reason = None


class RedisControl:
    KEY = "digitalizer:scheduler:paused"

    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url, socket_timeout=2)

    def paused(self) -> str | None:
        try:
            value: Any = self._redis.get(self.KEY)
        except Exception as exc:  # noqa: BLE001 - an unreadable switch must not stop the studio
            logger.warning("pause switch unreadable: %s", exc)
            return None
        return bytes(value).decode() if value else None

    def pause(self, reason: str) -> None:
        self._redis.set(self.KEY, reason or "paused")

    def resume(self) -> None:
        self._redis.delete(self.KEY)


class NoBackpressure:
    def over_limit(self) -> dict[str, int]:
        return {}


class RedisQueueDepth:
    """``LLEN`` of other systems' Celery queues (plain lists, as the kit's workers use)."""

    def __init__(self, sources: Sequence[BackpressureSource],
                 client_for: Callable[[str], Any] | None = None) -> None:
        self._sources = list(sources)
        self._client_for = client_for or _redis_client

    def over_limit(self) -> dict[str, int]:
        over: dict[str, int] = {}
        for source in self._sources:
            try:
                client = self._client_for(source.redis_url)
                depth = sum(int(client.llen(q)) for q in source.queues)
            except Exception as exc:  # noqa: BLE001 - unknown backlog = assume none, log it
                logger.warning("backpressure source %s unreadable: %s", source.name, exc)
                continue
            if depth > source.max_depth:
                over[source.name] = depth
        return over


_clients: dict[str, Any] = {}


def _redis_client(url: str) -> Any:
    if url not in _clients:
        import redis

        _clients[url] = redis.Redis.from_url(url, socket_timeout=2)
    return _clients[url]


@dataclass(frozen=True, slots=True)
class Capacity:
    mode: Mode
    lanes: dict[Lane, int]
    queues: dict[str, int] = field(default_factory=dict)
    held_stages: frozenset[str] = frozenset()      # bulk jobs of these stages wait
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"mode": str(self.mode), "lanes": {str(k): v for k, v in self.lanes.items()},
                "queues": dict(self.queues), "held_stages": sorted(self.held_stages),
                "reason": self.reason}


@dataclass
class Admission:
    policy: SchedulingConfig
    control: ControlStore = field(default_factory=InMemoryControl)
    backpressure: Backpressure = field(default_factory=NoBackpressure)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _windows: list[PeakWindow] = field(init=False)

    def __post_init__(self) -> None:
        self._windows = [PeakWindow.parse(w) for w in self.policy.peak_hours]

    def is_peak(self, now: datetime | None = None) -> bool:
        """Peak windows are local time of ``policy.timezone``; a naive ``now`` is UTC."""
        moment = now or self.clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        local = moment.astimezone(ZoneInfo(self.policy.timezone))
        return any(w.contains(local) for w in self._windows)

    def capacity(self, now: datetime | None = None) -> Capacity:
        p = self.policy
        queues = dict(p.queue_inflight)
        if reason := self.control.paused():
            return Capacity(Mode.PAUSED, {Lane.INTERACTIVE: p.interactive_inflight, Lane.BULK: 0},
                            queues, frozenset(), reason)
        if over := self.backpressure.over_limit():
            why = ", ".join(f"{name}={depth}" for name, depth in sorted(over.items()))
            return Capacity(Mode.CONTENDED, {Lane.INTERACTIVE: p.interactive_inflight,
                                             Lane.BULK: p.bulk_inflight_contended},
                            queues, frozenset(p.off_peak_stages), f"backlog: {why}")
        if self.is_peak(now):
            return Capacity(Mode.PEAK, {Lane.INTERACTIVE: p.interactive_inflight,
                                        Lane.BULK: p.bulk_inflight_peak},
                            queues, frozenset(p.off_peak_stages), "peak hours")
        return Capacity(Mode.NORMAL, {Lane.INTERACTIVE: p.interactive_inflight,
                                      Lane.BULK: p.bulk_inflight}, queues)
