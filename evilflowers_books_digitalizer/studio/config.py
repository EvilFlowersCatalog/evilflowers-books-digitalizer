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

    [orchestration]                             # the job ledger (studio.orchestration)
    budget = 2                                  # re-sends of a lost job before the run fails
    stale_after_seconds = 600                   # a local job with no heartbeat this long is lost
    [orchestration.stages.clean]                # where a stage runs: in-process or a service
    backend = "service"                         # local (default) | service
    task = "evilflowers_scan.clean"
    queue = "evilflowers_scan.standalone"
    interactive_queue = "evilflowers_scan.standalone.interactive"
    deadline_seconds = 3600
    batch = 32                                  # leaves per bulk job (service default 32;
                                                # local default [pipeline] chunk_size)
    interactive_batch = 1                       # leaves per operator re-run job

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

#: queues of this application (``digitalizer.*``); services bring their own
PORTAL_QUEUE = "digitalizer.portal"            # orchestration: runs, callbacks, pump, gate, publish
CPU_QUEUE = "digitalizer.cpu"                  # in-process clean / render (until the services take them)
AI_QUEUE = "digitalizer.ai"                    # in-process layout / formulas
INTERACTIVE_QUEUE = "digitalizer.interactive"  # in-process operator re-runs


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

    A part is a service task, except the orchestrator's own steps around a service (render's
    ``prepare``: metadata lookup, text files, the request), which stay in-process.
    """

    task: str
    queue: str
    interactive_queue: str
    backend: Backend = "service"


@dataclass(frozen=True, slots=True)
class StageRoute:
    """Where one stage's jobs run: this app's ``digitalizer.local.<stage>`` task or a service task."""

    backend: Backend
    task: str
    queue: str
    interactive_queue: str
    deadline_seconds: int
    #: leaves per bulk job of a chunked stage (clean); None = the project's
    #: ``[pipeline] chunk_size``. A service route defaults to :data:`SERVICE_BATCH`.
    batch: int | None = None
    #: leaves per interactive job: 1 = every re-run page is its own job, so an operator
    #: sees each page as soon as it is done and pages run in parallel
    interactive_batch: int = 1
    #: a stage that runs as several tasks (``SPLIT_STAGES`` / ``CHAINED_STAGES``): one route per part
    parts: dict[str, RoutePart] = field(default_factory=dict)

    def batch_size(self, *, interactive: bool, project_default: int) -> int:
        size = self.interactive_batch if interactive else (self.batch or project_default)
        return max(1, size)

    def part(self, name: str) -> RoutePart:
        try:
            return self.parts[name]
        except KeyError:
            raise KeyError(f"route {self.task or '(split)'} has no part {name!r}") from None


#: leaves per bulk job when a chunked stage runs on a service (evilflowers-books-digitalizer#4)
SERVICE_BATCH = 32

#: stages a service runs as parallel parts: every leaf batch goes to each of them
SPLIT_STAGES: dict[str, tuple[str, ...]] = {"analyze": ("hocr", "layout")}
#: stages a service runs as parts in order, one job each (the next is planned when the
#: previous one is applied)
CHAINED_STAGES: dict[str, tuple[str, ...]] = {"render": ("prepare", "book")}
#: the parts a service-backed split / chained stage must be configured with
REQUIRED_PARTS: dict[str, tuple[str, ...]] = {"analyze": ("hocr", "layout"), "render": ("book",)}
#: parts with a default route (overridable): planned on demand, or the orchestrator's own
DEFAULT_PARTS: dict[str, dict[str, RoutePart]] = {
    # the page image itself changes only on scan-service (confirmed "remove" boxes)
    "analyze": {"erase": RoutePart("evilflowers_scan.erase", "evilflowers_scan.standalone",
                                   "evilflowers_scan.standalone.interactive")},
    # metadata lookup, item text files and the render request: light, the orchestrator's
    "render": {"prepare": RoutePart("digitalizer.local.render_prepare", PORTAL_QUEUE,
                                    PORTAL_QUEUE, backend="local")},
}

#: the in-process default of every stage (today's stage bodies, now ledger jobs)
DEFAULT_ROUTES: dict[str, StageRoute] = {
    "clean": StageRoute("local", "digitalizer.local.clean", CPU_QUEUE, INTERACTIVE_QUEUE, 3600),
    "analyze": StageRoute("local", "digitalizer.local.analyze", AI_QUEUE, INTERACTIVE_QUEUE, 1800),
    "formulas": StageRoute("local", "digitalizer.local.formulas", AI_QUEUE, AI_QUEUE, 4 * 3600),
    "render": StageRoute("local", "digitalizer.local.render", CPU_QUEUE, CPU_QUEUE, 8 * 3600),
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
    #: Paths in a service job are relative to it; needed only by service-backed stages.
    storage_root: Path | None = None
    labelling: LabellingConfig = field(default_factory=LabellingConfig)

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


def _project(slug: str, raw: dict[str, Any], resolve: _Paths) -> ProjectConfig:
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
        layout_detectors=tuple(raw.get("layout_detectors", ["heuristic"])),
        formula_recognizer=raw.get("formula_recognizer", "none"),
        outputs=tuple(raw.get("outputs", ["pdf", "pdfa", "epub"])),
        auto_publish=raw.get("auto_publish", "gate"),
        metadata=metadata,
        publish=publish,
        quality=QualityPolicy(**raw.get("quality", {})),
        pipeline=dict(raw.get("pipeline", {})),
    )


def _parts(stage: str, raw: dict[str, Any]) -> dict[str, RoutePart]:
    """The parts of a split / chained stage on services: every required one configured.

    A chained stage's single service part may also be given as the stage's own ``task`` /
    ``queue`` (``[orchestration.stages.render] task = "evilflowers_render.book"``).
    """
    required = REQUIRED_PARTS[stage]
    if len(required) == 1 and required[0] not in raw and {"task", "queue"} <= raw.keys():
        raw = {**raw, required[0]: {k: raw[k] for k in ("task", "queue", "interactive_queue")
                                    if k in raw}}
    missing = [p for p in required if not {"task", "queue"} <= set(raw.get(p, {}))]
    if missing:
        raise ValueError(f"[orchestration.stages.{stage}] on services needs "
                         + ", ".join(f"[orchestration.stages.{stage}.{p}] with task and queue"
                                     for p in missing)
                         + " (a stage runs entirely in-process or entirely on services)")
    parts = dict(DEFAULT_PARTS.get(stage, {}))
    for name in (*required, *parts):
        if name in raw:
            p = raw[name]
            parts[name] = RoutePart(p["task"], p["queue"], p.get("interactive_queue", p["queue"]),
                                    backend=parts[name].backend if name in parts else "service")
    return parts


def _orchestration(raw: dict[str, Any]) -> OrchestrationConfig:
    routes = dict(DEFAULT_ROUTES)
    for stage, override in raw.get("stages", {}).items():
        if stage not in DEFAULT_ROUTES:
            raise ValueError(f"[orchestration.stages.{stage}]: unknown stage "
                             f"(known: {', '.join(DEFAULT_ROUTES)})")
        base = routes[stage]
        backend = override.get("backend", base.backend)
        if backend not in ("local", "service"):
            raise ValueError(f"[orchestration.stages.{stage}] backend must be local or service")
        split = backend == "service" and stage in REQUIRED_PARTS
        parts = _parts(stage, override) if split else {}
        if backend == "service" and not split and not {"task", "queue"} <= override.keys():
            raise ValueError(f"[orchestration.stages.{stage}] a service backend needs task and queue")
        queue = override.get("queue", base.queue)
        batch = override.get("batch", SERVICE_BATCH if backend == "service" else base.batch)
        routes[stage] = StageRoute(backend, "" if split else override.get("task", base.task),
                                   "" if split else queue,
                                   "" if split else override.get(
                                       "interactive_queue",
                                       queue if backend == "service" else base.interactive_queue),
                                   int(override.get("deadline_seconds", base.deadline_seconds)),
                                   batch=int(batch) if batch is not None else None,
                                   interactive_batch=int(override.get("interactive_batch",
                                                                      base.interactive_batch)),
                                   parts=parts)
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
    )
    _check_shared_storage(config)
    return config


def _check_shared_storage(config: StudioConfig) -> None:
    """A service-backed stage exchanges paths relative to the storage root: fail at load time,
    not on the first job, when the scans or the artifacts live outside it."""
    if not any(r.backend == "service" for r in config.orchestration.routes.values()):
        return
    if config.storage_root is None:
        raise ValueError("a service-backed stage needs a storage root "
                         f"([studio] storage_root or ${STORAGE_ROOT_ENV})")
    root = config.storage_root
    outside = [str(p) for p in (config.artifacts_root,
                                *(pc.scans_root for pc in config.projects.values()))
               if not p.is_relative_to(root)]
    if outside:
        raise ValueError(f"service-backed stages need every scan and artifact path under the "
                         f"storage root {root}; outside it: {', '.join(outside)}")
