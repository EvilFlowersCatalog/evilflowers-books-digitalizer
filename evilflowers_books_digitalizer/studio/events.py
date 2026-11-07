"""Events: what workers announce and the SPA streams (Redis pub/sub -> API -> SSE).

One JSON object per event, one Redis channel for the whole studio
(:data:`CHANNEL`). The API subscribes once and fans events out to every SSE
client whose filter matches (project / book). Events are *notifications*, not
the source of truth: a client that reconnects re-reads state over REST.

SSE wire format (``GET /api/v1/events?project=…&book=…``)::

    id: <monotonic id>
    event: <type>
    data: <Event.to_json()>

Keep-alive comment lines (``: ping``) every 15 s.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

CHANNEL = "studio:events"


class EventType(StrEnum):
    # discovery / scheduling
    IMPORT_STARTED = "import.started"        # data: {collection}
    IMPORT_FINISHED = "import.finished"      # data: {collection, books, new}
    BOOK_DISCOVERED = "book.discovered"      # data: {slug}
    BOOK_QUEUED = "book.queued"              # data: {run_id, kind}
    # progress
    RUN_STARTED = "run.started"              # data: {run_id, kind, pages}
    RUN_PROGRESS = "run.progress"            # data: {run_id, stage, done, total}
    RUN_LOG = "run.log"                      # data: {run_id, level, message}
    RUN_FINISHED = "run.finished"            # data: {run_id, status, error?}
    PAGE_UPDATED = "page.updated"            # data: {page_id, index, version, status}
    REGIONS_UPDATED = "regions.updated"      # data: {page_id, index, disputed}
    # outcome
    BOOK_STATUS = "book.status"              # data: {status, previous}
    BOOK_REVIEW_NEEDED = "book.review_needed"  # data: {flags, score}
    BOOK_READY = "book.ready"                # data: {score}
    BOOK_PUBLISHED = "book.published"        # data: {publication_id, entry_id, version}


@dataclass(frozen=True, slots=True)
class Event:
    type: EventType
    project: str                       # project slug
    book_id: int | None = None         # books.id (database key)
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def to_json(self) -> str:
        d = asdict(self)
        d["type"] = str(self.type)
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> Event:
        d = json.loads(raw)
        return cls(type=EventType(d["type"]), project=d["project"], book_id=d.get("book_id"),
                   data=d.get("data") or {}, ts=d.get("ts") or "")

    def matches(self, project: str | None = None, book_id: int | None = None) -> bool:
        """SSE filter: no filter = everything; a book filter implies its project."""
        if book_id is not None:
            return self.book_id == book_id
        return project is None or self.project == project
