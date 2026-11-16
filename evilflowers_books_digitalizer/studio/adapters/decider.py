"""Decider over HTTP: the shared decision services in namespace ``shared`` (knowledge#77).

Request (``POST /v1/systemone``, Cookbooks/Deploy-Decision-Models)::

    {"model": "multilingual",                        # Laya only: without it Laya may swap
     "state": "…" | {…},                             # text (≤ 50 000 characters)
     "images": ["<base64 PNG>"],                     # decider-vision only, ≤ 4, 448 px side
     "questions": {"<id>": {"type": "choice", "instructions": "…", "criteria": {…}}}}

Answer: ``answers.<id>`` with ``choice``, ``probabilities`` and ``confidence``, and the
``model`` the caller stores as ``decider_model``. No API key: a NetworkPolicy admits the
calling namespaces.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from evilflowers_books_digitalizer.studio.ports import Decision, PortError

logger = logging.getLogger(__name__)

#: Laya caps the serialised state at 50 000 characters
MAX_STATE = 50_000
#: seconds to establish the connection (the answer itself may take ``timeout``)
CONNECT_TIMEOUT = 5.0


class HttpDecider:
    def __init__(self, url: str, *, timeout: float = 60.0, model: str | None = None,
                 client: httpx.Client | None = None) -> None:
        self.url = url
        self.model = model
        self.timeout = timeout
        self._client = client

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        # a blocked NetworkPolicy drops packets: fail the connect fast, give the answer time
        timeout = httpx.Timeout(self.timeout, connect=min(CONNECT_TIMEOUT, self.timeout))
        try:
            if self._client is not None:
                response = self._client.post(self.url, json=body, timeout=timeout)
            else:
                response = httpx.post(self.url, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise PortError(f"decider {self.url}: {type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise PortError(f"decider {self.url}: HTTP {response.status_code}: "
                            f"{response.text[:300]}")
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise PortError(f"decider {self.url}: not JSON: {response.text[:200]}") from exc
        return data

    def choose(self, question: str, instructions: str, criteria: Mapping[str, str], *,
               state: str | Mapping[str, object] = "",
               images: Sequence[bytes] = ()) -> Decision:
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        if len(text) > MAX_STATE:
            text = text[:MAX_STATE]
            state = text
        body: dict[str, Any] = {"state": state, "questions": {question: {
            "type": "choice", "instructions": instructions, "criteria": dict(criteria)}}}
        if self.model:
            body["model"] = self.model
        if images:
            body["images"] = [base64.b64encode(i).decode("ascii") for i in images[:4]]
        data = self._post(body)
        answer = (data.get("answers") or {}).get(question)
        if not isinstance(answer, dict) or answer.get("choice") not in criteria:
            raise PortError(f"decider {self.url}: no valid answer to {question!r}: "
                            f"{str(data)[:300]}")
        choice = str(answer["choice"])
        probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
        p = probabilities.get(choice, float(answer.get("confidence") or 0.0))
        return Decision(choice, p, probabilities, str(data.get("model") or "unknown"))
