"""The CLI through the real argument parser: studio and db commands only (#6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evilflowers_books_digitalizer import cli


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "studio.toml"
    path.write_text(f"""
[studio]
database_url = "sqlite:///{tmp_path / 'studio.db'}"
storage_root = "{tmp_path}"
artifacts_root = "{{storage_root}}/digitized"
cache_dir = "{tmp_path / 'cache'}"
[projects.stu]
scans_root = "{{storage_root}}/raw-scans"
""", encoding="utf-8")
    return path


def test_db_upgrade_and_current(tmp_path, capsys):
    config = str(_config(tmp_path))
    assert cli.main(["db", "upgrade", "--config", config]) == 0
    assert "at revision 0008" in capsys.readouterr().out
    assert cli.main(["db", "current", "--config", config]) == 0
    assert "revision 0008" in capsys.readouterr().out


def test_studio_db_upgrade_prints_json(tmp_path, capsys):
    assert cli.main(["studio", "db-upgrade", "--config", str(_config(tmp_path))]) == 0
    assert '"revision": "0008"' in capsys.readouterr().out


@pytest.mark.parametrize("cmd", [
    ["studio", "serve", "--port", "9000"],
    ["studio", "worker", "--", "-B"],
    ["studio", "import", "stu", "fei"],
    ["studio", "run", "stu-fei-b1", "--kind", "rerun", "--pages", "1,2", "--interactive"],
    ["studio", "sample-labels", "--task", "scan_mark", "--n", "50"],
    ["studio", "export-labels", "--task", "title_page", "--out", "x.jsonl"],
    ["studio", "label", "--task", "scan_mark", "--operator", "jana"],
    ["db", "revision", "-m", "x"],
])
def test_commands_parse(cmd):
    args = cli.build_parser().parse_args(cmd)
    assert callable(args.func)


def test_the_worker_consumes_the_portal_queue_only():
    args = cli.build_parser().parse_args(["studio", "worker"])
    assert args.queues == "digitalizer.portal"


@pytest.mark.parametrize("retired", ["run-book", "run-source", "stats", "monitor",
                                     "publish-catalog", "build-catalog", "check"])
def test_the_batch_commands_are_gone(retired, capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([retired])


def test_title_page_cannot_be_sampled_or_labelled_by_hand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["studio", "sample-labels", "--task", "title_page"])
