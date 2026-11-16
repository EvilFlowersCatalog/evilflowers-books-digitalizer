"""Stage formulas, the orchestrator's half: which regions to re-read, and judging the answers.

``evilflowers_equation.standalone.formulas`` (PaddleOCR-VL on the shared vlm) reads the
``reocr`` regions; every answer is judged here, whoever recognized it: LaTeX is normalised
(:func:`fix_latex`) and a "formula" that reads like a drawing is rejected
(:func:`looks_hallucinated`). A rejection is written into ``regions.reason`` with the
:data:`REJECTED` prefix, which is what the quality gate counts; a region left without text is
a fallback (it stays an image crop in the outputs). A row the service itself marks
``disputed`` (its render-and-compare or cross-check, equation-service#8) keeps its text and
becomes a disputed region with the service's reason.
"""

from __future__ import annotations

import re

from evilflowers_books_digitalizer.db.models import Page, Region, Run
from evilflowers_books_digitalizer.studio.domain import (
    PageStatus,
    RegionAction,
    RegionKind,
    RegionStatus,
)
from evilflowers_books_digitalizer.studio.workers import runs
from evilflowers_books_digitalizer.studio.workers.context import Uow

REJECTED = "vlm rejected"
FAILED = "vlm gave no answer: kept as image"
DISPUTED = "vlm disputed"
#: detector-owned statuses; confirmed regions keep the operator's text
OPEN = (RegionStatus.PROPOSED, RegionStatus.DISPUTED)


def job_key(region_id: int) -> str:
    return f"region-{region_id}"


def region_id(key: str) -> int:
    return int(key.removeprefix("region-"))


def to_read(page: Page) -> list[Region]:
    """The regions of a page the recognizer must (re-)read: ``reocr`` ones no person decided."""
    if not page.clean_path:
        return []
    return [r for r in page.regions if r.action == RegionAction.REOCR and r.status in OPEN]


def task_of(region: Region) -> str:
    """``formula`` (math only) or ``ocr`` (a table / a mixed math + prose line)."""
    return "formula" if region.kind == RegionKind.FORMULA else "ocr"


# ---------------------------------------------------------------- LaTeX hygiene
def strip_delims(tex: str) -> str:
    tex = tex.strip()
    for open_, close in (("\\[", "\\]"), ("$$", "$$"), ("\\(", "\\)"), ("$", "$")):
        if tex.startswith(open_) and tex.endswith(close) and len(tex) > len(open_) + len(close):
            return tex[len(open_):-len(close)].strip()
    return tex


def fix_latex(tex: str) -> str:
    tex = strip_delims(tex)
    tex = re.sub(r"\\eqno\s*\{?\(?([\w.]+)\)?\}?", r"\\tag{\1}", tex)   # texmath knows \tag
    for env in ("align*", "align"):
        tex = tex.replace(f"\\begin{{{env}}}", "\\begin{aligned}").replace(
            f"\\end{{{env}}}", "\\end{aligned}")
    return tex


def looks_hallucinated(tex: str) -> bool:
    """Formula-model output that describes a drawing rather than math."""
    if re.search(r"Obr\.|Obrázok|Tab\.", tex):
        return True
    arrays = re.findall(r"\\begin\{array\}\{[lcr]+\}(.*?)\\end\{array\}", tex, re.S)
    if len(arrays) >= 2:
        cells = [c.strip() for a in arrays for c in re.split(r"\\\\|&", a) if c.strip()]
        # a LaTeX command (\beta) is one symbol; lone labels are 1-2 symbols
        size = [len(re.sub(r"[\\{}_^ ]", "", re.sub(r"\\[a-zA-Z]+", "x", c))) for c in cells]
        if cells and sum(n <= 2 for n in size) / len(cells) > 0.7:
            return True
    return bool(re.search(r"\\xrightarrow|\\longrightarrow", tex)) and len(tex) < 40


def judge(answer: str | None) -> tuple[str | None, str | None]:
    """Recognizer answer -> ``(text, reason)``; a rejected answer keeps no text."""
    if not answer or not answer.strip():
        return None, FAILED
    tex = fix_latex(answer)
    if looks_hallucinated(tex):
        return None, f"{REJECTED}: looks like a drawing"
    return tex, None


def merge_reason(current: str | None, new: str | None) -> str | None:
    """Keep a dispute reason, replace any earlier recognizer verdict (idempotent re-runs)."""
    parts = [p for p in (current or "").split("; ") if p and not p.startswith("vlm ")]
    return "; ".join([*parts, new] if new else parts) or None


def apply_answers(uow: Uow, run: Run, keys: list[str], answers: dict[str, str],
                  disputed: dict[str, str] | None = None) -> None:
    """The recognizer's answers for ``keys`` into the regions.

    A missing answer is a failure (the region stays an image crop); every answer passes
    :func:`judge` here, so the gate counts rejections the same way whoever recognized it.
    ``disputed``: key -> the service's reason for doubting an answer it gave.
    """
    rejected = 0
    disputed = disputed or {}
    for key in keys:
        # row lock: an operator PATCH waits for this short commit instead of racing it
        region = uow.session.get(Region, region_id(key), with_for_update=True)
        if region is None or region.status not in OPEN:
            continue            # the operator decided it while the recognizer ran
        text, reason = judge(answers.get(key))
        rejected += bool(reason and reason.startswith(REJECTED))
        if text is not None:
            region.text = text
            if key in disputed:
                reason = f"{DISPUTED}: {disputed[key] or 'the service doubts its answer'}"
                region.status = RegionStatus.DISPUTED
                if region.page.status == PageStatus.ANALYZED:
                    region.page.status = PageStatus.DISPUTED
        elif region.kind == RegionKind.FORMULA:
            region.text = None    # an OCR engine's guess at math is noise
        region.reason = merge_reason(region.reason, reason)
    runs.log(uow, run, f"formulas: {len(keys)} regions, {len(answers)} answered, "
                       f"{rejected} rejected, {len(set(disputed) & set(keys))} disputed")
