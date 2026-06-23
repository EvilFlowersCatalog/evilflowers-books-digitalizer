"""Back-compat shim — the WebDAV backend moved to :mod:`.sources.webdav`.

Existing imports (``from evilflowers_books_digitalizer.webdav import BookSource``)
and the notebooks keep working. New code should import from
:mod:`evilflowers_books_digitalizer.sources`.
"""

from __future__ import annotations

from evilflowers_books_digitalizer.sources.base import PAGES_SUBDIR
from evilflowers_books_digitalizer.sources.webdav import BookSource

__all__ = ["PAGES_SUBDIR", "BookSource"]
