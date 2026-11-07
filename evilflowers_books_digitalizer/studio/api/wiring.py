"""Composition root shared by the API and the ``studio`` CLI: config -> adapters -> Services.

Production adapters: SQLAlchemy (``db.store`` engine), Celery by task name,
Redis pub/sub, the IA-layout ArtifactStore and the IA exporter. None of them
connects at construction, so building the app or running ``studio import``
never needs more than the broker for the one call.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache

from sqlalchemy.orm import sessionmaker

from evilflowers_books_digitalizer.studio.adapters.artifacts import build_artifact_store
from evilflowers_books_digitalizer.studio.adapters.ia import build_ia_exporter
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.config import StudioConfig
from evilflowers_books_digitalizer.studio.ports import ArtifactStore

__all__ = ["artifact_resolver", "build_ia_exporter", "build_services"]


def build_services(config: StudioConfig) -> Services:
    from evilflowers_books_digitalizer.db.store import get_engine
    from evilflowers_books_digitalizer.studio.adapters.eventbus import RedisEventBus
    from evilflowers_books_digitalizer.studio.adapters.queue import CeleryTaskQueue

    sessions = sessionmaker(get_engine(config.database_url), expire_on_commit=False)
    return Services(config=config, sessions=sessions, queue=CeleryTaskQueue(config.redis_url),
                    bus=RedisEventBus(config.redis_url), artifacts_for=artifact_resolver(config))


def artifact_resolver(config: StudioConfig) -> Callable[[str], ArtifactStore | None]:
    """project slug -> its ArtifactStore (None for a project the config does not know)."""

    @cache
    def artifacts_for(project: str) -> ArtifactStore | None:
        return build_artifact_store(config, project) if project in config.projects else None

    return artifacts_for
