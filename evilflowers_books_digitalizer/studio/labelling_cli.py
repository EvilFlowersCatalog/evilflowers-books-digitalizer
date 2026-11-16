"""Terminal labelling (``evilflowers-digitalizer studio label``): the SPA's workflow for an
operator in a shell. Same use cases (studio.application.labelling); labels record ``via="cli"``.

Figures and tables cannot be drawn in a terminal: the crop is written to a file whose path is
printed (``--open`` opens it in the default viewer). Input and output are injected, so the
loop is tested without a terminal.
"""

from __future__ import annotations

import textwrap
import webbrowser
from collections.abc import Callable
from pathlib import Path

from evilflowers_books_digitalizer.studio.application import labelling as app
from evilflowers_books_digitalizer.studio.application.errors import ApplicationError, BadRequest
from evilflowers_books_digitalizer.studio.application.services import Services
from evilflowers_books_digitalizer.studio.labelling import SKIP, TASKS, udc_top

type Ask = Callable[[str], str]
type Say = Callable[[str], None]

_WRAP = 100


class Quit(Exception):
    """The operator typed ``q``."""


def label_loop(svc: Services, task: str, labeller: str, *, ask: Ask = input, say: Say = print,
               crops: Path | None = None, open_crops: bool = False) -> int:
    """Serve items until none is left or the operator quits; returns how many were answered."""
    t = app.task_or_404(task)
    if t.companion_of:
        raise BadRequest(f"{task} is answered on the {t.companion_of} screen: use --task {t.companion_of}")
    companion = next((c for c in TASKS.values() if c.companion_of == task), None)
    done = 0
    say(f"{t.title}  (digits or a label key; s = skip, unsure; q = quit)")
    while (item := app.next_item(svc, task, labeller)) is not None:
        say("")
        say(_render(item))
        if item.image_url:
            say(f"crop: {_save_crop(svc, item, crops, open_crops)}")
        try:
            answers = [app.LabelIn(task=task, value=_choose(item, t.labels, ask, say))]
            if companion and answers[0].value != SKIP:
                quality = _choose(item, companion.labels, ask, say, optional=companion.title)
                if quality:
                    answers.append(app.LabelIn(task=companion.name, value=quality))
        except Quit:
            break
        app.submit(svc, item.id, app.LabelsIn(labels=answers), labeller, via="cli")
        done += 1
    else:
        say("Nothing left for you in this task.")
    say(f"{done} answered.")
    return done


def _choose(item: app.LabelItemOut, labels: tuple[str, ...], ask: Ask, say: Say,
            optional: str | None = None) -> str:
    if not labels:                                  # subject: UDC top level, then the category
        return _choose_category(item, ask, say)
    if optional:
        say(f"{optional} (optional, Enter = no answer)")
    say("  " + "  ".join(f"[{i + 1}] {k}" for i, k in enumerate(labels)) + "  [s] skip")
    while True:
        raw = ask("> ").strip()
        if raw == "q":
            raise Quit
        if optional and raw == "":
            return ""
        if raw == "s":
            return SKIP
        if raw.isdigit() and 1 <= int(raw) <= len(labels):
            return labels[int(raw) - 1]
        if raw in labels:
            return raw
        say(f"  not a choice: {raw!r}")


def _choose_category(item: app.LabelItemOut, ask: Ask, say: Say) -> str:
    options = [o for o in item.payload.get("options", []) if isinstance(o, dict)]
    groups: dict[str, list[dict[str, str]]] = {}
    for o in options:
        groups.setdefault(udc_top(o.get("term")), []).append(o)
    tops = sorted(groups)
    while True:
        say("  " + "  ".join(f"[{i + 1}] {g}…" for i, g in enumerate(tops)) + "  [s] skip")
        raw = ask("top level> ").strip()
        if raw == "q":
            raise Quit
        if raw == "s":
            return SKIP
        if not (raw.isdigit() and 1 <= int(raw) <= len(tops)):
            say(f"  not a choice: {raw!r}")
            continue
        cats = groups[tops[int(raw) - 1]]
        for i, o in enumerate(cats):
            say(f"  [{i + 1}] {o.get('term')}  {o.get('label')}")
        raw = ask("category (b = back)> ").strip()
        if raw == "q":
            raise Quit
        if raw.isdigit() and 1 <= int(raw) <= len(cats):
            return str(cats[int(raw) - 1]["id"])


def _render(item: app.LabelItemOut) -> str:
    p = item.payload
    head = f"#{item.id} · {item.language or '?'} · {item.key}"
    match item.kind:
        case "section_pair":
            parts = [head]
            for name in ("a", "b"):
                sec = p.get(name) or {}
                parts.append(f"── Section {name.upper()}: {sec.get('title')}  "
                             f"({sec.get('entry_title') or sec.get('entry_id')}, p. {sec.get('first_page')})")
                parts.append(_wrap(sec.get("text") or "(no text)"))
            return "\n".join(parts)
        case "entry":
            toc = "\n".join(f"    {line}" for line in p.get("table_of_contents") or [])
            return "\n".join(x for x in (head, f"── {p.get('title')}", _wrap(p.get("summary") or ""),
                                         toc) if x)
        case _:
            parts = [head, f"── {p.get('entry_title')} · page {p.get('page')} · {p.get('region')}"]
            if p.get("printed_caption"):
                parts.append(f"caption: {p['printed_caption']}")
            if p.get("markdown") is not None:
                parts.append(p.get("markdown") or "(no markdown)")
            if p.get("page_text"):
                parts.append(_wrap(p["page_text"]))
            return "\n".join(parts)


def _wrap(text: str) -> str:
    return "\n".join(textwrap.fill(par, _WRAP, initial_indent="    ", subsequent_indent="    ")
                     for par in text.split("\n\n"))


def _save_crop(svc: Services, item: app.LabelItemOut, crops: Path | None, open_it: bool) -> str:
    try:
        img = app.item_image(svc, item.id)
    except ApplicationError as exc:
        return f"(not available: {exc})"
    folder = crops or svc.config.cache_dir / "labelling"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{item.id}.{img.media_type.removeprefix('image/') or 'webp'}"
    path.write_bytes(img.content)
    if open_it:
        webbrowser.open(path.resolve().as_uri())
    return str(path)


def status_table(svc: Services, labeller: str | None) -> str:
    rows = [("task", "labelled", "target", "skipped", "double", "items", "mine")]
    for t in app.list_tasks(svc, labeller):
        rows.append((t.name, str(t.labelled), str(t.target), str(t.skipped), str(t.double),
                     str(t.items), str(t.mine)))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows)
