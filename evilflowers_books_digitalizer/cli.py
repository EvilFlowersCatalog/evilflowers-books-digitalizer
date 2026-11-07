"""Command-line interface for the digitalization pipeline.

    evilflowers-digitalizer <command> [...]
    python -m evilflowers_books_digitalizer <command> [...]

Commands:
    check            preflight: mounts, writable dirs, disk, binaries [, catalog]
    list             list books on a source
    run-book         digitize the given books in turn [and publish each]
    run-source       digitize a faculty (process-pool runner) [and publish]
    run-corpus       digitize every configured faculty
    build-catalog    walk the sources and write a (ISBN-enriched) catalog .xlsx
    validate-catalog report Excel catalog match/miss against the mounted sources
    preview-cover    render one cover from catalog metadata (iterate on style)
    stats            summarize results (rich table; --json or --export csv|json|html)
    monitor          live TUI dashboard over the batch reports
    export-manifests write catalog entry manifests (*.entry.json) for produced books
    publish-book     import produced books into the EvilFlowers Catalog
    publish-catalog  import produced books into the EvilFlowers Catalog (idempotent)
    db               upgrade | current | import | revision — the processing database
    studio           restoration studio: serve | worker | import | run | export-ia |
                     export-training | db-upgrade (alias migrate) (config: $STUDIO_CONFIG)

Environment:
    EVILFLOWERS_DIGITALIZER_CONFIG  config file when --config is not given
    EVILFLOWERS_LOG_FORMAT          text (default) | json — log lines on stderr
    EVILFLOWERS_LOG_LEVEL           DEBUG | INFO (default) | WARN | ERROR
    EVILFLOWERS_API_KEY             catalog token (name set by [catalog].api_key_env)

Results (JSON rows/summaries) go to stdout; logs go to stderr.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.logs import add_file_handler, setup_logging
from evilflowers_books_digitalizer.runtime import CONFIG_ENV, build_catalog, load_runtime
from evilflowers_books_digitalizer.sources import build_source

logger = logging.getLogger("evilflowers_books_digitalizer.cli")


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        help=f"path to pipeline.toml (default: ${CONFIG_ENV}, else configs/pipeline.toml)",
    )


def _has_error(rows: list[dict[str, Any]]) -> bool:
    """True if a book failed to digitize or to publish."""
    return any(
        row.get("status") == "error" or (row.get("publish") or {}).get("status") == "error"
        for row in rows
    )


def _publish_hook(config_path: str | None) -> Callable[[dict[str, Any]], None]:
    """``on_result`` hook: publish each book that digitized (or was already done)."""
    from evilflowers_books_digitalizer.catalog.publisher import Publisher

    publisher: Publisher | None = None

    def hook(row: dict[str, Any]) -> None:
        nonlocal publisher
        if row.get("status") not in ("ok", "skipped"):
            return
        if publisher is None:
            publisher = Publisher(load_runtime(config_path))
        if not publisher.settings.enabled:
            row["publish"] = {"status": "error", "error": "[catalog].enabled is false"}
            return
        row["publish"] = publisher.publish_book(str(row["source"]), str(row["book_id"]))

    return hook


def cmd_check(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.preflight import Check, run_checks

    try:
        rt = load_runtime(args.config)
    except (OSError, ValueError) as exc:  # missing file, bad TOML
        print(Check("config", False, str(exc)).line())
        return 1
    checks = run_checks(rt, catalog=args.catalog)
    for check in checks:
        print(check.line())
    failed = [c for c in checks if not c.ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed", file=sys.stderr)
    return 1 if failed else 0


def cmd_list(args: argparse.Namespace) -> int:
    rt = load_runtime(args.config)
    books = build_source(rt.source, args.source).list_books()
    if args.limit:
        books = books[: args.limit]
    for book in books:
        print(book)
    print(f"\n{len(books)} books on {args.source}", file=sys.stderr)
    return 0


def cmd_run_book(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.runner import run_source

    _setup_run_logging(args)
    res = run_source(
        args.source,
        book_ids=list(args.book_ids),
        max_parallel=1,  # "each id in turn"
        jobs=args.jobs,
        keep_cache=args.keep_cache,
        config_path=args.config,
        on_result=_publish_hook(args.config) if args.publish else None,
    )
    print(json.dumps(res["rows"], indent=2, ensure_ascii=False))
    return 1 if _has_error(res["rows"]) else 0


def cmd_run_source(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.runner import run_source

    _setup_run_logging(args)
    res = run_source(
        args.source,
        limit=args.limit,
        config_path=args.config,
        on_result=_publish_hook(args.config) if args.publish else None,
    )
    if args.publish:
        published: dict[str, int] = {}
        for row in res["rows"]:
            status = (row.get("publish") or {}).get("status")
            if status:
                published[status] = published.get(status, 0) + 1
        print(json.dumps({"digitize": res["counts"], "publish": published}, indent=2))
    else:  # unchanged shape: the bare digitization counts
        print(json.dumps(res["counts"], indent=2))
    return 1 if _has_error(res["rows"]) else 0


def cmd_run_corpus(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.runner import run_corpus

    _setup_run_logging(args)
    res = run_corpus(limit=args.limit, config_path=args.config)
    print(json.dumps(res["totals"], indent=2))
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.monitor import monitor

    monitor(config_path=args.config, interval=args.interval, once=args.once)
    return 0


def _setup_run_logging(args: argparse.Namespace) -> None:
    """Add a timestamped file log (alongside the console) for long runs."""
    rt = load_runtime(args.config)
    log_arg = getattr(args, "log_file", None)
    log_file = Path(log_arg) if log_arg else rt.output_dir / "logs" / "digitizer.log"
    try:
        add_file_handler(log_file)
    except OSError as exc:  # a read-only output volume must not stop the run
        logger.warning("cannot write log file %s: %s", log_file, exc)
        return
    logger.info("logging to %s", log_file)


def cmd_build_catalog(args: argparse.Namespace) -> int:
    from tqdm import tqdm

    from evilflowers_books_digitalizer.metadata.draft import DraftBook, derive_isbn, write_draft_xlsx
    from evilflowers_books_digitalizer.metadata.isbn_lookup import IsbnEnricher, is_valid_isbn

    rt = load_runtime(args.config)
    faculty_names = rt.faculty_names()
    keys = [args.source] if args.source else rt.source_keys

    drafts: list[DraftBook] = []
    for key in keys:
        source = build_source(rt.source, key)
        book_ids = source.list_books()
        print(f"{key}: {len(book_ids)} books", file=sys.stderr)
        for book_id in book_ids:
            n_pages = None
            if args.pages:
                try:
                    n_pages = source.get_book(book_id).n_pages
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! {book_id}: {exc}", file=sys.stderr)
            drafts.append(DraftBook(key, book_id, faculty_names.get(key, key.upper()), n_pages))

    enricher = None
    if not args.no_enrich:
        enricher = IsbnEnricher(cache_dir=rt.cache_dir / "isbn_lookup")
        isbns = [i for d in drafts if is_valid_isbn(i := derive_isbn(d.book_id))]
        print(f"enriching {len(isbns)} ISBNs from the catalogue (cached)…", file=sys.stderr)
        for isbn in tqdm(isbns, desc="isbn", unit="isbn"):
            enricher.lookup(isbn)
        print(f"  resolved {enricher.hits}, no match {enricher.misses}, "
              f"transient {enricher.transient}", file=sys.stderr)

    out = (
        Path(args.out) if args.out
        else Path(rt.metadata.get("excel_path") or rt.config_dir / "catalog.xlsx")
    )
    write_draft_xlsx(drafts, out, enricher=enricher)
    print(f"wrote {out}  ({len(drafts)} books)")
    return 0


def cmd_validate_catalog(args: argparse.Namespace) -> int:
    rt = load_runtime(args.config)
    catalog = build_catalog(rt.metadata, base_dir=rt.config_dir)
    if catalog is None:
        print("metadata disabled or no excel_path — nothing to validate", file=sys.stderr)
        return 1
    keys = [args.source] if args.source else rt.source_keys
    overall = {"books": 0, "matched": 0, "missed": 0}
    for key in keys:
        try:
            books = build_source(rt.source, key).list_books()
        except FileNotFoundError as exc:
            print(f"{key}: cannot list ({exc})", file=sys.stderr)
            continue
        report = catalog.match_report(books)
        print(
            f"{key:4s}  books={report['books']:4d}  matched={report['matched']:4d}  "
            f"missed={report['missed']:4d}"
        )
        if report["miss_sample"]:
            print(f"      missing e.g.: {report['miss_sample'][:5]}")
        for field in overall:
            overall[field] += report[field]
    print(
        f"\nTOTAL books={overall['books']} matched={overall['matched']} "
        f"missed={overall['missed']} (catalog rows={len(catalog)})"
    )
    return 0


def cmd_preview_cover(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.covers.renderer import CoverRenderer

    rt = load_runtime(args.config)
    cover_cfg = dict(rt.cover)
    if args.template:
        cover_cfg["template"] = args.template
    cover_cfg.setdefault("enabled", True)
    renderer = CoverRenderer.from_config(cover_cfg)

    catalog = build_catalog(rt.metadata, base_dir=rt.config_dir)
    faculty = rt.faculty_names().get(args.source, args.source.upper())
    if catalog is not None:
        meta = catalog.lookup(args.book_id, faculty=faculty)
    else:
        from evilflowers_books_digitalizer.metadata.catalog import MetadataCatalog

        meta = MetadataCatalog.stub(args.book_id, faculty=faculty)
    meta.faculty = meta.faculty or faculty

    out = (
        Path(args.out) if args.out else Path(f"{args.source}_{args.book_id}.cover{renderer.suffix}")
    )
    renderer.render_to_file(meta, out)
    print(f"wrote {out}  (title={meta.title!r}, matched={meta.matched})")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.reporting import (
        latest_per_book,
        load_reports,
        summarize_by_source,
        summarize_reports,
        summarize_publish,
    )

    rt = load_runtime(args.config)
    rows = latest_per_book(load_reports(rt.output_dir))
    if args.source:
        rows = [r for r in rows if r.get("source") == args.source]
    if not rows:
        scope = f" for {args.source}" if args.source else ""
        print(f"no batch reports{scope} under {rt.output_dir} — run a batch first", file=sys.stderr)
        return 1

    if args.export:
        from evilflowers_books_digitalizer.dashboard import book_totals
        from evilflowers_books_digitalizer.exports import default_path, export_report

        out = Path(args.out) if args.out else default_path(rt.output_dir, args.export, args.source)
        export_report(
            rows, args.export, out,
            totals=book_totals(rt),
            publish=summarize_publish(rt.output_dir),
        )
        print(f"wrote {out}")
        return 0

    if args.json:
        print(json.dumps({"overall": summarize_reports(rows),
                          "by_source": summarize_by_source(rows)}, indent=2, ensure_ascii=False))
        return 0

    from rich.console import Console

    from evilflowers_books_digitalizer.dashboard import book_totals, build

    sources = [args.source] if args.source else None
    Console().print(build(rt, book_totals(rt), sources=sources))
    return 0


def cmd_export_manifests(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.catalog.publisher import export_manifests

    rt = load_runtime(args.config)
    sources = [args.source] if args.source else None
    paths = export_manifests(rt, sources, limit=args.limit)
    print(f"wrote {len(paths)} manifests under {rt.output_dir}")
    return 0


def cmd_publish_book(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.catalog.publisher import publish_books

    rows = publish_books(
        args.source, list(args.book_ids),
        config_path=args.config, dry_run=args.dry_run, force=args.force,
    )
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 1 if _has_error(rows) else 0


def cmd_publish_catalog(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.catalog.publisher import publish_corpus

    _setup_run_logging(args)
    res = publish_corpus(
        config_path=args.config,
        sources=[args.source] if args.source else None,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(json.dumps(res["totals"], indent=2, ensure_ascii=False))
    return 1 if res["totals"].get("error") else 0


def cmd_db(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.db import store

    rt = load_runtime(args.config)
    url = store.database_url(rt)
    match args.db_command:
        case "upgrade":
            store.upgrade(url, args.revision)
            print(f"{url}: at revision {store.current_revision(url)}")
        case "current":
            print(f"{url}: revision {store.current_revision(url) or '(empty — run db upgrade)'}")
        case "revision":
            store.revision(url, args.message)
        case "import":
            from evilflowers_books_digitalizer.db.importer import import_outputs

            store.upgrade(url)
            counts = import_outputs(url, rt.output_dir, rt.source_keys)
            print(json.dumps({"database": url, **counts}, indent=2))
    return 0


# ---------------------------------------------------------------- restoration studio
#: Celery app of the batch side (docs/design/studio-spec.md, "Queues and tasks")
STUDIO_CELERY_APP = "evilflowers_books_digitalizer.studio.workers.app:celery"


def _studio_services(args: argparse.Namespace) -> Any:
    from evilflowers_books_digitalizer.studio.api.wiring import build_services
    from evilflowers_books_digitalizer.studio.config import load_config

    return build_services(load_config(args.config))


def _studio_command(fn: Callable[[argparse.Namespace], Any]) -> Callable[[argparse.Namespace], int]:
    """Print a use case's result as JSON; an application refusal is one stderr line + exit 1."""
    def run(args: argparse.Namespace) -> int:
        from evilflowers_books_digitalizer.studio.application.errors import ApplicationError

        try:
            result = fn(args)
        except ApplicationError as exc:
            print(f"evilflowers-digitalizer studio: {exc}", file=sys.stderr)
            return 1
        if result is not None:
            data = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return 0
    return run


