"""The scheduler as operators see it: current admission, open jobs, the pause switch.

Pausing stops *new* bulk jobs from being sent (operator re-runs still go through);
jobs already running finish. Resuming lets the next pump (≤ 15 s) send again. The
switch lives in Redis, so the API and every orchestrator replica agree on it.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from evilflowers_books_digitalizer.db.models import Job, Run
from evilflowers_books_digitalizer.studio.application import dto
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.domain import JobStatus, RunStatus

_OPEN = [str(s) for s in JobStatus if s.open]
_ACTIVE = [str(RunStatus.QUEUED), str(RunStatus.RUNNING)]


def status(svc: Services) -> dto.SchedulerOut:
    cap = svc.admission().capacity()
    with svc.uow() as uow:
        rows = uow.session.execute(
            select(Job.status, Job.lane).join(Run)
            .where(Job.status.in_(_OPEN), Run.status.in_(_ACTIVE))).all()
    policy = svc.config.scheduling
    return dto.SchedulerOut(
        mode=str(cap.mode), reason=cap.reason,
        lanes={str(k): v for k, v in cap.lanes.items()}, queues=dict(cap.queues),
        held_stages=sorted(cap.held_stages), peak_hours=list(policy.peak_hours),
        timezone=policy.timezone,
        jobs=dict(Counter(f"{job_status}/{lane}" for job_status, lane in rows)))


def pause(svc: Services, cmd: dto.PauseIn, author: str | None) -> dto.SchedulerOut:
    svc.control.pause(f"{cmd.reason} ({author})" if author else cmd.reason)
    return status(svc)


def resume(svc: Services) -> dto.SchedulerOut:
    svc.control.resume()
    return status(svc)
