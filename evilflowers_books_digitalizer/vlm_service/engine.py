"""The model behind the service: a two-method interface so tests swap in a fake."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from PIL import Image

from . import recognition as rec

logger = logging.getLogger(__name__)


class Engine(Protocol):
    name: str

    def load(self) -> None:
        """Blocking; called once from a background thread before the service is ready."""
        ...

    def recognize(self, image: Image.Image, task: str) -> str:
        """One normalized crop -> raw model text. Raises ``RuntimeError`` on failure."""
        ...


class PaddleEngine:
    """PaddleOCR-VL through transformers, weights from a local safetensors directory."""

    def __init__(self, model_dir: Path, *, device: str = "cpu", dtype: str = "auto",
                 threads: int = 0, use_cache: bool | None = True) -> None:
        self.model_dir = model_dir
        self.device = device
        self.dtype = dtype
        self.threads = threads
        self.use_cache = use_cache
        self.name = model_dir.name
        self._lm: rec.LoadedModel | None = None

    def load(self) -> None:
        import torch

        if self.threads > 0:
            # torch sizes its pool from the HOST's cores, not the cgroup limit
            torch.set_num_threads(self.threads)
        self._lm = rec.load_model(self.model_dir, device=self.device, dtype=self.dtype)
        logger.info("loaded %s on %s (%s, %d threads)", self.model_dir, self._lm.device,
                    self.dtype, torch.get_num_threads())

    def recognize(self, image: Image.Image, task: str) -> str:
        if self._lm is None:
            raise RuntimeError("model not loaded")
        try:
            return rec.generate(self._lm, image, task, use_cache=self.use_cache)
        finally:
            rec.free_memory(self._lm.device)
