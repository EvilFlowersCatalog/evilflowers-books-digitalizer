"""Studio configuration: global settings + projects (TOML).

A *project* is everything that varies between libraries/clients, chosen by
name so new ones need configuration, not code (open/closed principle)::

    [studio]
    database_url = "postgresql+psycopg://studio:studio@127.0.0.1:55432/studio"   # or $STUDIO_DATABASE_URL
    redis_url = "redis://127.0.0.1:56379/0"                                      # or $STUDIO_REDIS_URL
    storage_root = "/data"                      # or $STUDIO_STORAGE_ROOT: the shared storage mount
    artifacts_root = "{storage_root}/digitized" # output tree (IA item layout per book)
    cache_dir = "/scratch/cache"                # scratch: cleaned TIFFs, viewer cache

    [projects.stu]
    name = "STU — Slovak University of Technology"
    scans_root = "{storage_root}/raw-scans"
    # where a book's frames are, relative to scans_root; {collection} and {book}
    # are captured, the rest is literal or glob.
    book_pattern = "{collection}/*-skeny/{book}"
    frames_glob = "stream_pages_tif/*.tif"      # inside the book dir (falls back to *.tif)
    item_id = "stu-{collection}-{book}"         # IA-compatible identifier template
    language = "slk"                            # MARC language code (IA `language`)
    ocr_language = "slk+eng"                    # sent to the OCR service per leaf
    outputs = ["pdf", "pdfa", "epub"]           # what render-service makes
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

    [orchestration]                             # the job ledger (studio.orchestration)
    budget = 2                                  # re-sends of a lost job before the run fails
    stale_after_seconds = 600                   # a local job with no heartbeat this long is lost
    [orchestration.stages.clean]                # optional: every stage has a default route
    task = "evilflowers_scan.clean"             # (DEFAULT_ROUTES); a table overrides fields
    queue = "evilflowers_scan.standalone"
    interactive_queue = "evilflowers_scan.standalone.interactive"
    deadline_seconds = 3600
    batch = 32                                  # leaves per bulk job
    interactive_batch = 1                       # leaves per operator re-run job

    [decider]                                   # the shared decision services (knowledge#77)
    url = "http://decider.shared.svc.cluster.local/v1/systemone"          # or $DECIDER_URL
    vision_url = "http://decider-vision.shared.svc.cluster.local/v1/systemone"  # $DECIDER_VISION_URL
    timeout = 60                                # or $DECIDER_TIMEOUT; empty url = that use is off

    [marks]                                     # scan marks across the book (#14)
    min_leaves = 3                              # a group on this many leaves is furniture: hide
    hash_distance = 6                           # dHash bits (scan-service's artefact_hash_distance)
    aspect_tolerance = 0.25                     # relative aspect-ratio difference within a group

    [title_page]                                # metadata prefill from the title page (#15)
    first_leaves = 4                            # title page and its verso
    last_leaves = 2                             # colophon

    [scheduling]                              # admission: digitalization yields at peak
    timezone = "Europe/Bratislava"
    peak_hours = ["mon-fri 07:00-19:00"]
    bulk_inflight = 8                           # jobs in flight off-peak
    bulk_inflight_peak = 2
    bulk_inflight_contended = 0                 # when a shared queue below is deep
    interactive_inflight = 4                    # operator re-runs (never held by peak)
    off_peak_stages = ["render"]                # whole-book stages wait for off-peak
    [scheduling.queue_inflight]                 # optional caps per queue
    "digitalizer.ai" = 2
    [[scheduling.backpressure]]                 # other services' backlog (e.g. the catalog's)
    name = "catalog-extraction"
    redis_url = "redis://redis.shared.svc.cluster.local:6379/7"
    queues = ["evilflowers_analyzer", "evilflowers_image", "evilflowers_equation"]
    max_depth = 25

    [labelling]                                 # hand labels for decider calibration (#7)
    base_url = "https://dev.evilflowers.elvira.stuba.sk/api/v1"   # catalog, read with GET only
    api_key_env = "EVILFLOWERS_LABELLING_API_KEY"                 # needs manage rights on the entries
    catalog_ids = []                            # empty = every catalog the key sees
    max_entries = 200                           # entries scanned per sample

Paths: ``{storage_root}`` expands to the storage root, then relative paths resolve
against the config file's directory. Environment variables ``STUDIO_DATABASE_URL`` /
``STUDIO_REDIS_URL`` / ``STUDIO_STORAGE_ROOT`` / ``STUDIO_CONFIG`` override the file.

The storage root is where *this* pod mounts the shared storage. Service jobs carry paths
relative to it, so the digitalizer and a service may mount it at different absolute
paths (each sets its own root: ``STUDIO_STORAGE_ROOT`` here, ``STORAGE_PATH`` in the
service); only the layout under the root must match.
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
#: overrides ``[studio] storage_root``: where this pod mounts the shared storage
STORAGE_ROOT_ENV = "STUDIO_STORAGE_ROOT"
_IDENT_OK = re.compile(r"[^A-Za-z0-9._-]+")

type AutoPublish = Literal["never", "gate", "review"]
type Backend = Literal["local", "service"]

#: the one queue of this application: orchestration (runs, callbacks, pump, gate, publish and the
#: orchestrator's own light steps). Every pixel and model job runs on a service's queue.
PORTAL_QUEUE = "digitalizer.portal"


@dataclass(frozen=True, slots=True)
class PublishConfig:
    target: str = "none"                       # "none" | "evilflowers"
    base_url: str = ""
    api_key_env: str = "EVILFLOWERS_API_KEY"
    catalog: str = "{collection}"
    files_base_url: str = ""                   # empty = upload copies instead of external_url
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        """Publication is optional: without a target a book ends in ``approved``."""
        return self.target not in ("", "none")


@dataclass(frozen=True, slots=True)
class RoutePart:
    """One task of a stage that runs as several (analyze: hocr ∥ layout; render: prepare → book).

    A part is a service task, except the orchestrator's own light steps around the services
    (``backend = "local"``: render's ``prepare``, clean's ``marks``, analyze's ``title_page``).
    """

    task: str
    queue: str
    interactive_queue: str
    backend: Backend = "service"


@dataclass(frozen=True, slots=True)
class StageRoute:
    """Where one stage's jobs run: a service task (clean, analyze, formulas, render) or this
    application's ``digitalizer.local.<stage>`` (gate, publish: no pixels, no models)."""

    backend: Backend
    task: str
    queue: str
    interactive_queue: str
    deadline_seconds: int
    #: leaves per bulk job of a leaf-batched stage (evilflowers-books-digitalizer#4)
    batch: int = 32
    #: leaves per interactive job: 1 = every re-run page is its own job, so an operator
    #: sees each page as soon as it is done and pages run in parallel
    interactive_batch: int = 1
    #: a stage that runs as several tasks (``SPLIT_STAGES`` / ``CHAINED_STAGES``) or has a
    #: ``TAIL_PARTS`` step: one route per part
    parts: dict[str, RoutePart] = field(default_factory=dict)

    def batch_size(self, *, interactive: bool) -> int:
        return max(1, self.interactive_batch if interactive else self.batch)

    def part(self, name: str) -> RoutePart:
        try:
            return self.parts[name]
        except KeyError:
            raise KeyError(f"route {self.task or '(split)'} has no part {name!r}") from None


#: leaves per bulk job of a leaf-batched stage (evilflowers-books-digitalizer#4)
SERVICE_BATCH = 32
#: the stages services run; ``gate`` and ``publish`` are the orchestrator's own
SERVICE_STAGES = ("clean", "analyze", "formulas", "render")

#: stages a service runs as parallel parts: every leaf batch goes to each of them
SPLIT_STAGES: dict[str, tuple[str, ...]] = {"analyze": ("hocr", "layout")}
#: stages a service runs as parts in order, one job each (the next is planned when the
#: previous one is applied)
CHAINED_STAGES: dict[str, tuple[str, ...]] = {"render": ("prepare", "book")}
#: a local step planned once after every other job of the stage is applied: book-wide work
#: over the stage's results (#14: scan marks grouped across the book; #15: title-page prefill)
TAIL_PARTS: dict[str, str] = {"clean": "marks", "analyze": "title_page"}


def _local(task: str) -> RoutePart:
    return RoutePart(task, PORTAL_QUEUE, PORTAL_QUEUE, backend="local")


def _service(task: str, queue: str) -> RoutePart:
    return RoutePart(task, queue, f"{queue}.interactive")


#: the parts of each stage (every one overridable by ``[orchestration.stages.<stage>.<part>]``)
DEFAULT_PARTS: dict[str, dict[str, RoutePart]] = {
    "clean": {"marks": _local("digitalizer.local.clean_marks")},
    "analyze": {
        "hocr": _service("evilflowers_ocr_worker.standalone.hocr",
                         "evilflowers_ocr_worker.standalone"),
        "layout": _service("evilflowers_analyzer.standalone.layout",
                           "evilflowers_analyzer.standalone"),
        # the page image itself changes only on scan-service (confirmed "remove" boxes)
        "erase": _service("evilflowers_scan.erase", "evilflowers_scan.standalone"),
        "title_page": _local("digitalizer.local.title_page"),
    },
    "render": {
        # metadata lookup, item text files and the render request: light, the orchestrator's
        "prepare": _local("digitalizer.local.render_prepare"),
        "book": RoutePart("evilflowers_render.book", "evilflowers_render.standalone",
                          "evilflowers_render.standalone"),
    },
}

#: every stage's route: the task names and queues the services declare (their contracts:
#: scan-service#1, ocr-service#7, analyzer#8, equation#8, render-service#1)
DEFAULT_ROUTES: dict[str, StageRoute] = {
    "clean": StageRoute("service", "evilflowers_scan.clean", "evilflowers_scan.standalone",
                        "evilflowers_scan.standalone.interactive", 3600,
                        parts=DEFAULT_PARTS["clean"]),
    # 3600 s also covers erase: scan-service's hard limit is 3300 s
    "analyze": StageRoute("service", "", "", "", 3600, parts=DEFAULT_PARTS["analyze"]),
    "formulas": StageRoute("service", "evilflowers_equation.standalone.formulas",
                           "evilflowers_equation.standalone",
                           "evilflowers_equation.standalone.interactive", 4 * 3600),
    # one book per job; a long book. Render is an off-peak stage anyway
    "render": StageRoute("service", "", "", "", 8 * 3600, parts=DEFAULT_PARTS["render"]),
    "gate": StageRoute("local", "digitalizer.local.gate", PORTAL_QUEUE, PORTAL_QUEUE, 600),
    "publish": StageRoute("local", "digitalizer.local.publish", PORTAL_QUEUE, PORTAL_QUEUE, 1800),
}


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    budget: int = 2                            # re-sends of a lost job before its run fails
    stale_after_seconds: int = 600             # local job silent this long = its worker is gone
    start_grace_seconds: int = 30              # a queued run older than this is started by the pump
    routes: dict[str, StageRoute] = field(default_factory=lambda: dict(DEFAULT_ROUTES))

    def route(self, stage: str) -> StageRoute:
        return self.routes[stage]


@dataclass(frozen=True, slots=True)
class BackpressureSource:
    """Another system's queues on Redis; deep queues mean it needs the cluster more."""

    name: str
    redis_url: str
    queues: tuple[str, ...]
    max_depth: int = 25


@dataclass(frozen=True, slots=True)
class SchedulingConfig:
    timezone: str = "Europe/Bratislava"
    #: ``"mon-fri 07:00-19:00"`` or ``"07:00-19:00"`` (every day); local time of ``timezone``
    peak_hours: tuple[str, ...] = ()
    bulk_inflight: int = 8
    bulk_inflight_peak: int = 2
    bulk_inflight_contended: int = 0
    interactive_inflight: int = 4
    #: stages whose bulk jobs are admitted off-peak only (a running job is never interrupted,
    #: so a whole-book job admitted just before the peak would hold its CPUs through it)
    off_peak_stages: tuple[str, ...] = ()
    queue_inflight: dict[str, int] = field(default_factory=dict)
    backpressure: tuple[BackpressureSource, ...] = ()


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
    outputs: tuple[str, ...] = ("pdf", "pdfa", "epub")
    auto_publish: AutoPublish = "gate"
    metadata: dict[str, Any] = field(default_factory=dict)
    publish: PublishConfig = field(default_factory=PublishConfig)
    quality: QualityPolicy = field(default_factory=QualityPolicy)
    #: ``[pipeline.scantailor]`` (sent to scan-service with every clean job; ``output_dpi`` is
    #: the item's ppi) and ``jp2_images`` (render-service builds IA's page package)
    pipeline: dict[str, Any] = field(default_factory=dict)

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
class LabellingConfig:
    """``[labelling]``: where hand-labelling items come from (evilflowers-books-digitalizer#7).

    The catalog is read with GET only; its region listing needs *manage* rights on the
    sampled entries, so the key is a dedicated dev key, never sent to the browser.
    """

    base_url: str = ""                          # e.g. https://dev.evilflowers.elvira.stuba.sk/api/v1
    api_key_env: str = "EVILFLOWERS_LABELLING_API_KEY"
    catalog_ids: tuple[str, ...] = ()           # empty = every catalog the key can see
    #: catalog entries scanned per sampling call (bounds the requests a sample costs)
    max_entries: int = 200
    timeout: float = 30.0
    seed: int = 7                               # shuffle within strata: reproducible samples

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)


