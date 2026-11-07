"""Item metadata and leaf semantics, compatible with the Internet Archive's book items.

Our model mirrors IA's two book-level documents so an item can round-trip:

* ``<id>_meta.xml`` — item metadata (Dublin-Core-like, flat, some fields repeatable)
  -> :class:`ItemMetadata`. Field names are IA's (``oclc-id`` is ``oclc_id`` in
  Python, ``page-progression`` is ``page_progression``); :meth:`ItemMetadata.to_ia`
  restores them.
* ``<id>_scandata.xml`` — one ``<page leafNum=…>`` per captured image ->
  :class:`Leaf` (page type, side, crop box, rotation, printed page number,
  include-in-access flag). A *leaf* is one page image; leaf numbers are 0-based
  capture order, exactly like IA.

The studio stores these values in the database (books.item_metadata, pages.*)
and the IA adapter writes/reads the XML files; nothing else needs to know the
XML shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import PurePath
from typing import Any, Protocol

# ---------------------------------------------------------------- leaf semantics
class PageType(StrEnum):
    """IA scandata ``pageType`` vocabulary — Scribe3 ``VALID_PAGE_TYPES``, IA's spelling."""

    NORMAL = "Normal"
    COVER = "Cover"
    TITLE = "Title"
    COPYRIGHT = "Copyright"
    CONTENTS = "Contents"
    TISSUE = "Tissue"
    COLOR_CARD = "Color Card"     # calibration target
    CHAPTER = "Chapter"
    WHITE_CARD = "White Card"
    FOLDOUT = "Foldout"
    ABSTRACT = "Abstract"
    APPENDIX = "Appendix"
    CONTRIBUTIONS = "Contributions"
    DELETE = "Delete"             # captured by mistake / duplicate
    FOREWORD = "Foreword"
    GLOSSARY = "Glossary"
    INDEX = "Index"
    INSCRIPTIONS = "Inscriptions"
    INTRODUCTION = "Introduction"
    LIST_OF_FIGURES = "List of Figures"
    LIST_OF_TABLES = "List of Tables"
    NOTES = "Notes"
    PREFACE = "Preface"
    QUOTATIONS = "Quotations"
    REFERENCE = "Reference"
    SUBCHAPTER = "Subchapter"
    SUBSUBCHAPTER = "SubSubchapter"


#: page types that default to ``addToAccessFormats=false`` when an operator sets them.
#: NB: IA tools (archive-pdf-tools, hocr-tools) key exclusion on the *flag*, never on the
#: type — so the flag is the single source of truth; this set only seeds its default.
NOT_IN_ACCESS = frozenset({PageType.COLOR_CARD, PageType.WHITE_CARD, PageType.DELETE})


def default_add_to_access(page_type: PageType) -> bool:
    return page_type not in NOT_IN_ACCESS


class HandSide(StrEnum):
    LEFT = "LEFT"                 # verso in a left-to-right book
    RIGHT = "RIGHT"               # recto


@dataclass(frozen=True, slots=True)
class Leaf:
    """One captured page image, IA scandata semantics.

    Two indices exist, exactly as at IA: the *leaf* number (every captured image;
    page images, hOCR ``page_%06d`` and scandata use it) and the *access* index
    (PDF/EPUB/BookReader/IIIF canvases), which counts only leaves with
    ``add_to_access``.
    """

    leaf_num: int                         # 0-based, contiguous = image name <id>_%04d
    page_type: PageType = PageType.NORMAL
    hand_side: HandSide | None = None
    add_to_access: bool = True            # addToAccessFormats — drives exclusion everywhere
    page_number: str | None = None        # printed number as a string ("21", "xii"); None = unnumbered
    crop_box: tuple[int, int, int, int] | None = None   # x, y, w, h — frame AFTER rotation
    rotate_degree: int = 0                # -90 | 0 | 90 | 180 (Scribe3: left -90, right 90)
    skew_angle: float | None = None       # degrees
    orig_file_name: str | None = None
    orig_width: int | None = None
    orig_height: int | None = None
    ppi: int | None = None
    book_start: bool = False              # first leaf of the book proper (bookStart)
    toc_title: str | None = None          # pageType@title (outline entry in archive-pdf-tools)
    toc_level: int | None = None          # pageType@level

    @property
    def in_access(self) -> bool:
        return self.add_to_access


