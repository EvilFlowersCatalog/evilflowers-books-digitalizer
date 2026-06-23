"""Best-effort bibliographic lookup by ISBN, to pre-fill the librarian draft.

When a book directory name is an ISBN (~80 % of them), we query catalogs to seed
the title / authors / year / publisher / place / edition so a librarian only
*verifies* instead of typing from scratch. Providers are tried in order:

1. **STU IPAC** (`kis.cvt.stuba.sk/arl-stu`) — the university's own ARL/Cosmotron
   catalogue, queried via its RSS search feed (`field=ISBN`). Authoritative for
   this corpus (the dir ids came from this OPAC) and by far the best coverage of
   the Slovak academic books that global catalogs lack.
2. **Open Library** (`openlibrary.org`) — fills foreign titles STU may not hold.
3. **Google Books** (`googleapis.com/books`) — optional; rate-limits hard without
   an API key, so it is not in the default provider list.

Everything is best-effort and **fail-safe**: network errors, misses, and
unparseable responses just yield ``{}`` (the cell stays blank for the librarian).
Genuine misses are cached on disk (re-runs are cheap); transient errors are not
(they retry next run). Stdlib only (``urllib``), no new dependency.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_USER_AGENT = "EvilFlowersBooksDigitalizer/0.1 (library digitization; +metadata draft)"
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class LookupTransientError(Exception):
    """A provider was unreachable/throttled — the result is unknown, not a miss.

    Distinguishing this from a genuine "no match" matters: a genuine miss is
    cached (don't re-query), a transient error is **not** (retry next run).
    """

# MARC / 3-letter language codes -> the ISO-639-1 codes the pipeline uses.
_LANG_MAP = {
    "slo": "sk", "slk": "sk", "sk": "sk",
    "cze": "cs", "ces": "cs", "cs": "cs",
    "eng": "en", "en": "en",
    "ger": "de", "deu": "de", "de": "de",
    "rus": "ru", "ru": "ru",
}


def normalize_isbn(isbn: str) -> str:
    """Strip to digits + trailing X, upper-cased."""
    return re.sub(r"[^0-9Xx]", "", str(isbn)).upper()


def is_valid_isbn(isbn: str) -> bool:
    """Checksum-validate an ISBN-10 or ISBN-13 (filters OPAC ids that aren't ISBNs)."""
    s = normalize_isbn(isbn)
    if len(s) == 10:
        if not re.fullmatch(r"\d{9}[\dX]", s):
            return False
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s))
        return total % 11 == 0
    if len(s) == 13:
        if not s.isdigit():
            return False
        total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(s))
        return total % 10 == 0
    return False


def _map_language(code: str | None) -> str | None:
    if not code:
        return None
    return _LANG_MAP.get(code.strip().lower())


