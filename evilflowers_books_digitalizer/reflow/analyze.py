"""Page image + hOCR -> semantic blocks (the reflow document model).

Routing principle: Tesseract `slk` is trusted for prose; the vision model is
consulted only where Tesseract demonstrably cannot read (math). Classification
is line-centric — every OCR line is ``text | math | inline | row | eqno`` — and
runs of same-class lines become blocks.

Guards learned in the experiment (``docs/reflow_edition.md``):

* a "formula" region holding a large sparse stroke is a drawing -> figure;
* saturated blue/violet ink is a library stamp -> erased, never content;
* a facing-page sliver beyond an empty gutter is clipped;
* running heads are detected per *book* (text repeated in the top band);
* a page that is mostly short low-confidence fragments (forms, worksheets)
  becomes one whole-page image instead of noise.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

import cv2
import numpy as np

from evilflowers_books_digitalizer.reflow.anchor import WORDLIKE
from evilflowers_books_digitalizer.reflow.model import Box, Line, Page, Par, Word, union

type Kind = Literal["heading", "para", "list", "olist", "table", "formula", "figure",
                    "caption", "eqno", "page"]

EQNO = re.compile(r"^\(\d+(\.\d+)+\)$")
HEADNUM = re.compile(r"^(\d+(\.\d+)*)\.?\s+[A-ZÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ]")
LISTITEM = re.compile(r"^([a-z]\)|\d{1,2}[.)]|[•·▪–])(\s+|$)")
CAPTION = re.compile(r"^(Obr\.|Tab\.|Tabuľka|Obrázok)\s*\d", re.I)
SENT_END = re.compile(r"[.!?:;]\**\s*$")
LOWER_START = re.compile(r"^[a-záäčďéíĺľňóôŕšťúýž]")
MATH_CHARS = "=+−×·/|^_∂∇∫∑√<>()"


@dataclass(slots=True)
class Segment:
    """One line of a paragraph: Tesseract text, or a line to re-read with the VLM."""

    kind: Literal["text", "inline"]
    md: str
    bbox: Box
    anchors: list[str]


@dataclass(slots=True)
class Block:
    kind: Kind
    page: int
    bbox: Box
    text: str = ""
    level: int = 0
    tag: str = ""                                          # equation number
    rows: list[list[str]] = field(default_factory=list)    # table
    items: list[str] = field(default_factory=list)         # list / olist
    segments: list[Segment] = field(default_factory=list)  # para lines
    anchors: list[str] = field(default_factory=list)       # formula: words inside it
    ink: list[Box] = field(default_factory=list)           # ink explained by this block

    @property
    def mixed(self) -> bool:
        """A formula region that also holds real words -> VLM ``OCR:`` task."""
        return bool(self.anchors)


@dataclass(slots=True)
class PageStats:
    body_size: float
    left: int
    right: int


# ---------------------------------------------------------------- helpers
def conf_of(words: list[Word]) -> float:
    return sum(w.conf for w in words) / max(1, len(words))


def wordlike_ratio(words: list[Word]) -> float:
    return sum(bool(WORDLIKE.match(w.text)) and w.conf >= 75 for w in words) / max(1, len(words))


def join_lines(lines: list[str]) -> str:
    """Join OCR lines into one paragraph, undoing end-of-line hyphenation."""
    text = ""
    for ln in (x.strip() for x in lines):
        if not text:
            text = ln
        elif re.search(r"\w-$", text) and LOWER_START.match(ln):
            text = text[:-1] + ln
        else:
            text += " " + ln
    return text


def page_stats(page: Page) -> PageStats:
    good = [ln for p in page.pars for ln in p.lines if ln.conf >= 85 and len(ln.words) >= 4]
    sizes = [ln.size for ln in good] or [30.0]
    lefts = [ln.bbox[0] for ln in good] or [0]
    rights = [ln.bbox[2] for ln in good] or [page.width]
    return PageStats(statistics.median(sizes), int(np.percentile(lefts, 10)),
                     int(np.percentile(rights, 90)))


def _norm_head(text: str) -> str:
    return re.sub(r"[\d\W_]+", " ", text).strip().lower()


# ---------------------------------------------------------------- book level
def running_heads(pages: list[Page], band: float = 0.16) -> set[str]:
    """Short lines in the top band that repeat across the book (author/chapter heads).

    Per-book rather than per-page: ScanTailor margins move the head out of any
    fixed band, but repetition gives it away.
    """
    counts: Counter[str] = Counter()
    for page in pages:
        seen = set()
        for par in page.pars:
            if par.bbox[3] < page.height * band and len(par.words) <= 6 and len(par.lines) == 1:
                key = _norm_head(par.text)
                if len(key) >= 3:
                    seen.add(key)
        counts.update(seen)
    floor = max(3, int(0.2 * len(pages)))
    return {text for text, n in counts.items() if n >= floor}


def is_form_page(page: Page) -> bool:
    """Worksheets/forms/tables-of-boxes: reflowing them yields noise, keep the image."""
    words = page.words
    if len(words) < 25:
        return False
    fragments = sum(len(p.words) <= 3 for p in page.pars) / max(1, len(page.pars))
    return wordlike_ratio(words) < 0.45 and fragments > 0.45 and len(page.pars) >= 10


# ---------------------------------------------------------------- page level
def clip_to_main_column(page: Page) -> Page:
    """Drop words of a facing-page sliver beyond an empty gutter in the outer 20%."""
    proj = np.zeros(page.width + 1, dtype=np.int32)
    for w in page.words:
        proj[w.bbox[0]:w.bbox[2]] += 1
    gutter = max(4, int(page.width * 0.015))
    empty = proj == 0

    def runs(a: int, b: int) -> list[tuple[int, int]]:
        out, start = [], None
        for x in range(a, b):
            if empty[x] and start is None:
                start = x
            elif not empty[x] and start is not None:
                out.append((start, x))
                start = None
        if start is not None:
            out.append((start, b))
        return [r for r in out if r[1] - r[0] >= gutter]

    left = [r for r in runs(0, int(page.width * 0.2)) if r[0] > 0]
    right = [r for r in runs(int(page.width * 0.8), page.width) if r[1] < page.width]
    lo = left[-1][1] - 1 if left else 0
    hi = right[0][0] + 1 if right else page.width
    if lo == 0 and hi == page.width:
        return page
    pars = []
    for par in page.pars:
        lines = []
        for ln in par.lines:
            ws = [w for w in ln.words if w.bbox[0] >= lo and w.bbox[2] <= hi]
            if ws:
                lines.append(Line(ws, union([w.bbox for w in ws]), ln.size))
        if lines:
            pars.append(Par(lines, union([ln.bbox for ln in lines])))
    return Page(page.width, page.height, pars)


def _is_furniture(par: Par, page: Page, heads: set[str]) -> bool:
    """Page numbers, running heads, rules, scanner noise."""
    x0, y0, x1, y1 = par.bbox
    text = par.text.strip()
    top, bottom = y1 < page.height * 0.12, y0 > page.height * 0.86
    if (top or bottom) and re.fullmatch(r"[\divxlcIVXLC\s.\-–]+", text):
        return True
    if len(par.lines) == 1 and _norm_head(text) in heads:
        return True
    if par.conf < 30 and (x1 - x0) < page.width * 0.06:
        return True  # vertical garbage strip (binding shadow, list-number column)
    return not any(c.isalnum() for c in text)


def _cells(ln: Line, gap: float) -> list[list[Word]]:
    cells, cur = [], [ln.words[0]]
    for a, b in zip(ln.words, ln.words[1:], strict=False):
        if b.bbox[0] - a.bbox[2] > gap:
            cells.append(cur)
            cur = []
        cur.append(b)
    cells.append(cur)
    return cells


def _line_class(ln: Line, stats: PageStats) -> str:
    text = ln.text.strip()
    conf = ln.conf
    if EQNO.match(text):
        return "eqno"
    wl = wordlike_ratio(ln.words)
    if conf < 50 or (conf < 85 and wl < 0.25):
        return "math"            # 2-D / display math: Tesseract text is garbage
    if wl == 0 and all(len(w.text) <= 4 for w in ln.words):
        return "math"            # fraction denominators, integral limits: "dt dt dt"
    if conf < 88 and sum(c in MATH_CHARS for c in text) >= 2 and wl < 0.75:
        return "inline"          # prose with inline math
    if any(c in text for c in "@©®"):
        return "inline"          # Tesseract's signature for a misread Greek letter
    cells = _cells(ln, stats.body_size * 1.6)
    if len(cells) >= 2 and all(len(" ".join(w.text for w in c)) <= 6 for c in cells[1:]) \
            and wordlike_ratio(cells[0]) >= 0.5 and len(cells[0]) >= 2:
        return "row"
    return "text"


def _words_md(words: list[Word]) -> str:
    return " ".join(w.text for w in words)


def _snap_rows(lines: list[Line], stats: PageStats) -> list[list[str]]:
    """Table rows from word geometry: value columns anchored on short right-hand cells."""
    gap = stats.body_size * 1.6
    centers = sorted((c[0].bbox[0] + c[-1].bbox[2]) / 2
                     for ln in lines for c in _cells(ln, gap)[1:])
    anchors: list[float] = []
    for x in centers:
        if not anchors or x - anchors[-1] >= stats.body_size * 2:
            anchors.append(x)
    tol = stats.body_size * 1.5
    rows = []
    for ln in lines:
        row = [""] * (len(anchors) + 1)
        label: list[Word] = []
        for w in ln.words:
            cx = (w.bbox[0] + w.bbox[2]) / 2
            k = min(range(len(anchors)), key=lambda i: abs(anchors[i] - cx)) if anchors else -1
            if k >= 0 and abs(anchors[k] - cx) < tol and len(w.text) <= 6 and \
                    (w.text.isdigit() or not label or w.bbox[0] - label[-1].bbox[2] > gap * 0.6):
                row[k + 1] = (row[k + 1] + " " + w.text).strip()
            else:
                label.append(w)
        row[0] = _words_md(label)
        rows.append(row)
    return rows


def _segment(ln: Line, stats: PageStats) -> Segment:
    inline = _line_class(ln, stats) == "inline"
    anchors = [w.text for w in ln.words if w.conf >= 80 and WORDLIKE.match(w.text)]
    return Segment("inline" if inline else "text", _words_md(ln.words), ln.bbox, anchors)


def _para(lines: list[Line], page_no: int, stats: PageStats) -> Block:
    return Block("para", page_no, union([ln.bbox for ln in lines]),
                 text=join_lines([_words_md(ln.words) for ln in lines]),
                 segments=[_segment(ln, stats) for ln in lines],
                 ink=[w.bbox for ln in lines for w in ln.words])


def _text_blocks(lines: list[Line], page_no: int, stats: PageStats) -> list[Block]:
    words = [w for ln in lines for w in ln.words]
    bbox = union([ln.bbox for ln in lines])
    ink = [w.bbox for w in words]
    first = lines[0].text.strip()
    size = statistics.median(ln.size for ln in lines)
    width = bbox[2] - bbox[0]
    if CAPTION.match(first) and len(lines) <= 3:
        return [Block("caption", page_no, bbox, text=join_lines([ln.text for ln in lines]), ink=ink)]
    if len(lines) <= 2 and len(words) <= 12 and not SENT_END.search(lines[-1].text):
        if m := HEADNUM.match(first):
            return [Block("heading", page_no, bbox, text=join_lines([ln.text for ln in lines]),
                          level=min(4, 1 + m[1].count(".")), ink=ink)]
        if size >= stats.body_size * 1.18 or (
                len(lines) == 1 and width < 0.5 * (stats.right - stats.left) and first[:1].isupper()
                and 2 <= len(words) <= 5 and wordlike_ratio(words) >= 0.8):
            return [Block("heading", page_no, bbox, text=join_lines([ln.text for ln in lines]),
                          level=2 if size >= stats.body_size * 1.4 else 4, ink=ink)]
    if LISTITEM.match(first):
        items: list[list[str]] = []
        for ln in lines:
            if LISTITEM.match(ln.text.strip()) or not items:
                items.append([])
            items[-1].append(ln.text)
        return [Block("list", page_no, bbox, items=[join_lines(it) for it in items], ink=ink)]
    out: list[Block] = []
    cur: list[Line] = []
    for ln in lines:  # split at an indented first line inside one OCR block
        if cur and ln.bbox[0] - stats.left > stats.body_size * 1.2 and SENT_END.search(cur[-1].text):
            out.append(_para(cur, page_no, stats))
            cur = []
        cur.append(ln)
    if cur:
        out.append(_para(cur, page_no, stats))
    return out


def classify(page: Page, page_no: int, heads: set[str]) -> tuple[list[Block], PageStats]:
    stats = page_stats(page)
    blocks: list[Block] = []
    for par in page.pars:
        if _is_furniture(par, page, heads):
            continue
        runs: list[tuple[str, list[Line]]] = []
        for ln in par.lines:
            cls = _line_class(ln, stats)
            cls = "text" if cls == "inline" else cls   # inline lines stay in the paragraph
            if runs and runs[-1][0] == cls and cls != "eqno":
                runs[-1][1].append(ln)
            else:
                runs.append((cls, [ln]))
        for cls, lines in runs:
            words = [w for ln in lines for w in ln.words]
            bbox = union([ln.bbox for ln in lines])
            match cls:
                case "math":
                    blocks.append(Block("formula", page_no, bbox, ink=[bbox], anchors=[
                        w.text for w in words if w.conf >= 60 and WORDLIKE.match(w.text)]))
                case "eqno":
                    blocks.append(Block("eqno", page_no, bbox, text=lines[0].text.strip("() ")))
                case "row":
                    blocks.append(_table(blocks, lines, page_no, bbox, words, stats))
                case _:
                    blocks.extend(_text_blocks(lines, page_no, stats))
    blocks = [b for b in blocks if b.kind != "table" or len(b.rows) >= 2] + \
        [_dissolve(b) for b in blocks if b.kind == "table" and len(b.rows) < 2]
    blocks.sort(key=lambda b: (b.bbox[1], b.bbox[0]))
    blocks = _merge_formulas(blocks, gap=stats.body_size * 1.5)
    min_side = stats.body_size * 0.5
    blocks = [b for b in blocks if not (b.kind == "formula" and (
        b.bbox[2] - b.bbox[0] < min_side or b.bbox[3] - b.bbox[1] < min_side
        or (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1]) < stats.body_size ** 2 * 1.2))]
    return _attach_eqno(blocks), stats


def _table(blocks: list[Block], lines: list[Line], page_no: int, bbox: Box, words: list[Word],
           stats: PageStats) -> Block:
    rows = _snap_rows(lines, stats)
    prev = blocks[-1] if blocks else None
    # the paragraph's unfinished last line carries the column header ("… nie áno")
    if prev is not None and prev.kind == "para" and not SENT_END.search(prev.text) and rows[0][0]:
        prev.text = join_lines([prev.text, rows[0][0]])
        rows[0][0] = ""
    return Block("table", page_no, bbox, rows=rows, ink=[w.bbox for w in words])


def _dissolve(b: Block) -> Block:
    text = " ".join(c for r in b.rows for c in r if c)
    return Block("para", b.page, b.bbox, text=text, segments=[Segment("text", text, b.bbox, [])],
                 ink=b.ink)


def _merge_formulas(blocks: list[Block], gap: float) -> list[Block]:
    out: list[Block] = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev and prev.kind == b.kind == "formula" and b.bbox[1] - prev.bbox[3] < gap:
            prev.bbox = union([prev.bbox, b.bbox])
            prev.ink += b.ink
            prev.anchors += b.anchors
            continue
        out.append(b)
    return out


def _attach_eqno(blocks: list[Block]) -> list[Block]:
    out: list[Block] = []
    for b in blocks:
        if b.kind == "eqno":
            prev = next((x for x in reversed(out[-3:]) if x.kind == "formula"), None)
            if prev and not prev.tag and b.bbox[1] - prev.bbox[3] < 250:
                prev.tag = b.text
                continue
            b.kind, b.text = "para", f"({b.text})"
            b.segments = [Segment("text", b.text, b.bbox, [])]
        out.append(b)
    return out


# ---------------------------------------------------------------- image side
def ink_mask(gray: np.ndarray) -> np.ndarray:
    return (gray < 140).astype(np.uint8)


def stamp_mask(img: np.ndarray) -> np.ndarray:
    """Library stamps: saturated blue/violet ink (printed content here is black)."""
    if img.ndim != 3:
        return np.zeros(img.shape[:2], np.uint8)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = ((s > 70) & (v < 235) & (h >= 95) & (h <= 160)).astype(np.uint8)
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))


def is_figure_like(gray: np.ndarray, bbox: Box, stats: PageStats) -> bool:
    """A region holding a big sparse stroke (axes, arrows, diagonals) is a drawing."""
    x0, y0, x1, y1 = bbox
    roi = ink_mask(gray[y0:y1, x0:x1])
    if roi.size == 0:
        return False
    n, _, cc, _ = cv2.connectedComponentsWithStats(roi)
    big = stats.body_size * 2.2
    return any(cc[i][2] > big and cc[i][3] > big and cc[i][4] < 0.5 * cc[i][2] * cc[i][3]
               for i in range(1, n))


def merge_boxes(boxes: list[Box], gap: int) -> list[Box]:
    boxes = list(boxes)
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] - gap < b[2] and b[0] - gap < a[2] and a[1] - gap < b[3] and b[1] - gap < a[3]:
                    boxes[i] = union([a, b])
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return boxes


def detect_figures(gray: np.ndarray, stats: PageStats, blocks: list[Block]) -> list[Box]:
    """Ink regions that no text/formula block explains."""
    h, w = gray.shape
    ink = ink_mask(gray)
    column = np.zeros_like(ink)
    column[int(h * 0.04):int(h * 0.94), max(0, stats.left - 60):min(w, stats.right + 60)] = 1
    ink &= column
    for b in blocks:
        for x0, y0, x1, y1 in b.ink:
            ink[max(0, y0 - 4):y1 + 4, max(0, x0 - 4):x1 + 4] = 0
    k = max(15, int(stats.body_size * 1.5))
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    n, _, cc, _ = cv2.connectedComponentsWithStats(closed)
    figs = [(int(x), int(y), int(x + bw), int(y + bh)) for x, y, bw, bh, _ in cc[1:]
            if bh > stats.body_size * 3 and bw > stats.body_size * 3 and bw * bh > 0.004 * w * h]
    return merge_boxes(figs, gap=int(stats.body_size * 2))


def detect_ordered_lists(gray: np.ndarray, blocks: list[Block], stats: PageStats) -> list[Block]:
    """Runs of >=3 short indented paragraphs with ink in the marker column whose numbers
    Tesseract lost -> ordered list."""
    ink = ink_mask(gray)
    marker = int(stats.body_size * 2.5)
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        j = i
        while j < len(blocks) and blocks[j].kind == "para" and len(blocks[j].text) < 160 and \
                abs(blocks[j].bbox[0] - blocks[i].bbox[0]) < stats.body_size * 1.3 and \
                blocks[j].bbox[0] - stats.left > stats.body_size * 0.5:
            j += 1
        run = blocks[i:j]
        if len(run) >= 3 and sum(
                ink[b.bbox[1]:b.bbox[3], max(0, b.bbox[0] - marker):b.bbox[0] - 4].sum() > 20
                for b in run) >= 0.7 * len(run):
            out.append(Block("olist", run[0].page, union([b.bbox for b in run]),
                             items=[re.sub(r"^[.,·•\s]+", "", b.text) for b in run],
                             ink=[w for b in run for w in b.ink] +
                             [(b.bbox[0] - marker, b.bbox[1], b.bbox[0], b.bbox[3]) for b in run]))
            i = j
            continue
        out.append(blocks[i])
        i += 1
    return out


def analyze_page(page: Page, image: np.ndarray, page_no: int, heads: set[str]) -> list[Block]:
    """All blocks of one page in reading order. ``image`` is BGR or grayscale."""
    if is_form_page(page):
        return [Block("page", page_no, (0, 0, page.width, page.height))]
    page = clip_to_main_column(page)
    gray = image.copy() if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    stamp = stamp_mask(image)
    gray[stamp > 0] = 255
    blocks, stats = classify(page, page_no, heads)

    def stamp_share(b: Block) -> float:
        x0, y0, x1, y1 = b.bbox
        s = stamp[y0:y1, x0:x1]
        return float(s.sum()) / max(1, int((ink_mask(gray[y0:y1, x0:x1]) | s).sum()))

    blocks = [b for b in blocks if not (b.kind in ("formula", "para") and stamp_share(b) > 0.5)]
    for b in blocks:
        if b.kind == "formula" and is_figure_like(gray, b.bbox, stats):
            b.kind = "figure"   # never send a drawing to the formula model
    blocks = detect_ordered_lists(gray, blocks, stats)
    figs = detect_figures(gray, stats, blocks)
    pad = int(stats.body_size)

    def inside(box: Box, f: Box) -> bool:
        return f[0] - pad <= box[0] and box[2] <= f[2] + pad and f[1] - pad <= box[1] and box[3] <= f[3] + pad

    kept = [b for b in blocks if not (
        b.kind in ("formula", "para") and len(b.text) < 40 and any(inside(b.bbox, f) for f in figs))]
    fig_boxes = merge_boxes(figs + [b.bbox for b in kept if b.kind == "figure"], gap=pad)
    kept = [b for b in kept if b.kind != "figure"] + [Block("figure", page_no, f) for f in fig_boxes]
    kept.sort(key=lambda b: (b.bbox[1], b.bbox[0]))
    return kept
