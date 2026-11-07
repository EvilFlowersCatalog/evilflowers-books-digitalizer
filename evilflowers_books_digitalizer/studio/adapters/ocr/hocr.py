"""hOCR plumbing: one Tesseract page in, one IA-shaped multi-page document out.

IA keys pages on ``id="page_%06d"`` (leaf number) and expects one ``ocr_page`` per
leaf, with excluded leaves present as empty pages, so hocr-tools and BookReader
can line text up with images. Tesseract's per-image runs all call themselves
``page_1``/``line_1_3``; :func:`relabel_page` makes ids unique per leaf.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence

from lxml import etree
from lxml import html as lhtml

from evilflowers_books_digitalizer.reflow.model import parse_pages

_ID_NUM = re.compile(r"^([a-z]+)_\d+")
_PPAGENO = re.compile(r"ppageno \d+")


def extract_page(document: str) -> str:
    """The first ``ocr_page`` element of an hOCR document, as HTML text."""
    root = lhtml.fromstring(document.encode(), parser=lhtml.HTMLParser(encoding="utf-8"))
    pages = root.xpath("//*[@class='ocr_page']")
    if not pages:
        return ""
    return etree.tostring(pages[0], method="html", encoding="unicode").strip()


def page_text(page_html: str) -> tuple[str, float]:
    """Plain text (one line per OCR line) and mean word confidence (0..100)."""
    pages = parse_pages(page_html) if page_html else []
    if not pages:
        return "", 0.0
    lines = [ln.text for par in pages[0].pars for ln in par.lines]
    words = pages[0].words
    conf = sum(w.conf for w in words) / len(words) if words else 0.0
    return "\n".join(lines), conf


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