def cmd_studio_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from evilflowers_books_digitalizer.studio.api.app import create_app
    from evilflowers_books_digitalizer.studio.config import load_config

    print(f"studio: serving http://{args.host}:{args.port}  (no auth — VPN only)", file=sys.stderr)
    uvicorn.run(create_app(load_config(args.config)), host=args.host, port=args.port,
                log_level="info")
    return 0


def cmd_studio_worker(args: argparse.Namespace) -> int:
    """Replace this process with a Celery worker (same interpreter, the batch side's app)."""
    import os

    # argparse.REMAINDER keeps a leading "--"; celery would read it as "extra config" and fail
    extra = args.celery_args[1:] if args.celery_args[:1] == ["--"] else args.celery_args
    argv = [sys.executable, "-m", "celery", "-A", STUDIO_CELERY_APP, "worker", "-Q", args.queues,
            *(["--concurrency", str(args.concurrency)] if args.concurrency else []),
            *extra]
    if args.config:
        os.environ["STUDIO_CONFIG"] = str(Path(args.config).resolve())
    os.execv(sys.executable, argv)
    return 0   # unreachable


@_studio_command
def cmd_studio_import(args: argparse.Namespace) -> Any:
    from evilflowers_books_digitalizer.studio.application.books import import_collection

    return import_collection(_studio_services(args), args.project, args.collection)


