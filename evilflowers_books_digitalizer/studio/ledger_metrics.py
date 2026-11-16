"""Prometheus gauges read from the ledger at scrape time, served by the orchestrator.

The database is the source of truth, so nothing is counted in process memory: a restart
loses nothing and there is no multiprocess bookkeeping. They are served by the component
that owns the ledger, the orchestrator (one replica), from its Celery main process
(:func:`serve`, started on ``worker_ready`` when ``DIGITALIZER_METRICS_PORT`` is set),
not by the portal: an alert on them must keep firing while the portal is down (#13).
Scraped by alloy-cluster (pod annotation ``k8s.grafana.com/scrape``, port ``metrics``);
names follow the observability contract (``<service>_…``, base units, ``_seconds``).

* ``digitalizer_jobs{stage,lane,status}``           open jobs of active runs
* ``digitalizer_job_oldest_planned_seconds{lane}``  how long the oldest admitted-later job waits
* ``digitalizer_runs{kind,status}``                 queued / running runs
* ``digitalizer_books{project,status}``             books per lifecycle status
* ``digitalizer_scheduler_capacity{lane}``          jobs admitted in flight now
* ``digitalizer_scheduler_mode{mode}``              1 for the current mode (normal/peak/contended/paused)
* ``digitalizer_ledger_scrape_ok``                  0 when the ledger could not be read (logged)
* ``digitalizer_marks{band}``                       scan marks on the books' pages (#14)
* ``digitalizer_mark_groups{source,band}``          book-wide mark groups and who decided them
* ``digitalizer_mark_decider_calls_saved``          decider-vision calls the grouping saved:
  occurrences of rule- and decider-judged groups, minus one call per decider-judged group
* ``digitalizer_title_page_fields{field,outcome}``  title-page suggestions the operators
  confirmed: accepted / changed / none (#15); ``outcome="pending"`` = suggested, not confirmed
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from evilflowers_books_digitalizer.db.models import Book, Job, MarkGroup, Page, Run, utcnow
from evilflowers_books_digitalizer.studio.application.uow import SessionFactory, UnitOfWork
from evilflowers_books_digitalizer.studio.domain import JobStatus, Lane, RunStatus
from evilflowers_books_digitalizer.studio.orchestration.scheduling import Admission, Mode

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

    from evilflowers_books_digitalizer.studio.config import StudioConfig

logger = logging.getLogger(__name__)

PORT_ENV = "DIGITALIZER_METRICS_PORT"
_OPEN = [str(s) for s in JobStatus if s.open]
_ACTIVE = [str(RunStatus.QUEUED), str(RunStatus.RUNNING)]


class LedgerCollector:
    def __init__(self, sessions: SessionFactory, admission: Callable[[], Admission]) -> None:
        self._sessions = sessions
        self._admission = admission

    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        jobs = GaugeMetricFamily("digitalizer_jobs", "Open jobs of active runs",
                                 labels=["stage", "lane", "status"])
        oldest = GaugeMetricFamily("digitalizer_job_oldest_planned_seconds",
                                   "Age of the oldest job waiting for admission",
                                   labels=["lane"])
        run_g = GaugeMetricFamily("digitalizer_runs", "Queued and running runs",
                                  labels=["kind", "status"])
        books = GaugeMetricFamily("digitalizer_books", "Books per lifecycle status",
                                  labels=["project", "status"])
        now = utcnow()
        with UnitOfWork(self._sessions) as uow:
            s = uow.session
            for stage, lane, status, n in s.execute(
                    select(Job.stage, Job.lane, Job.status, func.count()).join(Run)
                    .where(Job.status.in_(_OPEN), Run.status.in_(_ACTIVE))
                    .group_by(Job.stage, Job.lane, Job.status)):
                jobs.add_metric([stage, lane, status], n)
            waiting: dict[str, datetime] = {lane: since for lane, since in s.execute(
                select(Job.lane, func.min(Job.created_at)).join(Run)
                .where(Job.status == str(JobStatus.PLANNED), Run.status.in_(_ACTIVE))
                .group_by(Job.lane))}
            for kind, status, n in s.execute(
                    select(Run.kind, Run.status, func.count()).where(Run.status.in_(_ACTIVE))
                    .group_by(Run.kind, Run.status)):
                run_g.add_metric([kind, status], n)
            for project, status, n in s.execute(
                    select(Book.project, Book.status, func.count())
                    .group_by(Book.project, Book.status)):
                books.add_metric([project or "", status], n)
        for lane in Lane:
            since = waiting.get(str(lane))
            oldest.add_metric([str(lane)], (now - since).total_seconds() if since else 0.0)
        yield from (jobs, oldest, run_g, books)
        yield from self._decisions()
        yield from self._scheduler()

    def _decisions(self) -> Iterator[Any]:
        """Scan-mark grouping (#14) and title-page prefill (#15), from their rows."""
        from prometheus_client.core import GaugeMetricFamily

        marks = GaugeMetricFamily("digitalizer_marks", "Scan marks on the books' pages",
                                  labels=["band"])
        groups = GaugeMetricFamily("digitalizer_mark_groups", "Book-wide scan-mark groups",
                                   labels=["source", "band"])
        saved = GaugeMetricFamily("digitalizer_mark_decider_calls_saved",
                                  "decider-vision calls the book-wide grouping saved")
        fields = GaugeMetricFamily("digitalizer_title_page_fields",
                                   "Title-page suggestions by confirmation outcome",
                                   labels=["field", "outcome"])
        bands: Counter[str] = Counter()
        outcomes: Counter[tuple[str, str]] = Counter()
        n_saved = 0
        with UnitOfWork(self._sessions) as uow:
            s = uow.session
            for (rows,) in s.execute(select(Page.marks).where(Page.marks.is_not(None))):
                bands.update(str(r.get("band", "review")) for r in rows or [])
            for source, band, n, members in s.execute(
                    select(MarkGroup.source, MarkGroup.band, func.count(),
                           func.sum(MarkGroup.members))
                    .group_by(MarkGroup.source, MarkGroup.band)):
                groups.add_metric([source, band], n)
                if source in ("rule", "decider"):
                    n_saved += int(members or 0) - (n if source == "decider" else 0)
            for suggested, confirmed in s.execute(
                    select(Book.metadata_suggestions, Book.metadata_confirmed)
                    .where(Book.metadata_suggestions.is_not(None))):
                cases = (confirmed or {}).get("fields") or {}
                for name in ("title", "authors", "publisher", "year"):
                    if name in cases:
                        outcomes[(name, str(cases[name].get("outcome")))] += 1
                    elif isinstance((suggested or {}).get(name), dict):
                        outcomes[(name, "pending")] += 1
        for band, n in sorted(bands.items()):
            marks.add_metric([band], n)
        saved.add_metric([], n_saved)
        for (name, outcome), n in sorted(outcomes.items()):
            fields.add_metric([name, outcome], n)
        yield from (marks, groups, saved, fields)

    def _scheduler(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        cap = self._admission().capacity()
        capacity = GaugeMetricFamily("digitalizer_scheduler_capacity",
                                     "Jobs admitted in flight now", labels=["lane"])
        for lane, n in cap.lanes.items():
            capacity.add_metric([str(lane)], n)
        mode = GaugeMetricFamily("digitalizer_scheduler_mode", "1 for the current admission mode",
                                 labels=["mode"])
        for m in Mode:
            mode.add_metric([str(m)], 1.0 if m == cap.mode else 0.0)
        yield capacity
        yield mode


def registry(collector: LedgerCollector | _Guarded) -> CollectorRegistry:
    from prometheus_client import CollectorRegistry

    reg = CollectorRegistry(auto_describe=False)
    reg.register(collector)   # anything with collect(); auto_describe=False: never called early
    return reg


def render(collector: LedgerCollector) -> bytes:
    """The exposition text (tests; :func:`serve` answers scrapes itself)."""
    from prometheus_client import generate_latest

    return generate_latest(registry(collector))


def production_collector(config: StudioConfig) -> LedgerCollector:
    """A collector with its own engine. ``NullPool``: one connection per scrape and none kept,
    so a pool process the Celery parent forks later inherits no open Postgres socket."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from evilflowers_books_digitalizer.studio.workers.context import production_admission

    engine = create_engine(config.database_url, poolclass=NullPool)
    admission = production_admission(config)
    return LedgerCollector(sessionmaker(engine, expire_on_commit=False), lambda: admission)


def serve(port: int | None = None, collector: LedgerCollector | None = None) -> int | None:
    """Serve ``/metrics`` on ``port`` (default ``$DIGITALIZER_METRICS_PORT``) from a daemon
    thread. Returns the port, or None when no port is configured (every worker but the
    orchestrator). The collector is built lazily, on the first scrape's thread."""
    if port is None:
        raw = os.environ.get(PORT_ENV)
        if not raw:
            return None
        port = int(raw)
    from prometheus_client import start_http_server

    server, _thread = start_http_server(port, registry=registry(_Guarded(collector)))
    bound = int(server.server_address[1])
    logger.info("ledger metrics on :%d/metrics", bound)
    return bound


class _Guarded:
    """The served collector: built on the first scrape (a bad config never stops the worker)
    and never raising: a failed read is one JSON log line plus
    ``digitalizer_ledger_scrape_ok 0``, not a bare traceback on stderr from the HTTP server."""

    def __init__(self, inner: LedgerCollector | None) -> None:
        self._inner = inner

    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        ok = GaugeMetricFamily("digitalizer_ledger_scrape_ok",
                               "1 when the ledger could be read for this scrape")
        try:
            if self._inner is None:
                from evilflowers_books_digitalizer.studio.config import load_config

                self._inner = production_collector(load_config())
            families = list(self._inner.collect())
        except Exception:  # noqa: BLE001 - a scrape must answer; the reason goes to the log
            logger.exception("ledger metrics: could not read the ledger")
            families = []
        ok.add_metric([], 1.0 if families else 0.0)
        yield from families
        yield ok
