"""Classification step.

For now each book gets a simple category list from a pluggable
:class:`Classifier`. The same interface will later back the graph-database
classification, and the OCR sidecar text it consumes is also the input for
vector-database embeddings.
"""

from __future__ import annotations

from typing import Protocol

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep


class Classifier(Protocol):
    """Anything that maps book text to a list of category labels."""

    def classify(self, text: str) -> list[str]: ...


class KeywordClassifier:
    """Naive baseline: a category matches if any of its keywords occurs.

    Good enough to exercise the pipeline end to end; replace with an
    embedding- or LLM-based classifier later.
    """

    def __init__(self, categories: dict[str, list[str]]):
        self.categories = {
            label: [keyword.lower() for keyword in keywords]
            for label, keywords in categories.items()
        }

    def classify(self, text: str) -> list[str]:
        lowered = text.lower()
        return [
            label
            for label, keywords in self.categories.items()
            if any(keyword in lowered for keyword in keywords)
        ]


class ClassifyBook(PipelineStep):
    """OCR text (``artifacts['text']``) -> ``metadata['categories']``."""

    name = "classify"

    def __init__(self, classifier: Classifier):
        self.classifier = classifier

    def run(self, ctx: BookContext) -> BookContext:
        text_path = ctx.artifacts.get("text")
        if text_path is None or not text_path.exists():
            raise ValueError(f"no OCR text for {ctx.slug} — run the OCR step first")
        ctx.metadata["categories"] = self.classifier.classify(
            text_path.read_text(errors="ignore")
        )
        return ctx
