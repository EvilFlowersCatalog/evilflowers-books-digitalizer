"""Process-wide logging: text or JSON lines on stderr, with per-book context.

Configured from the environment so a k8s Job needs no flags:

* ``EVILFLOWERS_LOG_FORMAT`` — ``text`` (default) or ``json``;
* ``EVILFLOWERS_LOG_LEVEL`` — ``DEBUG`` … ``CRITICAL`` (default ``INFO``; ``WARN`` accepted).

A JSON line carries ``time`` (RFC 3339, UTC), ``level`` (``DEBUG|INFO|WARN|ERROR|
CRITICAL``), ``logger`` and ``msg``, plus whichever of :data:`CONTEXT_FIELDS` and
:data:`EXTRA_FIELDS` are known, and ``exc_type``/``exc`` for exceptions.

Context fields come from :func:`log_context`, a :mod:`contextvars` scope: the
batch worker enters ``log_context(source=…, book_id=…, slug=…)`` and the
pipeline runner ``log_context(step=…)``, so every line logged underneath carries
them without threading arguments through call sites. Per-line fields go in
``extra=`` (``logger.info("…", extra={"status": "published", "entry_id": …})``).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

FORMAT_ENV = "EVILFLOWERS_LOG_FORMAT"
LEVEL_ENV = "EVILFLOWERS_LOG_LEVEL"
FORMATS = ("text", "json")

#: Fields set through :func:`log_context`.
CONTEXT_FIELDS = ("source", "book_id", "slug", "step")
#: Per-line fields passed through ``extra=``.
EXTRA_FIELDS = ("duration_ms", "status", "entry_id")

_LEVEL_ALIASES = {"WARN": "WARNING"}
_LEVEL_LABELS = {"WARNING": "WARN"}
_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s%(context)s: %(message)s"

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "evilflowers_log_context"
)
_HANDLER_MARK = "_evilflowers_handler"
_configured_format: str | None = None


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add ``fields`` to every log line emitted inside the ``with`` block."""
    merged = {**current_context(), **{k: v for k, v in fields.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict[str, Any]:
    """A copy of the active :func:`log_context` fields."""
    return dict(_context.get({}))


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    """Context + extra fields for one record (an explicit ``extra=`` wins)."""
    fields = current_context()
    for key in CONTEXT_FIELDS + EXTRA_FIELDS:
        value = getattr(record, key, None)
        if value is not None:
            fields[key] = value
    return {k: fields[k] for k in CONTEXT_FIELDS + EXTRA_FIELDS if fields.get(k) is not None}


class JsonFormatter(logging.Formatter):
    """One JSON object per record (see the module docstring for the shape)."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, Any] = {
            "time": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": _LEVEL_LABELS.get(record.levelname, record.levelname),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_fields(record))
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable lines; context shows as ``[svf/<book_id> step=ocr]``."""

    def __init__(self) -> None:
        super().__init__(_TEXT_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        fields = _fields(record)
        parts: list[str] = []
        if "source" in fields or "book_id" in fields:
            parts.append("/".join(str(fields[k]) for k in ("source", "book_id") if k in fields))
        parts += [f"{k}={fields[k]}" for k in ("step", "status", "entry_id") if k in fields]
        record.context = f" [{' '.join(parts)}]" if parts else ""
        return super().format(record)


def _level(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    name = (value or "INFO").strip().upper()
    name = _LEVEL_ALIASES.get(name, name)
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        raise ValueError(f"unknown log level {value!r}")
    return level


def _format(value: str | None) -> str:
    name = (value or "text").strip().lower()
    if name not in FORMATS:
        raise ValueError(f"{FORMAT_ENV} must be one of {FORMATS}, got {value!r}")
    return name


def make_formatter(fmt: str | None = None) -> logging.Formatter:
    """The formatter for ``fmt`` (default: the configured or environment format)."""
    name = _format(fmt or _configured_format or os.environ.get(FORMAT_ENV))
    return JsonFormatter() if name == "json" else TextFormatter()


def is_json() -> bool:
    """True when stderr carries JSON lines (so nothing else may write there)."""
    return _format(_configured_format or os.environ.get(FORMAT_ENV)) == "json"


def setup_logging(
    fmt: str | None = None,
    level: str | int | None = None,
    *,
    stream: TextIO | None = None,
    force: bool = False,
) -> None:
    """Install the stderr handler on the root logger.

    Arguments default to ``EVILFLOWERS_LOG_FORMAT`` / ``EVILFLOWERS_LOG_LEVEL``.
    Idempotent: a second call is a no-op unless ``force`` (so a worker process
    can call it unconditionally without dropping a file handler the parent
    added). Raises :class:`ValueError` on an unknown format or level.
    """
    global _configured_format
    if _configured_format is not None and not force:
        return
    name = _format(fmt or os.environ.get(FORMAT_ENV))
    numeric = _level(level if level is not None else os.environ.get(LEVEL_ENV))

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if name == "json" else TextFormatter())
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(numeric)
    _configured_format = name
    logging.captureWarnings(True)


def add_file_handler(path: Path) -> logging.Handler:
    """Also write the log to ``path`` (same format as stderr). Returns the handler."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(make_formatter())
    logging.getLogger().addHandler(handler)
    return handler


def reset_logging() -> None:
    """Forget the configuration (tests only)."""
    global _configured_format
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
    _configured_format = None
