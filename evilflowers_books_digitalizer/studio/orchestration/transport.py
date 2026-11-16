"""Sending jobs over Celery, with the result coming back as ``link`` / ``link_error``.

Verified against Redis with no result backend (evilflowers-worker#5, 2026-11-16):

* ``link`` is applied with the task's return value, plus the bound ``job_id`` and
  ``task_id`` (the attempt it belongs to);
* ``link_error`` must be a ``bind=True`` task: a registered errback with more than one
  argument is otherwise *called* with ``(request, exc, traceback)`` in the failing
  worker; bound, it is always applied as a task with the failed task id;
* ``self.retry()`` fires neither; a killed worker fires neither (the message returns
  after the visibility timeout, and ``reconcile`` covers the rest).

No priority emulation: on a Redis db shared with kit workers a ``priority`` would write
to a ``<queue>\\x06\\x16<n>`` list those workers never read. Urgency is a separate
queue (the interactive lane) and the pump's admission order.
"""

from __future__ import annotations

from typing import Any

from celery import Celery

from evilflowers_books_digitalizer.studio.config import PORTAL_QUEUE
from evilflowers_books_digitalizer.studio.orchestration.ledger import Dispatch

RUN_TASK = "digitalizer.run"
JOB_DONE = "digitalizer.job_done"
JOB_FAILED = "digitalizer.job_failed"


class CeleryTransport:
    def __init__(self, app: Celery) -> None:
        self._app = app

    def _call(self, name: str, **options: Any) -> None:
        """A task of this app runs through its object (eager mode honours it); others by name."""
        task = self._app.tasks.get(name)
        if task is not None:
            task.apply_async(**options)
        else:
            self._app.send_task(name, **options)

    def send(self, dispatch: Dispatch) -> None:
        link = self._app.signature(JOB_DONE, kwargs={"job_id": dispatch.job_id,
                                                     "task_id": dispatch.task_id},
                                   queue=PORTAL_QUEUE)
        errback = self._app.signature(JOB_FAILED, kwargs={"job_id": dispatch.job_id},
                                      queue=PORTAL_QUEUE)
        self._call(dispatch.task, kwargs=dispatch.kwargs, queue=dispatch.queue,
                   task_id=dispatch.task_id, link=link, link_error=errback,
                   headers={"digitalizer_run_id": dispatch.run_id,
                            "digitalizer_job_id": dispatch.job_id,
                            "digitalizer_stage": dispatch.stage,
                            "digitalizer_lane": dispatch.lane})

    def start_run(self, run_id: int) -> None:
        self._call(RUN_TASK, args=[run_id], queue=PORTAL_QUEUE)

    def revoke(self, task_ids: list[str]) -> None:
        if task_ids and not self._app.conf.task_always_eager:
            self._app.control.revoke(task_ids, terminate=False)
