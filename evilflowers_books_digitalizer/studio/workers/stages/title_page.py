"""``analyze/title_page`` (``digitalizer.local.title_page``): metadata suggestions (#15).

Runs once after every analyze job of a run is applied (``TAIL_PARTS``): the first leaves'
and the last leaves' hOCR -> line candidates (:mod:`studio.title_page`) -> one ``choice``
question per field to ``decider`` (Laya, ``"model": "multilingual"``) -> the answers stored
in ``books.metadata_suggestions`` with their ``p``. The operator's metadata form shows them;
only the operator's confirmation writes ``item_metadata`` (never imported unconfirmed).

Skipped when the decider is off, when the run did not touch a title or colophon leaf, or once
an operator confirmed the book's suggestions. Fail soft: a failed call leaves that field
without a suggestion; the run never fails on it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from evilflowers_books_digitalizer.db.models import utcnow
from evilflowers_books_digitalizer.studio import title_page as tp
from evilflowers_books_digitalizer.studio.domain import RunKind
from evilflowers_books_digitalizer.studio.hocr import parse_pages
from evilflowers_books_digitalizer.studio.ports import PortError
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import WorkerContext

logger = logging.getLogger(__name__)


def _leaves(indices: list[int], first: int, last: int) -> tuple[list[int], set[int]]:
    ordered = sorted(indices)
    head = ordered[:first]
    tail = [i for i in ordered[-last:] if i not in head] if last else []
    return head + tail, set(head)


def prefill(ctx: WorkerContext, run_id: int) -> None:
    cfg = ctx.config.title_page
    decider = ctx.decider("text")
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        book = run.book
        wanted, first = _leaves([p.index for p in book.pages], cfg.first_leaves, cfg.last_leaves)
        touched = set(run.pages or []) if run.kind == RunKind.RERUN and run.pages else None
        reason = ("the decider is off ([decider] url)" if decider is None
                  else "an operator confirmed the metadata" if book.metadata_confirmed
                  else "no title or colophon leaf in this run"
                  if touched is not None and not touched & set(wanted) else None)
        if reason:
            runs.log(uow, run, f"title page: skipped, {reason}")
            return
        pages = {p.index: p for p in book.pages}
        hocr = {i: Path(str(pages[i].hocr_path)) for i in wanted if pages[i].hocr_path}
    assert decider is not None                    # checked above (reason)
    lines: list[tp.Candidate] = []
    for leaf in wanted:
        path = hocr.get(leaf)
        if path is None or not path.is_file():
            continue
        for page in parse_pages(path.read_bytes())[:1]:
            lines += tp.lines_of(leaf, page, first_order=len(lines))
    suggestions: dict[str, object] = {}
    model, failed = None, []
    state = tp.state(lines)
    for field in tp.FIELDS if lines else ():
        candidates = tp.rank(field, lines, first, cfg.max_candidates)
        if not candidates:
            continue
        try:
            decision = decider.choose(f"title_page_{field}", tp.INSTRUCTIONS[field],
                                      tp.criteria(candidates), state={"body": state})
        except PortError as exc:              # down or slow: do not wait once per field
            logger.warning("decider: title page %s of run %s: %s", field, run_id, exc)
            failed = [f for f in tp.FIELDS if f not in suggestions]
            break
        model = decision.model
        chosen = next((c for c in candidates if c.id == decision.choice), None)
        value = tp.value(field, chosen.text) if chosen else None
        suggestions[field] = {"value": value, "p": round(decision.p, 3),
                              "line": chosen.text if chosen else None,
                              "leaf": chosen.leaf if chosen else None}
    with ctx.uow() as uow:
        run = runs.load_run(uow, run_id)
        if suggestions:
            run.book.metadata_suggestions = {**suggestions, "decider_model": model,
                                             "at": utcnow().isoformat(timespec="seconds")}
        filled = sorted(k for k, v in suggestions.items()
                        if isinstance(v, dict) and v.get("value"))
        runs.log(uow, run, f"title page: {len(lines)} line(s) on {len(hocr)} leaf(s); "
                           f"suggested {', '.join(filled) or 'nothing'}"
                           + (f"; decider failed for {', '.join(failed)}" if failed else ""))
