"""Per-book progress heartbeats + the pipeline on_step hook."""

from __future__ import annotations

import json
import time

from evilflowers_books_digitalizer.pipeline.base import BookContext, Pipeline, PipelineStep
from evilflowers_books_digitalizer.progress import (
    BookProgress,
    clear,
    progress_dir,
    read_active,
)


def test_heartbeat_lifecycle(tmp_path):
    bp = BookProgress(tmp_path, "svf", "b1")
    bp.start(8)
    active = read_active(tmp_path)
    assert len(active) == 1
    assert active[0]["slug"] == "svf_b1"
    assert active[0]["step"] == "starting"
    assert active[0]["elapsed"] >= 0

    bp.update(3, 8, "mrc")  # 4th step (0-based index 3)
    [row] = read_active(tmp_path)
    assert row["step"] == "mrc" and row["step_index"] == 4 and row["n_steps"] == 8

    bp.finish()
    assert read_active(tmp_path) == []


def test_read_active_filters_stale(tmp_path):
    bp = BookProgress(tmp_path, "svf", "old")
    bp.start(5)
    # backdate the heartbeat so it looks like a dead worker
    path = progress_dir(tmp_path) / "svf_old.json"
    data = json.loads(path.read_text())
    data["updated_at"] = time.time() - 10_000
    path.write_text(json.dumps(data))

    assert read_active(tmp_path) == []                 # default TTL hides it
    assert len(read_active(tmp_path, ttl=20_000)) == 1  # generous TTL keeps it


def test_read_active_skips_malformed(tmp_path):
    BookProgress(tmp_path, "svf", "ok").start(3)
    (progress_dir(tmp_path) / "svf_bad.json").write_text("{not json")
    active = read_active(tmp_path)
    assert [w["book_id"] for w in active] == ["ok"]


def test_clear_by_source(tmp_path):
    BookProgress(tmp_path, "svf", "a").start()
    BookProgress(tmp_path, "mtf", "b").start()
    clear(tmp_path, "svf")
    assert {w["source"] for w in read_active(tmp_path)} == {"mtf"}
    clear(tmp_path)
    assert read_active(tmp_path) == []


class _Dummy(PipelineStep):
    def __init__(self, name):
        self.name = name

    def run(self, ctx):
        return ctx


def test_pipeline_on_step_callback(tmp_path):
    calls: list[tuple[int, int, str]] = []
    ctx = BookContext(source="svf", book_id="b", work_dir=tmp_path, output_dir=tmp_path)
    Pipeline([_Dummy("a"), _Dummy("b"), _Dummy("c")]).run(
        ctx, on_step=lambda i, total, name: calls.append((i, total, name))
    )
    assert calls == [(0, 3, "a"), (1, 3, "b"), (2, 3, "c")]


def test_pipeline_runs_without_callback(tmp_path):
    ctx = BookContext(source="svf", book_id="b", work_dir=tmp_path, output_dir=tmp_path)
    out = Pipeline([_Dummy("a")]).run(ctx)  # on_step optional
    assert out is ctx
