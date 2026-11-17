"""MCP server of the studio: the labelling workflow (#7) for an MCP client. Same use cases as
the SPA and the CLI (studio.application.labelling).

Two transports, one tool set:

* streamable HTTP at ``/mcp`` on the portal (``create_app``): VPN only, like the REST API
* stdio: ``evilflowers-digitalizer studio mcp`` (a local client starts it)

Two kinds of labeller (#16):

* **People.** ``next_label_item`` / ``submit_label``: the client shows the item to the person
  and relays THEIR answer. Recorded as ``labeller_kind=human``, ``via="mcp"``.
* **Agents** (``[labelling] agent_labellers = true``, off by default): a stronger model labels
  on its own through ``claim_label_items`` / ``submit_labels``. Recorded as
  ``labeller_kind=agent`` with its ``model``. The decider is then calibrated against that
  teacher, so people spot-check about 10 % of agent-labelled items and the summary reports
  agent<->human kappa (knowledge#65 accepts the thresholds only when it is high enough).

Neither kind ever sees the model's answer (``hidden``, ``stratum``) or another labeller's label.
"""

from __future__ import annotations

import json
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

AGENT_INSTRUCTIONS = """\
Restoration Studio labelling: labels that calibrate the EvilFlowers decision models (small
models that answer the same questions on figures, tables, section pairs, subjects and scan
marks). There are two separate flows; never mix them.

1. A PERSON labels (next_label_item -> submit_label). You show the item and relay the person's
   answer. Never answer for them, never suggest an answer. `labeller` is their name.

2. YOU label (agent flow: claim_label_items -> submit_labels). You are the teacher the deciders
   are calibrated against, and people spot-check a sample of your labels, so accuracy matters
   more than speed:
   - Call claim_label_items(task, n, labeller, model). `labeller` names you (e.g.
     "claude-agent"); parallel agents may share it. `model` is your exact model id (e.g.
     "claude-opus-5-5"). The claim leases the items to you (lease_expires_at); nobody else
     gets them meanwhile.
   - The result starts with `guide`: the question, every label's definition, the rules for
     close calls, when to skip, and a Slovak glossary. Read it before the first item and
     apply it literally. Most books are Slovak; captions say "Obr." for every kind of figure.
   - Each item follows as "Item <id>", then its image (figures, tables, scan marks), then its
     JSON (payload: caption, page text, markdown, section texts, title/summary/ToC). Judge from
     the item alone: no web search, no guessing what the book is.
   - Look at the image itself. Note image.width/height: crops under about 200 px are easy
     to misread.
   - Answer "skip" whenever the guide's skip cases apply or two labels fit equally well. A
     skip is stored but never exported, and a wrong label does more harm than a skip.
   - Submit all answers of a claim in one submit_labels call (same labeller and model):
     labels=[{item_id, task, value}, ...]. For an is_data_table item you judged "yes", also
     add {item_id, task: "table_quality", value}. For subject, value is the option's id.
   - Rejected answers come back with the reason; fix and resubmit only those. Release
     items you will not answer with release_label_items(labeller, item_ids).
   - Claim small batches (n = 5 is the default); a result too large for your output limit
     is cut and the rest released (see `note`).
   - Repeat until the claim returns no items (`note`). label_summary shows progress and
     agent<->human agreement.
You never see the decider's answer or any other labeller's label, by design."""


def build_mcp(svc: Services, *, streamable_http_path: str = "/mcp") -> FastMCP:
    """The tool set over ``svc``; ``streamable_http_path`` is where the HTTP transport answers."""
    agents = svc.config.labelling.agent_labellers
    mcp = FastMCP(
        "evilflowers-studio", instructions=AGENT_INSTRUCTIONS if agents else INSTRUCTIONS,
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
        """The next item for the PERSON `labeller` in `task`, or a note that none is left.

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
        the table_quality question on the same item. Answering again replaces the earlier answer.
        Never use this for your own answers: those go through submit_labels."""
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
        """Counts per label, language, labeller, interface, labeller kind (human/agent) and
        model; agreement and Cohen's kappa separately for human<->human and agent<->human pairs
        (overall, per language, and a confusion table); spot-check progress; whether the task
        reached its target."""
        return _call(labelling.summary, svc, task).model_dump()

    @mcp.tool()
    def export_labels(task: str, labeller_kind: str = "") -> str:
        """The task's labels as JSONL (one line per non-skip label), for knowledge#65.
        `labeller_kind` = "human" or "agent" keeps only those labels."""
        lines = _call(lambda: list(labelling.export_lines(svc, task, labeller_kind or None)))
        return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)

    if agents:
        _agent_tools(mcp, svc)
    return mcp


def _agent_tools(mcp: FastMCP, svc: Services) -> None:
    """claim / submit / release for a model labelling on its own (#16)."""

    @mcp.tool(structured_output=False)   # text + images
    def claim_label_items(task: str, labeller: str, model: str, n: int = 5) -> list[Any]:
        """YOU label (agent flow). Lease up to `n` unanswered items of `task` to you for the lease
        time; parallel claims never return the same item. A result is cut to fit the client's
        output limit (the rest is released at once, see `note`); per call at most figures 10,
        tables 8, scan marks 10, section pairs 12, subjects 15.

        Returns: a JSON header (guide: question, label definitions, rules, skip cases, Slovak
        glossary; lease_expires_at; options_by_catalog for subject; left; items = the ids you
        hold), then for every item "Item <id>", its crop image (figures, tables, scan marks) and
        its JSON. `labeller` names you, `model` is your model id (e.g. claude-opus-5-5). Answer
        with submit_labels."""
        claim, images = _call(labelling.claim_with_images, svc, task, n, labeller, model)
        head = claim.model_dump(exclude={"items"})
        head["items"] = [i.id for i in claim.items]
        out: list[Any] = [json.dumps(head, ensure_ascii=False)]
        for item in claim.items:
            out.append(f"Item {item.id}:")
            if (img := images.get(item.id)) is not None:
                out.append(Image(data=img.content, format=img.media_type.removeprefix("image/")))
            out.append(item.model_dump_json())
        return out

    @mcp.tool()
    def submit_labels(labeller: str, model: str, labels: list[dict[str, Any]]) -> dict[str, Any]:
        """YOUR answers (agent flow), recorded as labeller_kind=agent with `model`; releases the
        leases. `labels` = [{"item_id": 12, "task": "figure_kind", "value": "chart"}, ...] (up to
        100); value is a label key of the task or "skip". On a table item add a second entry
        {"task": "table_quality", ...} when it is a data table. Each item is stored on its own:
        rejected entries come back with the reason."""
        try:
            answers = [labelling.AgentLabelIn.model_validate(x) for x in labels]
        except ValueError as exc:
            raise ValueError(f"labels: {exc}") from exc
        return _call(labelling.submit_agent, svc, labeller, model, answers).model_dump()

    @mcp.tool()
    def release_label_items(labeller: str, item_ids: list[int], task: str = "") -> dict[str, Any]:
        """Give back items you claimed but will not answer: pass their `item_ids` (only your own
        leases on them are dropped, never another agent's under the same name)."""
        return {"released": _call(labelling.release, svc, labeller, task or None,
                                  item_ids=item_ids)}


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """An application refusal becomes a tool error the client can show (not a server crash)."""
    try:
        return fn(*args, **kwargs)
    except ApplicationError as exc:
        raise ValueError(str(exc)) from exc


__all__ = ["AGENT_INSTRUCTIONS", "INSTRUCTIONS", "build_mcp"]