class LeafRow(Protocol):
    """The ``pages`` columns a leaf is read from (``db.models.Page`` satisfies it).

    A protocol keeps this module free of project imports while giving the
    application and the IA adapter one shared mapping.
    """

    @property
    def index(self) -> int: ...
    @property
    def page_type(self) -> str: ...
    @property
    def hand_side(self) -> str | None: ...
    @property
    def add_to_access(self) -> bool: ...
    @property
    def page_number(self) -> str | None: ...
    @property
    def crop_box(self) -> list[int] | None: ...
    @property
    def rotate_degree(self) -> int: ...
    @property
    def skew_angle(self) -> float | None: ...
    @property
    def source_path(self) -> str | None: ...
    @property
    def dpi(self) -> int | None: ...
    @property
    def leaf_extra(self) -> dict[str, Any]: ...


def _opt_int(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def leaf_from_row(row: LeafRow) -> Leaf:
    """A pages row as an IA scandata leaf — the one mapping every exporter uses.

    Tolerant by design: an unknown page type reads as ``Normal`` and an unknown
    hand side as none, so one odd row cannot fail a render or an export.
    """
    extra = row.leaf_extra or {}
    crop = row.crop_box
    return Leaf(
        leaf_num=row.index,
        page_type=PageType(row.page_type) if row.page_type in PageType else PageType.NORMAL,
        hand_side=HandSide(row.hand_side) if row.hand_side and row.hand_side in HandSide
        else None,
        add_to_access=bool(row.add_to_access),
        page_number=row.page_number or None,
        crop_box=(int(crop[0]), int(crop[1]), int(crop[2]), int(crop[3]))
        if crop and len(crop) == 4 else None,
        rotate_degree=row.rotate_degree or 0,
        skew_angle=row.skew_angle,
        orig_file_name=PurePath(row.source_path).name if row.source_path else None,
        orig_width=_opt_int(extra.get("orig_width")),
        orig_height=_opt_int(extra.get("orig_height")),
        ppi=row.dpi,
        book_start=bool(extra.get("book_start", False)),
        toc_title=extra.get("toc_title") or None,
        toc_level=_opt_int(extra.get("toc_level")),
    )


def access_index(leaves: list[Leaf]) -> dict[int, int]:
    """leaf_num -> 0-based access index (PDF/EPUB page, IIIF canvas), access leaves only."""
    return {leaf.leaf_num: i for i, leaf in enumerate(lf for lf in leaves if lf.in_access)}


# ---------------------------------------------------------------- item metadata
#: IA fields that hold several values (one XML element each) — per archive.org metadata schema
REPEATABLE = frozenset({"collection", "creator", "subject", "isbn", "language", "description",
                        "notes", "oclc_id", "lccn", "external_identifier"})

#: python attribute -> IA field name, where they differ
_IA_NAME = {"oclc_id": "oclc-id", "page_progression": "page-progression",
            "possible_copyright_status": "possible-copyright-status",
            "external_identifier": "external-identifier", "identifier_ark": "identifier-ark",
            "copyright_region": "copyright-region"}

#: prefix for fields IA does not define (custom fields must be valid XML names)
CUSTOM_PREFIX = "evilflowers_"
_PY_NAME = {v: k for k, v in _IA_NAME.items()}

#: ISO 639-3 / 639-2T (what Tesseract and catalogs use) -> MARC / 639-2B (what IA records)
MARC_LANGUAGE = {"slk": "slo", "ces": "cze", "deu": "ger", "fra": "fre", "eng": "eng",
                 "hun": "hun", "pol": "pol", "rus": "rus", "ukr": "ukr", "lat": "lat"}


@dataclass(slots=True)
class ItemMetadata:
    """Book-level metadata with IA ``_meta.xml`` semantics (mediatype ``texts``)."""

    identifier: str                                  # [a-z0-9][a-z0-9._-]{4,99}, permanent
    title: str = ""
    mediatype: str = "texts"                         # set at creation, never changed (IA rule)
    creator: list[str] = field(default_factory=list)          # LC heading form "Surname, Given, dates"
    contributor: str = ""                             # library owning the physical copy (single)
    publisher: str = ""                               # "Place : Publisher" (IA's common form)
    date: str = ""                                    # YYYY | YYYY-MM | YYYY-MM-DD | [YYYY] | [n.d.]
    language: list[str] = field(default_factory=list)         # MARC codes: slo, cze, ger, eng
    subject: list[str] = field(default_factory=list)
    description: list[str] = field(default_factory=list)      # HTML allowed
    isbn: list[str] = field(default_factory=list)
    oclc_id: list[str] = field(default_factory=list)
    lccn: list[str] = field(default_factory=list)
    call_number: str = ""
    identifier_ark: str = ""
    external_identifier: list[str] = field(default_factory=list)   # urn:oclc:record:…
    openlibrary_edition: str = ""
    openlibrary_work: str = ""
    edition: str = ""
    volume: str = ""
    collection: list[str] = field(default_factory=list)       # first = primary collection
    rights: str = ""
    licenseurl: str = ""
    possible_copyright_status: str = ""               # e.g. NOT_IN_COPYRIGHT
    copyright_region: str = ""
    page_progression: str = "lr"                      # lr | rl
    notes: list[str] = field(default_factory=list)
    sponsor: str = ""
    sponsordate: str = ""                             # YYYYMMDD
    # capture / processing provenance
    scanner: str = ""
    scanningcenter: str = ""
    operator: str = ""
    camera: str = ""
    scandate: str = ""                                # YYYYMMDDHHMMSS UTC
    ppi: int | None = None
    imagecount: int | None = None                     # number of leaves
    foldoutcount: int | None = None
    ocr: str = ""                                     # e.g. "tesseract 5.5.0"
    ocr_parameters: str = ""                          # e.g. "-l slk+eng"
    ocr_detected_lang: str = ""
    #: anything else from meta.xml we do not model (kept verbatim for round-trips)
    extra: dict[str, list[str]] = field(default_factory=dict)

    # -- conversions ---------------------------------------------------------
    def to_ia(self) -> dict[str, str | list[str]]:
        """Flat dict with IA field names; empty values dropped; repeatables as lists."""
        out: dict[str, str | list[str]] = {}
        for f in fields(self):
            if f.name == "extra":
                continue
            value = getattr(self, f.name)
            if value in (None, "", []):
                continue
            out[_IA_NAME.get(f.name, f.name)] = [str(v) for v in value] if isinstance(value, list) \
                else str(value)
        for k, v in self.extra.items():
            out.setdefault(k, v if len(v) != 1 else v[0])
        return out

    @classmethod
    def from_ia(cls, data: dict[str, Any]) -> ItemMetadata:
        """Inverse of :meth:`to_ia`; accepts scalars or lists for any field."""
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        extra: dict[str, list[str]] = {}
        for ia_key, value in data.items():
            name = _PY_NAME.get(ia_key, ia_key.replace("-", "_"))
            values = [str(v) for v in value] if isinstance(value, list | tuple) else [str(value)]
            if name not in known or name == "extra":
                extra[ia_key] = values
            elif name in REPEATABLE:
                kwargs[name] = values
            elif name in ("ppi", "imagecount", "foldoutcount"):
                kwargs[name] = int(float(values[0])) if values and values[0] else None
            else:
                kwargs[name] = values[0] if values else ""
        if "identifier" not in kwargs:
            raise ValueError("IA metadata without identifier")
        return cls(**kwargs, extra=extra)

    def merge(self, other: ItemMetadata) -> ItemMetadata:
        """Fill empty fields from ``other`` (lower-priority source); never overwrite."""
        for f in fields(self):
            if f.name in ("identifier", "extra"):
                continue
            if getattr(self, f.name) in (None, "", []) and getattr(other, f.name) not in (None, "", []):
                setattr(self, f.name, getattr(other, f.name))
        for k, v in other.extra.items():
            self.extra.setdefault(k, v)
        return self

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly (database column / API); python field names."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ItemMetadata:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def for_item(cls, item_id: str, stored: dict[str, Any] | None, title: str | None = None
                 ) -> ItemMetadata:
        """A book's record with ``identifier`` pinned to its permanent item id.

        ``books.item_id`` is the identifier; a stale or edited ``identifier`` in
        the stored record must never rename the IA item, so it always loses.
        """
        meta = cls.from_dict({**(stored or {}), "identifier": item_id})
        meta.title = meta.title or (title or "")
        return meta


def marc_language(code: str) -> str:
    """'slk' / 'slk+eng' (Tesseract) -> 'slo' (first language, MARC)."""
    first = code.split("+")[0].strip().lower()
    return MARC_LANGUAGE.get(first, first)
