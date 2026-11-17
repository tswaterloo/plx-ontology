"""Briefing words, answer trails and the quicker answer path for the workspace backend."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping, Optional, Sequence

from . import models as m
from .serving import AGENT_FALLBACK, HOST_READ_SCHEMES, object_url, open_url, plain
from .answers import supplier_of, unavailable_answer


def _batched_words(view: Any, oid: str, got: Mapping[str, Any],
                   supplier: bool) -> Optional[tuple[str, Optional[str]]]:
    """(label, supplier) from one record's batched read, as ``card_words`` gives them;
    None when only the whole record decides (a vendor name withheld from the reader)."""
    label = plain(got.get("display")) or ""
    if not supplier:
        return label, None
    if "vendor_name" in (got.get("withheld") or ()):
        return None
    names = [{"property": "vendor_name", "value": v}
             for v in (got.get("values") or {}).get("vendor_name") or ()]
    return label, supplier_of(view, oid, {"values": names})


def customer_names(view: Any, candidate: Mapping[str, Any]) -> set[str]:
    """The customer's name and number as this reader reads them, normalised for an exact
    comparison (``customer_profit.normal_name``)."""
    from operating.customer_profit import normal_name
    card = view._call("plx.object", {"object_id": candidate["object_id"],
                                     "properties": ["name", "erp_code"]}) or {}
    names = {plain(v.get("value")) for v in card.get("values") or ()
             if v.get("property") in ("name", "erp_code")}
    names.add(plain(candidate.get("display")))
    return {normal_name(n) for n in names if n}


def _person(reader: Any) -> Optional[str]:
    """The address a reader's drafts are sent as (their learned style's key), else None."""
    email = getattr(reader, "email", None) or getattr(
        getattr(reader, "session", None), "email", None)
    return email.strip().lower() if isinstance(email, str) and "@" in email else None


FAST_ANSWER_ENV = "PLX_FAST_ANSWER"


def fast_off(environ: Mapping[str, str]) -> bool:
    """The phrase-rule path (``operating.fast_answer``) is switched off for this process
    (``PLX_FAST_ANSWER=off``): every question it would take goes to the agent. The IN-11
    comparison's off arm (``evals/fast_answer_compare.py``); on by default."""
    return str(environ.get(FAST_ANSWER_ENV) or "").strip().lower() == "off"


def quicker_answer(answer: m.AnswerPayload, reason: str) -> m.AnswerPayload:
    """``answer`` (the objective box's, given when the agent's full check did not run) as
    the quicker answer: ``assistant_unavailable`` names ``reason`` either way, so no
    research-free answer passes for a checked one; one that found nothing says the
    assistant was unavailable (``unavailable_answer``), never "not found"; one that found
    something keeps it, after the ``AGENT_FALLBACK`` note."""
    if answer.outcome == "not_found":
        answer.direct_answer, answer.outcome = unavailable_answer(
            reason, answer.trail, answer.facts, answer.subjects, [])
    elif not answer.direct_answer.startswith(AGENT_FALLBACK):
        answer.direct_answer = (AGENT_FALLBACK + answer.direct_answer)[:4000]
    answer.assistant_unavailable = reason
    return answer


def why_answer_trail(backend: "WorkspaceBackend", view: Any, question: str,
                 roots: Sequence[m.ObjectRef], facts: Sequence[m.Fact], *,
                 seconds: Optional[float] = None) -> Optional[m.EvidenceTrail]:
    """How the answer is known (``operating.why_trail``): for the first record the answer is
    about that a why plan covers, its steps read through this reader's own view, each
    source decided for the reader again (a step it may not open is counted, never named),
    values only from the facts the gate passed and this answer shows, and a suggested
    next action (nothing written or sent). None when no plan covers the question or the
    trail cannot be read; the answer stands without it."""
    from operating import why_trail

    started = time.perf_counter()
    try:
        if not roots:
            return None
        root = roots[0]       # the answer's subject (``agent_answer``)
        if root.type in ("Object", "Record", "") and ":" in root.object_id:
            # an object page's Ask names the record by id: its type is the id's prefix
            root = root.model_copy(update={"type": root.object_id.split(":", 1)[0]})
        plan = why_trail.select(question, root.type)
        if plan is None:
            return None
        # each fact with the records and fields the gate checked of it (a source's fields)
        claims = [{"text": f.text, "citations": [s.handle for s in f.sources],
                   "checked": [(s.handle, k) for s in f.sources for k in (s.fields or {})],
                   "check": f.check} for f in facts if f.sources]
        built = why_trail.build(plan, {"object_id": root.object_id, "object_type": root.type,
                                       "label": root.label},
                                call=view._call, decide=view.access.decide, claims=claims,
                                **({"seconds": seconds} if seconds is not None else {}))
    except Exception as error:  # noqa: BLE001 - the trail is extra; the answer stands
        logging.getLogger("operator_api.ask").info(json.dumps(
            {"trail": "not_built", "error": type(error).__name__}))
        return None
    shown = {s.handle: s for f in facts for s in f.sources}
    steps: list[m.TrailStep] = []
    unopenable: set[str] = set()
    for step in built["steps"]:
        handle = step.get("handle")
        source = None
        if handle:
            source = shown.get(handle) or backend._source(view, handle)
            if source.open_url is None:
                if step["kind"] != "verdict":
                    unopenable.add(handle)  # counted once, never shown
                    continue
                source = None
        title = str(step["label"])
        if title == why_trail.CHECKED_FACT and source is not None:
            title = f"Checked against the {source.label[:1].lower()}{source.label[1:]}"
        steps.append(m.TrailStep(kind=step["kind"], at=step.get("date"), title=title[:200],
                                 detail=step.get("value"), source=source,
                                 check=str(step.get("check") or "read_from_record")))
    hidden = built["hidden"] + len(unopenable)
    action = built.get("next_action")
    logging.getLogger("operator_api.ask").info(json.dumps({
        "trail": built["plan"], "steps": len(steps), "hidden": hidden,
        "verdict": built["verdict_state"], "reads": built["reads"],
        "ms": int((time.perf_counter() - started) * 1000)}))
    return m.EvidenceTrail(
        kind="why", title=built["title"], plan=built["plan"], steps=steps,
        hidden_steps=hidden,
        hidden_message=(f"{hidden} more step is in a place you can't open." if hidden == 1
                        else f"{hidden} more steps are in places you can't open."
                        if hidden else None),
        may_hide_more=bool(built["may_be_withheld"]), complete=not built["incomplete"],
        next_action=m.NextAction(text=action["text"][:400], rule=action["rule"])
        if action else None)


def answer_trail(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                 found: Mapping[str, Any], late_pos: Sequence[tuple[m.LatePo, Optional[str]]],
                 claims: Sequence[Mapping[str, Any]]) -> Optional[m.EvidenceTrail]:
    """The evidence trail (``operator_api.trail``): the PO's lateness trail, a customer order's
    materials, or the answer's dated checked claims, each step decided for this reader."""
    from . import trail as trails

    roots = list(found.get("roots") or ())
    if request.object_id and not any(r.get("object_id") == request.object_id for r in roots):
        roots.insert(0, {"object_id": request.object_id})
    return trails.for_answer(backend, view, request.question, roots, late_pos, claims,
                             ambiguous=list(found.get("ambiguous") or ()),
                             object_url=object_url, open_url=open_url,
                             host_schemes=HOST_READ_SCHEMES)
