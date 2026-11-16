"""Title-page suggestions for the operator's metadata form (#15): read and confirm.

The orchestrator stores what the decider picked from the title page's OCR lines in
``books.metadata_suggestions`` (``stages.title_page``). The SPA prefills the form with them
and their ``p``; nothing reaches ``item_metadata`` until the operator confirms. Confirming
writes the operator's final values through the same validation as any metadata edit
(``item_metadata.apply_patch``) and keeps the comparison in ``books.metadata_confirmed``:
per suggested field the outcome is

* ``accepted``: the final value is the suggestion, or only reformats the chosen line (every word
  of it is on that line: "Novák, Ján" from "Ján Novák, Peter Kováč");
* ``changed``: the operator typed another value (the chosen line, or "none", was wrong);
* ``none``: the field is left empty (the page does not say it).

Each one is a labelled ``title_page`` case for labelling mode's export (#7).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict

from evilflowers_books_digitalizer.studio.application.books import item_metadata_of
from evilflowers_books_digitalizer.studio.application.errors import BadRequest
from evilflowers_books_digitalizer.studio.application.item_metadata import apply_patch
from evilflowers_books_digitalizer.studio.application.lookup import get_book as load_book
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.application.uow import utcnow
from evilflowers_books_digitalizer.studio.title_page import FIELDS, METADATA_FIELD


class SuggestionOut(BaseModel):
    field: str                     # title | authors | publisher | year
    metadata_field: str            # the ItemMetadata field it fills (title, creator, …)
    value: str | list[str] | None
    p: float | None
    line: str | None
    leaf: int | None


class TitlePageOut(BaseModel):
    suggestions: list[SuggestionOut]
    decider_model: str | None
    suggested_at: str | None
    confirmed: dict[str, Any] | None


class ConfirmIn(BaseModel):
    """The operator's final values (empty = the page does not say it)."""

    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    authors: list[str] | None = None
    publisher: str | None = None
    year: str | None = None


def _norm(value: object) -> str:
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value)
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.replace(",", " ").split())


def _words(value: object) -> set[str]:
    return set(re.findall(r"\w+", _norm(value)))


def _outcome(suggested: object, line: object, final: object) -> str:
    """Whether the decider picked the right line: the final value is the suggestion, or every
    word of it comes from the chosen line (the operator only reformatted it to IA's form,
    "Surname, Given" or "Place : Publisher")."""
    if not _norm(final):
        return "none"
    if _norm(suggested) == _norm(final):
        return "accepted"
    words = _words(final)
    return "accepted" if words and words <= _words(line) else "changed"


def get(svc: Services, book_id: int) -> TitlePageOut:
    with svc.uow() as uow:
        book = load_book(uow.session, book_id)
        raw = dict(book.metadata_suggestions or {})
        confirmed = dict(book.metadata_confirmed) if book.metadata_confirmed else None
    rows = [SuggestionOut(field=f, metadata_field=METADATA_FIELD[f], value=s.get("value"),
                          p=s.get("p"), line=s.get("line"), leaf=s.get("leaf"))
            for f in FIELDS if isinstance(s := raw.get(f), dict)]
    return TitlePageOut(suggestions=rows, decider_model=raw.get("decider_model"),
                        suggested_at=raw.get("at"), confirmed=confirmed)


def confirm(svc: Services, book_id: int, body: ConfirmIn, who: str | None) -> TitlePageOut:
    """Store the operator's values as metadata and the comparison as labelled cases."""
    with svc.uow() as uow:
        book = load_book(uow.session, book_id)
        raw = dict(book.metadata_suggestions or {})
        if not any(isinstance(raw.get(f), dict) for f in FIELDS):
            raise BadRequest(f"book {book_id} has no title-page suggestions to confirm")
        final: dict[str, Any] = body.model_dump()
        patch: dict[str, Any] = {}
        for field in FIELDS:
            value = final.get(field)
            if value:
                patch[METADATA_FIELD[field]] = value
        meta = apply_patch(item_metadata_of(book).as_dict(), patch)
        book.item_metadata = meta.as_dict()
        book.title = meta.title or book.title
        cases = {}
        for field in FIELDS:
            s = raw.get(field)
            if not isinstance(s, dict):
                continue
            cases[field] = {"suggested": s.get("value"), "line": s.get("line"),
                            "leaf": s.get("leaf"), "p": s.get("p"), "final": final.get(field),
                            "outcome": _outcome(s.get("value"), s.get("line"), final.get(field))}
        book.metadata_confirmed = {"fields": cases, "labeller": who,
                                   "at": utcnow().isoformat(timespec="seconds") + "Z",
                                   "decider_model": raw.get("decider_model")}
    return get(svc, book_id)
