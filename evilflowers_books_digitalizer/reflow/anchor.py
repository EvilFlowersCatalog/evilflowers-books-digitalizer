"""Anchor VLM prose to Tesseract: Tesseract `slk` is the diacritic authority.

The vision model reads math well but "rewrites" Slovak prose toward its
language priors (Greek look-alikes, Polish ł, Czech ě, dropped ť/ľ — see
``docs/reflow_edition.md`` §4). Every prose token it emits is therefore snapped
to the nearest high-confidence Tesseract word on the same line:

* **order-aware** — anchors are consumed left to right within a small window,
  so a correct ``polia`` never snaps to a ``pole`` two words further on;
* **fold-equal first** — a diacritic- or script-only difference (``funkcίου`` vs
  ``funkciou``, ``polá`` vs ``poľa``) is the drift signature and always snaps;
* **dictionary-arbitrated** — when only the VLM's word is real Slovak, it wins.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

#: inline/display math spans in VLM output; prose outside them is anchored
MATH = re.compile(r"(\\\(.+?\\\)|\\\[.+?\\\]|\$\$.+?\$\$|\$.+?\$)", re.S)
TOKEN = re.compile(r"[^\W\d_]{2,}")
#: letters that never occur in Slovak prose
FOREIGN = re.compile(r"[Ͱ-ϿЀ-ӿěřůłĚŘŮŁőűŐŰ]")
WORDLIKE = re.compile(
    r"^[\"„“(]*[A-Za-zÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽáäčďéíľĺňóôŕšťúýž]{3,}[.,;:!?)\"“]*$")

WINDOW = 4          # look-ahead over unconsumed anchors
MAX_RATIO = 0.34    # max edit distance / anchor length for a non-fold match

_CONFUSABLE = str.maketrans({
    "ο": "o", "υ": "u", "ί": "i", "ι": "i", "α": "a", "ε": "e", "ρ": "p", "ν": "v",
    "κ": "k", "τ": "t", "χ": "x", "ό": "o", "ύ": "u", "ά": "a", "έ": "e",
    "о": "o", "а": "a", "е": "e", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
    "к": "k", "ł": "l", "Ł": "L", "đ": "d",
})
_HACEK_APOS = re.compile(r"([tdlTDL])['’ʼ`](?=[\w\s.,;:!?)]|$)")
_HACEK = {"t": "ť", "d": "ď", "l": "ľ", "T": "Ť", "D": "Ď", "L": "Ľ"}


def fold(s: str) -> str:
    """Diacritic- and script-insensitive skeleton (``funkcίου`` == ``funkciou``)."""
    s = s.translate(_CONFUSABLE)
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c)).lower()


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def outside_math(text: str) -> str:
    return MATH.sub(" ", text)


def has_foreign(text: str) -> bool:
    return bool(FOREIGN.search(outside_math(text)))


def normalize_hacek(text: str) -> str:
    """Typewriter-style háček (``uskutočnit’``, ``opät'``) -> ť/ď/ľ, outside math."""
    return "".join(p if MATH.fullmatch(p or "") else
                   _HACEK_APOS.sub(lambda m: _HACEK[m[1]], p)
                   for p in MATH.split(text))


def _strip_punct(word: str) -> str:
    return re.sub(r"^[^\w]+|[^\w]+$", "", word)


class Lexicon:
    """Slovak hunspell oracle, queried once per book (``hunspell -l`` lists misspellings).

    Disabled (``ok()`` -> ``None``) when hunspell or the dictionary is missing;
    anchoring then simply prefers Tesseract.
    """

    def __init__(self, words: set[str], dictionary: str | Path | None) -> None:
        self.known: set[str] = set()
        self.enabled = False
        cand = sorted({w for w in words if len(w) >= 3 and w.isalpha()})
        dic = Path(dictionary).expanduser() if dictionary else None
        if not cand or dic is None or not dic.with_suffix(".dic").exists() \
                or shutil.which("hunspell") is None:
            return
        result = subprocess.run(["hunspell", "-d", str(dic), "-i", "utf-8", "-l"],
                                input="\n".join(cand), capture_output=True, text=True)
        if result.returncode == 0:
            bad = set(result.stdout.split())
            self.known = {w for w in cand if w not in bad}
            self.enabled = True

    def ok(self, word: str) -> bool | None:
        return (word in self.known) if self.enabled else None


@dataclass(slots=True)
class AnchorStats:
    fixed: int = 0
    kept_vlm: int = 0
    anchors: int = 0
    missing: int = 0


def anchor(vlm_text: str, anchors: list[str], lexicon: Lexicon | None = None
           ) -> tuple[str, AnchorStats]:
    """Snap VLM prose tokens onto Tesseract ``anchors`` (see module docstring)."""
    clean = [a for a in (_strip_punct(x) for x in anchors) if len(a) >= 3]
    stats = AnchorStats(anchors=len(clean))
    used: set[int] = set()
    pos = 0

    def fix(m: re.Match[str]) -> str:
        nonlocal pos
        tok = m[0]
        if len(tok) < 3:
            return tok
        ft = fold(tok)
        best_i, best_d = None, float("inf")
        for i in range(pos, min(len(clean), pos + WINDOW)):
            a = clean[i]
            if abs(len(a) - len(tok)) > 2:
                continue
            d = 0.0 if fold(a) == ft else levenshtein(ft, fold(a)) + 0.5
            if d < best_d:
                best_i, best_d = i, d
        if best_i is None or best_d / max(len(clean[best_i]), 1) > MAX_RATIO:
            return tok
        best = clean[best_i]
        used.add(best_i)
        pos = best_i + 1
        if best == tok:
            return tok
        if lexicon is not None and lexicon.ok(tok) is True and lexicon.ok(best) is False:
            stats.kept_vlm += 1
            return tok
        stats.fixed += 1
        return best

    text = normalize_hacek(vlm_text)
    out = "".join(p if MATH.fullmatch(p or "") else TOKEN.sub(fix, p) for p in MATH.split(text))
    stats.missing = sum(1 for i, a in enumerate(clean) if i not in used and a not in text)
    return out, stats


def trustworthy(text: str, stats: AnchorStats) -> bool:
    """Accept anchored VLM text only if it kept the words and left no foreign letters."""
    return stats.missing <= max(1, 0.25 * stats.anchors) and not has_foreign(text)
