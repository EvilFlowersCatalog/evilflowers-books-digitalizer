"""TaskQueue adapters: Celery by task *name* (no import of task functions) and an inline recorder.

Keeping the API process free of worker imports (ScanTailor, onnxruntime, …) is the
point: ``send_task("digitalizer.run", …)`` only needs the broker URL.

Broker contract (shared with every worker on the digitalizer's Redis db, kit services
included, evilflowers-worker#5):

* plain lists, **no priority emulation**: a ``priority`` would land in a
  ``<queue>\\x06\\x16<n>`` list that kit workers never read, and KEDA / redis_exporter
  would see an empty queue. Urgency is the interactive queue + the pump's admission order;
* one ``visibility_timeout`` for every consumer of the db (the shortest one wins):
  12 h, the kit's default, above the longest single job (a whole-book render);
* no result backend: results come back as ``link`` / ``link_error`` callbacks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from evilflowers_books_digitalizer.studio.config import (
    AI_QUEUE,
    CPU_QUEUE,
    INTERACTIVE_QUEUE,
    PORTAL_QUEUE,
)

#: every queue this application consumes
QUEUES = (PORTAL_QUEUE, CPU_QUEUE, AI_QUEUE, INTERACTIVE_QUEUE)
VISIBILITY_TIMEOUT = 12 * 3600
TRANSPORT_OPTIONS: dict[str, Any] = {"visibility_timeout": VISIBILITY_TIMEOUT}


class CeleryTaskQueue:
    def __init__(self, broker_url: str, app: Any | None = None) -> None:
        if app is None:
            from celery import Celery

            app = Celery("digitalizer-api", broker=broker_url)
            app.conf.broker_transport_options = dict(TRANSPORT_OPTIONS)
        self._app = app

    def _send(self, name: str, args: list[Any]) -> str:
        return str(self._app.send_task(name, args=args, queue=PORTAL_QUEUE).id)

    def enqueue_run(self, run_id: int, *, interactive: bool = False) -> str:
        # one queue for starts: the orchestrator is light; the lane decides admission
        return self._send("digitalizer.run", [run_id])

    def enqueue_discover(self, project: str, collection: str | None = None) -> str:
        return self._send("digitalizer.discover", [project, collection])

    def enqueue_export(self, project: str) -> str:
        return self._send("digitalizer.export_training", [project])

    def revoke(self, task_id: str) -> None:
        self._app.control.revoke(task_id, terminate=False)


@dataclass
class RecordingTaskQueue:
    """Test double: records what would be enqueued; ``on_run`` can execute inline."""

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    on_run: Any = None   # callable(run_id) executed synchronously when set

    def _record(self, name: str, *args: Any, **kwargs: Any) -> str:
        self.calls.append((name, args, kwargs))
        return uuid.uuid4().hex

    def enqueue_run(self, run_id: int, *, interactive: bool = False) -> str:
        task_id = self._record("digitalizer.run", run_id, interactive=interactive)
        if self.on_run is not None:
            self.on_run(run_id)
        return task_id

    def enqueue_discover(self, project: str, collection: str | None = None) -> str:
        return self._record("digitalizer.discover", project, collection)

    def enqueue_export(self, project: str) -> str:
        return self._record("digitalizer.export_training", project)

    def revoke(self, task_id: str) -> None:
        self.revoked.append(task_id)
