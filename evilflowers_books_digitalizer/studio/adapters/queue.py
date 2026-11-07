"""TaskQueue adapters: Celery by task *name* (no import of task functions) and an inline recorder.

Keeping the API process free of worker imports (ScanTailor, torch, …) is the
point: ``send_task("studio.run", …)`` only needs the broker URL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

#: queue names (docs/design/studio-spec.md "Queues and tasks")
INTERACTIVE, CPU, AI, IO = "interactive", "cpu", "ai", "io"

#: Redis emulates priorities with one list per step and BRPOPs them in *ascending*
#: order (kombu ``_brpop_start``), so 0 is the most urgent: operator re-runs use it,
#: batch work the middle of the range.
INTERACTIVE_PRIORITY = 0
DEFAULT_PRIORITY = 5
#: shared by the worker app and the API client, so both agree on the list names.
#: ``visibility_timeout`` must exceed the longest single task (a whole-book render
#: or formulas stage): with ``acks_late`` Redis redelivers an unacked message after
#: it, and a second worker would start the same stage while the first still runs.
TRANSPORT_OPTIONS: dict[str, Any] = {
    "queue_order_strategy": "priority",
    "priority_steps": list(range(10)),
    "visibility_timeout": 24 * 3600,
}


class CeleryTaskQueue:
    def __init__(self, broker_url: str, app: Any | None = None) -> None:
        if app is None:
            from celery import Celery

            app = Celery("studio-client", broker=broker_url, backend=broker_url)
            app.conf.broker_transport_options = dict(TRANSPORT_OPTIONS)
        self._app = app

    def _send(self, name: str, args: list[Any], queue: str,
              priority: int = DEFAULT_PRIORITY) -> str:
        result = self._app.send_task(name, args=args, queue=queue, priority=priority)
        return str(result.id)

    def enqueue_run(self, run_id: int, *, interactive: bool = False) -> str:
        return self._send("studio.run", [run_id], INTERACTIVE if interactive else IO,
                          priority=INTERACTIVE_PRIORITY if interactive else DEFAULT_PRIORITY)

    def enqueue_discover(self, project: str, collection: str | None = None) -> str:
        return self._send("studio.discover", [project, collection], IO)

    def enqueue_export(self, project: str) -> str:
        return self._send("studio.export_training", [project], IO)

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
        task_id = self._record("studio.run", run_id, interactive=interactive)
        if self.on_run is not None:
            self.on_run(run_id)
        return task_id

    def enqueue_discover(self, project: str, collection: str | None = None) -> str:
        return self._record("studio.discover", project, collection)

    def enqueue_export(self, project: str) -> str:
        return self._record("studio.export_training", project)

    def revoke(self, task_id: str) -> None:
        self.revoked.append(task_id)
