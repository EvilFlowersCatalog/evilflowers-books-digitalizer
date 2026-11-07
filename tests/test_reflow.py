"""Reflow edition: anchoring, LaTeX hygiene, hOCR model, page analysis, EPUB build."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from evilflowers_books_digitalizer.reflow import ReflowSettings, build_edition
from evilflowers_books_digitalizer.reflow.analyze import (
    Block,
    Segment,
    classify,
    is_form_page,
    join_lines,
    running_heads,
)
from evilflowers_books_digitalizer.reflow.anchor import (
    anchor,
    fold,
    has_foreign,
    normalize_hacek,
    trustworthy,
)
from evilflowers_books_digitalizer.reflow.edition import RegionDecision, apply_decisions
from evilflowers_books_digitalizer.reflow.model import Line, Page, Par, Word, parse_pages
from evilflowers_books_digitalizer.reflow.vlm import (
    Job,
    fix_latex,
    looks_hallucinated,
    split_tag,
    validate_math,
)

needs_pandoc = pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")


# ---------------------------------------------------------------- builders
def line(text: str, y: int, conf: int = 95, x: int = 100, size: float = 30.0, gap: int = 12) -> Line:
    words, cx = [], x
    for token in text.split():
        w = max(10, len(token) * 15)
        words.append(Word(token, conf, (cx, y, cx + w, y + 30)))
        cx += w + gap
    return Line(words, (x, y, cx, y + 30), size)


def par(*lines: Line) -> Par:
    return Par(list(lines), (min(ln.bbox[0] for ln in lines), lines[0].bbox[1],
                             max(ln.bbox[2] for ln in lines), lines[-1].bbox[3]))


def hocr(pages: list[list[list[str]]]) -> str:
    """Minimal Tesseract-like hOCR: pages -> paragraphs -> lines (words conf 95)."""
    out = ['<html><body>']
    for p in pages:
        out.append('<div class="ocr_page" title="bbox 0 0 1400 2000">')
        y = 120
        for paragraph in p:
            out.append(f'<p class="ocr_par" title="bbox 100 {y} 1300 {y + 40 * len(paragraph)}">')
            for text in paragraph:
                out.append(f'<span class="ocr_line" title="bbox 100 {y} 1300 {y + 30}; x_size 30">')
                x = 100
                for token in text.split():
                    w = len(token) * 15
                    out.append(f'<span class="ocrx_word" title="bbox {x} {y} {x + w} {y + 30}; '
                               f'x_wconf 95">{token}</span> ')
                    x += w + 12
                out.append('</span>')
                y += 40
            out.append('</p>')
            y += 30
        out.append('</div>')
    return "".join(out) + "</body></html>"


# ---------------------------------------------------------------- anchoring
def test_fold_is_diacritic_and_script_insensitive():
    assert fold("funkcίου") == fold("funkciou") == "funkciou"
    assert fold("polá") == fold("poľa")
    assert fold("kładných") == "kladnych"


def test_anchor_repairs_slovak_drift():
    vlm = "s vektorovou funkcίου vyjadrit kładných zdroje polá"
    tess = ["vektorovou", "funkciou", "vyjadriť", "kladných", "zdroje", "poľa"]
    text, stats = anchor(vlm, tess)
    assert text == "s vektorovou funkciou vyjadriť kladných zdroje poľa"
    assert stats.fixed == 4 and trustworthy(text, stats)


def test_anchor_is_order_aware():
    # a correct "polia" must not snap to "pole" two words further on
    text, _ = anchor("polia a pole", ["polia", "a", "pole"])
    assert text == "polia a pole"


def test_anchor_leaves_math_alone():
    text, _ = anchor(r"vektor \(c = b - a\) platí", ["vektor", "platí"])
    assert r"\(c = b - a\)" in text


def test_merged_or_foreign_words_are_not_trustworthy():
    text, stats = anchor("indučněnéčiary sú", ["indukčné", "čiary", "sú"])
    assert has_foreign(text) and not trustworthy(text, stats)


def test_hacek_apostrophe():
    assert normalize_hacek("uskutočnit’ a opät' vzt'ah") == "uskutočniť a opäť vzťah"
    assert normalize_hacek(r"\(f'(x)\) a t'") == r"\(f'(x)\) a ť"


# ---------------------------------------------------------------- LaTeX hygiene
def test_fix_latex_and_tags():
    assert fix_latex(r"\[\begin{align*}a&=b\end{align*}\]") == r"\begin{aligned}a&=b\end{aligned}"
    assert fix_latex(r"x=1\eqno{(3)}") == r"x=1\tag{3}"
    assert split_tag(r"x=1 \tag{1.2}") == ("x=1", "1.2")


def test_hallucination_guard():
    assert looks_hallucinated(r"\[q\xrightarrow{\quad}Obr.\,1.1.4.1\]")
    labels = r"\begin{array}{l}y\\f_y\\j\end{array}\begin{array}{l}\beta\\\alpha\end{array}"
    assert looks_hallucinated(labels)
    assert not looks_hallucinated(r"f=\sqrt{f_x^2+f_y^2+f_z^2}")


@needs_pandoc
def test_validate_math_batch():
    ok = validate_math({r"\frac{a}{b}", "$x^2", r"\frac{a}{", r"\begin{matrix}a&b"})
    assert r"\frac{a}{b}" in ok and "$x^2" in ok
    assert r"\frac{a}{" not in ok


FAKE_WORKER = '''
import json, sys
from pathlib import Path
args = sys.argv[1:]
max_jobs = int(args[args.index("--max-jobs") + 1])
jobs, cache = json.loads(Path(args[-2]).read_text()), Path(args[-1])
res = json.loads(cache.read_text()) if cache.exists() else {}
todo = [j for j in jobs if j["id"] not in res or res[j["id"]].get("error")][:max_jobs]
for j in todo:
    n = res.get(j["id"], {}).get("attempts", 0) + 1
    if j["id"].endswith("_9") and n == 1:      # first try of this job "OOMs"
        res[j["id"]] = {"text": "", "task": j["task"], "attempts": n, "error": "MPS out of memory"}
    else:
        res[j["id"]] = {"text": "x^" + j["id"][-1], "task": j["task"], "attempts": n}
cache.write_text(json.dumps(res))
'''


def test_vlm_runner_recycles_workers_and_retries(tmp_path, monkeypatch):
    import sys

    from evilflowers_books_digitalizer.reflow import vlm

    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER)
    monkeypatch.setattr(vlm, "WORKER", worker)
    monkeypatch.setattr(vlm, "JOBS_PER_PROCESS", 3)
    jobs = [Job(0, tmp_path / "p.png", (0, 0, 10, i), "formula") for i in range(10)]
    out = vlm.run_jobs(jobs, tmp_path / "cache.json",
                       vlm.VlmSettings(python=Path(sys.executable), model=tmp_path))
    assert len(out) == 10                                   # all done, incl. the OOM retry
    cache = json.loads((tmp_path / "cache.json").read_text())
    assert cache[jobs[9].key]["attempts"] == 2


def test_job_key_is_content_addressed():
    a = Job(3, Path("x.tif"), (1, 2, 3, 4), "formula")
    assert a.key == "p0003_formula_1_2_3_4"
    assert a.key != Job(3, Path("x.tif"), (1, 2, 3, 4), "ocr").key


# ---------------------------------------------------------------- model + analysis
def test_parse_multi_page_hocr():
    pages = parse_pages(hocr([[["Prvá strana"]], [["Druhá strana", "pokračuje"]]]))
    assert len(pages) == 2 and pages[1].pars[0].text == "Druhá strana\npokračuje"
    assert pages[0].width == 1400 and pages[0].pars[0].words[0].conf == 95


def test_join_lines_dehyphenates():
    assert join_lines(["kanalizá-", "cie a voda"]) == "kanalizácie a voda"
    assert join_lines(["STU -", "Bratislava"]) == "STU - Bratislava"


def test_running_heads_are_detected_per_book():
    pages = [Page(1400, 2000, [par(line("Henrich Pifko", 100)),
                               par(line("Telo strany s dostatočne dlhým textom číslo", 600))])
             for _ in range(6)]
    assert running_heads(pages) == {"henrich pifko"}
    blocks, _ = classify(pages[0], 0, running_heads(pages))
    assert [b.kind for b in blocks] == ["para"]


def test_line_routing():
    page = Page(1400, 2000, [
        par(line("1.3.5 Divergencia a rotácia", 100)),
        par(line("Pri opise fyzikálnych polí sa často stretávame s výrazmi", 200),
            line("ktoré možno formálne vyjadriť ako vektorový súčin", 240)),
        par(line("Ae ar? OF, | — ae", 320, conf=35)),
    ])
    blocks, _ = classify(page, 0, set())
    kinds = [b.kind for b in blocks]
    assert kinds == ["heading", "para", "formula"]
    assert blocks[0].level == 3
    assert blocks[1].text.startswith("Pri opise") and blocks[1].text.endswith("súčin")


def test_form_page_becomes_whole_page_image():
    fragments = [par(line(t, 100 + i * 50, conf=55)) for i, t in enumerate(
        ["G2", "0,5", "L3 ve", "| —", "1 a", "80%", "Cy", "X 1", "2 3", "ok"] * 3)]
    assert is_form_page(Page(1400, 2000, fragments))
    prose = [par(line("Toto je normálny odsek s riadnym textom knihy", 100 + i * 50)) for i in range(30)]
    assert not is_form_page(Page(1400, 2000, prose))


# ---------------------------------------------------------------- end to end (no VLM)
@needs_pandoc
def test_build_edition_without_vlm(tmp_path):
    doc = hocr([
        [["1 VEKTORY"], ["Vektory zapisujeme tučnými písmenami a ich veľkosť", "je vždy nezáporná."]],
        [["pokračovanie odseku z predchádzajúcej strany je tu."]],
    ])
    (tmp_path / "book.hocr").write_text(doc)
    pages = []
    for i in range(2):
        img = np.full((2000, 1400), 255, np.uint8)
        p = tmp_path / f"p{i}.png"
        cv2.imwrite(str(p), img)
        pages.append(p)
    out = tmp_path / "out" / "book.epub"
    report = build_edition(pages, tmp_path / "book.hocr", out, tmp_path / "work",
                           {"title": "Vektory", "authors": ["Ivan Červeň"], "language": "slk"},
                           ReflowSettings(vlm=None, hunspell_dict=None))
    assert out.exists() and report.pages == 2
    with zipfile.ZipFile(out) as z:
        xhtml = " ".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml"))
    assert "Vektory zapisujeme" in xhtml
    md = (tmp_path / "work" / "book.md").read_text()
    # the paragraph continues across the page break
    assert "nezáporná. pokračovanie" not in md   # sentence ended -> new paragraph
    assert "# 1 VEKTORY" in md or "## 1 VEKTORY" in md


# ---------------------------------------------------------------- region decisions (studio)
def _para_block(text: str, y: int) -> Block:
    seg = Segment("text", text, (100, y, 1300, y + 30), text.split())
    return Block("para", 0, (100, y, 1300, y + 30), text=text, segments=[seg])


def test_decisions_drop_crop_relabel_and_answer_formulas():
    blocks = [_para_block("Úvodný odsek knihy.", 100), _para_block("PEČIATKA KNIŽNICE", 200),
              _para_block("Ae ar? OF", 300), _para_block("Kapitola druhá", 400),
              Block("caption", 0, (100, 500, 900, 530), text="Obr. 1 Náčrt")]
    decisions = [RegionDecision("stamp", (90, 190, 1310, 240), "remove"),
                 RegionDecision("formula", (90, 290, 1310, 340), "reocr", "a^2+b^2=c^2"),
                 RegionDecision("section_header", (90, 390, 1310, 440), "keep"),
                 RegionDecision("picture", (90, 600, 900, 900), "as_image")]
    out, answers = apply_decisions(blocks, decisions, 0)
    assert [b.kind for b in out] == ["para", "formula", "heading", "caption", "figure"]
    assert all("PEČIATKA" not in b.text for b in out)
    assert out[2].text == "Kapitola druhá" and out[2].level == 3
    assert answers == {Job(0, Path(), (90, 290, 1310, 340), "formula").key: "a^2+b^2=c^2"}


def test_a_large_text_region_does_not_turn_headings_into_prose():
    heading = Block("heading", 0, (100, 100, 700, 140), text="1 VEKTORY", level=2)
    blocks = [heading, _para_block("Telo kapitoly.", 200), _para_block("Ďalší odsek.", 300)]
    out, _ = apply_decisions(blocks, [RegionDecision("text", (90, 90, 1310, 900))], 0)
    assert [b.kind for b in out] == ["heading", "para", "para"]
    out, _ = apply_decisions(out, [RegionDecision("text", (90, 95, 710, 145))], 0)
    assert [b.kind for b in out] == ["para", "para", "para"]       # a region drawn on it


def test_decisions_trim_only_the_lines_of_a_paragraph_under_a_region():
    lines = [Segment("text", t, (100, y, 1300, y + 30), t.split())
             for t, y in (("prvý riadok", 100), ("x = y + z", 140), ("tretí riadok", 180))]
    para = Block("para", 0, (100, 100, 1300, 210), text="…", segments=lines)
    out, answers = apply_decisions([para], [RegionDecision("formula", (90, 135, 1310, 175))], 0)
    assert [b.kind for b in out] == ["para", "formula"] and not answers   # no LaTeX: a crop
    assert [s.md for s in out[0].segments] == ["prvý riadok", "tretí riadok"]


def test_without_decisions_the_edition_is_built_as_before(tmp_path, monkeypatch):
    """``decisions=None`` keeps the CLI path: no apply_decisions call at all."""
    import evilflowers_books_digitalizer.reflow.edition as edition

    monkeypatch.setattr(edition, "apply_decisions",
                        lambda *a, **k: pytest.fail("decisions applied without being given"))
    monkeypatch.setattr(edition, "_pandoc_epub", lambda work, md, out, *a: out.write_text(md))
    (tmp_path / "book.hocr").write_text(hocr([[["Len obyčajný text knihy na strane."]]]))
    img = tmp_path / "p0.png"
    cv2.imwrite(str(img), np.full((2000, 1400), 255, np.uint8))
    report = build_edition([img], tmp_path / "book.hocr", tmp_path / "b.epub", tmp_path / "w",
                           {"title": "T"}, ReflowSettings(vlm=None))
    assert report.pages == 1 and "obyčajný" in (tmp_path / "b.epub").read_text()


def test_decisions_must_cover_every_page(tmp_path):
    (tmp_path / "book.hocr").write_text(hocr([[["a"]]]))
    with pytest.raises(ValueError, match="region-decision lists"):
        build_edition([tmp_path / "p0.png"], tmp_path / "book.hocr", tmp_path / "b.epub",
                      tmp_path / "w", {}, ReflowSettings(), decisions=[])


@needs_pandoc
def test_build_edition_honours_region_decisions(tmp_path):
    (tmp_path / "book.hocr").write_text(hocr([[
        ["Prvý odsek textu je tu a pokračuje ďalej."],     # y 120..150
        ["Pečiatka knižnice STU Bratislava"],              # y 190..220
        ["Tretí odsek ešte niečo hovorí o knihe."],        # y 260..290
    ]]))
    img = np.full((2000, 1400, 3), 255, np.uint8)
    cv2.rectangle(img, (150, 700), (550, 950), (0, 0, 0), 6)    # a drawing kept as a crop
    page = tmp_path / "p0.png"
    cv2.imwrite(str(page), img)
    decisions = [[RegionDecision("stamp", (90, 180, 1310, 230), "remove"),
                  RegionDecision("formula", (100, 400, 700, 470), "reocr", "a^2+b^2=c^2"),
                  RegionDecision("picture", (120, 680, 600, 980), "as_image")]]
    out = tmp_path / "book.epub"
    report = build_edition([page], tmp_path / "book.hocr", out, tmp_path / "work",
                           {"title": "Rozhodnutia", "language": "slk"},
                           ReflowSettings(vlm=None), decisions=decisions)
    md = (tmp_path / "work" / "book.md").read_text()
    assert "Pečiatka" not in md and "Prvý odsek" in md and "Tretí odsek" in md
    assert "$$a^2+b^2=c^2$$" in md and report.formulas == 1 and report.vlm_jobs == 0
    assert report.figures == 1 and "![Obrázok](assets/" in md
    with zipfile.ZipFile(out) as z:
        xhtml = " ".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml"))
    assert "<math" in xhtml and "Pečiatka" not in xhtml
