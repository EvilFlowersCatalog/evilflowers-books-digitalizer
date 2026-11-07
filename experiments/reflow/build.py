"""Reflow a scanned book into a born-digital edition: Markdown -> EPUB 3 (MathML) + LaTeX PDF.

usage: build.py <bookdir> <meta.json> [--no-vlm] [--pages 5-57]

<bookdir>/pages/pNNN.(jpg|png) + <bookdir>/hocr/pNNN.hocr  ->  <bookdir>/out/
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from analyze import SENT_END, Block, analyze, join_lines, stamp_mask

VLM_PY = Path.home() / ".local/share/evilflowers-tools/venv-docres/bin/python"
VLM_SCRIPT = Path(__file__).parent / "paddle_vl.py"


# ---------------------------------------------------------------- anchoring
def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


TOKEN = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
MATH = re.compile(r"(\\\(.+?\\\)|\\\[.+?\\\]|\$\$.+?\$\$|\$.+?\$)", re.S)


ANCHOR_WINDOW = 4   # look-ahead over not-yet-consumed anchors

# Greek/Cyrillic glyphs a VLM substitutes for Latin ones, plus non-decomposable letters
_CONFUSABLE = str.maketrans({
    "ο": "o", "υ": "u", "ί": "i", "ι": "i", "α": "a", "ε": "e", "ρ": "p", "ν": "v", "κ": "k",
    "τ": "t", "χ": "x", "ό": "o", "ύ": "u", "ά": "a", "έ": "e",
    "о": "o", "а": "a", "е": "e", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "к": "k",
    "ł": "l", "Ł": "L", "đ": "d",
})


def fold(s: str) -> str:
    """Diacritic- and script-insensitive skeleton: 'funkcίου' == 'funkciou', 'polá' == 'poľa'."""
    import unicodedata
    s = s.translate(_CONFUSABLE)
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c)).lower()


HUNSPELL_DICT = Path.home() / "Library/Spelling/sk_SK"
HACEK_APOS = re.compile(r"([tdlTDL])['’ʼ`](?=[\w\s.,;:!?)]|$)")
_HACEK = {"t": "ť", "d": "ď", "l": "ľ", "T": "Ť", "D": "Ď", "L": "Ľ"}


def normalize_hacek(text: str) -> str:
    """Typewriter-style háček: VLM writes 'uskutočnit’' / 'opät'' for ť/ď/ľ."""
    parts = MATH.split(text)
    return "".join(p if MATH.fullmatch(p or "") else HACEK_APOS.sub(lambda m: _HACEK[m.group(1)], p)
                   for p in parts)


class Lexicon:
    """Slovak hunspell oracle, queried in one batch per book (hunspell -l lists misspellings)."""

    def __init__(self, words: set[str]) -> None:
        self.known: set[str] = set()
        cand = sorted({w for w in words if len(w) >= 3 and w.isalpha()})
        if not cand or not HUNSPELL_DICT.with_suffix(".dic").exists():
            self.enabled = False
            return
        r = subprocess.run(["hunspell", "-d", str(HUNSPELL_DICT), "-i", "utf-8", "-l"],
                           input="\n".join(cand), capture_output=True, text=True)
        bad = set(r.stdout.split())
        self.known = {w for w in cand if w not in bad}
        self.enabled = True

    def ok(self, w: str) -> bool | None:
        return (w in self.known) if self.enabled else None


LEX: Lexicon | None = None