@dataclass(frozen=True, slots=True)
class DeciderConfig:
    """``[decider]``: the shared decision services (knowledge Decision-Models, knowledge#77).

    An empty URL switches its uses off: marks stay ``review`` (#14), no metadata is prefilled
    (#15). A call that fails or times out does the same for that book; a run never fails on it.
    """

    url: str = ""                               # decider (Laya, text): POST /v1/systemone
    vision_url: str = ""                        # decider-vision (Strands, images)
    model: str = "multilingual"                 # Laya checkpoint (without it Laya may swap)
    timeout: float = 60.0


@dataclass(frozen=True, slots=True)
class MarksConfig:
    """``[marks]``: scan marks grouped across a whole book (evilflowers-books-digitalizer#14).

    The defaults are scan-service's own (``artefact_min_repeats``, ``artefact_hash_distance``),
    so a mark repeated within one clean job and one repeated across the book count the same.
    """

    min_leaves: int = 3
    hash_distance: int = 6
    aspect_tolerance: float = 0.25
    #: the decider's p above which its verdict bands without a person: ``hide`` for an
    #: artefact, ``keep`` for content. None until knowledge#65 calibrates ``scan_mark``: then
    #: every verdict is ``review`` (stored with its p, never acted on)
    hide_above: float | None = None
    keep_above: float | None = None


