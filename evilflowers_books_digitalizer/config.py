"""Project-wide constants.

Runtime configuration lives in TOML: ``configs/pipeline*.toml`` for the batch
pipeline (:mod:`.runtime`) and ``configs/studio.*.toml`` for the studio
(:mod:`.studio.config`).
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
