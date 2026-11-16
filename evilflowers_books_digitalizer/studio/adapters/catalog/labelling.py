"""``LabelSource`` over the EvilFlowers Catalog REST API: read-only, GET only.

Contract read on EvilFlowersCatalog ``develop`` 709b712 (2026-11-16), the branch dev runs:

* ``GET entries?extraction_status=available`` — paginated ``{items, metadata{pages}}``;
  an entry has ``id, catalog_id, title, summary, language{alpha2}, categories[{id, term, label}]``
* ``GET entries/{id}/extraction/regions?kind=figure|table`` — ``{response: {run_id, items}}``;
  needs *manage* rights. A region: ``id, run_id, kind, page, seq, bbox, caption,
  printed_caption, description, category, markdown, band, p, decision, decider_model``
* ``GET entries/{id}/extraction/regions/{rid}/image`` — the crop (webp), any run, manage
* ``GET entries/{id}/extraction/paragraphs?pdf_page=`` — ``{items[{page, section, text}]}``
* ``GET entries/{id}/chapters`` — ``{sections[{key, title, first_page, last_page, topical}],
  links[{source, target, links}]}``
* ``GET sections/{key}/related`` — ``[{key, entry_id, title, first_page, score, via,
  relation, relation_p}]`` (cross-book; pairs banded ``hide`` are dropped by the catalog)
* ``GET catalogs/{cid}/entries/{eid}`` — detail with ``table_of_contents``
* ``GET categories?catalog_id=`` — flat ``{id, term, label{lang: text}}``

What a labeller must not see (the model's answer and model-written text) goes to
``hidden``: ``band, p, decision, decider_model``, the section ``relation``/``relation_p``,
the region ``description``/``category``, and an entry's current ``categories``.
Gaps (no section text route, no unfiltered pair listing, no region key) are written up in
docs/design/catalog-labelling-requests.md.
"""

from __future__ import annotations

import logging
import os
import random
from collections.abc import Iterator, Mapping
from typing import Any

import httpx

from evilflowers_books_digitalizer.studio.config import LabellingConfig
from evilflowers_books_digitalizer.studio.labelling import (
    SECTION_TEXT_CHARS,
    ItemKind,
    item_key,
    pair_language,
    region_key,
)
from evilflowers_books_digitalizer.studio.ports import ImageBytes, LabelCandidate, PortError

log = logging.getLogger(__name__)

#: region fields that are a model's answer or model-written text: never shown to a labeller
_REGION_HIDDEN = ("band", "p", "score", "decision", "decider_model", "description", "category")
#: page text shown around a figure (the catalog has no page render)
_PAGE_TEXT_CHARS = 1200
#: related sections asked per entry (bounds the requests a section sample costs)
_SECTIONS_PER_ENTRY = 4
_RELATED_LIMIT = 10
#: preferred label languages of a category (the corpus is Slovak first)
_LABEL_LANGS = ("sk", "en")


