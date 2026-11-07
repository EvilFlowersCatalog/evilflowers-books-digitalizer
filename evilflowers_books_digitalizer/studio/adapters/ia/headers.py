"""IA-S3 upload headers equivalent to ``ia upload --metadata …`` (internetarchive CLI).

The rules mirror the ``internetarchive`` library (``iarequest.prepare_headers``):

* one header per value: ``x-archive-meta-<field>`` for a single value,
  ``x-archive-meta01-<field>``, ``x-archive-meta02-<field>`` … for repeats;
* values that are not plain ASCII or contain whitespace are sent as
  ``uri(<percent-encoded UTF-8>)`` — raw UTF-8 is not allowed in HTTP headers;
* ``_`` in a header name becomes ``--``: RFC 822 header names may not carry
  underscores and IA-S3 turns ``--`` back into ``_`` (our ``evilflowers_*``
  custom fields depend on this);
* ``identifier`` is the bucket name, never a header; ``mediatype`` is always
  ``texts`` for books.

``x-archive-queue-derive: 0`` stops IA from re-deriving when we upload our own
derivatives (PDF, hOCR, EPUB), which would otherwise be overwritten.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

from evilflowers_books_digitalizer.studio.metadata import ItemMetadata

_WHITESPACE = re.compile(r"\s")


def needs_quote(value: str) -> bool:
    return not value.isascii() or _WHITESPACE.search(value) is not None


def encode_value(value: str) -> str:
    return f"uri({quote(value)})" if needs_quote(value) else value


def meta_headers(meta: ItemMetadata) -> dict[str, str]:
    """``x-archive-meta*`` headers for every non-empty metadata field."""
    fields = meta.to_ia()
    fields.pop("identifier", None)
    fields["mediatype"] = "texts"
    headers: dict[str, str] = {}
    for key, value in fields.items():
        values = [v for v in (value if isinstance(value, list) else [value]) if v]
        name = key.replace("_", "--")
        if len(values) == 1:
            headers[f"x-archive-meta-{name}"] = encode_value(values[0])
            continue
        for i, v in enumerate(values, start=1):
            headers[f"x-archive-meta{i:02d}-{name}"] = encode_value(v)
    return headers


def upload_headers(meta: ItemMetadata, *, queue_derive: bool = False,
                   auto_make_bucket: bool = True, size_hint: int | None = None) -> dict[str, str]:
    """Headers for the first PUT of an item (metadata + bucket creation + derive flag)."""
    headers = meta_headers(meta)
    if auto_make_bucket:
        headers["x-archive-auto-make-bucket"] = "1"
    if not queue_derive:
        headers["x-archive-queue-derive"] = "0"
    if size_hint is not None:
        headers["x-archive-size-hint"] = str(size_hint)
    return headers


def decode_value(value: str) -> str:
    """Inverse of :func:`encode_value` (tests, debugging)."""
    return unquote(value[4:-1]) if value.startswith("uri(") and value.endswith(")") else value