@dataclass(frozen=True, slots=True)
class TitlePageConfig:
    """``[title_page]``: title, authors, publisher and year picked from OCR lines (#15)."""

    first_leaves: int = 4
    last_leaves: int = 2
    max_candidates: int = 12                    # criteria per question (+ "none")


@dataclass(frozen=True, slots=True)
class StudioConfig:
    database_url: str
    redis_url: str
    artifacts_root: Path
    cache_dir: Path
    projects: dict[str, ProjectConfig]
    config_path: Path | None = None
    orchestration: OrchestrationConfig = field(default_factory=OrchestrationConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    #: the services' ``STORAGE_PATH``: one root holding the scans and ``artifacts_root``.
    #: Paths in a service job are relative to it (required by :func:`load_config`).
    storage_root: Path | None = None
    labelling: LabellingConfig = field(default_factory=LabellingConfig)
    decider: DeciderConfig = field(default_factory=DeciderConfig)
    marks: MarksConfig = field(default_factory=MarksConfig)
    title_page: TitlePageConfig = field(default_factory=TitlePageConfig)

    def project(self, slug: str) -> ProjectConfig:
        try:
            return self.projects[slug]
        except KeyError:
            raise KeyError(f"unknown project {slug!r} (known: {', '.join(self.projects)})") from None


STORAGE_PLACEHOLDER = "{storage_root}"


@dataclass(frozen=True, slots=True)
class _Paths:
    """How a path value in the file becomes a path: ``{storage_root}`` first, then the file's dir."""

    base: Path
    storage_root: Path | None

    def __call__(self, value: str | Path) -> Path:
        text = str(value)
        if STORAGE_PLACEHOLDER in text:
            if self.storage_root is None:
                raise ValueError(f"{text!r} uses {STORAGE_PLACEHOLDER} but no storage root is set "
                                 f"([studio] storage_root or ${STORAGE_ROOT_ENV})")
            text = text.replace(STORAGE_PLACEHOLDER, str(self.storage_root))
        p = Path(text).expanduser()
        return p if p.is_absolute() else (self.base / p)


def _resolve(value: str | Path, base: Path) -> Path:
    return _Paths(base, None)(value)


#: project keys of the retired in-process stages (#6): the services choose their own engines
RETIRED_PROJECT_KEYS = ("layout_detectors", "formula_recognizer")
RETIRED_PIPELINE_KEYS = ("tesseract", "heuristic", "layout", "vlm", "reflow", "docres",
                         "chunk_size")


def _project(slug: str, raw: dict[str, Any], resolve: _Paths) -> ProjectConfig:
    retired = [k for k in RETIRED_PROJECT_KEYS if k in raw] + [
        f"pipeline.{k}" for k in RETIRED_PIPELINE_KEYS if k in raw.get("pipeline", {})]
    if retired:
        raise ValueError(f"[projects.{slug}]: {', '.join(retired)} configured the in-process "
                         "stages, which are gone (evilflowers-books-digitalizer#6): OCR, layout, "
                         "formulas and render run on the services; remove these keys")
    publish = PublishConfig(**{k: v for k, v in raw.get("publish", {}).items()
                               if k in PublishConfig.__dataclass_fields__},
                            options={k: v for k, v in raw.get("publish", {}).items()
                                     if k not in PublishConfig.__dataclass_fields__})
    metadata = dict(raw.get("metadata", {}))
    if metadata.get("excel_path"):
        metadata["excel_path"] = str(resolve(metadata["excel_path"]))
    return ProjectConfig(
        slug=slug,
        name=raw.get("name", slug),
        scans_root=resolve(raw["scans_root"]),
        book_pattern=raw.get("book_pattern", "{collection}/{book}"),
        frames_glob=raw.get("frames_glob", "*.tif"),
        item_id=raw.get("item_id", "{collection}-{book}"),
        language=raw.get("language", "slk"),
        ocr_language=raw.get("ocr_language", "slk+eng"),
        collections=dict(raw.get("collections", {})),
        outputs=tuple(raw.get("outputs", ["pdf", "pdfa", "epub"])),
        auto_publish=raw.get("auto_publish", "gate"),
        metadata=metadata,
        publish=publish,
        quality=QualityPolicy(**raw.get("quality", {})),
        pipeline=dict(raw.get("pipeline", {})),
    )


_ROUTE_KEYS = {"task", "queue", "interactive_queue"}
_STAGE_KEYS = _ROUTE_KEYS | {"backend", "deadline_seconds", "batch", "interactive_batch"}


def _part(stage: str, name: str, base: RoutePart, raw: dict[str, Any]) -> RoutePart:
    if unknown := set(raw) - _ROUTE_KEYS:
        raise ValueError(f"[orchestration.stages.{stage}.{name}]: unknown keys {sorted(unknown)}")
    queue = raw.get("queue", base.queue)
    interactive = raw.get("interactive_queue",
                          base.interactive_queue if "queue" not in raw else queue)
    return RoutePart(raw.get("task", base.task), queue, interactive, backend=base.backend)


def _route(stage: str, base: StageRoute, raw: dict[str, Any]) -> StageRoute:
    """``base`` (a default route) with the fields of ``[orchestration.stages.<stage>]`` over it.

    A split / chained stage takes its parts as subtables (``[….analyze.hocr]``); render's one
    service part may also be given as the stage's own ``task`` / ``queue``."""
    if raw.get("backend", base.backend) != base.backend:
        raise ValueError(f"[orchestration.stages.{stage}] backend = {raw['backend']!r}: "
                         f"{stage} runs on {base.backend} only (the in-process stages are gone, "
                         "evilflowers-books-digitalizer#6)")
    parts = dict(base.parts)
    flat = {k: raw[k] for k in _ROUTE_KEYS if k in raw}
    if stage in CHAINED_STAGES and flat:      # render: task/queue name its service part
        raw = {**{k: v for k, v in raw.items() if k not in _ROUTE_KEYS},
               "book": {**flat, **raw.get("book", {})}}
        flat = {}
    for name, value in raw.items():
        if isinstance(value, dict):
            if name not in parts:
                raise ValueError(f"[orchestration.stages.{stage}.{name}]: unknown part "
                                 f"(known: {', '.join(parts) or 'none'})")
            parts[name] = _part(stage, name, parts[name], value)
        elif name not in _STAGE_KEYS:
            raise ValueError(f"[orchestration.stages.{stage}]: unknown key {name!r}")
    if flat and (stage in SPLIT_STAGES):
        raise ValueError(f"[orchestration.stages.{stage}] runs as parts "
                         f"({', '.join(SPLIT_STAGES[stage])}): configure them as subtables")
    queue = flat.get("queue", base.queue)
    interactive = flat.get("interactive_queue",
                           base.interactive_queue if "queue" not in flat else queue)
    return StageRoute(base.backend, flat.get("task", base.task), queue, interactive,
                      int(raw.get("deadline_seconds", base.deadline_seconds)),
                      batch=int(raw.get("batch", base.batch)),
                      interactive_batch=int(raw.get("interactive_batch", base.interactive_batch)),
                      parts=parts)


def _orchestration(raw: dict[str, Any]) -> OrchestrationConfig:
    routes = dict(DEFAULT_ROUTES)
    for stage, override in raw.get("stages", {}).items():
        if stage not in DEFAULT_ROUTES:
            raise ValueError(f"[orchestration.stages.{stage}]: unknown stage "
                             f"(known: {', '.join(DEFAULT_ROUTES)})")
        routes[stage] = _route(stage, routes[stage], override)
    return OrchestrationConfig(
        budget=int(raw.get("budget", 2)),
        stale_after_seconds=int(raw.get("stale_after_seconds", 600)),
        start_grace_seconds=int(raw.get("start_grace_seconds", 30)),
        routes=routes)


def _scheduling(raw: dict[str, Any]) -> SchedulingConfig:
    sources = tuple(BackpressureSource(name=s.get("name", s["redis_url"]), redis_url=s["redis_url"],
                                       queues=tuple(s["queues"]), max_depth=int(s.get("max_depth", 25)))
                    for s in raw.get("backpressure", []))
    defaults = SchedulingConfig()
    return SchedulingConfig(
        timezone=raw.get("timezone", defaults.timezone),
        peak_hours=tuple(raw.get("peak_hours", ())),
        bulk_inflight=int(raw.get("bulk_inflight", defaults.bulk_inflight)),
        bulk_inflight_peak=int(raw.get("bulk_inflight_peak", defaults.bulk_inflight_peak)),
        bulk_inflight_contended=int(raw.get("bulk_inflight_contended",
                                            defaults.bulk_inflight_contended)),
        interactive_inflight=int(raw.get("interactive_inflight", defaults.interactive_inflight)),
        off_peak_stages=tuple(raw.get("off_peak_stages", ())),
        queue_inflight={str(k): int(v) for k, v in raw.get("queue_inflight", {}).items()},
        backpressure=sources)


def _labelling(raw: dict[str, Any]) -> LabellingConfig:
    unknown = set(raw) - set(LabellingConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"[labelling]: unknown keys {', '.join(sorted(unknown))}")
    d = LabellingConfig()
    return LabellingConfig(
        base_url=str(raw.get("base_url", d.base_url)).rstrip("/"),
        api_key_env=str(raw.get("api_key_env", d.api_key_env)),
        catalog_ids=tuple(str(c) for c in raw.get("catalog_ids", ())),
        max_entries=int(raw.get("max_entries", d.max_entries)),
        timeout=float(raw.get("timeout", d.timeout)),
        seed=int(raw.get("seed", d.seed)))


def _table[T](name: str, cls: type[T], raw: dict[str, Any]) -> T:
    """A flat config table onto its dataclass: unknown keys are an error, types from the defaults."""
    fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
    if unknown := set(raw) - set(fields):
        raise ValueError(f"[{name}]: unknown keys {', '.join(sorted(unknown))}")
    return cls(**raw)


def _decider(raw: dict[str, Any]) -> DeciderConfig:
    """``[decider]``, then the kit's env names (``JudgeSettings``: knowledge#77 sets them on the
    digitalizer's pods): ``DECIDER_URL``, ``DECIDER_VISION_URL``, ``DECIDER_MODEL``,
    ``DECIDER_TIMEOUT``; ``DECIDER_ENABLED=false`` switches both off."""
    d = _table("decider", DeciderConfig, raw)
    env = os.environ
    if env.get("DECIDER_ENABLED", "true").strip().lower() in ("0", "false", "no", "off"):
        return DeciderConfig(model=d.model, timeout=d.timeout)
    return DeciderConfig(url=env.get("DECIDER_URL") or d.url,
                         vision_url=env.get("DECIDER_VISION_URL") or d.vision_url,
                         model=env.get("DECIDER_MODEL") or d.model,
                         timeout=float(env.get("DECIDER_TIMEOUT") or d.timeout))


def load_config(path: str | Path | None = None) -> StudioConfig:
    """Read the studio config (``path`` > ``$STUDIO_CONFIG`` > ``configs/studio.toml``)."""
    from evilflowers_books_digitalizer.config import PROJECT_ROOT

    cfg_path = Path(path or os.environ.get(CONFIG_ENV) or PROJECT_ROOT / "configs" / "studio.toml")
    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)
    base = cfg_path.absolute().parent
    studio = raw.get("studio", {})
    root_value = os.environ.get(STORAGE_ROOT_ENV) or studio.get("storage_root")
    storage_root = _resolve(root_value, base) if root_value else None
    resolve = _Paths(base, storage_root)
    config = StudioConfig(
        database_url=os.environ.get("STUDIO_DATABASE_URL") or studio.get(
            "database_url", f"sqlite:///{_resolve('studio.db', base)}"),
        redis_url=os.environ.get("STUDIO_REDIS_URL") or studio.get("redis_url", "redis://127.0.0.1:6379/0"),
        artifacts_root=resolve(studio.get("artifacts_root", "../output/studio")),
        cache_dir=resolve(studio.get("cache_dir", "../.cache/studio")),
        projects={slug: _project(slug, p, resolve) for slug, p in raw.get("projects", {}).items()},
        config_path=cfg_path,
        orchestration=_orchestration(raw.get("orchestration", {})),
        scheduling=_scheduling(raw.get("scheduling", {})),
        storage_root=storage_root,
        labelling=_labelling(raw.get("labelling", {})),
        decider=_decider(raw.get("decider", {})),
        marks=_table("marks", MarksConfig, raw.get("marks", {})),
        title_page=_table("title_page", TitlePageConfig, raw.get("title_page", {})),
    )
    _check_shared_storage(config)
    return config


def _check_shared_storage(config: StudioConfig) -> None:
    """The services exchange paths relative to the storage root: fail at load time, not on the
    first job, when it is missing or the scans or the artifacts live outside it."""
    if config.storage_root is None:
        raise ValueError("the services need the shared storage root "
                         f"([studio] storage_root or ${STORAGE_ROOT_ENV})")
    root = config.storage_root
    outside = [str(p) for p in (config.artifacts_root,
                                *(pc.scans_root for pc in config.projects.values()))
               if not p.is_relative_to(root)]
    if outside:
        raise ValueError(f"the services need every scan and artifact path under the "
                         f"storage root {root}; outside it: {', '.join(outside)}")
