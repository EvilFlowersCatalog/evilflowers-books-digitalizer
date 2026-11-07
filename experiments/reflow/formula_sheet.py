"""Side-by-side sheet: scan crop | LaTeX re-rendered, for a visual pass/fail check of VLM formulas.

usage: formula_sheet.py <bookdir> <n> <offset>
"""
import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

book = Path(sys.argv[1])
n, off = int(sys.argv[2]), int(sys.argv[3])
cache = json.loads((book / "vlm_cache.json").read_text())
items = [(k, v) for k, v in cache.items() if v["task"] == "formula" and v["text"].strip() and "_formula_" in k]
items = items[off:off + n]
tmp = book / "fsheet"
tmp.mkdir(exist_ok=True)
rows = []
for k, v in items:
    m = re.match(r"p(\d+)_formula_(\d+)_(\d+)_(\d+)_(\d+)", k)
    pno, x0, y0, x1, y1 = (int(g) for g in m.groups())
    page = next((book / "pages").glob(f"p{pno:03d}.*"))
    crop = Image.open(page).convert("RGB").crop((x0 - 10, y0 - 10, x1 + 10, y1 + 10))
    tex = v["text"].strip()
    tex = re.sub(r"^\\\[|\\\]$", "", tex).replace("align*", "aligned")
    doc = ("\\documentclass{article}\\usepackage[paperwidth=30cm,paperheight=8cm,margin=5mm]{geometry}"
           "\\usepackage{amsmath,amssymb}\\pagestyle{empty}"
           "\\begin{document}\\Large$\\displaystyle " + tex + "$\\end{document}")
    tf = tmp / f"{k}.tex"
    tf.write_text(doc)
    r = subprocess.run(["xelatex", "-interaction=batchmode", "-output-directory", str(tmp), str(tf)],
                       capture_output=True, text=True)
    pdf = tf.with_suffix(".pdf")
    if r.returncode == 0 and pdf.exists():
        subprocess.run(["mutool", "draw", "-r", "150", "-o", str(tmp / f"{k}.png"), str(pdf), "1"],
                       capture_output=True)
        rend = Image.open(tmp / f"{k}.png").convert("RGB")
        from PIL import ImageOps
        bb = ImageOps.invert(rend.convert("L")).getbbox()
        if bb:
            rend = rend.crop((max(0, bb[0] - 6), max(0, bb[1] - 6), bb[2] + 6, bb[3] + 6))
    else:
        rend = Image.new("RGB", (400, 60), "white")
        ImageDraw.Draw(rend).text((5, 20), "LaTeX COMPILE FAILED", fill="red")
    rows.append((k, crop, rend))

W = max(c.width for _, c, _ in rows) + max(r.width for _, _, r in rows) + 40
H = sum(max(c.height, r.height) + 30 for _, c, r in rows)
sheet = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(sheet)
y = 0
cw = max(c.width for _, c, _ in rows)
for k, c, r in rows:
    d.text((4, y + 2), k, fill="gray")
    sheet.paste(c, (0, y + 16))
    sheet.paste(r, (cw + 40, y + 16))
    y += max(c.height, r.height) + 30
sheet.thumbnail((1600, 2400))
out = book / f"formula_sheet_{off}.png"
sheet.save(out)
print(out, len(rows))