def anchor(vlm_text: str, anchors: list[str]) -> tuple[str, dict]:
    """Snap VLM prose tokens to Tesseract's high-confidence words (diacritics authority).

    Order-aware: anchors are consumed left to right, each at most once, and a token may
    only match within the next ANCHOR_WINDOW unconsumed anchors. This prevents a correct
    VLM ``polia`` snapping to a Tesseract ``pole`` two words further on.
    """
    clean = [re.sub(r"^[^\w]+|[^\w]+$", "", a) for a in anchors]
    clean = [a for a in clean if len(a) >= 3]
    fixed, used = 0, set()
    pos = 0   # index of the first unconsumed anchor

    def fix_token(m: re.Match) -> str:
        nonlocal fixed, pos
        tok = m.group(0)
        if len(tok) < 3:
            return tok
        best_i, bd = None, 1e9
        ft = fold(tok)
        for i in range(pos, min(len(clean), pos + ANCHOR_WINDOW)):
            a = clean[i]
            if abs(len(a) - len(tok)) > 2:
                continue
            # same skeleton = pure diacritic/script drift: the strongest possible match
            d = 0 if fold(a) == ft else _lev(ft, fold(a)) + 0.5
            if d < bd:
                best_i, bd = i, d
        if best_i is not None and bd / max(len(clean[best_i]), 1) <= 0.34:
            best = clean[best_i]
            used.add(best_i)
            pos = best_i + 1
            if best != tok:
                # dictionary arbitration: keep the VLM word when only it is a real Slovak word
                if LEX is not None and LEX.ok(tok) is True and LEX.ok(best) is False:
                    kept_vlm[0] += 1
                    return tok
                fixed += 1
                return best
        return tok

    kept_vlm = [0]
    vlm_text = normalize_hacek(vlm_text)

    parts = MATH.split(vlm_text)
    out = []
    for p in parts:
        out.append(p if MATH.fullmatch(p or "") else TOKEN.sub(fix_token, p))
    missing = [a for i, a in enumerate(clean) if i not in used and a not in vlm_text]
    return "".join(out), {"fixed": fixed, "anchors": len(clean), "missing": len(missing),
                          "kept_vlm": kept_vlm[0]}


# ---------------------------------------------------------------- VLM jobs
def job_key(pno: int, bbox, task: str) -> str:
    """Content-addressed cache key: survives changes to block ordering/indices."""
    return f"p{pno:03d}_{task}_" + "_".join(str(int(v)) for v in bbox)


def formula_task(b: Block) -> str:
    return "ocr" if b.segments and b.segments[0]["kind"] == "mixed" else "formula"


FOREIGN = re.compile(r"[Ͱ-ϿЀ-ӿěřůłĚŘŮŁőűŐŰ]")


def has_foreign(text: str) -> bool:
    """Letters that never occur in Slovak prose, outside of math."""
    return bool(FOREIGN.search(MATH.sub(" ", text)))


def collect_jobs(pages: dict[int, list[Block]], images: dict[int, Path]) -> list[dict]:
    jobs = []
    for pno, blocks in pages.items():
        for b in blocks:
            if b.kind == "formula":
                task = formula_task(b)
                jobs.append({"id": job_key(pno, b.bbox, task), "image": str(images[pno]), "bbox": list(b.bbox),
                             "task": task, "pad": 10})
            elif b.kind == "para":
                for s in b.segments:
                    if s["kind"] == "inline":
                        jobs.append({"id": job_key(pno, s["bbox"], "ocr"), "image": str(images[pno]),
                                     "bbox": list(s["bbox"]), "task": "ocr", "pad": 8})
    return jobs


def run_vlm(jobs: list[dict], cache: Path) -> dict:
    todo_path = cache.with_suffix(".jobs.json")
    todo_path.write_text(json.dumps(jobs, ensure_ascii=False))
    if jobs:
        subprocess.run([str(VLM_PY), str(VLM_SCRIPT), str(todo_path), str(cache)], check=True)
    return json.loads(cache.read_text()) if cache.exists() else {}


# ---------------------------------------------------------------- markdown
MD_ESC = re.compile(r"([\\`*_\[\]#<>$|@])")   # '@' would become a pandoc citation


def esc(text: str) -> str:
    """Escape OCR text for pandoc markdown, keeping our own **/* emphasis markers."""
    parts = re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text)
    out = []
    for p in parts:
        if re.fullmatch(r"\*\*[^*]+\*\*|\*[^*]+\*", p):
            out.append(p)
        else:
            out.append(MD_ESC.sub(r"\\\1", p))
    s = "".join(out)
    return re.sub(r"^(\d+)\.(\s)", r"\1\\.\2", s)


def _inner(span: str) -> str:
    return re.sub(r"^(\\\(|\\\[|\$\$|\$)|(\\\)|\\\]|\$\$|\$)$", "", span).strip()


