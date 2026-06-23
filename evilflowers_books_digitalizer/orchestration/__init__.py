"""Prefect orchestration for the digitalization corpus."""

from evilflowers_books_digitalizer.orchestration.flows import (
    digitize_book,
    digitize_corpus,
    digitize_source,
)

__all__ = ["digitize_book", "digitize_corpus", "digitize_source"]
