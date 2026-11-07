"""Build the reflowable edition of one book: pages + hOCR -> Markdown -> EPUB 3 (MathML).

Entry point: :func:`build_edition`. Pure Python + three CLI tools: ``pandoc``
(required), ``potrace`` (optional — line-art figures become SVG) and
``hunspell`` (optional — dictionary arbitration while anchoring).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from evilflowers_books_digitalizer.reflow import vlm as vlm_mod
from evilflowers_books_digitalizer.reflow.analyze import (
    LOWER_START,
    SENT_END,
    Block,
    analyze_page,
    join_lines,
    running_heads,
    stamp_mask,
)
from evilflowers_books_digitalizer.reflow.anchor import (
    MATH,
    TOKEN,
    Lexicon,
    anchor,
    normalize_hacek,
    outside_math,
    trustworthy,
)
from evilflowers_books_digitalizer.reflow.model import Page, parse_file

logger = logging.getLogger(__name__)

_MD_ESC = re.compile(r"([\\`*_\[\]#<>$|@])")   # '@' would become a pandoc citation

CSS = """
body { font-family: serif; line-height: 1.45; hyphens: auto; -webkit-hyphens: auto; }
h1, h2, h3, h4 { font-family: sans-serif; line-height: 1.2; hyphens: none; }
h4 { font-size: 1em; font-style: italic; }
p { margin: 0; text-indent: 1.2em; text-align: justify; }
h1 + p, h2 + p, h3 + p, h4 + p, figure + p, table + p, ol + p, ul + p { text-indent: 0; }
figure { margin: 1em 0; text-align: center; }
img { max-width: 100%; }
figcaption { font-size: 0.9em; font-style: italic; }
table { border-collapse: collapse; margin: 0.8em auto; }
td, th { padding: 0.15em 0.6em; border-bottom: 1px solid #ccc; }
.page-image img { width: 100%; }
"""


@dataclass(slots=True)
class ReflowSettings:
    vlm: vlm_mod.VlmSettings | None = None
    hunspell_dict: str | None = None
    pandoc: str = "pandoc"
    vector_figures: bool = True
    asset_scale: float = 0.5        # 600 dpi pages -> ~300 dpi figures in the EPUB


@dataclass(slots=True)
class ReflowReport:
    pages: int = 0
    page_images: int = 0
    formulas: int = 0
    formula_images: int = 0
    mixed: int = 0
    inline_ok: int = 0
    fallbacks: int = 0
    rejected: int = 0
    figures: int = 0
    tables: int = 0
    running_heads: list[str] = field(default_factory=list)
    vlm_jobs: int = 0


def esc(text: str) -> str:
    return re.sub(r"^(\d+)\.(\s)", r"\1\\.\2", _MD_ESC.sub(r"\\\1", text))


# ---------------------------------------------------------------- region decisions
@dataclass(frozen=True, slots=True)
class RegionDecision:
    """A decided region on one page (e.g. the studio's region rows), in page-image pixels.

    Plain strings so this module stays free of the studio: ``kind`` is a layout
    label (``formula``, ``picture``, ``section_header``, ``stamp`` …) and ``action``
    one of ``keep`` / ``remove`` / ``as_image`` / ``reocr``. ``latex`` is a formula's
    already recognised LaTeX (no VLM is run for it).
    """

    kind: str
    bbox: tuple[int, int, int, int]
    action: str = "keep"
    latex: str | None = None


#: label -> heading level (``_Markdown`` renders level n as ``#`` * max(2, min(n, 4)))
_HEADING_LEVEL = {"title": 1, "section_header": 3}
_PROSE = frozenset({"text", "list_item", "footnote"})
_FURNITURE = frozenset({"page_header", "page_footer"})
_MOSTLY = 0.5     # a block belongs to a region when this share of its area lies inside it
_FILLS = 0.4      # ... and, to be relabelled, covers this share of the region


def _inside(box: tuple[int, int, int, int], region: tuple[int, int, int, int]) -> float:
    ix = max(0, min(box[2], region[2]) - max(box[0], region[0]))
    iy = max(0, min(box[3], region[3]) - max(box[1], region[1]))
    return ix * iy / max(1, (box[2] - box[0]) * (box[3] - box[1]))


def _carve(blocks: list[Block], region: tuple[int, int, int, int]) -> list[Block]:
    """Drop the blocks (and paragraph lines) that lie mostly inside ``region``."""
    out = []
    for b in blocks:
        if _inside(b.bbox, region) >= _MOSTLY:
            continue
        if b.kind == "para" and b.segments:
            kept = [s for s in b.segments if _inside(s.bbox, region) < _MOSTLY]
            if not kept:
                continue
            b.segments = kept
        out.append(b)
    return out


def _relabel(blocks: list[Block], d: RegionDecision) -> None:
    """Relabel the block the region *is*: mostly inside it and filling most of it, so a
    large ``text`` region never turns the headings it spans into prose."""
    for b in blocks:
        if _inside(b.bbox, d.bbox) < _MOSTLY or _inside(d.bbox, b.bbox) < _FILLS:
            continue
        text = b.text or " ".join(s.md for s in b.segments)
        if d.kind in _HEADING_LEVEL and b.kind in ("para", "caption", "heading", "formula"):
            if b.kind != "heading":
                b.level = _HEADING_LEVEL[d.kind]
            b.kind, b.text, b.segments = "heading", text, []
        elif d.kind == "caption" and b.kind in ("para", "heading"):
            b.kind, b.text, b.segments = "caption", text, []
        elif d.kind in _PROSE and b.kind in ("heading", "caption", "formula") and text:
            b.kind, b.anchors = "para", []


def apply_decisions(blocks: list[Block], decisions: Sequence[RegionDecision], page_no: int
                    ) -> tuple[list[Block], dict[str, str]]:
    """Operator/studio decisions over the heuristic analysis of one page.

    Returns the new block list and the formula answers (``vlm.Job`` key -> LaTeX):

    * ``remove`` (or a running head/foot label) -> the blocks under it are dropped;
    * ``as_image`` / ``picture`` -> one figure crop replaces the blocks under it;
    * ``formula`` -> one formula block replaces them; with ``latex`` it goes through
      the usual pandoc validation to MathML, without (or invalid) it is a crop;
    * text labels (``title``, ``section_header``, ``caption``, ``text`` …) relabel
      the blocks under them.
    """
    answers: dict[str, str] = {}
    for d in decisions:
        if d.action == "remove" or d.kind in _FURNITURE:
            blocks = _carve(blocks, d.bbox)
        elif d.action == "as_image" or d.kind == "picture":
            blocks = [*_carve(blocks, d.bbox), Block("figure", page_no, d.bbox)]
        elif d.kind == "formula":
            blocks = [*_carve(blocks, d.bbox), Block("formula", page_no, d.bbox)]
            if d.latex and d.latex.strip():
                answers[vlm_mod.Job(page_no, Path(), d.bbox, "formula").key] = d.latex
        else:
            _relabel(blocks, d)
    blocks.sort(key=lambda b: (b.bbox[1], b.bbox[0]))
    return blocks, answers


def _whiten(image: np.ndarray, decisions: Sequence[RegionDecision]) -> np.ndarray:
    for d in decisions:
        if d.action == "remove":
            x0, y0, x1, y1 = d.bbox
            image[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 255
    return image


# ---------------------------------------------------------------- entry point
def build_edition(
    pages: Sequence[Path],
    hocr: Path,
    out_epub: Path,
    work_dir: Path,
    metadata: dict[str, object],
    settings: ReflowSettings,
    cover: Path | None = None,
    decisions: Sequence[Sequence[RegionDecision]] | None = None,
) -> ReflowReport:
    """``pages`` (cleaned page images, in order) + multi-page ``hocr`` -> ``out_epub``.

    ``decisions`` (one list per page, e.g. the studio's region rows) override the
    heuristic analysis (:func:`apply_decisions`): removed regions are whitened and
    dropped, ``as_image`` regions become crops, formulas use their stored LaTeX.
    With decisions given the VLM is never run — the regions carry the answers.
    """
    hocr_pages = parse_file(hocr)
    if len(hocr_pages) != len(pages):
        raise ValueError(f"hOCR has {len(hocr_pages)} pages but {len(pages)} images were given")
    if decisions is not None and len(decisions) != len(pages):
        raise ValueError(f"{len(decisions)} region-decision lists for {len(pages)} pages")
    work_dir.mkdir(parents=True, exist_ok=True)
    report = ReflowReport(pages=len(pages))
    heads = running_heads(hocr_pages)
    report.running_heads = sorted(heads)

    analyzed: list[list[Block]] = []
    answers: dict[str, str] = {}
    for i, (img_path, page) in enumerate(zip(pages, hocr_pages, strict=True)):
        image = cv2.imread(str(img_path), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError(f"cannot read page image {img_path}")
        if image.ndim == 3 and image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        if decisions is None:
            analyzed.append(analyze_page(page, image, i, heads))
            continue
        blocks, page_answers = apply_decisions(
            analyze_page(page, _whiten(image, decisions[i]), i, heads), decisions[i], i)
        analyzed.append(blocks)
        answers.update(page_answers)

    jobs = _jobs(analyzed, pages)
    report.vlm_jobs = len(jobs) if decisions is None else 0
    if jobs and settings.vlm is not None and decisions is None:
        answers = vlm_mod.run_jobs(jobs, work_dir / "vlm_cache.json", settings.vlm)
    lexicon = Lexicon(_vocabulary(analyzed, answers), settings.hunspell_dict)
    valid = vlm_mod.validate_math(_math_candidates(jobs, answers), settings.pandoc)

    writer = _Markdown(work_dir / "assets", settings, lexicon, valid, report)
    for i, blocks in enumerate(analyzed):
        color = cv2.imread(str(pages[i]), cv2.IMREAD_COLOR)
        if color is None:
            raise ValueError(f"cannot read page image {pages[i]}")
        if decisions is not None:
            color = _whiten(color, decisions[i])
        writer.page(i, blocks, color, hocr_pages[i],
                    {j.key: answers[j.key] for j in jobs if j.page == i and j.key in answers})
    md = writer.finish()
    (work_dir / "book.md").write_text(md)
    _pandoc_epub(work_dir, md, out_epub, metadata, cover, settings.pandoc)
    return report


def _jobs(analyzed: list[list[Block]], pages: Sequence[Path]) -> list[vlm_mod.Job]:
    jobs = []
    for i, blocks in enumerate(analyzed):
        for b in blocks:
            if b.kind == "formula":
                jobs.append(vlm_mod.Job(i, pages[i], b.bbox, "ocr" if b.mixed else "formula"))
            elif b.kind == "para":
                jobs += [vlm_mod.Job(i, pages[i], s.bbox, "ocr") for s in b.segments if s.kind == "inline"]
    return jobs


def _vocabulary(analyzed: list[list[Block]], answers: dict[str, str]) -> set[str]:
    words = {w for text in answers.values() for w in TOKEN.findall(outside_math(normalize_hacek(text)))}
    for blocks in analyzed:
        for b in blocks:
            words.update(re.sub(r"[^\w]", "", a) for s in b.segments for a in s.anchors)
    return words


def _inline_key(tex: str) -> str:
    return "$" + vlm_mod.split_tag(vlm_mod.fix_latex(tex))[0]


def _math_candidates(jobs: list[vlm_mod.Job], answers: dict[str, str]) -> set[str]:
    out = set()
    for job in jobs:
        text = answers.get(job.key)
        if not text:
            continue
        spans = MATH.findall(normalize_hacek(text))
        if job.task == "formula":
            out.add(vlm_mod.split_tag(vlm_mod.fix_latex(text))[0])
        elif len(spans) == 1 and not outside_math(text).strip():   # display-only OCR answer
            out.add(vlm_mod.split_tag(vlm_mod.fix_latex(spans[0]))[0])
        else:
            out.update(_inline_key(span) for span in spans)
    return {s for s in out if s.strip("$")}


# ---------------------------------------------------------------- markdown writer
class _Markdown:
    def __init__(self, assets: Path, settings: ReflowSettings, lexicon: Lexicon, valid: set[str],
                 report: ReflowReport) -> None:
        self.assets = assets
        self.settings = settings
        self.lexicon = lexicon
        self.valid = valid
        self.report = report
        self.out: list[str] = []
        self.open_para: str | None = None
        assets.mkdir(parents=True, exist_ok=True)

    # -- prose ---------------------------------------------------------------
    def _inline_md(self, text: str) -> str | None:
        """Anchored VLM text -> markdown, or None if any math span fails validation."""
        parts = []
        for piece in MATH.split(text):
            if not piece:
                continue
            if MATH.fullmatch(piece):
                tex, tag = vlm_mod.split_tag(vlm_mod.fix_latex(piece))
                if "$" + tex not in self.valid:
                    return None
                parts.append(f"${tex}$" + (f" ({tag})" if tag else ""))
            else:
                parts.append(_MD_ESC.sub(r"\\\1", piece))
        return "".join(parts)

    def _vlm_prose(self, answer: str | None, anchors: list[str]) -> str | None:
        if not answer:
            return None
        text, stats = anchor(answer, anchors, self.lexicon)
        return self._inline_md(text) if trustworthy(text, stats) else None

    def _flush(self) -> None:
        if self.open_para is not None:
            self.out.append(self.open_para)
            self.open_para = None

    def _paragraph(self, b: Block, page: int, answers: dict[str, str]) -> None:
        parts = []
        for s in b.segments or []:
            md = None
            if s.kind == "inline":
                md = self._vlm_prose(answers.get(vlm_mod.Job(page, Path(), s.bbox, "ocr").key), s.anchors)
                self.report.inline_ok += md is not None
                self.report.fallbacks += md is None
            parts.append(md if md is not None else esc(s.md))
        text = join_lines(parts) if parts else esc(b.text)
        # continuation across a page break or a Tesseract block split
        if self.open_para is not None and LOWER_START.match(text) and not SENT_END.search(self.open_para):
            self.open_para = join_lines([self.open_para, text])
            return
        self._flush()
        self.open_para = text

    # -- images --------------------------------------------------------------
    def _save(self, img: np.ndarray, box: tuple[int, int, int, int], name: str, line_art: bool) -> str:
        h, w = img.shape[:2]
        x0, y0, x1, y1 = box
        pad = 14
        crop = img[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        scale = self.settings.asset_scale
        if scale != 1.0:
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        lo, hi = np.percentile(gray, 2), np.percentile(gray, 60)
        norm = np.clip((gray.astype(np.float32) - lo) / max(1.0, hi - lo) * 255, 0, 255).astype(np.uint8)
        norm[norm > 225] = 255                         # paper -> pure white
        midtones = float(((norm > 40) & (norm < 215)).mean())
        if line_art and midtones < 0.06 and self.settings.vector_figures and shutil.which("potrace"):
            big = cv2.resize(norm, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            _, bw = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            pbm, svg = self.assets / f"{name}.pbm", self.assets / f"{name}.svg"
            cv2.imwrite(str(pbm), bw)
            subprocess.run(["potrace", "-s", "--flat", "-t", "4", "-a", "1.0", "-O", "0.4",
                            str(pbm), "-o", str(svg)], check=True)
            pbm.unlink()
            svg.write_text(re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg.read_text()))  # epubcheck OPF-073
            return f"assets/{svg.name}"
        png = self.assets / f"{name}.png"
        cv2.imwrite(str(png), norm, [cv2.IMWRITE_PNG_COMPRESSION, 9])
        return f"assets/{png.name}"

    @staticmethod
    def _width(box: tuple[int, int, int, int], body_px: int) -> str:
        return f"{{width={max(5, min(100, round(100 * (box[2] - box[0] + 28) / body_px)))}%}}"

    # -- page ----------------------------------------------------------------
    def page(self, n: int, blocks: list[Block], img: np.ndarray, page: Page,
             answers: dict[str, str]) -> None:
        img = img.copy()
        img[stamp_mask(img) > 0] = 255
        body_px = max([b.bbox[2] - b.bbox[0] for b in blocks if b.kind == "para"] or [page.width // 2])
        captions = {i: b for i, b in enumerate(blocks) if b.kind == "caption"}
        used: set[int] = set()
        for i, b in enumerate(blocks):
            name = f"p{n + 1:04d}_{i}"
            if b.kind == "para":
                self._paragraph(b, n, answers)
                continue
            self._flush()
            match b.kind:
                case "page":
                    path = self._save(img, b.bbox, name, line_art=False)
                    self.out.append(f"![Strana {n + 1}]({path}){{.page-image width=100%}}\\")
                    self.report.page_images += 1
                case "heading":
                    self.out.append("#" * max(2, min(b.level, 4)) + " " + esc(b.text).replace("\\.", "."))
                case "formula":
                    self._formula(b, n, img, name, body_px, answers)
                case "figure":
                    cap = min((c for c in captions if c not in used),
                              key=lambda c: abs(captions[c].bbox[1] - b.bbox[3]), default=None)
                    text = ""
                    if cap is not None and abs(captions[cap].bbox[1] - b.bbox[3]) < 6 * (b.bbox[3] - b.bbox[1] + 1):
                        used.add(cap)
                        text = esc(captions[cap].text)
                    path = self._save(img, b.bbox, name, line_art=True)
                    alt = text or "Obrázok"
                    self.out.append(f"![{alt}]({path}){self._width(b.bbox, body_px)}" + ("" if text else "\\"))
                    self.report.figures += 1
                case "caption" if i not in used:
                    self.out.append(f"*{esc(b.text)}*")
                case "list" | "olist":
                    self.out.append(self._list(b))
                case "table":
                    self.out.append(self._table(b))
                    self.report.tables += 1

    def _formula(self, b: Block, page: int, img: np.ndarray, name: str, body_px: int,
                 answers: dict[str, str]) -> None:
        job = vlm_mod.Job(page, Path(), b.bbox, "ocr" if b.mixed else "formula")
        answer = answers.get(job.key)
        if answer and vlm_mod.looks_hallucinated(answer):
            self.report.rejected += 1
            answer = None
        md = None
        # an OCR-task answer that is nothing but one math span is a display formula
        spans = MATH.findall(answer or "")
        display_only = bool(answer) and len(spans) == 1 and not outside_math(answer or "").strip()
        if answer and (job.task == "formula" or display_only):
            tex, tag = vlm_mod.split_tag(vlm_mod.fix_latex(spans[0] if display_only else answer))
            if tex in self.valid:
                md = f"$${tex}" + (f" \\tag{{{tag or b.tag}}}" if (tag or b.tag) else "") + "$$"
                self.report.formulas += 1
        elif answer:
            md = self._vlm_prose(answer, b.anchors)
            if md is not None:
                md += f" ({b.tag})" if b.tag else ""
                self.report.mixed += 1
        if md is None:
            path = self._save(img, b.bbox, name, line_art=False)
            md = f"![Vzorec]({path}){self._width(b.bbox, body_px)}\\"
            self.report.formula_images += 1
        self.out.append(md)

    @staticmethod
    def _list(b: Block) -> str:
        lines = []
        ordered = b.kind == "olist"
        for k, item in enumerate(b.items):
            m = re.match(r"^(\d{1,2})[.)]\s*", item)
            ordered = ordered or bool(m)
            body = esc(re.sub(r"^(\d{1,2}[.)]|[a-z]\)|[•·▪–])\s*", "", item))
            marker = re.match(r"^([a-z])\)", item)
            if marker and not ordered:
                body = f"{marker[1]}) {body}"
            lines.append(f"{(int(m[1]) if m and k == 0 else 1)}. {body}" if ordered else f"- {body}")
        return "\n".join(lines)

    @staticmethod
    def _table(b: Block) -> str:
        ncol = max(len(r) for r in b.rows)
        rows = [r + [""] * (ncol - len(r)) for r in b.rows]
        head, body = (rows[0], rows[1:]) if not rows[0][0] else ([""] * ncol, rows)
        out = ["| " + " | ".join(esc(c) for c in head) + " |",
               "|" + "|".join([":---"] + ["---:"] * (ncol - 1)) + "|"]
        return "\n".join(out + ["| " + " | ".join(esc(c) for c in r) + " |" for r in body])

    def finish(self) -> str:
        self._flush()
        return "\n\n".join(self.out) + "\n"


# ---------------------------------------------------------------- pandoc
def _epub_metadata(metadata: dict[str, object]) -> dict[str, object]:
    meta: dict[str, object] = {"title": metadata.get("title") or "Bez názvu",
                               "lang": metadata.get("language") or metadata.get("ocr_language") or "sk"}
    if isinstance(meta["lang"], str) and meta["lang"].startswith("slk"):
        meta["lang"] = "sk"
    if authors := metadata.get("authors"):
        meta["author"] = list(authors) if isinstance(authors, list | tuple) else [str(authors)]
    for src, dst in (("publisher", "publisher"), ("year", "date"), ("description", "description")):
        if metadata.get(src):
            meta[dst] = str(metadata[src])
    if metadata.get("isbn"):
        meta["identifier"] = [{"scheme": "ISBN-13", "text": str(metadata["isbn"])}]
    return meta


def _pandoc_epub(work_dir: Path, md: str, out: Path, metadata: dict[str, object],
                 cover: Path | None, pandoc: str) -> None:
    (work_dir / "book.md").write_text(md)
    (work_dir / "epub.css").write_text(CSS)
    (work_dir / "meta.yaml").write_text(json.dumps(_epub_metadata(metadata), ensure_ascii=False))
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [pandoc, "book.md", "--from", "markdown+implicit_figures+pipe_tables",
           "--metadata-file", "meta.yaml", "--math-method=mathml", "--css", "epub.css",
           "--toc", "--toc-depth=3", "--split-level=2", "-o", str(out.resolve())]
    if cover and cover.exists():
        cmd += ["--epub-cover-image", str(cover.resolve())]
    proc = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc failed (exit {proc.returncode}): {proc.stderr[-1500:]}")