class CatalogLabelSource:
    """Reads candidates from one catalog; the API key never leaves this process."""

    def __init__(self, config: LabellingConfig, *, client: httpx.Client | None = None) -> None:
        self._config = config
        if client is None:
            key = os.environ.get(config.api_key_env, "")
            if not key:
                raise PortError(f"labelling: ${config.api_key_env} is empty (a catalog API key "
                                "with manage rights on the sampled entries)")
            client = httpx.Client(base_url=config.base_url + "/", timeout=config.timeout,
                                  headers={"Authorization": f"Bearer {key}"})
        self._http = client
        self._categories: dict[str, list[dict[str, Any]]] = {}
        #: GETs the catalog answered 403 (entries the key cannot manage), since construction
        self.refused = 0

    # ------------------------------------------------------------ transport (GET only)
    def _get(self, path: str, **params: Any) -> Any:
        try:
            resp = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as exc:
            raise PortError(f"catalog GET {path}: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code == 403:
            # /entries is visibility-filtered, but the extraction routes need manage rights per
            # entry: one refused entry is skipped, not the whole sample
            self.refused += 1
            log.warning("labelling: catalog refused GET %s (403); entry skipped", path)
            return None
        if resp.status_code >= 400:
            raise PortError(f"catalog GET {path}: HTTP {resp.status_code}")
        data = resp.json()
        return data["response"] if isinstance(data, dict) and set(data) == {"response"} else data

    def _entries(self, max_entries: int) -> Iterator[dict[str, Any]]:
        """Entries with an active extraction run, shuffled page order (seeded), up to ``max_entries``."""
        catalogs: list[str | None] = list(self._config.catalog_ids) or [None]
        seen = 0
        for catalog_id in catalogs:
            first = self._get("entries", extraction_status="available", catalog_id=catalog_id,
                              limit=50, page=1) or {}
            pages = int((first.get("metadata") or {}).get("pages") or 1)
            order = list(range(1, pages + 1))
            random.Random(self._config.seed).shuffle(order)
            for page in order:
                data = first if page == 1 else (self._get(
                    "entries", extraction_status="available", catalog_id=catalog_id, limit=50,
                    page=page) or {})
                for entry in data.get("items", []):
                    yield entry
                    seen += 1
                    if seen >= max_entries:
                        return

    # ------------------------------------------------------------ LabelSource
    def candidates(self, kind: str, *, max_entries: int) -> Iterator[LabelCandidate]:
        match kind:
            case ItemKind.FIGURE | ItemKind.TABLE:
                yield from self._regions(kind, max_entries)
            case ItemKind.SECTION_PAIR:
                yield from self._section_pairs(max_entries)
            case ItemKind.ENTRY:
                yield from self._subject_entries(max_entries)
            case _:
                raise PortError(f"labelling: unknown item kind {kind!r}")

    def complete(self, candidate: LabelCandidate) -> LabelCandidate:
        match candidate.kind:
            case ItemKind.FIGURE | ItemKind.TABLE:
                page = candidate.payload.get("page")
                if isinstance(page, int):
                    candidate.payload["page_text"] = self._page_text(str(candidate.ref["entry_id"]), page)
            case ItemKind.SECTION_PAIR:
                for side in ("a", "b"):
                    sec = candidate.payload[side]
                    assert isinstance(sec, dict)
                    sec["text"] = self._section_text(sec)
            case ItemKind.ENTRY:
                detail = self._get(f"catalogs/{candidate.ref['catalog_id']}/entries/"
                                   f"{candidate.ref['entry_id']}") or {}
                candidate.payload["table_of_contents"] = _toc_lines(detail.get("table_of_contents"))
                candidate.payload["options"] = self._category_options(str(candidate.ref["catalog_id"]))
        return candidate

    def image(self, ref: Mapping[str, object]) -> ImageBytes:
        path = f"entries/{ref['entry_id']}/extraction/regions/{ref['region_id']}/image"
        try:
            resp = self._http.get(path)
        except httpx.HTTPError as exc:
            raise PortError(f"catalog GET {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise PortError(f"catalog GET {path}: HTTP {resp.status_code}")
        return ImageBytes(resp.content, resp.headers.get("content-type", "image/webp"))

    # ------------------------------------------------------------ figures and tables
    def _regions(self, kind: str, max_entries: int) -> Iterator[LabelCandidate]:
        for entry in self._entries(max_entries):
            data = self._get(f"entries/{entry['id']}/extraction/regions", kind=str(kind)) or {}
            for r in data.get("items", []):
                key = region_key(r["kind"], int(r["page"]), int(r.get("seq") or 0))
                payload: dict[str, object] = {
                    "entry_title": entry.get("title"), "page": r["page"], "region": key,
                    "bbox": r.get("bbox"), "printed_caption": r.get("printed_caption")}
                if kind == ItemKind.TABLE:
                    payload["markdown"] = r.get("markdown") or ""
                yield LabelCandidate(
                    kind=str(kind), key=item_key(kind, str(entry["id"]), str(r["id"])),
                    ref={"entry_id": str(entry["id"]), "run_id": str(r.get("run_id") or data.get("run_id")),
                         "region": key, "region_id": str(r["id"])},
                    language=_language(entry), payload=payload,
                    hidden={k: r.get(k) for k in _REGION_HIDDEN if r.get(k) is not None})

    def _page_text(self, entry_id: str, page: int) -> str:
        data = self._get(f"entries/{entry_id}/extraction/paragraphs", pdf_page=page, limit=100) or {}
        return _clip(" ".join(p.get("text", "") for p in data.get("items", [])), _PAGE_TEXT_CHARS)

    # ------------------------------------------------------------ section pairs
    def _section_pairs(self, max_entries: int) -> Iterator[LabelCandidate]:
        rng = random.Random(self._config.seed)
        for entry in self._entries(max_entries):
            chapters = self._get(f"entries/{entry['id']}/chapters") or {}
            sections = {s["key"]: s for s in chapters.get("sections", []) if s.get("key")}
            lang = _language(entry)
            for link in chapters.get("links", []):           # intra-book pairs
                a, b = sections.get(link.get("source")), sections.get(link.get("target"))
                if a and b:
                    yield _pair(entry, a, entry, b, lang, lang, {"via": "intra", "links": link.get("links")})
            topical = [s for s in sections.values() if s.get("topical", True)]
            rng.shuffle(topical)
            for a in topical[:_SECTIONS_PER_ENTRY]:               # cross-book pairs
                for hit in self._get(f"sections/{a['key']}/related", limit=_RELATED_LIMIT) or []:
                    other = {"id": hit["entry_id"], "title": None, "language": None}
                    b = {"key": hit["key"], "title": hit.get("title"), "first_page": hit.get("first_page")}
                    yield _pair(entry, a, other, b, lang, None,
                                {k: hit.get(k) for k in ("score", "via", "relation", "relation_p")})

    def _section_text(self, sec: dict[str, Any]) -> str:
        """The section's first paragraphs: paragraphs whose heading is the section's title,
        from its first page on. Workaround until the catalog serves section text by key."""
        entry_id, title = sec.get("entry_id"), (sec.get("title") or "").strip()
        first, last_raw = sec.get("first_page"), sec.get("last_page")
        if not (entry_id and title and isinstance(first, int)):
            return ""
        last = last_raw if isinstance(last_raw, int) else first + 3
        parts: list[str] = []
        for page in range(first, min(last, first + 3) + 1):
            data = self._get(f"entries/{entry_id}/extraction/paragraphs", pdf_page=page, limit=100) or {}
            parts += [p.get("text", "") for p in data.get("items", [])
                      if (p.get("section") or "").strip() == title]
            if sum(map(len, parts)) >= SECTION_TEXT_CHARS:
                break
        return _clip("\n\n".join(parts), SECTION_TEXT_CHARS)

    # ------------------------------------------------------------ subjects
    def _subject_entries(self, max_entries: int) -> Iterator[LabelCandidate]:
        for entry in self._entries(max_entries):
            yield LabelCandidate(
                kind=ItemKind.ENTRY, key=item_key(ItemKind.ENTRY, str(entry["id"])),
                ref={"entry_id": str(entry["id"]), "catalog_id": str(entry.get("catalog_id"))},
                language=_language(entry),
                payload={"title": entry.get("title"), "summary": entry.get("summary") or ""},
                hidden={"categories": [c.get("id") for c in entry.get("categories") or []]})

    def _category_options(self, catalog_id: str) -> list[dict[str, Any]]:
        if catalog_id not in self._categories:
            data = self._get("categories", catalog_id=catalog_id, paginate="false")
            items = data.get("items", []) if isinstance(data, dict) else (data or [])
            self._categories[catalog_id] = sorted(
                ({"id": str(c["id"]), "term": c.get("term") or "", "label": _label(c)} for c in items),
                key=lambda c: c["term"])
        return self._categories[catalog_id]


# ---------------------------------------------------------------- helpers
def _language(entry: Mapping[str, Any]) -> str | None:
    lang = entry.get("language") or {}
    code = lang.get("alpha2") if isinstance(lang, dict) else None
    return str(code).lower() if code else None


def _label(category: Mapping[str, Any]) -> str:
    label = category.get("label")
    if isinstance(label, dict):
        for lang in _LABEL_LANGS:
            if label.get(lang):
                return str(label[lang])
        return str(next(iter(label.values()), "") or category.get("term") or "")
    return str(label or category.get("term") or "")


def _toc_lines(toc: Any) -> list[str]:
    """The catalog's ToC is an untyped list: strings, or objects with a title/label."""
    out: list[str] = []
    for item in toc or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            text = item.get("title") or item.get("label") or item.get("name") or ""
            if text:
                out.append(("  " * int(item.get("level") or 0)) + str(text))
    return out[:80]


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def _pair(entry_a: Mapping[str, Any], a: Mapping[str, Any], entry_b: Mapping[str, Any],
          b: Mapping[str, Any], lang_a: str | None, lang_b: str | None,
          hidden: dict[str, object]) -> LabelCandidate:
    def side(entry: Mapping[str, Any], sec: Mapping[str, Any]) -> dict[str, object]:
        return {"key": sec["key"], "entry_id": str(entry["id"]), "entry_title": entry.get("title"),
                "title": sec.get("title"), "first_page": sec.get("first_page"),
                "last_page": sec.get("last_page"), "text": ""}

    return LabelCandidate(
        kind=ItemKind.SECTION_PAIR, key=item_key(ItemKind.SECTION_PAIR, a["key"], b["key"]),
        ref={"entry_id": str(entry_a["id"]), "section_a": a["key"], "section_b": b["key"],
             "entry_b_id": str(entry_b["id"])},
        language=pair_language(lang_a, lang_b) if lang_b else lang_a,
        payload={"a": side(entry_a, a), "b": side(entry_b, b)},
        hidden={k: v for k, v in hidden.items() if v is not None})
