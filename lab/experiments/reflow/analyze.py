"""Page image + Tesseract hOCR -> semantic blocks (the reflow document model).

Routing principle (from the probe): Tesseract `slk` is trusted for prose; a VLM is
consulted only for regions Tesseract demonstrably cannot read (math).
Classification is line-centric: each OCR line is text | math | row (table row),
then runs of same-class lines become blocks.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from hocr_blocks import Line, Page, Par, Word, parse

Box = tuple[int, int, int, int]

EQNO = re.compile(r"^\(\d+(\.\d+)+\)$")              # (1.3.5.1)
HEADNUM = re.compile(r"^(\d+(\.\d+)*)\.?\s+[A-ZÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ]")
LISTITEM = re.compile(r"^([a-z]\)|\d{1,2}[.)]|[•·▪–])(\s+|$)")
CAPTION = re.compile(r"^(Obr\.|Tab\.|Tabuľka|Obrázok)\s*\d", re.I)
SENT_END = re.compile(r"[.!?:;]\**\s*$")
WORDLIKE = re.compile(r"^[\"„“(]*[A-Za-zÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽáäčďéíľĺňóôŕšťúýž]{3,}[.,;:!?)\"“]*$")


@dataclass
class Block:
    kind: str                      # heading|para|list|olist|formula|figure|caption|table
    page: int
    bbox: Box
    text: str = ""
    level: int = 0
    latex: str = ""
    tag: str = ""
    rows: list[list[str]] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    image: str = ""
    conf: float = 0.0
    source: str = "tesseract"
    words: list[Box] = field(default_factory=list)   # ink actually explained by this block
    segments: list[dict] = field(default_factory=list)  # para lines: text | inline (VLM)


def _union(boxes: list[Box]) -> Box:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _conf(words: list[Word]) -> float:
    return sum(w.conf for w in words) / max(1, len(words))


def _wordlike_ratio(words: list[Word]) -> float:
    return sum(bool(WORDLIKE.match(w.text)) and w.conf >= 75 for w in words) / max(1, len(words))


def _word_md(w: Word) -> str:
    t = w.text
    if w.bold and len(t) > 1:
        return f"**{t}**"
    if w.italic and len(t) > 1:
        return f"*{t}*"
    return t


def _words_md(words: list[Word]) -> str:
    out = " ".join(_word_md(w) for w in words)
    out = re.sub(r"(?<!\*)\* \*(?!\*)", " ", out)       # merge adjacent italics
    return re.sub(r"\*\* \*\*", " ", out)


def join_lines(lines: list[str]) -> str:
    """Join OCR lines into one paragraph, undoing end-of-line hyphenation."""
    text = ""
    for ln in lines:
        ln = ln.strip()
        if not text:
            text = ln
        elif re.search(r"\w-\**$", text) and re.match(r"^\**[a-záäčďéíĺľňóôŕšťúýž]", ln):
            text = re.sub(r"-(\**)$", r"\1", text) + ln
        else:
            text += " " + ln
    return text


@dataclass
class PageStats:
    body_size: float
    left: int
    right: int


def page_stats(page: Page) -> PageStats:
    good = [ln for p in page.pars for ln in p.lines if _conf(ln.words) >= 85 and len(ln.words) >= 4]
    sizes = [ln.size for ln in good] or [30.0]
    lefts = [ln.bbox[0] for ln in good] or [0]
    rights = [ln.bbox[2] for ln in good] or [page.width]
    return PageStats(statistics.median(sizes), int(np.percentile(lefts, 10)), int(np.percentile(rights, 90)))


def _is_furniture(par: Par, page: Page) -> bool:
    """Page numbers, running heads, rules, scanner noise."""
    x0, y0, x1, y1 = par.bbox
    t = par.text.strip()
    top, bottom = y1 < page.height * 0.10, y0 > page.height * 0.88
    if (top or bottom) and re.fullmatch(r"[\divxlcIVXLC\s.\-–]+", t):
        return True
    if top and len(par.words) <= 4 and len(par.lines) == 1:
        return True  # running head
    if par.mean_conf < 30 and (x1 - x0) < page.width * 0.06:
        return True  # vertical garbage strip (binding shadow / list-number column)
    return not any(c.isalnum() for c in t)


def _cells(ln: Line, gap_min: float) -> list[list[Word]]:
    cells, cur = [], [ln.words[0]]
    for a, b in zip(ln.words, ln.words[1:]):
        if b.bbox[0] - a.bbox[2] > gap_min:
            cells.append(cur)
            cur = []
        cur.append(b)
    cells.append(cur)
    return cells


def _line_class(ln: Line, stats: PageStats) -> str:
    t = ln.text.strip()
    conf = _conf(ln.words)
    if EQNO.match(t):
        return "eqno"
    wl = _wordlike_ratio(ln.words)
    mathchars = sum(c in "=+−×·/|^_∂∇∫∑√<>()" for c in t)
    if conf < 50 or (conf < 85 and wl < 0.25):
        return "math"            # 2-D / display math: Tesseract text is garbage
    if wl == 0 and all(len(w.text) <= 4 for w in ln.words):
        return "math"            # fraction denominators, integral limits: "dt dt dt", "K S"
    if conf < 88 and mathchars >= 2 and wl < 0.75:
        return "inline"          # prose with inline math: VLM + Tesseract anchoring
    if any(c in t for c in "@©®"):
        return "inline"          # Tesseract's signature for a misread Greek letter (ω -> ©/@)
    cells = _cells(ln, stats.body_size * 1.6)
    if len(cells) >= 2 and all(len(" ".join(w.text for w in c)) <= 6 for c in cells[1:]) and \
            _wordlike_ratio(cells[0]) >= 0.5 and len(cells[0]) >= 2:
        return "row"
    return "text"


def _merge_formulas(blocks: list[Block], gap: float) -> list[Block]:
    out: list[Block] = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev and prev.kind == b.kind == "formula" and b.bbox[1] - prev.bbox[3] < gap:
            prev.bbox = _union([prev.bbox, b.bbox])
            prev.words += b.words
            anchors = prev.segments[0]["anchors"] + b.segments[0]["anchors"]
            prev.segments = [{"kind": "mixed" if anchors else "display", "anchors": anchors}]
            continue
        out.append(b)
    return out


def analyze_page(page: Page, page_no: int) -> list[Block]:
    stats = page_stats(page)
    blocks: list[Block] = []
    for par in page.pars:
        if _is_furniture(par, page):
            continue
        runs: list[tuple[str, list[Line]]] = []
        for ln in par.lines:
            c = _line_class(ln, stats)
            if c == "inline":
                c = "text"       # stays in the paragraph; segment flagged below
            if runs and runs[-1][0] == c and c != "eqno":
                runs[-1][1].append(ln)
            else:
                runs.append((c, [ln]))
        for cls, lines in runs:
            words = [w for ln in lines for w in ln.words]
            bbox = _union([ln.bbox for ln in lines])
            wboxes = [w.bbox for w in words]
            conf = _conf(words)
            first = lines[0].text.strip()
            if cls == "math":
                # real words inside a math line ("… a ďalej …") -> OCR task, not formula task
                anchors = [w.text for w in words if w.conf >= 60 and WORDLIKE.match(w.text)]
                blocks.append(Block("formula", page_no, bbox, conf=conf, source="vlm", words=[bbox],
                                    segments=[{"kind": "mixed" if anchors else "display",
                                               "anchors": anchors}]))
            elif cls == "eqno":
                blocks.append(Block("eqno", page_no, bbox, text=first.strip("()"), conf=conf, words=wboxes))
            elif cls == "row":
                rows = _snap_rows(lines, stats)
                # a leading row whose label is prose (the paragraph's last line) is the header
                if len(rows) >= 1 and blocks and blocks[-1].kind == "para" and \
                        not SENT_END.search(blocks[-1].text) and len(rows[0][0]) > 0:
                    blocks[-1].text = join_lines([blocks[-1].text, rows[0][0]])
                    rows[0][0] = ""
                if blocks and blocks[-1].kind == "table" and blocks[-1].page == page_no and \
                        bbox[1] - blocks[-1].bbox[3] < stats.body_size * 2:
                    blocks[-1].rows += rows
                    blocks[-1].bbox = _union([blocks[-1].bbox, bbox])
                    blocks[-1].words += wboxes
                else:
                    blocks.append(Block("table", page_no, bbox, rows=rows, conf=conf, words=wboxes))
            else:
                blocks.extend(_text_blocks(lines, page_no, stats, conf))
    # a one-row "table" is not a table: dissolve back into text
    for b in blocks:
        if b.kind == "table" and len([r for r in b.rows if any(r)]) < 2:
            b.kind = "para"
            b.text = " ".join(c for r in b.rows for c in r if c)
            b.segments = [{"kind": "text", "md": b.text, "bbox": b.bbox, "anchors": []}]
    blocks = _merge_formulas(blocks, gap=stats.body_size * 1.5)
    # noise-sized "formulas" (rules, specks, binding dirt) are not content
    min_side = stats.body_size * 0.5
    blocks = [b for b in blocks if not (b.kind == "formula" and (
        b.bbox[2] - b.bbox[0] < min_side or b.bbox[3] - b.bbox[1] < min_side or
        (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1]) < (stats.body_size ** 2) * 1.2))]
    return _attach_eqno(blocks)


def _snap_rows(lines: list[Line], stats: PageStats) -> list[list[str]]:
    """Table rows from word geometry: value columns anchored on short right-hand cells."""
    gap = stats.body_size * 1.6
    centers = []
    for ln in lines:
        for c in _cells(ln, gap)[1:]:
            centers.append((c[0].bbox[0] + c[-1].bbox[2]) / 2)
    anchors: list[float] = []
    for x in sorted(centers):
        if anchors and x - anchors[-1] < stats.body_size * 2:
            continue
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
        row[0] = _words_md(label) if label else ""
        rows.append(row)
    return rows


def _text_blocks(lines: list[Line], page_no: int, stats: PageStats, conf: float) -> list[Block]:
    words = [w for ln in lines for w in ln.words]
    bbox = _union([ln.bbox for ln in lines])
    wboxes = [w.bbox for w in words]
    first = lines[0].text.strip()
    size = statistics.median(ln.size for ln in lines)
    width = bbox[2] - bbox[0]
    body_w = stats.right - stats.left
    if CAPTION.match(first) and len(lines) <= 3:
        return [Block("caption", page_no, bbox, text=join_lines([_words_md(ln.words) for ln in lines]),
                      conf=conf, words=wboxes)]
    if len(lines) <= 2 and len(words) <= 12 and not SENT_END.search(lines[-1].text):
        m = HEADNUM.match(first)
        if m:
            return [Block("heading", page_no, bbox, text=join_lines([ln.text for ln in lines]),
                          level=min(4, 1 + m.group(1).count(".")), conf=conf, words=wboxes)]
        if size >= stats.body_size * 1.18 or (len(lines) == 1 and width < 0.5 * body_w and
                                               first[:1].isupper() and 2 <= len(words) <= 5 and
                                               _wordlike_ratio(words) >= 0.8):
            return [Block("heading", page_no, bbox, text=join_lines([ln.text for ln in lines]),
                          level=2 if size >= stats.body_size * 1.4 else 4, conf=conf, words=wboxes)]
    if LISTITEM.match(first):
        items: list[list[str]] = []
        for ln in lines:
            if LISTITEM.match(ln.text.strip()) or not items:
                items.append([])
            items[-1].append(_words_md(ln.words))
        return [Block("list", page_no, bbox, items=[join_lines(it) for it in items], conf=conf, words=wboxes)]
    # split paragraphs at indented first lines inside one OCR block
    out: list[Block] = []
    cur: list[Line] = []
    for ln in lines:
        if cur and ln.bbox[0] - stats.left > stats.body_size * 1.2 and SENT_END.search(cur[-1].text):
            out.append(_para(cur, page_no, conf, stats))
            cur = []
        cur.append(ln)
    if cur:
        out.append(_para(cur, page_no, conf, stats))
    return out


def _para(lines: list[Line], page_no: int, conf: float, stats: PageStats | None = None) -> Block:
    segs = []
    for ln in lines:
        inline = stats is not None and _line_class(ln, stats) == "inline"
        segs.append({"kind": "inline" if inline else "text", "md": _words_md(ln.words),
                     "bbox": ln.bbox,
                     "anchors": [w.text for w in ln.words if w.conf >= 80 and WORDLIKE.match(w.text)]})
    return Block("para", page_no, _union([ln.bbox for ln in lines]),
                 text=join_lines([_words_md(ln.words) for ln in lines]), conf=conf,
                 words=[w.bbox for ln in lines for w in ln.words], segments=segs)


def _attach_eqno(blocks: list[Block]) -> list[Block]:
    out: list[Block] = []
    for b in blocks:
        if b.kind == "eqno":
            prev = next((x for x in reversed(out[-3:]) if x.kind == "formula"), None)
            if prev and not prev.tag and b.bbox[1] - prev.bbox[3] < 250:
                prev.tag = b.text
                continue
            b.kind, b.text = "para", f"({b.text})"
        out.append(b)
    return out


def _ink(gray: np.ndarray) -> np.ndarray:
    return (gray < 140).astype(np.uint8)


def detect_ordered_lists(gray: np.ndarray, blocks: list[Block], stats: PageStats) -> list[Block]:
    """Runs of >=3 single-line items whose numbers Tesseract lost -> ordered list.

    Evidence required: ink blobs in the marker column left of each item.
    """
    ink = _ink(gray)
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        j = i
        while j < len(blocks) and blocks[j].kind == "para" and len(blocks[j].text) < 160 and \
                abs(blocks[j].bbox[0] - blocks[i].bbox[0]) < stats.body_size * 1.3 and \
                blocks[j].bbox[0] - stats.left > stats.body_size * 0.5:
            j += 1
        run = blocks[i:j]
        if len(run) >= 3:
            marked = 0
            for b in run:
                x0, y0, _, y1 = b.bbox
                strip = ink[y0:y1, max(0, x0 - int(stats.body_size * 2.5)):x0 - 4]
                marked += strip.sum() > 20
            if marked >= 0.7 * len(run):
                out.append(Block("olist", run[0].page, _union([b.bbox for b in run]),
                                 items=[re.sub(r"^[.,·•\s]+", "", b.text) for b in run],
                                 words=[w for b in run for w in b.words] +
                                 [(b.bbox[0] - int(stats.body_size * 2.5), b.bbox[1], b.bbox[0], b.bbox[3]) for b in run]))
                i = j
                continue
        out.append(blocks[i])
        i += 1
    return out


def detect_figures(gray: np.ndarray, stats: PageStats, blocks: list[Block]) -> list[Box]:
    """Ink regions that no text/formula block explains -> figures."""
    h, w = gray.shape
    ink = _ink(gray)
    x0, x1 = max(0, stats.left - 60), min(w, stats.right + 60)
    col = np.zeros_like(ink)
    col[int(h * 0.04):int(h * 0.94), x0:x1] = 1
    ink &= col
    for b in blocks:
        for bx0, by0, bx1, by1 in b.words:
            ink[max(0, by0 - 4):by1 + 4, max(0, bx0 - 4):bx1 + 4] = 0
    k = max(15, int(stats.body_size * 1.5))
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    n, _, cc, _ = cv2.connectedComponentsWithStats(closed)
    figs: list[Box] = []
    for i in range(1, n):
        x, y, bw, bh, _ = cc[i]
        if bh > stats.body_size * 3 and bw > stats.body_size * 3 and bw * bh > 0.004 * w * h:
            figs.append((int(x), int(y), int(x + bw), int(y + bh)))
    return _merge_boxes(figs, gap=int(stats.body_size * 2))


def _merge_boxes(boxes: list[Box], gap: int) -> list[Box]:
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] - gap < b[2] and b[0] - gap < a[2] and a[1] - gap < b[3] and b[1] - gap < a[3]:
                    boxes[i] = _union([a, b])
                    del boxes[j]
                    changed = True
                    break
            if changed:
                break
    return boxes


def _inside(b: Box, f: Box, pad: int = 12) -> bool:
    return f[0] - pad <= b[0] and b[2] <= f[2] + pad and f[1] - pad <= b[1] and b[3] <= f[3] + pad


def clip_to_main_column(page: Page) -> Page:
    """Drop words from a facing-page sliver: anything beyond an empty vertical gutter
    in the outer 20% of the page."""
    proj = np.zeros(page.width + 1, dtype=np.int32)
    for p in page.pars:
        for w in p.words:
            proj[w.bbox[0]:w.bbox[2]] += 1
    gutter = int(page.width * 0.015)
    lo, hi = 0, page.width

    def zero_runs(a: int, b: int) -> list[tuple[int, int]]:
        runs, start = [], None
        for x in range(a, b):
            if proj[x] == 0 and start is None:
                start = x
            elif proj[x] != 0 and start is not None:
                runs.append((start, x))
                start = None
        if start is not None:
            runs.append((start, b))
        return runs

    left_runs = [r for r in zero_runs(0, int(page.width * 0.2)) if r[1] - r[0] >= gutter and r[0] > 0]
    if left_runs:
        lo = left_runs[-1][1] - 1
    right_runs = [r for r in zero_runs(int(page.width * 0.8), page.width) if r[1] - r[0] >= gutter and r[1] < page.width]
    if right_runs:
        hi = right_runs[0][0] + 1
    if lo == 0 and hi == page.width:
        return page
    new_pars = []
    for p in page.pars:
        lines = []
        for ln in p.lines:
            ws = [w for w in ln.words if w.bbox[0] >= lo and w.bbox[2] <= hi]
            if ws:
                lines.append(Line(ws, _union([w.bbox for w in ws]), ln.size))
        if lines:
            new_pars.append(Par(lines, _union([ln.bbox for ln in lines])))
    page.pars = new_pars
    return page


def is_figure_like(gray: np.ndarray, bbox: Box, stats: PageStats) -> bool:
    """A 'formula' region is really a drawing if it holds an ink stroke that is big in
    BOTH directions (axes, arrows, diagonals). Fraction bars are flat, brackets and
    integral signs are thin, so genuine math passes."""
    x0, y0, x1, y1 = bbox
    roi = _ink(gray[y0:y1, x0:x1])
    if roi.size == 0:
        return False
    n, _, cc, _ = cv2.connectedComponentsWithStats(roi)
    big = stats.body_size * 2.2
    for i in range(1, n):
        _, _, w, h, area = cc[i]
        if w > big and h > big and area < 0.5 * w * h:   # sparse & large = line art
            return True
    return False


def stamp_mask(bgr: np.ndarray) -> np.ndarray:
    """Library marks (ownership stamps, inventory numbers) are saturated blue/violet ink;
    printed content in these books is black. Returns a uint8 mask of stamp ink."""
    if bgr.ndim != 3:
        return np.zeros(bgr.shape[:2], np.uint8)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = ((s > 70) & (v < 235) & (h >= 95) & (h <= 160)).astype(np.uint8)   # blue..violet
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))


def _stamp_fraction(stamp: np.ndarray, gray: np.ndarray, bbox: Box) -> float:
    x0, y0, x1, y1 = bbox
    ink = _ink(gray[y0:y1, x0:x1]) | stamp[y0:y1, x0:x1]
    return float(stamp[y0:y1, x0:x1].sum()) / max(1, int(ink.sum()))


def analyze(image: Path, hocr: Path, page_no: int) -> tuple[list[Block], PageStats, Page]:
    page = clip_to_main_column(parse(hocr))
    stats = page_stats(page)
    bgr = cv2.imread(str(image), cv2.IMREAD_COLOR)
    stamp = stamp_mask(bgr)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray[stamp > 0] = 255                   # stamps are not content: erase before figure detection
    blocks = analyze_page(page, page_no)
    blocks = [b for b in blocks if not (b.kind in ("formula", "para") and _stamp_fraction(stamp, gray, b.bbox) > 0.5)]
    for b in blocks:
        if b.kind == "formula" and is_figure_like(gray, b.bbox, stats):
            b.kind, b.source = "figure", "image"   # never send a drawing to the formula model
    blocks = detect_ordered_lists(gray, blocks, stats)
    figs = detect_figures(gray, stats, blocks)
    # grow figures over small text/formula fragments inside them (labels: a, b, A, u ...)
    kept: list[Block] = []
    for b in blocks:
        host = next((f for f in figs if _inside(b.bbox, f, pad=int(stats.body_size))), None)
        if host and b.kind in ("formula", "para") and len(b.text) < 40:
            continue
        kept.append(b)
    for f in figs:
        kept.append(Block("figure", page_no, f, source="image"))
    # a promoted formula-figure overlapping a detected figure: merge into one drawing
    figs_all = [b for b in kept if b.kind == "figure"]
    merged = _merge_boxes([b.bbox for b in figs_all], gap=int(stats.body_size))
    kept = [b for b in kept if b.kind != "figure"] + [Block("figure", page_no, f, source="image") for f in merged]
    kept.sort(key=lambda b: (b.bbox[1], b.bbox[0]))
    return kept, stats, page