def _year(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d{4}", str(text))
    return int(m.group()) if m else None


def _get_text(url: str, timeout: float, *, retries: int = 2) -> str | None:
    """GET a URL's body as text. Raise :class:`LookupTransientError` on
    throttle/5xx/network (retried with backoff); ``None`` on a 4xx miss."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https hosts
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in _TRANSIENT_STATUS and attempt < retries:
                wait = _retry_after(exc) or (2.0 * (attempt + 1))
                logger.debug("%s -> HTTP %s, retry in %.1fs", url, exc.code, wait)
                time.sleep(wait)
                continue
            if exc.code in _TRANSIENT_STATUS:
                raise LookupTransientError(f"HTTP {exc.code}") from exc
            return None  # 4xx (e.g. 404) = genuine miss
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise LookupTransientError(str(exc)) from exc
    raise LookupTransientError("exhausted retries")


def _get_json(url: str, timeout: float, *, retries: int = 2) -> Any:
    """Like :func:`_get_text` but parse the body as JSON (``None`` on miss)."""
    text = _get_text(url, timeout, retries=retries)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return float(value) if value else None
    except ValueError:
        return None


#: STU ARL/Cosmotron IPAC RSS search feed (one record per <item>).
STU_OPAC_FEED = (
    "https://kis.cvt.stuba.sk/arl-stu/sk/vysledky/"
    "?st=feed&feed=rss&field=ISBN&boolop1=and&kvant=all&term={isbn}"
)
_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_CREATOR_RE = re.compile(r"<dc:creator>(.*?)</dc:creator>", re.S)
_PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_CDATA_RE = re.compile(r"<description><!\[CDATA\[(.*?)\]\]></description>", re.S)
# authors in the CDATA are anchor texts immediately followed by a "; J###" role code
_CDATA_AUTHOR_RE = re.compile(r">([^<>]+?)</a>\s*;\s*J\d+")
# imprint line: "Place : Publisher, YEAR"
_IMPRINT_RE = re.compile(r"([^<>:\n]+?)\s*:\s*([^<>,\n]+?)\s*,\s*(\d{4})")
_EDITION_RE = re.compile(r"<br>\s*(\d+\.[^<\n]*?vyd\.[^<\n]*)")


def _from_stu_opac(isbn: str, timeout: float) -> dict[str, Any]:
    """Query the STU IPAC RSS feed by ISBN; parse the first record."""
    text = _get_text(STU_OPAC_FEED.format(isbn=urllib.parse.quote(isbn)), timeout)
    if not text:
        return {}
    item_match = _ITEM_RE.search(text)
    if not item_match:
        return {}  # "Nájdených záznamov: 0"
    item = item_match.group(1)
    title_m = _TITLE_RE.search(item)
    out: dict[str, Any] = {"_source": "STU IPAC"}
    if title_m:
        out["title"] = _unescape(title_m.group(1).strip())
    pub = _PUBDATE_RE.search(item)
    if pub:
        pub_year = _year(pub.group(1))  # pubDate is often a 2-digit year -> None
        if pub_year:
            out["year"] = pub_year

    cdata = _CDATA_RE.search(item)
    if cdata:
        body = cdata.group(1)
        authors = [_unescape(a.strip()) for a in _CDATA_AUTHOR_RE.findall(body)]
        if authors:
            out["authors"] = authors
        imprint = _IMPRINT_RE.search(body)
        if imprint:
            out["place"] = _unescape(imprint.group(1).strip())
            out["publisher"] = _unescape(imprint.group(2).strip())
            out.setdefault("year", int(imprint.group(3)))  # imprint year if pubDate gave none
        edition = _EDITION_RE.search(body)
        if edition:
            out["edition"] = _unescape(edition.group(1).strip())
    if not out.get("authors"):
        creator = _CREATOR_RE.search(item)
        if creator:
            out["authors"] = [_unescape(creator.group(1).replace(",", "").strip())]
    return out if out.get("title") else {}


def _unescape(text: str) -> str:
    import html

    return html.unescape(text).replace("\xa0", " ").strip()


def _from_openlibrary(isbn: str, timeout: float) -> dict[str, Any]:
    url = (
        "https://openlibrary.org/api/books?"
        + urllib.parse.urlencode({"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"})
    )
    data = _get_json(url, timeout) or {}
    rec = data.get(f"ISBN:{isbn}")
    if not rec:
        return {}
    out: dict[str, Any] = {
        "title": rec.get("title"),
        "subtitle": rec.get("subtitle"),
        "authors": [a["name"] for a in rec.get("authors", []) if a.get("name")],
        "publisher": "; ".join(p["name"] for p in rec.get("publishers", []) if p.get("name")) or None,
        "year": _year(rec.get("publish_date")),
        "_source": "Open Library",
    }
    return {k: v for k, v in out.items() if v}


def _from_google(isbn: str, timeout: float) -> dict[str, Any]:
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(
        {"q": f"isbn:{isbn}", "country": "SK"}
    )
    data = _get_json(url, timeout) or {}
    items = data.get("items") or []
    if not items:
        return {}
    info = items[0].get("volumeInfo", {})
    out: dict[str, Any] = {
        "title": info.get("title"),
        "subtitle": info.get("subtitle"),
        "authors": list(info.get("authors", [])),
        "publisher": info.get("publisher"),
        "year": _year(info.get("publishedDate")),
        "language": _map_language(info.get("language")),
        "_source": "Google Books",
    }
    return {k: v for k, v in out.items() if v}


class IsbnEnricher:
    """Cached, multi-provider ISBN -> metadata lookup."""

    #: Provider name -> fetch function.
    PROVIDERS = {
        "stu_opac": _from_stu_opac,
        "openlibrary": _from_openlibrary,
        "google": _from_google,
    }

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        providers: tuple[str, ...] = ("stu_opac", "openlibrary"),
        timeout: float = 10.0,
        delay: float = 0.34,
    ):
        unknown = set(providers) - set(self.PROVIDERS)
        if unknown:
            raise ValueError(f"unknown ISBN providers: {sorted(unknown)}")
        self.providers = providers
        self.timeout = timeout
        self.delay = delay  # politeness pause between live provider calls (avoid 429)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0
        self.transient = 0  # ISBNs skipped due to throttle/network (retry next run)

    def _cache_path(self, isbn: str) -> Path | None:
        return self.cache_dir / f"{isbn}.json" if self.cache_dir else None

    def lookup(self, isbn: str) -> dict[str, Any]:
        """Return merged metadata for an ISBN (``{}`` on miss/invalid/error).

        Tries each provider until one returns a title. A genuine miss is cached
        so repeated runs don't re-hit the network; a *transient* error (throttle,
        network) is **not** cached, so a later run retries it.
        """
        norm = normalize_isbn(isbn)
        if not is_valid_isbn(norm):
            return {}

        cache_path = self._cache_path(norm)
        if cache_path and cache_path.exists():
            return json.loads(cache_path.read_text()) or {}

        result: dict[str, Any] = {}
        had_transient = False
        for name in self.providers:
            if self.delay:
                time.sleep(self.delay)
            try:
                found = self.PROVIDERS[name](norm, self.timeout)
            except LookupTransientError as exc:
                logger.debug("%s transient for %s: %s", name, norm, exc)
                had_transient = True
                continue
            if found.get("title"):
                result = found
                break

        if result:
            self.hits += 1
        elif had_transient:
            self.transient += 1
            return {}  # unknown, not a miss — don't cache, retry later
        else:
            self.misses += 1
        if cache_path:
            cache_path.write_text(json.dumps(result, ensure_ascii=False))
        return result
