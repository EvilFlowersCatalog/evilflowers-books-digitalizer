"""The ports a use case may need, bundled once per process by a shell (API, CLI).

Use cases take ``Services`` and touch only the ports they need; the shells decide
which adapters fill it (Celery + Redis in production, recorders + in-memory bus
in tests). Nothing here constructs an adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from evilflowers_books_digitalizer.studio.application.uow import SessionFactory, UnitOfWork
from evilflowers_books_digitalizer.studio.config import ProjectConfig, StudioConfig
from evilflowers_books_digitalizer.studio.orchestration.scheduling import (
    Admission,
    Backpressure,
    ControlStore,
    InMemoryControl,
    NoBackpressure,
)
from evilflowers_books_digitalizer.studio.ports import ArtifactStore, EventBus, TaskQueue

#: project slug -> the project's ArtifactStore (None when no adapter is wired)
type ArtifactsFor = Callable[[str], ArtifactStore | None]


def _no_artifacts(_project: str) -> ArtifactStore | None:
    return None


@dataclass(frozen=True, slots=True)
class Services:
    config: StudioConfig
    sessions: SessionFactory
    queue: TaskQueue
    bus: EventBus
    artifacts_for: ArtifactsFor = _no_artifacts
    #: the scheduler's pause switch (Redis in production: shared with the orchestrator)
    control: ControlStore = field(default_factory=InMemoryControl)
    backpressure: Backpressure = field(default_factory=NoBackpressure)

    def uow(self) -> UnitOfWork:
        return UnitOfWork(self.sessions, self.bus)

    def admission(self) -> Admission:
        return Admission(self.config.scheduling, self.control, self.backpressure)

    def project(self, slug: str | None) -> ProjectConfig | None:
        """The project's config, or None for legacy books / projects no longer configured."""
        return self.config.projects.get(slug) if slug else None
