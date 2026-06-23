"""Out-of-band per-book progress heartbeats for the live TUI.

Each worker writes a small JSON file under ``<output_dir>/.progress/`` as it
moves through the pipeline and removes it on completion. The monitor reads the
directory to show which books are *in flight* across the parallel workers — the
same decoupling as the JSONL reports, so watching never touches the running
batch.

Heartbeats refresh at each step boundary; a file whose ``updated_at`` is older
than ``ttl`` is treated as stale (a worker that died hard) and ignored, and
``clear()`` wipes leftovers at the start of a run.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

PROGRESS_DIRNAME = ".progress"

#: A heartbeat older than this (seconds) is considered stale and hidden. Set
#: generously: it only needs to outlast the slowest single step, and its real
#: job is to hide leftovers from a crashed run rather than to time workers out.
DEFAULT_TTL = 3600.0


def progress_dir(output_dir: Path | str) -> Path:
    return Path(output_dir) / PROGRESS_DIRNAME


class BookProgress:
    """Writes/removes one book's heartbeat file. One instance per worker."""

    def __init__(self, output_dir: Path | str, source: str, book_id: str):
        self.source = source
        self.book_id = book_id
        self.path = progress_dir(output_dir) / f"{source}_{book_id}.json"
        self.started_at = time.time()
        self.n_steps = 0

    def start(self, n_steps: int = 0) -> None:
        """Mark the book as picked up by a worker (before the first step)."""
        self.n_steps = n_steps
        self._write(step="starting", step_index=0)

    def update(self, index: int, total: int, name: str) -> None:
        """Pipeline ``on_step`` hook: a step (0-based ``index`` of ``total``) begins."""
        self.n_steps = total
        self._write(step=name, step_index=index + 1)

    def finish(self) -> None:
        """Remove the heartbeat (book done, errored, or skipped)."""
        self.path.unlink(missing_ok=True)

    def _write(self, *, step: str, step_index: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": self.source,
            "book_id": self.book_id,
            "slug": f"{self.source}_{self.book_id}",
            "pid": os.getpid(),
            "step": step,
            "step_index": step_index,
            "n_steps": self.n_steps,
            "started_at": self.started_at,
            "updated_at": time.time(),
        }
        # Atomic replace so the monitor never reads a half-written file.
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.path)


def read_active(output_dir: Path | str, *, ttl: float = DEFAULT_TTL) -> list[dict[str, Any]]:
    """In-flight workers (fresh heartbeats), oldest first, with ``elapsed`` filled in."""
    directory = progress_dir(output_dir)
    if not directory.exists():
        return []
    now = time.time()
    rows: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            row = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if now - row.get("updated_at", 0) > ttl:
            continue  # stale: worker likely died without cleaning up
        row["elapsed"] = now - row.get("started_at", now)
        rows.append(row)
    return sorted(rows, key=lambda r: r.get("started_at", 0))


def clear(output_dir: Path | str, source: str | None = None) -> None:
    """Remove heartbeats (optionally just one source's) — call at run start."""
    directory = progress_dir(output_dir)
    if not directory.exists():
        return
    pattern = f"{source}_*.json" if source else "*.json"
    for path in directory.glob(pattern):
        path.unlink(missing_ok=True)
