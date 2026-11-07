"""Publisher for ``publish.target = "none"``: records nothing remotely.

Projects without a catalog (local trials, IA-only libraries) still run the same
use cases; returning a :class:`PublishResult` instead of raising keeps the
publish stage and the Publication row uniform. The entry id is the permanent
item identifier, so a republish maps to the same "entry".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from evilflowers_books_digitalizer.studio.metadata import ItemMetadata
from evilflowers_books_digitalizer.studio.ports import PublishResult


@dataclass(slots=True)
class NullPublisher:
    calls: list[str] = field(default_factory=list)

    def publish(self, item_id: str, metadata: ItemMetadata, files: dict[str, Path],
                *, entry_id: str | None = None) -> PublishResult:
        self.calls.append(item_id)
        return PublishResult(entry_id or item_id, None, created=entry_id is None, acquisitions={})
