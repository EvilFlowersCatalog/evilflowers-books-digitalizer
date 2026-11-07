"""Fetch-or-NotFound helpers shared by every use case (one message format, one place)."""

from __future__ import annotations

from evilflowers_books_digitalizer.studio.application.errors import NotFound
from evilflowers_books_digitalizer.studio.application.uow import (
    Book,
    Page,
    Region,
    Run,
    Session,
    select,
)


def get_book(session: Session, book_id: int) -> Book:
    book = session.get(Book, book_id)
    if book is None:
        raise NotFound(f"book {book_id} not found")
    return book


def find_book(session: Session, ref: str) -> Book:
    """A book by database id or by IA item identifier (CLI convenience)."""
    book = session.get(Book, int(ref)) if ref.isdigit() else None
    book = book or session.scalar(select(Book).where(Book.item_id == ref.lower()))
    if book is None:
        raise NotFound(f"book {ref!r} not found")
    return book


def get_page(session: Session, page_id: int) -> Page:
    page = session.get(Page, page_id)
    if page is None:
        raise NotFound(f"page {page_id} not found")
    return page


def get_region(session: Session, region_id: int) -> Region:
    region = session.get(Region, region_id)
    if region is None:
        raise NotFound(f"region {region_id} not found")
    return region


def get_run(session: Session, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    return run
