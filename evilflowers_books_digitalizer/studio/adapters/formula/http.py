"""``formula_recognizer = "paddleocr-vl-http"``: PaddleOCR-VL behind the ``vlm`` service.

The client does the cropping (same padding / aspect / size rules as the sidecar,
:mod:`evilflowers_books_digitalizer.vlm_service.recognition`), so only small PNGs
cross the network and the service never needs the page store. Each page image is
opened once for all its regions.

Failure semantics follow the port: a region the service could not read (per-item
error, read timeout of its batch, unreadable page) is simply absent from the result
and stays an image crop. The service being unreachable or permanently busy after
the retries raises :class:`PortError` — the run fails at this stage (``PortError`` is
not one of the workers' transient errors) and can be re-run once the service is up.

Guards: :func:`fix_latex` is applied to formula answers here (idempotent; the
``formulas`` stage applies it again). :func:`looks_hallucinated` is only *counted*
and logged here, never used to drop an answer: the stage's ``judge`` must see the
answer to record the rejection the quality gate counts.
"""

from __future__ import annotations

import base64
import io
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from evilflowers_books_digitalizer.reflow.vlm import fix_latex, looks_hallucinated
from evilflowers_books_digitalizer.studio.ports import FormulaJob, PortError
from evilflowers_books_digitalizer.vlm_service.recognition import crop_from

logger = logging.getLogger(__name__)

ENDPOINT = "/v1/formulas"


@dataclass(frozen=True, slots=True)
class HttpVlmSettings:
    url: str
    #: read timeout of ONE batch request; on CPU a formula takes ~5-30 s, plus queueing
    timeout_s: float = 600.0
    connect_timeout_s: float = 10.0
    batch_size: int = 4
    #: attempts per batch on 503 / connection errors (the first try included)
    attempts: int = 6
    backoff_s: float = 2.0
    max_backoff_s: float = 60.0

    @classmethod
    def from_mapping(cls, s: dict[str, Any]) -> HttpVlmSettings:
        if not s.get("url"):
            raise PortError("[pipeline.vlm] url is required for 'paddleocr-vl-http'")
        d = cls(url="")
        return cls(url=str(s["url"]).rstrip("/"),
                   timeout_s=float(s.get("timeout_s", d.timeout_s)),
                   connect_timeout_s=float(s.get("connect_timeout_s", d.connect_timeout_s)),
                   batch_size=max(1, int(s.get("batch_size", d.batch_size))),
                   attempts=max(1, int(s.get("attempts", d.attempts))),
                   backoff_s=float(s.get("backoff_s", d.backoff_s)),
                   max_backoff_s=float(s.get("max_backoff_s", d.max_backoff_s)))


def _png_b64(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=False)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class HttpFormulaRecognizer:
    def __init__(self, settings: HttpVlmSettings, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=settings.url, transport=transport,
            timeout=httpx.Timeout(settings.timeout_s, connect=settings.connect_timeout_s))

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ port
    def recognize(self, jobs: Sequence[FormulaJob]) -> dict[str, str]:
        answers: dict[str, str] = {}
        suspicious = 0
        for batch in self._batches(jobs):
            for key, text in self._post(batch).items():
                task = batch[key][1]
                if task == "formula":
                    text = fix_latex(text)
                    suspicious += looks_hallucinated(text)
                answers[key] = text
        if suspicious:
            logger.info("vlm-http: %d of %d answers look like drawings (the stage rejects them)",
                        suspicious, len(answers))
        logger.info("vlm-http: %d jobs, %d answered", len(jobs), len(answers))
        return answers

    # ------------------------------------------------------------ crops
    def _batches(self, jobs: Sequence[FormulaJob]) -> Iterator[dict[str, tuple[str, str]]]:
        """``{key: (png_b64, task)}`` batches; each page image is opened once."""
        by_image: dict[Path, list[FormulaJob]] = {}
        for job in jobs:
            by_image.setdefault(job.image, []).append(job)
        batch: dict[str, tuple[str, str]] = {}
        for image, group in by_image.items():
            try:
                with Image.open(image) as page:
                    page.load()
                    for job in group:
                        b = job.bbox
                        crop = crop_from(page, (b.x0, b.y0, b.x1, b.y1))
                        batch[job.key] = (_png_b64(crop), "ocr" if job.task == "ocr" else "formula")
                        if len(batch) >= self.settings.batch_size:
                            yield batch
                            batch = {}
            except OSError as exc:
                logger.warning("vlm-http: cannot read %s (%s): its %d regions stay images",
                               image, exc, len(group))
        if batch:
            yield batch

    # ------------------------------------------------------------ transport
    def _post(self, batch: dict[str, tuple[str, str]]) -> dict[str, str]:
        body = {"items": [{"key": k, "image": png, "task": task} for k, (png, task) in batch.items()]}
        backoff = self.settings.backoff_s
        last = ""
        for attempt in range(1, self.settings.attempts + 1):
            retry_after = 0.0
            try:
                resp = self._client.post(ENDPOINT, json=body)
            except httpx.TimeoutException as exc:
                if not isinstance(exc, httpx.ConnectTimeout):
                    # the service is (still) working on it: re-sending would queue the
                    # same crops twice. Give these regions up; they stay images.
                    logger.warning("vlm-http: batch of %d timed out after %.0f s: %s",
                                   len(batch), self.settings.timeout_s, exc)
                    return {}
                last = f"connect timeout: {exc}"
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return self._parse(resp, batch)
                if resp.status_code != 503:
                    raise PortError(f"vlm service {resp.status_code}: {resp.text[:300]}")
                last = f"503: {resp.text[:200]}"
                retry_after = _retry_after(resp)
            if attempt < self.settings.attempts:
                # the server's estimate wins when it has one; else exponential backoff
                wait = min(retry_after or backoff, self.settings.max_backoff_s)
                logger.info("vlm-http: %s; retry %d/%d in %.0f s", last, attempt,
                            self.settings.attempts - 1, wait)
                self._sleep(wait)
                backoff = min(backoff * 2, self.settings.max_backoff_s)
        raise PortError(f"vlm service at {self.settings.url} unavailable after "
                        f"{self.settings.attempts} attempts: {last}")

    @staticmethod
    def _parse(resp: httpx.Response, batch: dict[str, tuple[str, str]]) -> dict[str, str]:
        try:
            results = resp.json()["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PortError(f"vlm service sent an unexpected body: {resp.text[:300]}") from exc
        out: dict[str, str] = {}
        for r in results:
            key, text = r.get("key"), r.get("text")
            if r.get("error"):
                logger.warning("vlm-http: %s failed: %s", key, r["error"])
            elif key in batch and isinstance(text, str) and text.strip():
                out[key] = text
        return out


def _retry_after(resp: httpx.Response) -> float:
    try:
        return float(resp.headers.get("Retry-After", 0))
    except ValueError:
        return 0.0
