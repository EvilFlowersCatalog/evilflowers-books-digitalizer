"""Command-line interface of the books digitalizer (portal + orchestration).

    evilflowers-digitalizer <command> [...]
    python -m evilflowers_books_digitalizer <command> [...]

Commands:
    studio           serve | worker | import | run | export-ia | export-training |
                     sample-labels | label | labelling-status | export-labels | mcp |
                     db-upgrade (alias migrate)
    db               upgrade | current | revision — the studio database

Every command reads the studio config (``--config``, else ``$STUDIO_CONFIG``, else
``configs/studio.toml``). The batch pipeline's commands (``run-*``, ``stats``, ``monitor``,
``publish-*``, ``build-catalog``, …) were retired with it (evilflowers-books-digitalizer#6):
digitalization runs as standalone service jobs the orchestrator dispatches.

Environment:
    STUDIO_CONFIG          studio TOML when --config is not given
    EVILFLOWERS_LOG_FORMAT text (default) | json — log lines on stderr
    EVILFLOWERS_LOG_LEVEL  DEBUG | INFO (default) | WARN | ERROR

Results (JSON) go to stdout; logs go to stderr.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from evilflowers_books_digitalizer.logs import setup_logging

logger = logging.getLogger("evilflowers_books_digitalizer.cli")

#: Celery app of the orchestrator (docs/design/target-architecture.md)
STUDIO_CELERY_APP = "evilflowers_books_digitalizer.studio.workers.app:celery"
CONFIG_HELP = "studio TOML (default: $STUDIO_CONFIG, else configs/studio.toml)"


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
    """Replace this process with the orchestrator's Celery worker (same interpreter)."""
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
def cmd_studio_sample_labels(args: argparse.Namespace) -> Any:
    from evilflowers_books_digitalizer.studio.application.labelling import sample

    return sample(_studio_services(args), args.task, args.n, max_entries=args.max_entries)


def _labeller(args: argparse.Namespace) -> str | None:
    import os

    return (args.operator or os.environ.get("STUDIO_OPERATOR") or "").strip() or None


@_studio_command
def cmd_studio_label(args: argparse.Namespace) -> Any:
    """Label in the terminal (interactive), or record one answer (``--item`` + ``--value``)."""
    from evilflowers_books_digitalizer.studio.application import labelling
    from evilflowers_books_digitalizer.studio.application.errors import BadRequest
    from evilflowers_books_digitalizer.studio.labelling_cli import label_loop

    svc = _studio_services(args)
    who = _labeller(args)
    if args.item is not None:
        if not args.value:
            raise BadRequest("--item needs --value (a label key, or skip)")
        answers = [labelling.LabelIn(task=args.task, value=args.value)]
        if args.table_quality:
            answers.append(labelling.LabelIn(task="table_quality", value=args.table_quality))
        out = labelling.submit(svc, args.item, labelling.LabelsIn(labels=answers), who, via="cli")
        return {"item_id": out.id, "recorded": out.mine}
    label_loop(svc, args.task, labelling.require_labeller(who),
               crops=Path(args.crops) if args.crops else None, open_crops=args.open)
    return None


@_studio_command
def cmd_studio_labelling_status(args: argparse.Namespace) -> Any:
    from evilflowers_books_digitalizer.studio.labelling_cli import status_table

    print(status_table(_studio_services(args), _labeller(args)))
    return None


def cmd_studio_mcp(args: argparse.Namespace) -> int:
    """The studio MCP server over stdio (the same tools as ``/mcp`` on the portal)."""
    from evilflowers_books_digitalizer.studio.api.mcp import build_mcp

    build_mcp(_studio_services(args)).run("stdio")
    return 0


