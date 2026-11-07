"""Operator edits of IA item metadata (``PATCH /books/{id}/metadata``).

``ItemMetadata`` is a plain dataclass (frozen contract), so the IA format rules
an operator can break by typing are enforced here: dates, scandate, MARC
language codes, page progression, the permanent identifier. Everything that
reaches ``books.item_metadata`` has passed :func:`apply_patch`.
"""

from __future__ import annotations

import re
from dataclasses import fields
from typing import Any

from pydantic import TypeAdapter, ValidationError

from evilflowers_books_digitalizer.studio.application.errors import Conflict, Invalid
from evilflowers_books_digitalizer.studio.metadata import (
    CUSTOM_PREFIX,
    REPEATABLE,
    ItemMetadata,
    marc_language,
)

_FIELDS = frozenset(f.name for f in fields(ItemMetadata))
#: set at creation, never changed (IA rule; every artifact is named after the identifier)
_IMMUTABLE = ("identifier", "mediatype")
_FORMATS: dict[str, re.Pattern[str]] = {
    "date": re.compile(r"\d{4}(-\d{2}(-\d{2})?)?|\[\d{4}\]|\[n\.d\.\]"),
    "scandate": re.compile(r"\d{14}"),
    "sponsordate": re.compile(r"\d{8}"),
    "page_progression": re.compile(r"lr|rl"),
}
_MARC = re.compile(r"[a-z]{3}")
_ADAPTER = TypeAdapter(ItemMetadata)


def apply_patch(current: dict[str, Any], patch: dict[str, Any]) -> ItemMetadata:
    """Merge a partial update into the stored metadata and validate the result."""
    unknown = sorted(set(patch) - _FIELDS)
    if unknown:
        raise Invalid(f"unknown metadata field(s): {', '.join(unknown)}")
    _check_immutable(current, patch)
    merged = {**current, **{k: _as_list(k, v) for k, v in patch.items()}}
    try:
        meta = _ADAPTER.validate_python(merged)
    except ValidationError as exc:
        raise Invalid(_first_error(exc)) from None
    meta.language = [marc_language(code) for code in meta.language]
    _check_formats(meta)
    _check_extra(current.get("extra") or {}, meta.extra)
    return meta


def _as_list(name: str, value: Any) -> Any:
    """Repeatable IA fields accept a single string from a form field."""
    return [value] if name in REPEATABLE and isinstance(value, str) else value


def _check_immutable(current: dict[str, Any], patch: dict[str, Any]) -> None:
    for name in _IMMUTABLE:
        if name in patch and current.get(name) and patch[name] != current[name]:
            raise Conflict(f"{name} is permanent and cannot be changed")


def _check_formats(meta: ItemMetadata) -> None:
    for name, pattern in _FORMATS.items():
        value = getattr(meta, name)
        if value and not pattern.fullmatch(value):
            raise Invalid(f"{name} {value!r} does not match the IA format")
    bad = [code for code in meta.language if not _MARC.fullmatch(code)]
    if bad:
        raise Invalid(f"language must be MARC codes (e.g. slo, cze, eng), got {bad}")


def _check_extra(before: dict[str, Any], after: dict[str, list[str]]) -> None:
    """New custom fields must carry the evilflowers_ prefix; imported IA ones are kept verbatim."""
    new = [k for k in after if k not in before and not k.startswith(CUSTOM_PREFIX)]
    if new:
        raise Invalid(f"custom metadata fields must start with {CUSTOM_PREFIX!r}: {new}")


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()))
    return f"{where}: {err.get('msg')}" if where else str(err.get("msg"))