def tex_inline(vlm: str) -> str | None:
    """VLM text with \\( \\) / \\[ \\] math -> pandoc markdown with $ / $$.

    Returns None when any math span fails LaTeX validation (caller falls back)."""
    pieces = MATH.split(vlm)
    out = []
    for p in pieces:
        if not p:
            continue
        if MATH.fullmatch(p):
            tex = fix_latex(_inner(p))
            # \tag is display-only: lift an equation number out of inline math as text
            m = re.search(r"\\tag\{([^}]*)\}", tex)
            tex = re.sub(r"\s*\\tag\{[^}]*\}", "", tex).strip()
            if not tex or not latex_ok(tex, inline=True):
                return None
            out.append(f"${tex}$" + (f" ({m.group(1)})" if m else ""))
        else:
            out.append(MD_ESC.sub(r"\\\1", p))
    return "".join(out)


# ---------------------------------------------------------------- LaTeX validation
_LATEX_OK: dict[str, bool] = {}
_LATEX_CACHE: Path | None = None


def _compile(key: str) -> bool:
    import hashlib
    import tempfile
    inline = key.startswith("$")
    tex = key[1:] if inline else key
    body = f"${tex}$" if inline else f"\\[{tex}\\]"
    doc = ("\\documentclass{article}\\usepackage{amsmath,amssymb}\\begin{document}\n" + body +
           "\n\\end{document}\n")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / (hashlib.md5(tex.encode()).hexdigest() + ".tex")
        f.write_text(doc)
        r = subprocess.run(["xelatex", "-halt-on-error", "-interaction=nonstopmode", "-no-pdf",
                            "-output-directory", d, str(f)], capture_output=True, text=True, timeout=60)
        return r.returncode == 0


def latex_ok(tex: str, inline: bool = False) -> bool:
    """Compile check, display (\\[..\\]) or inline ($..$) context; keys prefixed '$' = inline."""
    key = ("$" + tex) if inline else tex
    if key not in _LATEX_OK:
        _LATEX_OK[key] = _compile(key)
    return _LATEX_OK[key]


def prevalidate(vlm: dict, cache: Path) -> dict:
    """Compile every math snippet once (parallel, cached): a single malformed VLM matrix
    must not take down the whole PDF."""
    from concurrent.futures import ThreadPoolExecutor
    global _LATEX_OK
    if cache.exists():
        _LATEX_OK.update(json.loads(cache.read_text()))
    snippets = set()
    for r in vlm.values():
        if r.get("error") or not r["text"].strip():
            continue
        if r["task"] == "formula":
            snippets.add(fix_latex(r["text"]))
        for span in MATH.findall(normalize_hacek(r["text"])) if r["task"] == "ocr" else []:
            tex = re.sub(r"\s*\\tag\{[^}]*\}", "", fix_latex(_inner(span))).strip()
            if tex:
                snippets.add("$" + tex)       # inline context
    todo = [s for s in snippets if s not in _LATEX_OK]
    with ThreadPoolExecutor(8) as ex:
        for s, ok in zip(todo, ex.map(_compile, todo)):
            _LATEX_OK[s] = ok
    cache.write_text(json.dumps(_LATEX_OK, ensure_ascii=False))
    bad = sum(not _LATEX_OK[s] for s in snippets)
    return {"snippets": len(snippets), "invalid": bad}


def looks_hallucinated(tex: str) -> bool:
    """Formula-model output that describes a drawing rather than math."""
    if re.search(r"Obr\.|Obrázok|Tab\.", tex):
        return True                               # caption text inside "math"
    arrays = re.findall(r"\\begin\{array\}\{[lcr]+\}(.*?)\\end\{array\}", tex, re.S)
    if len(arrays) >= 2:
        cells = [c.strip() for a in arrays for c in re.split(r"\\\\|&", a) if c.strip()]
        if cells and sum(len(re.sub(r"[\\{}_^ ]", "", c)) <= 2 for c in cells) / len(cells) > 0.7:
            return True                           # stacked arrays of lone labels = figure labels
    if len(re.findall(r"\\xrightarrow|\\longrightarrow", tex)) and len(tex) < 40:
        return True                               # an arrow and a letter: a vector drawing
    return False


