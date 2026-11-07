"""``pages`` rows <-> :class:`Leaf` (IA scandata semantics).

The DB stores scandata fields as columns plus ``leaf_extra`` for the
operator-only ones (``book_start``, ``toc_title``, ``toc_level``; optionally the
original frame size). Reading lives in ``studio.metadata.leaf_from_row`` (the
application's API export uses it too); writing (import) lives here, next to it.
"""

from __future__ import annotations

from collections.abc import Iterable

from evilflowers_books_digitalizer.db.models import Page
from evilflowers_books_digitalizer.studio.metadata import Leaf, leaf_from_row

_EXTRA_KEYS = ("book_start", "toc_title", "toc_level", "orig_width", "orig_height")


def leaf_from_page(page: Page) -> Leaf:
    """Row -> leaf via :func:`studio.metadata.leaf_from_row`, shared with the application."""
    return leaf_from_row(page)


def leaves_from_pages(pages: Iterable[Page]) -> list[Leaf]:
    return [leaf_from_page(p) for p in sorted(pages, key=lambda p: p.index)]


def apply_leaf(page: Page, leaf: Leaf) -> Page:
    """Copy scandata fields onto a row (import); keeps processing state untouched."""
    page.index = leaf.leaf_num
    page.page_type = leaf.page_type.value
    page.hand_side = leaf.hand_side.value if leaf.hand_side else None
    page.add_to_access = leaf.add_to_access
    page.page_number = leaf.page_number
    page.crop_box = list(leaf.crop_box) if leaf.crop_box else None
    page.rotate_degree = leaf.rotate_degree
    page.skew_angle = leaf.skew_angle
    page.dpi = leaf.ppi
    extra = {k: v for k, v in dict(page.leaf_extra or {}).items() if k not in _EXTRA_KEYS}
    extra.update({k: v for k, v in {
        "book_start": leaf.book_start or None, "toc_title": leaf.toc_title,
        "toc_level": leaf.toc_level, "orig_width": leaf.orig_width,
        "orig_height": leaf.orig_height}.items() if v is not None})
    page.leaf_extra = extra
    return page
