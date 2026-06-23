"""Pipeline framework: context object, step interface, runner."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BookContext:
    """Mutable state passed through the pipeline for a single book.

    Steps read and write:

    * ``tiffs`` — downloaded page scans, in page order
    * ``artifacts`` — named file outputs (``raw_pdf``, ``pdf``, ``text``, ...)
    * ``metadata`` — accumulated enrichments and classifications
    """

    source: str
    book_id: str
    work_dir: Path  # local directory holding downloaded scans
    output_dir: Path  # where final artifacts land
    tiffs: list[Path] = field(default_factory=list)
    artifacts: dict[str, Path] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return f"{self.source}_{self.book_id}"


class PipelineStep(ABC):
    """One transformation applied to a :class:`BookContext`."""

    #: Human-readable step name, used in logs.
    name: str = "step"

    @abstractmethod
    def run(self, ctx: BookContext) -> BookContext:
        """Transform the context in place and return it."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class Pipeline:
    """Ordered execution of steps with per-step logging and timing."""

    def __init__(self, steps: Sequence[PipelineStep]):
        self.steps = list(steps)

    def __repr__(self) -> str:
        names = " -> ".join(step.name for step in self.steps)
        return f"Pipeline({names})"

    def run(
        self,
        ctx: BookContext,
        *,
        on_step: Callable[[int, int, str], None] | None = None,
    ) -> BookContext:
        """Run every step, returning the final context.

        ``on_step(index, total, name)`` is invoked as each step *starts* (0-based
        index), letting a caller publish progress — see
        :class:`~evilflowers_books_digitalizer.progress.BookProgress`.
        """
        total = len(self.steps)
        logger.info("pipeline start: %s (%d steps)", ctx.slug, total)
        for index, step in enumerate(self.steps):
            if on_step is not None:
                on_step(index, total, step.name)
            started = time.perf_counter()
            ctx = step.run(ctx)
            elapsed = time.perf_counter() - started
            logger.info("step %-16s done in %6.1fs (%s)", step.name, elapsed, ctx.slug)
        logger.info("pipeline done: %s", ctx.slug)
        return ctx
