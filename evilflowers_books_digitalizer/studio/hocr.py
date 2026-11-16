"""hOCR: the per-leaf pages ocr-service writes in, one IA-shaped multi-page document out.

* reading: page -> paragraph -> line -> word, with boxes, ``x_size`` and ``x_wconf``
  (:func:`parse_pages`); the title-page prefill (#15) ranks lines by these;
* writing: IA keys pages on ``id="page_%06d"`` (leaf number) and expects one ``ocr_page``
  per leaf, with excluded leaves present as empty pages, so hocr-tools and BookReader can
  line text up with images. A per-image OCR run calls itself ``page_1``/``line_1_3``;
  :func:`relabel_page` makes ids unique per leaf.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from lxml import etree
from lxml import html as lhtml

type Box = tuple[int, int, int, int]

_BBOX = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")
_WCONF = re.compile(r"x_wconf (\d+)")
_XSIZE = re.compile(r"x_size ([\d.]+)")
_LINE_XPATH = (
    ".//*[contains(@class,'ocr_line') or contains(@class,'ocr_header') "
    "or contains(@class,'ocr_caption') or contains(@class,'ocr_textfloat')]"
)
_ID_NUM = re.compile(r"^([a-z]+)_\d+")
_PPAGENO = re.compile(r"ppageno \d+")


# ---------------------------------------------------------------- reading
def bbox_of(title: str) -> Box:
    m = _BBOX.search(title)
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4])) if m else (0, 0, 0, 0)


@dataclass(slots=True)
class Word:
    text: str
    conf: int
    bbox: Box


@dataclass(slots=True)
class Line:
    words: list[Word]
    bbox: Box
    size: float  # hOCR x_size: glyph height in px (0 when the engine gave none)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]


@dataclass(slots=True)
class Par:
    lines: list[Line]
    bbox: Box

    @property
    def words(self) -> list[Word]:
        return [w for ln in self.lines for w in ln.words]


@dataclass(slots=True)
class Page:
    width: int
    height: int
    pars: list[Par] = field(default_factory=list)

    @property
    def words(self) -> list[Word]:
        return [w for p in self.pars for w in p.words]

    @property
    def lines(self) -> list[Line]:
        return [ln for p in self.pars for ln in p.lines]


def parse_pages(markup: bytes | str) -> list[Page]:
    """All ``ocr_page`` elements of an hOCR document, in order."""
    # explicit UTF-8: lxml falls back to Latin-1 for bytes without a charset <meta>
    data = markup.encode() if isinstance(markup, str) else markup
    if not data.strip():
        return []
    root = lhtml.fromstring(data, parser=lhtml.HTMLParser(encoding="utf-8"))
    return [_page(el) for el in root.xpath("//*[@class='ocr_page']")]


def _page(page_el: lhtml.HtmlElement) -> Page:
    _, _, w, h = bbox_of(page_el.get("title", ""))
    page = Page(w, h)
    for par_el in page_el.xpath(".//*[@class='ocr_par']"):
        lines: list[Line] = []
        for ln_el in par_el.xpath(_LINE_XPATH):
            words = []
            for w_el in ln_el.xpath(".//*[@class='ocrx_word']"):
                text = "".join(w_el.itertext()).strip()
                if text:
                    title = w_el.get("title", "")
                    m = _WCONF.search(title)
                    words.append(Word(text, int(m[1]) if m else 0, bbox_of(title)))
            if words:
                title = ln_el.get("title", "")
                ms = _XSIZE.search(title)
                lines.append(Line(words, bbox_of(title), float(ms[1]) if ms else 0.0))
        if lines:
            page.pars.append(Par(lines, bbox_of(par_el.get("title", ""))))
    return page


# ---------------------------------------------------------------- writing
def relabel_page(page_html: str, leaf: int) -> str:
    """Give a single-page hOCR fragment leaf-unique ids (``page_%06d``, ``line_%06d_N``)."""
    root = lhtml.fragment_fromstring(page_html, create_parent=False)
    for el in root.iter():
        el_id = el.get("id")
        if el_id:
            el.set("id", _ID_NUM.sub(lambda m: f"{m[1]}_{leaf:06d}", el_id))
    root.set("id", f"page_{leaf:06d}")
    title = root.get("title", "")
    root.set("title", _PPAGENO.sub(f"ppageno {leaf}", title) if "ppageno" in title
             else f"{title}; ppageno {leaf}".lstrip("; "))
    return etree.tostring(root, method="html", encoding="unicode").strip()


def empty_page(leaf: int, width: int, height: int) -> str:
    """Placeholder for a leaf without text (excluded or never OCR'd)."""
    return (f"<div class='ocr_page' id='page_{leaf:06d}' "
            f"title='bbox 0 0 {width} {height}; ppageno {leaf}'></div>")


def hocr_document(pages: Sequence[str], *, title: str = "", ocr_system: str = "tesseract") -> str:
    """Wrap page fragments into one XHTML hOCR document."""
    body = "\n".join(pages)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n<head>\n'
        f"<title>{html.escape(title)}</title>\n"
        '<meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>\n'
        f"<meta name='ocr-system' content='{html.escape(ocr_system)}'/>\n"
        "<meta name='ocr-capabilities' content='ocr_page ocr_carea ocr_par ocr_line ocrx_word'/>\n"
        f"</head>\n<body>\n{body}\n</body>\n</html>\n")
