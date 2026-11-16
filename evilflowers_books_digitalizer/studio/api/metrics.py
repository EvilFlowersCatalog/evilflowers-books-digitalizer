"""``GET /metrics``: Prometheus gauges read from the ledger at scrape time.

The database is the source of truth, so nothing is counted in process memory: any API
replica answers the same, a restart loses nothing, and there is no multiprocess
bookkeeping. Scraped by alloy-cluster (pod annotation ``k8s.grafana.com/scrape``);
names follow the observability contract (``<service>_…``, base units, ``_seconds``).

* ``digitalizer_jobs{stage,lane,status}``           open jobs of active runs
* ``digitalizer_job_oldest_planned_seconds{lane}``  how long the oldest admitted-later job waits
* ``digitalizer_runs{kind,status}``                 queued / running runs
* ``digitalizer_books{project,status}``             books per lifecycle status
* ``digitalizer_scheduler_capacity{lane}``          jobs admitted in flight now
* ``digitalizer_scheduler_mode{mode}``              1 for the current mode (normal/peak/contended/paused)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from evilflowers_books_digitalizer.db.models import Book, Job, Run, utcnow
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.domain import JobStatus, Lane, RunStatus
from evilflowers_books_digitalizer.studio.orchestration.scheduling import Mode

router = APIRouter()
_OPEN = [str(s) for s in JobStatus if s.open]
_ACTIVE = [str(RunStatus.QUEUED), str(RunStatus.RUNNING)]


class LedgerCollector:
    def __init__(self, services: Services) -> None:
        self._svc = services

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
        with self._svc.uow() as uow:
            s = uow.session
            for stage, lane, status, n in s.execute(
                    select(Job.stage, Job.lane, Job.status, func.count()).join(Run)
                    .where(Job.status.in_(_OPEN), Run.status.in_(_ACTIVE))
                    .group_by(Job.stage, Job.lane, Job.status)):
                jobs.add_metric([stage, lane, status], n)
            waiting = dict(s.execute(
                select(Job.lane, func.min(Job.created_at)).join(Run)
                .where(Job.status == str(JobStatus.PLANNED), Run.status.in_(_ACTIVE))
                .group_by(Job.lane)).all())
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
        yield from self._scheduler()

    def _scheduler(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        cap = self._svc.admission().capacity()
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


def render(services: Services) -> tuple[bytes, str]:
    from prometheus_client import CollectorRegistry, generate_latest
    from prometheus_client.exposition import CONTENT_TYPE_LATEST

    registry = CollectorRegistry(auto_describe=False)
    registry.register(LedgerCollector(services))
    return generate_latest(registry), CONTENT_TYPE_LATEST


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    body, content_type = render(request.app.state.studio.services)
    return Response(content=body, media_type=content_type)
