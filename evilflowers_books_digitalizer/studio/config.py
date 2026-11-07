"""Studio configuration: global settings + projects (TOML).

A *project* is everything that varies between libraries/clients, chosen by
name so new ones need configuration, not code (open/closed principle)::

    [studio]
    database_url = "postgresql+psycopg://studio:studio@127.0.0.1:55432/studio"   # or $STUDIO_DATABASE_URL
    redis_url = "redis://127.0.0.1:56379/0"                                      # or $STUDIO_REDIS_URL
    artifacts_root = "/data/digitized"          # output tree (IA item layout per book)
    cache_dir = "/data/cache"                   # scratch: cleaned TIFFs, viewer cache

    [projects.stu]
    name = "STU — Slovak University of Technology"
    scans_root = "/data/raw-scans"
    # where a book's frames are, relative to scans_root; {collection} and {book}
    # are captured, the rest is literal or glob.
    book_pattern = "{collection}/*-skeny/{book}"
    frames_glob = "stream_pages_tif/*.tif"      # inside the book dir (falls back to *.tif)
    item_id = "stu-{collection}-{book}"         # IA-compatible identifier template
    language = "slk"                            # MARC language code (IA `language`)
    ocr_language = "slk+eng"
    layout_detectors = ["heuristic"]            # + "yolo" once its weights are available
    formula_recognizer = "none"                 # or "paddleocr-vl"
    outputs = ["pdf", "pdfa", "epub"]
    auto_publish = "gate"                       # never | gate (publish when the quality gate passes) | review
    [projects.stu.collections]
    fei = "Fakulta elektrotechniky a informatiky STU"
    [projects.stu.metadata]
    providers = ["excel", "opac"]               # tried in order; later ones fill gaps
    excel_path = "catalog.xlsx"
    [projects.stu.publish]
    target = "evilflowers"                      # adapter name
    base_url = "https://dev.evilflowers.elvira.stuba.sk/api/v1"
    api_key_env = "EVILFLOWERS_API_KEY"
    catalog = "{collection}"                    # template; per-book override via metadata
    files_base_url = "https://elvira.stuba.sk/digitized"   # external_url = base + item path
    [projects.stu.quality]                      # QualityPolicy overrides
    min_ocr_conf = 80.0

Relative paths resolve against the config file's directory. Environment
variables ``STUDIO_DATABASE_URL`` / ``STUDIO_REDIS_URL`` / ``STUDIO_CONFIG``
override the file.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evilflowers_books_digitalizer.studio.domain import QualityPolicy

CONFIG_ENV = "STUDIO_CONFIG"
_IDENT_OK = re.compile(r"[^A-Za-z0-9._-]+")

type AutoPublish = Literal["never", "gate", "review"]


@dataclass(frozen=True, slots=True)
class PublishConfig:
    target: str = "none"                       # "none" | "evilflowers"
    base_url: str = ""
    api_key_env: str = "EVILFLOWERS_API_KEY"
    catalog: str = "{collection}"
    files_base_url: str = ""                   # empty = upload copies instead of external_url
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    slug: str
    name: str
    scans_root: Path
    book_pattern: str = "{collection}/{book}"
    frames_glob: str = "*.tif"
    item_id: str = "{collection}-{book}"
    language: str = "slk"
    ocr_language: str = "slk+eng"
    collections: dict[str, str] = field(default_factory=dict)
    layout_detectors: tuple[str, ...] = ("heuristic",)
    formula_recognizer: str = "none"
    outputs: tuple[str, ...] = ("pdf", "pdfa", "epub")
    auto_publish: AutoPublish = "gate"
    metadata: dict[str, Any] = field(default_factory=dict)
    publish: PublishConfig = field(default_factory=PublishConfig)
    quality: QualityPolicy = field(default_factory=QualityPolicy)
    pipeline: dict[str, Any] = field(default_factory=dict)   # ScanTailor/render knobs (pipeline.toml shape)

    def item_identifier(self, collection: str, book: str) -> str:
        """IA-compatible, permanent identifier: lowercase ``[a-z0-9][a-z0-9._-]{4,79}``.

        IA allows 5–100 chars (80 recommended), first char alphanumeric, ASCII
        letters/digits/``_-.`` only; lowercase avoids any case-sensitivity question.
        """
        raw = self.item_id.format(collection=collection, book=book, project=self.slug).lower()
        ident = _IDENT_OK.sub("_", raw).lstrip("._-").rstrip("._-")[:80].rstrip("._-")
        if len(ident) < 5:
            ident = (ident + "_item")[:80]
        return ident


@dataclass(frozen=True, slots=True)
class StudioConfig:
    database_url: str
    redis_url: str
    artifacts_root: Path
    cache_dir: Path
    projects: dict[str, ProjectConfig]
    config_path: Path | None = None

    def project(self, slug: str) -> ProjectConfig:
        try:
            return self.projects[slug]
        except KeyError:
            raise KeyError(f"unknown project {slug!r} (known: {', '.join(self.projects)})") from None


def _resolve(value: str | Path, base: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def _project(slug: str, raw: dict[str, Any], base: Path) -> ProjectConfig:
    publish = PublishConfig(**{k: v for k, v in raw.get("publish", {}).items()
                               if k in PublishConfig.__dataclass_fields__},
                            options={k: v for k, v in raw.get("publish", {}).items()
                                     if k not in PublishConfig.__dataclass_fields__})
    metadata = dict(raw.get("metadata", {}))
    if metadata.get("excel_path"):
        metadata["excel_path"] = str(_resolve(metadata["excel_path"], base))
    return ProjectConfig(
        slug=slug,
        name=raw.get("name", slug),
        scans_root=_resolve(raw["scans_root"], base),
        book_pattern=raw.get("book_pattern", "{collection}/{book}"),
        frames_glob=raw.get("frames_glob", "*.tif"),
        item_id=raw.get("item_id", "{collection}-{book}"),
        language=raw.get("language", "slk"),
        ocr_language=raw.get("ocr_language", "slk+eng"),
        collections=dict(raw.get("collections", {})),
        layout_detectors=tuple(raw.get("layout_detectors", ["heuristic"])),
        formula_recognizer=raw.get("formula_recognizer", "none"),
        outputs=tuple(raw.get("outputs", ["pdf", "pdfa", "epub"])),
        auto_publish=raw.get("auto_publish", "gate"),
        metadata=metadata,
        publish=publish,
        quality=QualityPolicy(**raw.get("quality", {})),
        pipeline=dict(raw.get("pipeline", {})),
    )


def load_config(path: str | Path | None = None) -> StudioConfig:
    """Read the studio config (``path`` > ``$STUDIO_CONFIG`` > ``configs/studio.toml``)."""
    from evilflowers_books_digitalizer.config import PROJECT_ROOT

    cfg_path = Path(path or os.environ.get(CONFIG_ENV) or PROJECT_ROOT / "configs" / "studio.toml")
    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)
    base = cfg_path.absolute().parent
    studio = raw.get("studio", {})
    return StudioConfig(
        database_url=os.environ.get("STUDIO_DATABASE_URL") or studio.get(
            "database_url", f"sqlite:///{_resolve('studio.db', base)}"),
        redis_url=os.environ.get("STUDIO_REDIS_URL") or studio.get("redis_url", "redis://127.0.0.1:6379/0"),
        artifacts_root=_resolve(studio.get("artifacts_root", "../output/studio"), base),
        cache_dir=_resolve(studio.get("cache_dir", "../.cache/studio"), base),
        projects={slug: _project(slug, p, base) for slug, p in raw.get("projects", {}).items()},
        config_path=cfg_path,
    )
