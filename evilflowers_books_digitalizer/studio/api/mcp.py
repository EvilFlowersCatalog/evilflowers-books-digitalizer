"""MCP server of the studio: the labelling workflow (#7) for an operator working through an
MCP client. Same use cases as the SPA and the CLI (studio.application.labelling).

Two transports, one tool set:

* streamable HTTP at ``/mcp`` on the portal (``create_app``): VPN only, like the REST API
* stdio: ``evilflowers-digitalizer studio mcp`` (a local client starts it)

**The labels must be human.** Calibration (knowledge#65) fits the decider against people's
judgement; an agent's own answer would teach the decider to agree with another model. The
tool descriptions therefore tell the client to show the item to the person and relay their
answer. Every label records ``via="mcp"``, so the export can be filtered.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

from evilflowers_books_digitalizer.studio.application import labelling
from evilflowers_books_digitalizer.studio.application.errors import ApplicationError
from evilflowers_books_digitalizer.studio.application.services import Services

INSTRUCTIONS = """\
Restoration Studio labelling: hand labels that calibrate the EvilFlowers decision models.
The answers MUST come from the person you are working with, never from you: show them the
item (text, and the crop image for figures and tables), ask, and pass their answer to
submit_label. If they are unsure, submit "skip". Never guess, never pre-fill an answer and
never say what you think the answer is before they decide (that anchors them).
Flow: list_label_tasks -> next_label_item -> (show, ask) -> submit_label -> next_label_item.
`labeller` is the person's name; it is recorded on every label."""


def build_mcp(svc: Services, *, streamable_http_path: str = "/mcp") -> FastMCP:
    """The tool set over ``svc``; ``streamable_http_path`` is where the HTTP transport answers."""
    mcp = FastMCP(
        "evilflowers-studio", instructions=INSTRUCTIONS,
        stateless_http=True, json_response=True, streamable_http_path=streamable_http_path,
        # VPN only, like the REST API; the host check would refuse the ingress' Host header
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @mcp.tool()
    def list_label_tasks(labeller: str = "") -> list[dict[str, Any]]:
        """The labelling tasks: label keys and their meaning, the target, and progress
        (labelled, skipped, doubly labelled; with `labeller`: how many are left for them)."""
        return [t.model_dump() for t in _call(labelling.list_tasks, svc, labeller or None)]

    @mcp.tool(structured_output=False)   # text + an image: plain content, not a JSON schema
    def next_label_item(task: str, labeller: str) -> list[Any]:
        """The next item for `labeller` in `task`, or a note that none is left.

        Returns the item (JSON: id, language, what to show) and, for figures and tables, the crop
        image. Show both to the person and ask the task's question; do not answer it yourself."""
        item = _call(labelling.next_item, svc, task, labeller)
        if item is None:
            return [{"task": task, "item": None,
                     "note": "Nothing left for this labeller; sample more with sample_label_items."}]
        out: list[Any] = [{"task": task, "question": labelling.task_or_404(task).title,
                           "item": item.model_dump(exclude={"image_url"})}]
        if item.image_url:
            img = _call(labelling.item_image, svc, item.id)
            out.append(Image(data=img.content, format=img.media_type.removeprefix("image/")))
        return out

    @mcp.tool()
    def submit_label(item_id: int, task: str, value: str, labeller: str,
                     table_quality: str = "") -> dict[str, Any]:
        """Record the PERSON's answer (`value`: one of the task's label keys, or "skip" when they
        are unsure). For an is_data_table item, `table_quality` optionally records their answer to
        the table_quality question on the same item. Answering again replaces the earlier answer."""
        answers = [labelling.LabelIn(task=task, value=value)]
        if table_quality:
            answers.append(labelling.LabelIn(task="table_quality", value=table_quality))
        item = _call(labelling.submit, svc, item_id, labelling.LabelsIn(labels=answers), labeller,
                     via="mcp")
        return {"item_id": item.id, "recorded": item.mine}

    @mcp.tool()
    def sample_label_items(task: str, n: int = 100) -> dict[str, Any]:
        """Add up to `n` new items for `task` from the catalog (stratified by language and the
        model's confidence; never shown). Takes minutes for large `n`."""
        return _call(labelling.sample, svc, task, n).model_dump()

    @mcp.tool()
    def label_summary(task: str) -> dict[str, Any]:
        """Counts per label, language, labeller and interface; agreement and Cohen's kappa on
        doubly labelled items; whether the task reached its target."""
        return _call(labelling.summary, svc, task).model_dump()

    @mcp.tool()
    def export_labels(task: str) -> str:
        """The task's labels as JSONL (one line per non-skip label), for knowledge#65."""
        import json

        lines = _call(lambda: list(labelling.export_lines(svc, task)))   # a generator: run it here
        return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)

    return mcp


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """An application refusal becomes a tool error the client can show (not a server crash)."""
    try:
        return fn(*args, **kwargs)
    except ApplicationError as exc:
        raise ValueError(str(exc)) from exc


__all__ = ["INSTRUCTIONS", "build_mcp"]
