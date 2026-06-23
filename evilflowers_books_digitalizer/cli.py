"""Command-line interface for the digitalization pipeline.

    python -m evilflowers_books_digitalizer <command> [...]

Commands:
    list             list books on a source
    run-book         digitize one book (no Prefect; direct, for testing)
    run-source       digitize a faculty via the Prefect flow
    run-corpus       digitize every configured faculty via the Prefect flow
    validate-catalog report Excel catalog match/miss against the mounted sources
    preview-cover    render one cover from catalog metadata (iterate on style)
    serve            apply the Prefect deployments in deploy/prefect.yaml
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
    from evilflowers_books_digitalizer.orchestration.flows import digitize_source

    res = digitize_source(args.source, limit=args.limit, config_path=args.config)
    print(json.dumps(res["counts"], indent=2))
    return 0


def cmd_run_corpus(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.orchestration.flows import digitize_corpus

    res = digitize_corpus(limit=args.limit, config_path=args.config)
    print(json.dumps(res["totals"], indent=2))
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


def cmd_serve(args: argparse.Namespace) -> int:
    import subprocess

    prefect_file = Path(__file__).resolve().parent.parent / "deploy" / "prefect.yaml"
    cmd = ["prefect", "--no-prompt", "deploy", "--all", "--prefect-file", str(prefect_file)]
    print("running:", " ".join(cmd))
    return subprocess.call(cmd)


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

    p = sub.add_parser("run-source", help="digitize a faculty via Prefect")
    p.add_argument("source")
    p.add_argument("--limit", type=int)
    _add_config_arg(p)
    p.set_defaults(func=cmd_run_source)

    p = sub.add_parser("run-corpus", help="digitize all faculties via Prefect")
    p.add_argument("--limit", type=int)
    _add_config_arg(p)
    p.set_defaults(func=cmd_run_corpus)

    p = sub.add_parser("validate-catalog", help="Excel match/miss report")
    p.add_argument("source", nargs="?", help="one source, or all if omitted")
    _add_config_arg(p)
    p.set_defaults(func=cmd_validate_catalog)

    p = sub.add_parser("preview-cover", help="render one cover to a file")
    p.add_argument("source")
    p.add_argument("book_id")
    p.add_argument("--template", help="banner | minimal")
    p.add_argument("--out", help="output path")
    _add_config_arg(p)
    p.set_defaults(func=cmd_preview_cover)

    p = sub.add_parser("serve", help="apply Prefect deployments")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
