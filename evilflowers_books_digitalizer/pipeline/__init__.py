"""Reusable digitalization pipeline.

A :class:`Pipeline` is an ordered list of :class:`PipelineStep` instances that
each transform a :class:`BookContext`. Steps are composable, so the same
framework will later carry embedding and graph-classification stages.
"""

from evilflowers_books_digitalizer.pipeline.base import BookContext, Pipeline, PipelineStep

__all__ = ["BookContext", "Pipeline", "PipelineStep"]