@_studio_command
def cmd_studio_run(args: argparse.Namespace) -> Any:
    from evilflowers_books_digitalizer.studio.application.dto import RunIn
    from evilflowers_books_digitalizer.studio.application.lookup import find_book
    from evilflowers_books_digitalizer.studio.application.runs import create_run

    svc = _studio_services(args)
    with svc.uow() as uow:
        book_id = find_book(uow.session, args.book).id
    pages = [int(p) for p in args.pages.split(",")] if args.pages else None
    cmd = RunIn(kind=args.kind, pages=pages, interactive=args.interactive or None)
    return create_run(svc, book_id, cmd, args.operator)


@_studio_command
def cmd_studio_export_ia(args: argparse.Namespace) -> Any:
    from evilflowers_books_digitalizer.studio.api.wiring import build_ia_exporter
    from evilflowers_books_digitalizer.studio.application.export import export_ia
    from evilflowers_books_digitalizer.studio.application.lookup import find_book

    svc = _studio_services(args)
    with svc.uow() as uow:
        book_id = find_book(uow.session, args.book).id
    path = export_ia(svc, build_ia_exporter(svc.config), book_id, Path(args.out))
    return {"book_id": book_id, "zip": str(path)}


@_studio_command
def cmd_studio_export_training(args: argparse.Namespace) -> Any:
    from evilflowers_books_digitalizer.studio.application.export import export_training

    return export_training(_studio_services(args), args.project)


