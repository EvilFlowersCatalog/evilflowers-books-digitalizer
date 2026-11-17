"""Hand labels for decision-model calibration (evilflowers-books-digitalizer#7): pure rules.

The label sets are the *exact keys* the callers send to the decider, so a fitted
threshold maps one to one onto a caller's answer:

* ``figure_kind``: evilflowers-image-service#6 (``decider-vision`` choice ``kind``)
* ``is_data_table`` / ``table_quality``: evilflowers-text-service#20
* ``section_relation``: the catalog's edge sweep, EvilFlowersCatalog#190 (choice ``relation``,
  section A then section B, in the sweep's orientation)
* ``subject``: EvilFlowersCatalog#191, a choice among the catalog's own categories; the label is
  the category id (the set differs per catalog, so it is not fixed here)
* ``scan_mark``: the studio's own book-wide mark groups (#14, decider-vision question
  ``scan_mark``, scan-service#2): one crop per group, not one per occurrence
* ``title_page``: the title-page prefill (#15). Not labelled on a screen: every metadata form an
  operator confirms is a labelled case per field (accepted / changed / none), exported in the
  same JSONL

Every task also accepts :data:`SKIP`: stored, never exported as a label.
No I/O here; the application layer (``studio.application.labelling``) stores and serves.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

SKIP = "skip"
#: share of labelled items that a second person also labels (Cohen's kappa in the summary)
SECOND_LABELLER_SHARE = 0.1
#: who produced a label (#16): a person, or a model labelling on its own
HUMAN, AGENT = "human", "agent"
LABELLER_KINDS = (HUMAN, AGENT)
#: share of agent-labelled items a person also answers (the spot check, #16)
AGENT_SPOT_CHECK_SHARE = 0.1
#: agent<->human kappa from which knowledge#65 may fit thresholds on agent labels (proposal, #16)
AGENT_KAPPA_MIN = 0.7
#: characters of section text shown per side: what the edge sweep sends (EvilFlowersCatalog#190)
SECTION_TEXT_CHARS = 1500


class ItemKind(StrEnum):
    FIGURE = "figure"
    TABLE = "table"
    SECTION_PAIR = "section_pair"
    ENTRY = "entry"
    MARK_GROUP = "mark_group"          # a studio book's mark group (studio DB, not the catalog)
    BOOK = "book"                      # a studio book's confirmed metadata form


#: item kinds sampled from the studio's own database (the rest come from the catalog)
STUDIO_KINDS = (ItemKind.MARK_GROUP,)


@dataclass(frozen=True, slots=True)
class LabelTask:
    name: str
    item_kind: ItemKind
    #: allowed answers besides ``skip``; empty = the item carries its own options (``subject``)
    labels: tuple[str, ...]
    #: non-skip labels the calibration needs ("Done when" of #7)
    target: int
    title: str
    #: answered on the screen of another task (same item), never asked alone first
    companion_of: str | None = None
    #: labelled by hand on an item (False: collected elsewhere, e.g. the metadata form)
    by_hand: bool = True

    def accepts(self, value: str, options: Iterable[str] = ()) -> bool:
        return value == SKIP or value in (self.labels or tuple(options))


TASKS: dict[str, LabelTask] = {t.name: t for t in (
    LabelTask("figure_kind", ItemKind.FIGURE,
              ("chart", "diagram", "photo", "map", "table", "formula", "stamp", "decoration", "scan",
               "blank"), 200, "What is this image?"),
    LabelTask("is_data_table", ItemKind.TABLE, ("yes", "no"), 200,
              "Is this a data table (not a ToC, a form or page layout)?"),
    LabelTask("table_quality", ItemKind.TABLE, ("correct", "minor_errors", "broken"), 100,
              "Is the markdown a faithful copy of the table?", companion_of="is_data_table"),
    LabelTask("section_relation", ItemKind.SECTION_PAIR, ("same", "pre", "example", "loose", "none"),
              200, "How is section B related to section A?"),
    LabelTask("subject", ItemKind.ENTRY, (), 200, "Which subject (UDC category) is this book?"),
    LabelTask("scan_mark", ItemKind.MARK_GROUP,
              ("stamp", "sticker", "bleed_through", "edge", "content"), 200,
              "Is this mark part of the printed page, or something added to or around it?"),
    LabelTask("title_page", ItemKind.BOOK, ("accepted", "changed", "none"), 200,
              "Did the title-page suggestion hold (per field, from confirmed metadata forms)?",
              by_hand=False),
)}
#: tasks a person answers on an item (SPA, CLI, MCP); ``sample-labels`` / ``label`` take these
SAMPLED_TASKS: dict[str, LabelTask] = {k: t for k, t in TASKS.items() if t.by_hand}

#: what each label means, as the callers' question criteria word it
LABEL_HINTS: dict[str, str] = {
    "table": "table drawn as an image", "stamp": "stamp, signature or logo", "scan": "scanned full page",
    "no": "ToC, form or page layout", "minor_errors": "a few wrong cells", "broken": "unusable",
    "same": "explains the same idea", "pre": "A is a prerequisite of B", "example": "B is an example of A",
    "loose": "loosely related", "none": "unrelated",
    "sticker": "sticker or label: call number, barcode", "bleed_through": "shows through from the other side",
    "edge": "scan edge, finger, shadow", "content": "part of the printed page",
}


#: Slovak (and Czech) words the corpus prints in captions and headings, for a reader without them
SLOVAK_GLOSSARY: dict[str, str] = {
    "Obr. / Obrázok (cs Obrázek)": "Figure (used for every kind of figure: do not read the kind from it)",
    "Tab. / Tabuľka (cs Tabulka)": "Table", "Graf": "chart / plot", "Schéma": "diagram, schematic",
    "Mapa": "map", "Fotografia / Foto / Snímka": "photograph", "Rovnica / Vzorec": "equation / formula",
    "Príklad / Úloha / Cvičenie": "example / problem / exercise", "Definícia / Veta / Dôkaz":
    "definition / theorem / proof", "Kapitola / Úvod / Záver / Obsah / Register / Literatúra":
    "chapter / introduction / conclusion / table of contents / index / bibliography",
}

#: Everything an agent needs to answer a task as a careful person would (#16). The label keys
#: and the short criteria are the deciders' own (studio.md section 8, knowledge Decision-Models);
#: the definitions, rules and skip cases spell them out for a reader who only sees the item.
LABEL_GUIDES: dict[str, dict[str, object]] = {
    "figure_kind": {
        "look_at": "the crop image first; then payload.printed_caption (the caption printed in the "
                   "book, may be empty), payload.page and payload.page_text (OCR text of that page, "
                   "to see what the figure illustrates). Decide what the crop IS, not what the "
                   "caption calls it: Slovak captions say 'Obr.' for every kind of figure.",
        "labels": {
            "chart": "data plotted against axes or as proportions: line, bar, column, pie, scatter, "
                     "histogram, box plot, a plotted function y=f(x) with axes and ticks, a nomogram",
            "diagram": "a drawn schematic of structure or process: flowchart, block or circuit diagram, "
                       "UML, graph/tree of nodes and edges, technical or engineering drawing, geometric "
                       "construction, labelled anatomical or mechanical drawing, any line art that "
                       "explains (not decorates)",
            "photo": "a photograph of a real object, scene or person, including micrographs, X-rays, "
                     "screenshots of photographic content and halftone reproductions of photos",
            "map": "a geographic or cartographic depiction: country, city, terrain, site plan with "
                   "geographic layout, star chart",
            "table": "a table drawn as an image: a grid of rows and columns of text or numbers that "
                     "the extraction cut out as a picture",
            "formula": "one or more mathematical, chemical or physical expressions rendered as an "
                       "image (equations, matrices, reaction schemes written as formulas)",
            "stamp": "a stamp, seal, signature, publisher or institution logo, emblem, library "
                     "ownership mark: added identification, not content of the text",
            "decoration": "an ornament, vignette, border, divider rule, decorative initial, background "
                          "pattern or icon that carries no information of its own",
            "scan": "the crop is (nearly) the whole scanned page or a large block of ordinary text "
                    "captured as an image, not a figure on the page",
            "blank": "nothing recognisable: an empty or white area, uniform grey, scanner noise, a "
                     "tiny speck",
        },
        "rules": [
            "Choose the kind that covers most of the crop's area and purpose. A chart with a small "
            "inset photo is a chart; a photo with arrows and labels drawn on it is still a photo.",
            "Data values on axes make it a chart; arrows and boxes that explain a process make it a "
            "diagram. A geometric figure (triangle, circle constructions) is a diagram, a function "
            "plot with a scale is a chart.",
            "A crop that is a table of numbers is 'table' even if the caption says Obr.",
            "Several small formulas without a figure around them are 'formula'.",
            "A logo or emblem on a title page or cover is 'stamp', a repeated ornament under a "
            "chapter heading is 'decoration'.",
        ],
        "skip_when": [
            "the crop is cut so that its kind cannot be told",
            "two labels fit equally well after applying the rules",
            "the image is too small or too blurred to see what it shows (see image.width/height; "
            "under about 200 px on the long side be especially careful)",
        ],
    },
    "is_data_table": {
        "look_at": "the crop image of the table region; payload.markdown is the extraction's text "
                   "copy and may be wrong, so decide is_data_table from the IMAGE, not the markdown.",
        "labels": {
            "yes": "a data table: rows and columns of values (numbers, measurements, parameters, "
                   "comparisons, records), usually with a header row or column; a two-column "
                   "key/value table of data also counts",
            "no": "not a data table: a table of contents, index, list of references, a form with "
                  "empty fields, page layout or multi-column running text detected as a grid, a "
                  "header/footer, a figure, a matrix or formula, a single line of text",
        },
        "rules": [
            "Ruling lines alone do not make a data table; the content must be tabular data.",
            "A timetable, a price list or a chemical property table is 'yes'; a ToC with page "
            "numbers is 'no' even though it has two columns.",
        ],
        "skip_when": ["the crop is unreadable or shows only a fragment whose content cannot be told"],
        "companion": "table_quality",
    },
    "table_quality": {
        "look_at": "compare payload.markdown cell by cell with the crop image. Answer ONLY for an "
                   "item you labelled is_data_table = yes; leave it out otherwise.",
        "labels": {
            "correct": "the markdown is a faithful copy: same rows and columns, every cell's text "
                       "and numbers match (differences in whitespace, bold or how a merged header "
                       "is spread are fine)",
            "minor_errors": "the structure is right but a few cells are wrong, missing, merged or "
                            "split, or diacritics (č, ť, ľ, ô, ä …) are lost in a few cells; the table "
                            "is still usable",
            "broken": "unusable: columns shifted or swapped, rows merged, many cells wrong or empty, "
                      "numbers changed, garbled text, or the markdown is empty",
        },
        "rules": [
            "Check numbers digit by digit, including decimal commas (Slovak uses 3,14).",
            "A wrong number is worse than a lost diacritic: two or more changed numbers make it "
            "'broken' unless the table is large and they are isolated.",
        ],
        "skip_when": ["the crop is too small or blurred to read the cells (leave table_quality out)"],
    },
    "section_relation": {
        "look_at": "payload.a and payload.b: each has the book title (entry_title), the section "
                   "title, its pages and the first paragraphs of its text (up to 1500 characters). "
                   "The relation is DIRECTED: read it as 'section B is ... of section A'.",
        "labels": {
            "same": "B explains the same idea as A: largely the same concept, definition or method, "
                    "so one could stand in for the other",
            "pre": "A is a prerequisite of B: B builds on what A introduces, and a reader should "
                   "read A first",
            "example": "B is an example of A: B applies, illustrates or exercises the concept A "
                       "explains (worked example, problem set, case study)",
            "loose": "loosely related: same broad field or topic, but neither explains, requires "
                     "nor exemplifies the other",
            "none": "unrelated: different topics",
        },
        "rules": [
            "Judge from the texts, not from the titles alone; titles like 'Úvod' or 'Introduction' "
            "say nothing.",
            "If B is a prerequisite of A (the reverse direction) or A is an example of B, answer "
            "'loose': only the A->B direction of pre and example is a label.",
            "Two sections in different languages can still be 'same'.",
        ],
        "skip_when": ["either text is too short or garbled to tell what it is about",
                      "the text is mostly a table of contents, references or formulas"],
    },
    "subject": {
        "look_at": "payload.title, payload.summary and payload.table_of_contents of the book; the "
                   "allowed answers are the category ids in options (each with its UDC term and a "
                   "label). The answer is the option's id, never the term.",
        "labels": {},
        "rules": [
            "UDC: the first digit of the term is the top-level class (0 general/computing, 1 "
            "philosophy/psychology, 2 religion, 3 social sciences/law/economics/education, 5 "
            "mathematics/natural sciences, 6 applied sciences/medicine/engineering/agriculture, 7 "
            "arts/sport, 8 language/literature, 9 geography/history).",
            "Pick the most specific option that describes the main subject of the whole book, not "
            "one chapter. When the specific class is not offered, pick its top-level option.",
            "An engineering textbook on a mathematical method is filed under the field it teaches "
            "(the ToC tells which).",
        ],
        "skip_when": ["the title, summary and ToC do not say enough to choose",
                      "no option fits the book"],
    },
    "scan_mark": {
        "look_at": "the crop of one mark (its clearest occurrence); payload.title is the book, "
                   "payload.leaf the page, payload.leaves and payload.occurrences how often the same "
                   "mark repeats across the book. A mark on many leaves is more likely added "
                   "(stamp, sticker) or a scan artefact (edge) than content.",
        "labels": {
            "stamp": "a stamp, signature or logo added to the page (library stamp, ownership seal, "
                     "inventory number written by hand)",
            "sticker": "a sticker or label stuck on the page: call number, barcode, RFID or "
                       "shelf label",
            "bleed_through": "ink showing through from the other side of the leaf (mirrored, faint "
                             "text or image)",
            "edge": "a scan artefact at the border: page edge, book gutter shadow, finger, clamp, "
                    "dark band or binding",
            "content": "part of the printed page itself: text, figure, ornament, page number, "
                       "printed header",
        },
        "rules": [
            "Printed content repeated on every page (a running header, a page frame) is 'content'.",
            "Mirrored or faint text is 'bleed_through' even when it repeats.",
        ],
        "skip_when": ["the crop is too small or blurred to tell what the mark is"],
    },
}


def agent_guide(name: str) -> dict[str, object]:
    """The task as an agent sees it: question, label definitions, rules, when to skip (#16)."""
    t = task(name)
    guide = dict(LABEL_GUIDES.get(t.name, {}))
    labels = guide.pop("labels", {}) or {}
    assert isinstance(labels, dict)
    return {"task": t.name, "question": t.title, "item_kind": str(t.item_kind),
            "labels": {k: labels.get(k) or LABEL_HINTS.get(k) or k for k in t.labels},
            "skip": f"answer {SKIP!r} when unsure; a skip is stored, never exported as a label",
            **guide}


def task(name: str) -> LabelTask:
    try:
        return TASKS[name]
    except KeyError:
        raise KeyError(f"unknown labelling task {name!r} (known: {', '.join(TASKS)})") from None


def tasks_for(kind: ItemKind | str) -> list[LabelTask]:
    return [t for t in TASKS.values() if t.item_kind == kind]


# ---------------------------------------------------------------- keys
_REGION_ABBR = {"figure": "fig", "table": "tab", "equation": "eq"}


def region_key(kind: str, page: int, seq: int) -> str:
    """``p012-fig01``: the catalog returns no region key (EvilFlowersCatalog AdminRegion has no
    ``locator_key``), so it is built from the 1-based PDF page and the region's ``seq``."""
    return f"p{page:03d}-{_REGION_ABBR.get(kind, kind[:3])}{seq:02d}"


def item_key(kind: ItemKind | str, *parts: str) -> str:
    """Unique per catalog item; a section pair is the same item in either orientation."""
    if kind == ItemKind.SECTION_PAIR:
        parts = tuple(sorted(parts))
    return ":".join((str(kind), *parts))


def pair_language(a: str | None, b: str | None) -> str | None:
    if a == b:
        return a
    return "mixed" if a and b else (a or b)


def udc_top(term: str | None) -> str:
    """Top level of a UDC class: its first digit (``004.8`` -> ``0``), as the catalog graph's
    ``field``; categories are flat in the catalog, so the tree is derived from the term."""
    for ch in unicodedata.normalize("NFKC", term or ""):
        if ch.isdigit():
            return ch
    return "?"


# ---------------------------------------------------------------- sampling
def stratum(language: str | None, band: str | None) -> str:
    return f"{language or '??'}/{band or 'none'}"


def confidence_bucket(band: object, p: object) -> str | None:
    """The sampling bucket of the model's answer: its band when the catalog has one, else
    the raw probability in three steps. Bands stay empty until knowledge#65 sets thresholds
    (from these very labels), so on dev the unsure cases are found through ``p``."""
    if band:
        return str(band)
    if isinstance(p, (int, float)):
        return "p<0.5" if p < 0.5 else "p<0.8" if p < 0.8 else "p>=0.8"
    return None


def stratified(groups: Mapping[str, Sequence[str]], n: int) -> list[str]:
    """Take ``n`` keys round-robin over the strata (each already shuffled), so small strata,
    such as Slovak items or the decider's ``review`` band, are not drowned by a big one."""
    queues = {s: list(keys) for s, keys in sorted(groups.items()) if keys}
    out: list[str] = []
    while queues and len(out) < n:
        for s in list(queues):
            out.append(queues[s].pop(0))
            if not queues[s]:
                del queues[s]
            if len(out) >= n:
                break
    return out


# ---------------------------------------------------------------- agreement
def cohens_kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    """Cohen's kappa of two raters over ``(a, b)`` answers; None when undefined.

    With fewer than two items, or when chance agreement is 1 (both always gave one and the
    same answer), kappa is not defined; the summary then reports raw agreement only.
    """
    n = len(pairs)
    if n < 2:
        return None
    observed = sum(a == b for a, b in pairs) / n
    first = Counter(a for a, _ in pairs)
    second = Counter(b for _, b in pairs)
    expected = sum(first[k] * second.get(k, 0) for k in first) / (n * n)
    if expected >= 1.0:
        return None
    return (observed - expected) / (1.0 - expected)
