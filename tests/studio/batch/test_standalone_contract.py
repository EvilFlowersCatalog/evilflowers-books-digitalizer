"""The kit's ``StandaloneJob.inputs`` on every service message (evilflowers-books-digitalizer#9).

The end-to-end service tests (``test_*_service.py``) run every stage's real messages through
``standalone.accept``; these pin the derivation and the refusals.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from evilflowers_books_digitalizer.studio.orchestration import contract
from evilflowers_books_digitalizer.studio.testing import standalone


@pytest.mark.parametrize(("fields", "expected"), [
    ({"leaves": [{"leaf": 1, "frame": "s/b/1.tif"}, {"leaf": 2, "frame": "s/b/2.tif"}]},
     ["s/b/1.tif", "s/b/2.tif"]),                                              # clean
    ({"leaves": [{"leaf": 2, "image": "i/2.tif", "mask": []},
                 {"leaf": 1, "image": "i/1.tif", "mask": []}]},
     ["i/2.tif", "i/1.tif"]),                                                  # hocr, layout, erase
    ({"regions": [{"key": "region-1", "image": "i/3.tif"}, {"key": "region-2", "image": "i/3.tif"},
                  {"key": "region-3", "image": "i/4.tif"}]},
     ["i/3.tif", "i/4.tif"]),                                                  # formulas: distinct
    ({"request": "i/i_studio/render/request-7.json"},
     ["i/i_studio/render/request-7.json"]),                                    # render
    ({"leaves": []}, []),
])
def test_inputs_are_every_file_the_job_reads_in_order(fields: dict, expected: list[str]) -> None:
    assert contract.input_paths(fields) == expected


def _message(tmp_path: Path, inputs: list[str]) -> dict:
    return {"run_id": "1", "job": "clean-0001-a1", "token": "t", "inputs": inputs,
            "output_dir": "item/jobs/1-a1", "outcome": "item/jobs/1-a1/outcome.json"}


def test_accept_takes_a_message_the_kit_takes(tmp_path: Path) -> None:
    (tmp_path / "a.tif").write_bytes(b"x")
    job = standalone.accept(tmp_path, {**_message(tmp_path, ["a.tif"]), "leaves": []})
    assert job.inputs == ["a.tif"]


@pytest.mark.parametrize(("inputs", "error"), [
    ([], ValidationError),                       # the kit requires at least one input
    (["missing.tif"], FileNotFoundError),
    (["../outside.tif"], ValueError),
    (["/etc/hosts"], ValueError),
])
def test_accept_refuses_what_a_service_would_refuse(tmp_path: Path, inputs: list[str],
                                                    error: type[Exception]) -> None:
    with pytest.raises(error):
        standalone.accept(tmp_path, _message(tmp_path, inputs))


def test_a_message_without_inputs_is_never_built() -> None:
    from evilflowers_books_digitalizer.db.models import Job, Run

    job, run = Job(id=3, stage="clean", params={}, attempt=1), Run(id=1)
    with pytest.raises(contract.PortError, match="no input file"):
        contract.message(job, run, "t", contract.Storage(Path("/data")), Path("/data/x/item"),
                         {"leaves": []})