@_studio_command
def cmd_studio_db_upgrade(args: argparse.Namespace) -> Any:
    from evilflowers_books_digitalizer.db import store
    from evilflowers_books_digitalizer.studio.config import load_config

    url = load_config(args.config).database_url
    store.upgrade(url, args.revision)
    return {"database": url.split("@")[-1], "revision": store.current_revision(url)}


def _add_studio_parser(sub: Any) -> None:
    p = sub.add_parser("studio", help="restoration studio: API, workers, imports, runs, exports")
    st = p.add_subparsers(dest="studio_command", required=True)

    def add(name: str, help_: str, func: Callable[[argparse.Namespace], int],
            aliases: Sequence[str] = ()) -> argparse.ArgumentParser:
        q: argparse.ArgumentParser = st.add_parser(name, help=help_, aliases=list(aliases))
        q.add_argument("--config", help="studio TOML (default: $STUDIO_CONFIG, else configs/studio.toml)")
        q.set_defaults(func=func)
        return q

    q = add("serve", "REST + SSE API and the SPA", cmd_studio_serve)
    q.add_argument("--host", default="127.0.0.1", help="bind address (no auth — VPN only)")
    q.add_argument("--port", type=int, default=8000)
    q = add("worker", "start a Celery worker (batch side)", cmd_studio_worker)
    q.add_argument("-Q", "--queues", default="interactive,cpu,ai,io")
    q.add_argument("--concurrency", type=int)
    q.add_argument("celery_args", nargs=argparse.REMAINDER, help="passed through to celery worker")
    q = add("import", "enqueue discovery of a project's scan tree", cmd_studio_import)
    q.add_argument("project")
    q.add_argument("collection", nargs="?")
    q = add("run", "create and enqueue a run for one book", cmd_studio_run)
    q.add_argument("book", help="book id or IA item identifier")
    q.add_argument("--kind", choices=("full", "rerun", "render", "publish"), default="full")
    q.add_argument("--pages", help="leaf indices for a rerun, comma separated")
    q.add_argument("--interactive", action="store_true", help="use the interactive queue")
    q.add_argument("--operator", help="recorded as the run's author")
    q = add("export-ia", "write a book's Internet Archive item zip", cmd_studio_export_ia)
    q.add_argument("book", help="book id or IA item identifier")
    q.add_argument("--out", default=".", help="output directory")
    q = add("export-training", "enqueue the labelled-regions export", cmd_studio_export_training)
    q.add_argument("project")
    q = add("db-upgrade", "apply the studio database migrations", cmd_studio_db_upgrade,
            aliases=("migrate",))
    q.add_argument("revision", nargs="?", default="head")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evilflowers-digitalizer",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="preflight: mounts, dirs, disk, binaries; exit 1 on failure")
    p.add_argument("--catalog", action="store_true",
                   help="also check the API key, catalog reachability and target catalogs")
    _add_config_arg(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("list", help="list books on a source")
    p.add_argument("source")
    p.add_argument("--limit", type=int)
    _add_config_arg(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run-book", help="digitize books one after another (resumable)")
    p.add_argument("source")
    p.add_argument("book_ids", nargs="+", metavar="ID", help="book directory name(s)")
    p.add_argument("--publish", action="store_true",
                   help="publish each book to the catalog right after it succeeds")
    p.add_argument("--jobs", type=int, help="OCR threads (default: [orchestration].ocr_jobs)")
    p.add_argument("--keep-cache", action="store_true")
    p.add_argument("--log-file", help="log file (default: <output_dir>/logs/digitizer.log)")
    _add_config_arg(p)
    p.set_defaults(func=cmd_run_book)

    p = sub.add_parser("run-source", help="digitize a faculty (process pool)")
    p.add_argument("source")
    p.add_argument("--limit", type=int)
    p.add_argument("--publish", action="store_true",
                   help="publish each book to the catalog as soon as it succeeds")
    p.add_argument("--log-file", help="log file (default: <output_dir>/logs/digitizer.log)")
    _add_config_arg(p)
    p.set_defaults(func=cmd_run_source)

    p = sub.add_parser("run-corpus", help="digitize all configured faculties")
    p.add_argument("--limit", type=int)
    p.add_argument("--log-file", help="log file (default: <output_dir>/logs/digitizer.log)")
    _add_config_arg(p)
    p.set_defaults(func=cmd_run_corpus)

    p = sub.add_parser("build-catalog", help="generate a catalog .xlsx from the sources")
    p.add_argument("source", nargs="?", help="one source, or all if omitted")
    p.add_argument("--out", help="output path (default: [metadata].excel_path)")
    p.add_argument("--no-enrich", action="store_true", help="skip ISBN lookup (faster, fewer fields)")
    p.add_argument("--pages", action="store_true", help="also fetch page counts (slow: one ls/book)")
    _add_config_arg(p)
    p.set_defaults(func=cmd_build_catalog)

    p = sub.add_parser("validate-catalog", help="Excel match/miss report")
    p.add_argument("source", nargs="?", help="one source, or all if omitted")
    _add_config_arg(p)
    p.set_defaults(func=cmd_validate_catalog)

    p = sub.add_parser("preview-cover", help="render one cover to a file")
    p.add_argument("source")
    p.add_argument("book_id")
    p.add_argument("--template", help="cover template (default: stu)")
    p.add_argument("--out", help="output path")
    _add_config_arg(p)
    p.set_defaults(func=cmd_preview_cover)

    p = sub.add_parser("stats", help="summarize batch results (rich table; or export)")
    p.add_argument("source", nargs="?", help="scope to one source, or all if omitted")
    p.add_argument("--json", action="store_true", help="print the summary as JSON instead of a table")
    p.add_argument("--export", choices=("csv", "json", "html"), help="write an export file instead of printing")
    p.add_argument("--out", help="export path (default: <output_dir>/stats[_<source>].<ext>)")
    _add_config_arg(p)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("monitor", help="live TUI dashboard")
    p.add_argument("--interval", type=float, default=5.0, help="refresh seconds")
    p.add_argument("--once", action="store_true", help="render once and exit")
    _add_config_arg(p)
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("db", help="processing database: migrations + import of existing outputs")
    db_sub = p.add_subparsers(dest="db_command", required=True)
    q = db_sub.add_parser("upgrade", help="apply migrations (default: to head)")
    q.add_argument("revision", nargs="?", default="head")
    _add_config_arg(q)
    q = db_sub.add_parser("current", help="show the schema revision")
    _add_config_arg(q)
    q = db_sub.add_parser("import", help="record already-produced books (reports + output tree)")
    _add_config_arg(q)
    q = db_sub.add_parser("revision", help="autogenerate a migration from model changes (dev)")
    q.add_argument("-m", "--message", required=True)
    _add_config_arg(q)
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("export-manifests", help="write catalog entry manifests for produced books")
    p.add_argument("source", nargs="?", help="one source, or all if omitted")
    p.add_argument("--limit", type=int)
    _add_config_arg(p)
    p.set_defaults(func=cmd_export_manifests)

    p = sub.add_parser("publish-book", help="import produced books into the catalog")
    p.add_argument("source")
    p.add_argument("book_ids", nargs="+", metavar="ID", help="book directory name(s)")
    p.add_argument("--dry-run", action="store_true",
                   help="build + validate payloads and write manifests; no HTTP")
    p.add_argument("--force", action="store_true",
                   help="ignore earlier published/skipped report rows (never duplicates)")
    _add_config_arg(p)
    p.set_defaults(func=cmd_publish_book)

    p = sub.add_parser("publish-catalog", help="import produced books into the catalog (idempotent)")
    p.add_argument("source", nargs="?", help="one source, or all if omitted")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true", help="re-publish books already in the report")
    p.add_argument("--dry-run", action="store_true", help="build manifests but don't push")
    p.add_argument("--log-file", help="log file (default: <output_dir>/logs/digitizer.log)")
    _add_config_arg(p)
    p.set_defaults(func=cmd_publish_catalog)

    _add_studio_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        setup_logging()
    except ValueError as exc:  # bad EVILFLOWERS_LOG_FORMAT / _LEVEL
        print(f"evilflowers-digitalizer: {exc}", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