@_studio_command
def cmd_studio_export_labels(args: argparse.Namespace) -> Any:
    """JSONL to ``--out`` (or stdout); the summary (counts, kappa) is printed as JSON on stderr
    and written next to ``--out`` as ``<out>.summary.json``."""
    from evilflowers_books_digitalizer.studio.application.labelling import export_lines, summary

    svc = _studio_services(args)
    lines = [json.dumps(line, ensure_ascii=False) + "\n" for line in export_lines(svc, args.task)]
    info = summary(svc, args.task).model_dump(mode="json")
    if args.out and args.out != "-":
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(lines), encoding="utf-8")
        out.with_name(out.name + ".summary.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        sys.stdout.write("".join(lines))
    print(json.dumps(info, indent=2, ensure_ascii=False), file=sys.stderr)
    return None


@_studio_command
def cmd_studio_db_upgrade(args: argparse.Namespace) -> Any:
    from evilflowers_books_digitalizer.db import store
    from evilflowers_books_digitalizer.studio.config import load_config

    url = load_config(args.config).database_url
    store.upgrade(url, args.revision)
    return {"database": url.split("@")[-1], "revision": store.current_revision(url)}


def cmd_db(args: argparse.Namespace) -> int:
    from evilflowers_books_digitalizer.db import store
    from evilflowers_books_digitalizer.studio.config import load_config

    url = load_config(args.config).database_url
    shown = url.split("@")[-1]
    match args.db_command:
        case "upgrade":
            store.upgrade(url, args.revision)
            print(f"{shown}: at revision {store.current_revision(url)}")
        case "current":
            print(f"{shown}: revision {store.current_revision(url) or '(empty — run db upgrade)'}")
        case "revision":
            store.revision(url, args.message)
    return 0


def _add_studio_parser(sub: Any) -> None:
    p = sub.add_parser("studio", help="restoration studio: API, orchestrator, imports, runs, labels")
    st = p.add_subparsers(dest="studio_command", required=True)

    def add(name: str, help_: str, func: Callable[[argparse.Namespace], int],
            aliases: Sequence[str] = ()) -> argparse.ArgumentParser:
        q: argparse.ArgumentParser = st.add_parser(name, help=help_, aliases=list(aliases))
        q.add_argument("--config", help=CONFIG_HELP)
        q.set_defaults(func=func)
        return q

    q = add("serve", "REST + SSE API and the SPA", cmd_studio_serve)
    q.add_argument("--host", default="127.0.0.1", help="bind address (no auth — VPN only)")
    q.add_argument("--port", type=int, default=8000)
    q = add("worker", "start the orchestrator's Celery worker", cmd_studio_worker)
    q.add_argument("-Q", "--queues", default="digitalizer.portal",
                   help="add -- -B to the single orchestrator worker (beat: pump, reconcile)")
    q.add_argument("--concurrency", type=int)
    q.add_argument("celery_args", nargs=argparse.REMAINDER, help="passed through to celery worker")
    q = add("import", "enqueue discovery of a project's scan tree", cmd_studio_import)
    q.add_argument("project")
    q.add_argument("collection", nargs="?")
    q = add("run", "create and enqueue a run for one book", cmd_studio_run)
    q.add_argument("book", help="book id or IA item identifier")
    q.add_argument("--kind", choices=("full", "rerun", "render", "publish"), default="full")
    q.add_argument("--pages", help="leaf indices for a rerun, comma separated")
    q.add_argument("--interactive", action="store_true", help="use the interactive lane")
    q.add_argument("--operator", help="recorded as the run's author")
    q = add("export-ia", "write a book's Internet Archive item zip", cmd_studio_export_ia)
    q.add_argument("book", help="book id or IA item identifier")
    q.add_argument("--out", default=".", help="output directory")
    q = add("export-training", "enqueue the labelled-regions export", cmd_studio_export_training)
    q.add_argument("project")
    from evilflowers_books_digitalizer.studio.labelling import SAMPLED_TASKS, TASKS

    q = add("sample-labels", "sample catalog items for hand labelling (decider calibration)",
            cmd_studio_sample_labels)
    q.add_argument("--task", required=True, choices=sorted(SAMPLED_TASKS))
    q.add_argument("--n", type=int, default=250, help="items to add (stratified by language and band)")
    q.add_argument("--max-entries", type=int, help="catalog entries to scan (default: [labelling])")
    q = add("export-labels", "write a task's labels as JSONL (+ summary with Cohen's kappa)",
            cmd_studio_export_labels)
    q.add_argument("--task", required=True, choices=sorted(TASKS))
    q.add_argument("--out", help="JSONL file (default: stdout); the summary goes next to it")
    q = add("label", "label items in the terminal (or record one answer with --item/--value)",
            cmd_studio_label)
    q.add_argument("--task", required=True, choices=sorted(SAMPLED_TASKS))
    q.add_argument("--operator", help="your name, recorded on every label (default: $STUDIO_OPERATOR)")
    q.add_argument("--item", type=int, help="answer this item only (non-interactive)")
    q.add_argument("--value", help="with --item: a label key of the task, or skip")
    q.add_argument("--table-quality", help="with --item on an is_data_table item: its table_quality")
    q.add_argument("--crops", help="directory for figure/table crops (default: <cache_dir>/labelling)")
    q.add_argument("--open", action="store_true", help="open each crop in the default image viewer")
    q = add("labelling-status", "progress of every labelling task", cmd_studio_labelling_status)
    q.add_argument("--operator", help="also count your own answers (default: $STUDIO_OPERATOR)")
    add("mcp", "MCP server over stdio: the labelling tools for an MCP client", cmd_studio_mcp)
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
    _add_studio_parser(sub)

    p = sub.add_parser("db", help="the studio database: migrations")
    db_sub = p.add_subparsers(dest="db_command", required=True)
    q = db_sub.add_parser("upgrade", help="apply migrations (default: to head)")
    q.add_argument("revision", nargs="?", default="head")
    q.add_argument("--config", help=CONFIG_HELP)
    q = db_sub.add_parser("current", help="show the schema revision")
    q.add_argument("--config", help=CONFIG_HELP)
    q = db_sub.add_parser("revision", help="autogenerate a migration from model changes (dev)")
    q.add_argument("-m", "--message", required=True)
    q.add_argument("--config", help=CONFIG_HELP)
    p.set_defaults(func=cmd_db)
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
