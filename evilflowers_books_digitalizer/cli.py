"""Command-line interface for the digitalization pipeline.

    python -m evilflowers_books_digitalizer <command> [...]

Commands:
    list             list books on a source
    run-book         digitize one book (direct, for testing)
    run-source       digitize a faculty (process-pool runner)
    run-corpus       digitize every configured faculty
    build-catalog    walk the sources and write a (ISBN-enriched) catalog .xlsx
    validate-catalog report Excel catalog match/miss against the mounted sources
    preview-cover    render one cover from catalog metadata (iterate on style)
    stats            summarize results (rich table; --json or --export csv|json|html)
    monitor          live TUI dashboard over the batch reports
    export-manifests write catalog entry manifests (*.entry.json) for produced books
    publish-book     import one produced book into the EvilFlowers Catalog
    publish-catalog  import produced books into the EvilFlowers Catalog (idempotent)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from evilflowers_books_digitalizer.runtime import build_catalog, load_runtime
from evilflowers_books_digitalizer.sources import build_source


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="path to pipeline.toml (default: configs/pipeline.toml)")


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
    from evilflowers_books_digitalizer.batch import process_book

    row = process_book(
        args.source,
        args.book_id,
        jobs=args.jobs,
        keep_cache=args.keep_cache,
        config_path=args.config,
    )
    print(json.dumps(row, indent=2, ensure_ascii=False))
    return 1 if row.get("status") == "error" else 0


def cmd_run_source(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.runner import run_source

    _setup_run_logging(args)
    res = run_source(args.source, limit=args.limit, config_path=args.config)
    print(json.dumps(res["counts"], indent=2))
    return 0


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
    log_file = Path(args.log_file) if args.log_file else rt.output_dir / "logs" / "digitizer.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    logging.info("logging to %s", log_file)


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

    out = Path(args.out) if args.out else Path(rt.metadata.get("excel_path", "configs/catalog.xlsx"))
    write_draft_xlsx(drafts, out, enricher=enricher)
    print(f"wrote {out}  ({len(drafts)} books)")
    return 0


def cmd_validate_catalog(args: argparse.Namespace) -> int:
    rt = load_runtime(args.config)
    catalog = build_catalog(rt.metadata)
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

    catalog = build_catalog(rt.metadata)
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
    from evilflowers_books_digitalizer.catalog.publisher import publish_book

    row = publish_book(args.source, args.book_id, config_path=args.config, dry_run=args.dry_run)
    print(json.dumps(row, indent=2, ensure_ascii=False))
    return 1 if row.get("status") == "error" else 0


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evilflowers-digitalizer",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list books on a source")
    p.add_argument("source")
    p.add_argument("--limit", type=int)
    _add_config_arg(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run-book", help="digitize one book directly")
    p.add_argument("source")
    p.add_argument("book_id")
    p.add_argument("--jobs", type=int)
    p.add_argument("--keep-cache", action="store_true")
    _add_config_arg(p)
    p.set_defaults(func=cmd_run_book)

    p = sub.add_parser("run-source", help="digitize a faculty (process pool)")
    p.add_argument("source")
    p.add_argument("--limit", type=int)
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

    p = sub.add_parser("export-manifests", help="write catalog entry manifests for produced books")
    p.add_argument("source", nargs="?", help="one source, or all if omitted")
    p.add_argument("--limit", type=int)
    _add_config_arg(p)
    p.set_defaults(func=cmd_export_manifests)

    p = sub.add_parser("publish-book", help="import one produced book into the catalog")
    p.add_argument("source")
    p.add_argument("book_id")
    p.add_argument("--dry-run", action="store_true", help="build the manifest but don't push")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
