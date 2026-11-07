"""Batch fixtures (helpers live in ``batch_harness`` so test modules can import them)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from batch_harness import PROJECT, Studio, eager_celery, make_studio

from evilflowers_books_digitalizer.studio.workers import context

# the `integration` marker and its opt-in (-m integration) live in pyproject.toml


@pytest.fixture(autouse=True)
def _eager_celery() -> Iterator[None]:
    eager_celery()
    yield
    context.configure(None)


@pytest.fixture
def studio(tmp_path: Path) -> Studio:
    return make_studio(tmp_path)


@pytest.fixture
def discovered(studio: Studio) -> Studio:
    from evilflowers_books_digitalizer.studio.workers import tasks

    tasks.discover.delay(PROJECT)
    return studio
