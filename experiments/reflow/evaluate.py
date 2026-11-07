"""Evaluate anchoring on inline/mixed segments: does the VLM drift Slovak, does anchoring fix it?

usage: evaluate.py <out_dir>   (reads report.json written by build.py)
"""
import json
import re
import sys
from pathlib import Path

SK_ONLY = "ľŕĺôäťĽŔĹÔÄŤ"
SHARED = "čšžňďČŠŽŇĎ"
FOREIGN = re.compile(r"[Ͱ-ϿЀ-ӿěřůłĚŘŮŁ]")
MATH = re.compile(r"(\\\(.+?\\\)|\\\[.+?\\\]|\$\$.+?\$\$|\$.+?\$)", re.S)


def prose(s: str) -> str:
    return MATH.sub(" ", s or "")


def counts(s: str) -> tuple[int, int, int]:
    p = prose(s)
    return sum(c in SK_ONLY for c in p), sum(c in SHARED for c in p), len(FOREIGN.findall(p))


rep = json.loads((Path(sys.argv[1]) / "report.json").read_text())
rows = rep["inline"]
tot = {"vlm": [0, 0, 0], "final": [0, 0, 0], "tess": [0, 0, 0]}
fixed = anchors = missing = 0
examples = []
for r in rows:
    for k in ("vlm", "final", "tess"):
        if k in r:
            for i, v in enumerate(counts(r[k])):
                tot[k][i] += v
    fixed += r.get("fixed", 0)
    anchors += r.get("anchors", 0)
    missing += r.get("missing", 0)
    if r.get("fixed") and len(examples) < 8:
        examples.append(r)

print(f"segments={len(rows)}  anchors={anchors}  tokens snapped to Tesseract={fixed}  "
      f"anchors missing from VLM={missing}  fallbacks={rep.get('fallbacks')}")
print(f"{'':6s} {'sk-only':>8s} {'shared':>7s} {'foreign':>8s}")
for k, (a, b, c) in tot.items():
    print(f"{k:6s} {a:8d} {b:7d} {c:8d}")
print("\nexamples (raw VLM -> anchored):")
for r in examples:
    print("  VLM  :", r["vlm"][:150].replace("\n", " "))
    print("  FINAL:", r["final"][:150].replace("\n", " "))
    print()