def fix_latex(tex: str) -> str:
    tex = tex.strip()
    tex = re.sub(r"^\\\[|\\\]$", "", tex).strip()
    tex = re.sub(r"^\$\$|\$\$$", "", tex).strip()
    tex = re.sub(r"\\eqno\s*\{?\(?([\w.]+)\)?\}?", r"\\tag{\1}", tex)   # texmath knows \tag, not \eqno
    tex = tex.replace("\\begin{align*}", "\\begin{aligned}").replace("\\end{align*}", "\\end{aligned}")
    tex = tex.replace("\\begin{align}", "\\begin{aligned}").replace("\\end{align}", "\\end{aligned}")
    return tex


def export_figure(img: np.ndarray, b: Block, assets: Path, name: str, vector: bool) -> tuple[str, str]:
    h, w = img.shape[:2]
    pad = 14
    x0, y0, x1, y1 = b.bbox
    crop = img[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    # paper -> pure white, ink -> pure black (levels), kills bleed-through haze
    lo, hi = np.percentile(gray, 2), np.percentile(gray, 60)
    norm = np.clip((gray.astype(np.float32) - lo) / max(1.0, hi - lo) * 255, 0, 255).astype(np.uint8)
    norm[norm > 225] = 255
    png = assets / f"{name}.png"
    cv2.imwrite(str(png), norm, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    midtones = float(((norm > 40) & (norm < 215)).mean())
    if vector and midtones < 0.06:
        big = cv2.resize(norm, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        _, bw = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        pbm = assets / f"{name}.pbm"
        cv2.imwrite(str(pbm), bw)
        svg = assets / f"{name}.svg"
        subprocess.run(["potrace", "-s", "--flat", "-t", "4", "-a", "1.0", "-O", "0.4", str(pbm), "-o", str(svg)],
                       check=True)
        pbm.unlink()
        # EPUB 3 forbids external DTD identifiers in SVG (epubcheck OPF-073)
        svg.write_text(re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg.read_text()))
        return str(svg.relative_to(assets.parent)), "svg"
    return str(png.relative_to(assets.parent)), "png"


def blocks_to_md(pages: dict[int, list[Block]], vlm: dict, images: dict[int, Path], out: Path,
                 vector: bool, report: dict) -> str:
    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    md: list[str] = []
    pending_para: str | None = None   # last paragraph, kept open for cross-page joins

    def flush() -> None:
        nonlocal pending_para
        if pending_para is not None:
            md.append(pending_para)
            pending_para = None

    for pno in sorted(pages):
        blocks = pages[pno]
        img = cv2.imread(str(images[pno]))
        img[stamp_mask(img) > 0] = 255          # library stamps never reach the edition
        body_px =max([b.bbox[2] - b.bbox[0] for b in blocks if b.kind == "para"] or [1250])
        captions ={i: b for i, b in enumerate(blocks) if b.kind == "caption"}
        used_caps: set[int] = set()
        for bi, b in enumerate(blocks):
            key = f"p{pno:03d}_b{bi}"
            if b.kind == "para":
                parts = []
                for si, s in enumerate(b.segments or [{"kind": "text", "md": b.text}]):
                    r = vlm.get(job_key(pno, s["bbox"], "ocr")) if s["kind"] == "inline" else None
                    if r and r.get("error"):
                        r = None
                    if s["kind"] == "inline" and r:
                        txt, st = anchor(r["text"], s["anchors"])
                        report["inline"].append({"id": f"{key}_s{si}", "tess": s["md"], "vlm": r["text"],
                                                 "final": txt, **st})
                        md_txt = None
                        if not (st["missing"] > max(1, 0.25 * st["anchors"]) or has_foreign(txt)):
                            md_txt = tex_inline(txt)          # None if a math span won't compile
                        if md_txt is None:
                            # VLM dropped/merged words, left non-Slovak letters, or broke LaTeX
                            parts.append(esc(s["md"]))
                            report["fallbacks"] += 1
                        else:
                            parts.append(md_txt)
                    else:
                        parts.append(esc(s["md"]))
                text = join_lines(parts)
                starts_lower = bool(re.match(r"^[a-záäčďéíĺľňóôŕšťúýž]", text))
                # continuation: previous paragraph unfinished + this one starts lowercase
                # (across a page break, or a Tesseract block split mid-paragraph)
                if pending_para is not None and starts_lower and not SENT_END.search(pending_para):
                    pending_para = join_lines([pending_para, text])
                    report["page_joins"] += 1
                    continue
                flush()
                pending_para = text
                continue
            flush()
            if b.kind == "heading":
                md.append("#" * min(b.level, 4) + " " + esc(b.text).replace("\\.", "."))
            elif b.kind == "formula":
                r = vlm.get(job_key(pno, b.bbox, formula_task(b)))
                if r and (r.get("error") or not r["text"].strip() or looks_hallucinated(r["text"])):
                    if r and not r.get("error"):
                        report["rejected"].append({"page": pno, "bbox": list(b.bbox), "vlm": r["text"][:200]})
                    r = None
                if r and r["task"] == "formula" and not latex_ok(fix_latex(r["text"])):
                    report["latex_invalid"] += 1
                    r = None
                if not r:
                    # trailing "\" = inline image (not a captioned figure); alt text for a11y
                    md.append(f"![Vzorec]({_crop_png(img, b, assets, key)}){{{_width(b, body_px)}}}\\")
                    report["formula_images"] += 1
                elif r["task"] == "formula":
                    tag = f" \\tag{{{b.tag}}}" if b.tag else ""
                    md.append(f"$${fix_latex(r['text'])}{tag}$$")
                    report["formulas"] += 1
                else:
                    txt, st = anchor(r["text"], b.segments[0]["anchors"])
                    report["inline"].append({"id": key, "vlm": r["text"], "final": txt, **st})
                    md_txt = None if has_foreign(txt) else tex_inline(txt)
                    if md_txt is None:
                        md.append(f"![Vzorec]({_crop_png(img, b, assets, key)}){{{_width(b, body_px)}}}\\")
                        report["fallbacks"] += 1
                        continue
                    tag = f" ({b.tag})" if b.tag else ""
                    md.append(md_txt + tag)
                    report["mixed"] += 1
            elif b.kind == "figure":
                cap_i = min((i for i in captions if i not in used_caps),
                            key=lambda i: abs(captions[i].bbox[1] - b.bbox[3]), default=None)
                cap = ""
                if cap_i is not None and abs(captions[cap_i].bbox[1] - b.bbox[3]) < 400:
                    used_caps.add(cap_i)
                    cap = esc(captions[cap_i].text)
                path, kind = export_figure(img, b, assets, f"fig_{pno:03d}_{bi}", vector)
                report["figures"][kind] += 1
                if cap:
                    md.append(f"![{cap}]({path}){{{_width(b, body_px)}}}")
                else:
                    md.append(f"![Obrázok]({path}){{{_width(b, body_px)}}}\\")
            elif b.kind == "caption":
                if bi not in used_caps:
                    md.append(f"*{esc(b.text)}*")
            elif b.kind in ("list", "olist"):
                start = None
                items = []
                for it in b.items:
                    m = re.match(r"^(\d{1,2})[.)]\s*", it)
                    if m and start is None:
                        start = int(m.group(1))
                    items.append(re.sub(r"^(\d{1,2}[.)]|[a-z]\)|[•·▪–])\s*", "", it))
                if b.kind == "list" and md and md[-1].startswith(("1. ", "- ")) and _list_kind(md[-1]) == ("ol" if start else "ul"):
                    md[-1] += "\n" + "\n".join(_li(items, start, cont=True))
                else:
                    ordered = b.kind == "olist" or start is not None
                    md.append("\n".join(_li(items, start if start else (1 if ordered else None))))
            elif b.kind == "table":
                ncol = max(len(r) for r in b.rows)
                rows = [r + [""] * (ncol - len(r)) for r in b.rows]
                head = rows[0] if not rows[0][0] else [""] * ncol
                body = rows[1:] if not rows[0][0] else rows
                lines = ["| " + " | ".join(esc(c) for c in head) + " |",
                         "|" + "|".join([":---"] + ["---:"] * (ncol - 1)) + "|"]
                lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in body]
                md.append("\n".join(lines))
                report["tables"] += 1
    flush()
    return "\n\n".join(md) + "\n"


def _width(b: Block, body_px: int) -> str:
    """Keep an image at its printed size relative to the text column (no blow-ups)."""
    pct = max(5, min(100, round(100 * (b.bbox[2] - b.bbox[0] + 28) / body_px)))
    return f"width={pct}%"


def _li(items: list[str], start: int | None, cont: bool = False) -> list[str]:
    if start is None:
        return [f"- {esc(it)}" for it in items]
    return [f"{'1' if cont else start}. {esc(it)}" if i == 0 and not cont else f"1. {esc(it)}"
            for i, it in enumerate(items)]


def _list_kind(s: str) -> str:
    return "ol" if s.startswith("1. ") or re.match(r"^\d+\. ", s) else "ul"


def _first_flow(blocks: list[Block]) -> int:
    return next((i for i, b in enumerate(blocks) if b.kind not in ("figure", "caption")), -1)


def _crop_png(img: np.ndarray, b: Block, assets: Path, name: str) -> str:
    x0, y0, x1, y1 = b.bbox
    p = assets / f"{name}.png"
    cv2.imwrite(str(p), img[max(0, y0 - 8):y1 + 8, max(0, x0 - 8):x1 + 8])
    return str(p.relative_to(assets.parent))


# ---------------------------------------------------------------- outputs
CSS = """
body { font-family: serif; line-height: 1.45; hyphens: auto; -webkit-hyphens: auto; }
h1, h2, h3, h4 { font-family: sans-serif; line-height: 1.2; hyphens: none; }
h3 { font-size: 1.15em; } h4 { font-size: 1em; font-style: italic; }
p { margin: 0; text-indent: 1.2em; text-align: justify; }
h1 + p, h2 + p, h3 + p, h4 + p, figure + p, div + p, table + p, ol + p, ul + p { text-indent: 0; }
figure { margin: 1em 0; text-align: center; }
figure img { max-width: 100%; }
figcaption { font-size: 0.9em; font-style: italic; }
table { border-collapse: collapse; margin: 0.8em auto; font-size: 0.95em; }
td, th { padding: 0.15em 0.6em; border-bottom: 1px solid #ccc; }
math[display="block"] { margin: 0.6em 0; }
"""

LATEX_HEADER = r"""
\usepackage{polyglossia}
\setmainlanguage{slovak}
\usepackage{amsmath}
\IfFileExists{microtype.sty}{\usepackage{microtype}}{}
\setlength{\parindent}{1.2em}
\setlength{\parskip}{0pt}
\usepackage{float}
\let\origfigure\figure
\let\endorigfigure\endfigure
\renewenvironment{figure}[1][2] {\origfigure[H]} {\endorigfigure}
"""


def build_outputs(out: Path, md_path: Path, meta: dict, cover: Path | None) -> dict:
    res = {}
    (out / "epub.css").write_text(CSS)
    common = ["pandoc", str(md_path), "--from", "markdown+implicit_figures+pipe_tables",
              "--metadata-file", str(out / "meta.yaml"), "--resource-path", str(out), "--toc", "--toc-depth=3"]
    epub_cmd = common + ["--math-method=mathml", "--css", str(out / "epub.css"), "--split-level=2",
                         "-o", str(out / "book.epub")]
    if cover:
        epub_cmd += ["--epub-cover-image", str(cover)]
    t = time.time()
    r = subprocess.run(epub_cmd, capture_output=True, text=True)
    res["epub"] = {"ok": r.returncode == 0, "secs": round(time.time() - t, 1), "log": r.stderr[-2000:]}
    (out / "header.tex").write_text(LATEX_HEADER)
    pdf_cmd = common + ["--pdf-engine=xelatex", "-H", str(out / "header.tex"),
                        "-V", "documentclass=article", "-V", "papersize=a5", "-V", "fontsize=10pt",
                        "-V", "geometry:top=18mm,bottom=20mm,inner=17mm,outer=15mm",
                        "-M", "lang=", "-V", "colorlinks=true", "-V", "linkcolor=black",
                        "-V", "secnumdepth=-1", "-o", str(out / "book.pdf")]
    t = time.time()
    r = subprocess.run(pdf_cmd, capture_output=True, text=True)
    res["pdf"] = {"ok": r.returncode == 0, "secs": round(time.time() - t, 1), "log": r.stderr[-3000:]}
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bookdir", type=Path)
    ap.add_argument("meta", type=Path)
    ap.add_argument("--no-vlm", action="store_true")
    ap.add_argument("--no-vector", action="store_true")
    ap.add_argument("--pages", default="")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    book = a.bookdir
    out = book / a.out
    out.mkdir(exist_ok=True)
    meta = json.loads(a.meta.read_text())
    images = {int(p.stem[1:]): p for p in sorted((book / "pages").iterdir()) if p.stem.startswith("p")}
    lo, hi = (int(x) for x in a.pages.split("-")) if a.pages else (min(images), max(images))
    skip = set(meta.pop("skip_pages", []))
    pages: dict[int, list[Block]] = {}
    for pno in range(lo, hi + 1):
        if pno in skip or pno not in images:
            continue
        blocks, _, _ = analyze(images[pno], book / "hocr" / f"p{pno:03d}.hocr", pno)
        pages[pno] = blocks
    (out / "blocks.json").write_text(json.dumps({p: [asdict(b) for b in bs] for p, bs in pages.items()},
                                                ensure_ascii=False, indent=0, default=str))
    jobs = [] if a.no_vlm else collect_jobs(pages, images)
    t = time.time()
    vlm = run_vlm(jobs, book / "vlm_cache.json") if jobs else {}
    vlm = {k: v for k, v in vlm.items() if k in {j["id"] for j in jobs}}   # only this analysis' jobs
    vlm_secs = time.time() - t
    # one hunspell batch over every word either engine produced
    global LEX
    words: set[str] = set()
    for r in vlm.values():
        words |= set(TOKEN.findall(MATH.sub(" ", normalize_hacek(r.get("text", "")))))
    for bs in pages.values():
        for b in bs:
            for s in b.segments:
                words |= {re.sub(r"^[^\w]+|[^\w]+$", "", a) for a in s.get("anchors", [])}
    LEX = Lexicon(words)
    t = time.time()
    latex_stats = prevalidate(vlm, book / "latex_ok.json") if vlm else {}
    latex_secs = round(time.time() - t, 1)
    report = {"pages": len(pages), "vlm_jobs": len(jobs), "vlm_secs": round(vlm_secs, 1), "formulas": 0,
              "mixed": 0, "formula_images": 0, "tables": 0, "figures": {"svg": 0, "png": 0},
              "page_joins": 0, "fallbacks": 0, "latex_invalid": 0, "latex": latex_stats,
              "latex_secs": latex_secs, "lexicon_words": len(LEX.known) if LEX else 0,
              "inline": [], "rejected": []}
    md = blocks_to_md(pages, vlm, images, out, vector=not a.no_vector, report=report)
    md_path = out / "book.md"
    md_path.write_text(md)
    cover = None
    if meta.get("cover_page") in images:
        c = cv2.imread(str(images[meta.pop("cover_page")]))
        c = cv2.resize(c, (int(c.shape[1] * 1600 / c.shape[0]), 1600), interpolation=cv2.INTER_AREA)
        cover = out / "cover.jpg"
        cv2.imwrite(str(cover), c, [cv2.IMWRITE_JPEG_QUALITY, 88])
    meta.pop("cover_page", None)
    (out / "meta.yaml").write_text("---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}"
                                                       for k, v in meta.items()) + "\n...\n")
    report["build"] = build_outputs(out, md_path, meta, cover)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    summary = {k: v for k, v in report.items() if k != "inline"}
    print(json.dumps(summary, ensure_ascii=False, indent=1)[:3000])


if __name__ == "__main__":
    main()
