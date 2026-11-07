"""The pdf layer maps leaves the way the PDF on disk was rendered, not by today's flags."""

from __future__ import annotations

from evilflowers_books_digitalizer.db.models import Book, Page
from evilflowers_books_digitalizer.studio.application import pages
from evilflowers_books_digitalizer.studio.application.pages import PDF_PAGES_STAT


def _ids(services, book_id) -> list[int]:
    s = services.sessions()
    try:
        return [p.id for p in s.query(Page).filter_by(book_pk=book_id).order_by(Page.index)]
    finally:
        s.close()


def test_pdf_layer_uses_the_rendered_map(services, seeded):
    first, second = _ids(services, seeded.book_id)
    s = services.sessions()
    s.get(Book, seeded.book_id).stats = {PDF_PAGES_STAT: {"0": 0, "1": 1}}
    s.get(Page, first).add_to_access = False                  # excluded after the render
    s.commit()
    s.close()
    assert pages.page_media(services, second).access_index == 1  # still the PDF's page 2
    assert pages.page_media(services, first).access_index == 0


def test_pdf_layer_falls_back_to_the_flags_without_a_map(services, seeded):
    first, second = _ids(services, seeded.book_id)
    s = services.sessions()
    s.get(Page, first).add_to_access = False
    s.commit()
    s.close()
    assert pages.page_media(services, second).access_index == 0
    assert pages.page_media(services, first).access_index is None
