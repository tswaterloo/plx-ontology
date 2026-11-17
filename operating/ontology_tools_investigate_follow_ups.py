"""Follow-ups and drafts an investigation records on its case (``record_draft``,
``follow_up_recorder``) and the answer check they rest on.

Split from ``operating/ontology_tools_investigate.py`` (``record_follow_ups`` stays
there); moved unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence
from ontology.json_contract import validate
from ontology.types import Actor
from .cases import GAP_STATES
from .ontology_tools_policy import load_contracts
from .ontology_tools_investigate_plans import PLAN_DIR, _SET_HANDLE_TEXT, default_engines


# =========================================================================== follow-ups

FOLLOW_UP_ACTION = "workbench.follow_up.record"
DRAFT_ACTION = "workbench.draft.record"
# Gap states a follow-up records as they are (operating.cases.GAP_STATES, the console's and the
# case store's). An absence needs the gap guard's proof, which this path does not run (the guard
# would refuse it, as it refuses the console's FollowUps), so it is recorded as unknown with a note.
_FOLLOW_UP_STATES = frozenset(GAP_STATES) - {"absent_in_source"}
MAX_FOLLOW_UP_CITATIONS = 20  # the catalog's citation_list max_items
MAX_MISSING = 500  # a follow-up's ``missing``: the duplicate key, cut so its gap code stays
_PARTIAL = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")  # a typed reason, as the contract's codes
# Gaps that ask no person, so they become no follow-up: the executor's notes on how it answered
# (twins named, weaker candidates not followed, an input a person corrected; a plan's retry
# ``note_gap`` too) and its own run limits (step budget, page cap, a primitive the host does not
# serve or that failed, an engine that failed): asking again, not a department, answers them.
_NOTE_REASONS = frozenset({"root_twins_not_roots", "weaker_candidates_not_followed",
                           "input_person_corrected", "page_cap", "step_budget", "time_budget",
                           "step_unavailable", "step_failed", "root_lookup_incomplete",
                           "engine_failed", "not_grouped"})
# Gaps about the host's own state: a source it configures no reader for, a capture state its
# projection does not state, a projection it has not built. Its operator answers them, never a
# department or an undeclared source owner, so they become no follow-up either (they stay in
# the answer).
HOST_STATE_REASONS = frozenset({"source_not_configured", "freshness_unknown",
                                "projection_not_built"})


def record_draft(path: Any, actor: Actor, case_id: str, statement: str,
                 citations: Sequence[str], *, session: Any) -> dict[str, Any]:
    """Record one worded draft on case ``case_id`` as ``workbench.draft.record`` through
    ``path.execute_internal`` (an ``ActionPath``: workbench-internal, approval none, read back
    from the case store, nothing sent), citing ``citations`` (at most
    ``MAX_FOLLOW_UP_CITATIONS``). A draft already on the case (same statement, ignoring case
    and spacing as the case store does) is not
    recorded again. The one draft path of the plan engines' draft requests and of the
    console's late-order supplier draft. Returns ``{"outcome": recorded | exists | refused,
    "record_id", "read_back"}`` or ``"refused"`` (the catalog's code)."""
    from operating.action_catalog import ActionRefused

    def same(text: Any) -> str:   # the case store's own finding identity: case and spacing
        return " ".join(str(text).split()).casefold()

    view = path.cases.get(actor, case_id)
    for bucket in (view.get("findings") or {}).values():
        for finding in bucket:
            if same(finding.get("statement")) == same(statement):
                return {"outcome": "exists", "record_id": finding.get("id"),
                        "read_back": None}
    parameters: dict[str, Any] = {"statement": statement}
    if citations:
        parameters["citations"] = list(citations)[:MAX_FOLLOW_UP_CITATIONS]
    try:
        record = path.execute_internal(actor, DRAFT_ACTION, case_id, parameters,
                                       session=session)
    except ActionRefused as exc:
        return {"outcome": "refused", "refused": exc.code}
    return {"outcome": "recorded", "record_id": record["read_back"].get("record_id"),
            "read_back": record["read_back"]["outcome"]}


def check_answer(answer: Any, contracts: Mapping[str, Any] | None = None) -> None:
    """``ValueError("not_an_investigate_answer")`` unless ``answer`` satisfies the
    ``plx.investigate`` output contract with a procedure and every gap names who can answer
    (what ``record_follow_ups`` records; a caller checks it before opening a case)."""
    contracts = contracts or load_contracts()
    gaps = answer.get("gaps") if isinstance(answer, Mapping) else None
    if (not isinstance(gaps, list) or "procedure" not in answer
            or validate(dict(answer), contracts["tools"]["plx.investigate"]["output"],
                        root=contracts, limit=1)
            or not all(gap.get("who_can_answer") for gap in gaps)):
        raise ValueError("not_an_investigate_answer")


def follow_up_recorder(path: Any, session: Any, actor: Actor | None = None
                       ) -> Callable[[Actor, str, Mapping[str, Any]], list[dict[str, Any]]]:
    """``InvestigateTools(follow_ups=...)``: the ``follow_ups`` option of ``plx.investigate``
    records through ``path`` (an ``ActionPath``) with the host's ``session``, as ``actor``
    (the host's session actor, which the case store gates itself), else as the reader."""
    def record(reader: Actor, case_id: str, answer: Mapping[str, Any]) -> list[dict[str, Any]]:
        return _record_follow_ups(path, actor or reader, case_id, answer, session=session)
    return record


def _record_follow_ups(path: Any, actor: Actor, case_id: str, answer: Mapping[str, Any], *,
                       session: Any, engines: Mapping[str, Mapping[str, Any]] | None = None,
                       partial: str | None = None) -> list[dict[str, Any]]:
    from operating.action_catalog import ActionRefused

    plan_id = answer["procedure"].split(":", 1)[1].split("@", 1)[0]
    try:
        plan = json.loads((PLAN_DIR / f"{plan_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = {}
    engines = default_engines() if engines is None else engines
    drafts = [engines[e["engine"]]["draft"] for e in plan.get("evidence") or ()
              if e.get("kind") == "compute" and "draft" in engines.get(e.get("engine"), {})]
    notes = _NOTE_REASONS | HOST_STATE_REASONS | {(step.get("retry") or {}).get("note_gap")
                             for step in plan.get("steps") or ()}
    entries = [e for e in answer.get("answer") or ()
               if e.get("kind") == "property" and isinstance(e.get("citation"), Mapping)]
    root_id = (answer.get("root") or {}).get("object_id")
    # The root each record names: one case may hold the gaps of several roots.
    about = (root_id or "no single root")[:300]

    def cited(gap: Mapping[str, Any]) -> list[str]:
        names = {*((gap.get("searched") or {}).get("sources") or ()),
                 str(gap["subject"]).split(" ", 1)[0]}
        found = [e for e in entries
                 if f"{e['object_type']}.{e['property']}" in names or e["object_type"] in names]
        if not found and partial is None:
            found = [e for e in entries if e.get("object_id") == root_id]
        return list(dict.fromkeys(e["citation"]["l1_handle"] for e in found))

    view = path.cases.get(actor, case_id)   # CaseNotFound before anything is written
    known = {g.get("missing") for g in view.get("gaps") or ()}
    noted = {f.get("statement") for bucket in (view.get("findings") or {}).values()
             for f in bucket}
    out: list[dict[str, Any]] = []

    def execute(action: str, subject: str, indexes: list[int],
                parameters: dict[str, Any]) -> None:
        item: dict[str, Any] = {"action": action, "subject": subject, "gaps": indexes}
        try:
            record = path.execute_internal(actor, action, case_id, parameters, session=session)
        except ActionRefused as exc:
            out.append({**item, "outcome": "refused", "refused": exc.code})
            return
        out.append({**item, "outcome": "recorded",
                    "record_id": record["read_back"].get("record_id"),
                    "read_back": record["read_back"]["outcome"]})

    groups: dict[tuple[str, tuple[str, ...]], tuple[list[str], list[str], list[int]]] = {}
    # Gaps of a partial answer that no read entry matches: never recorded (docstring).
    unread: dict[tuple[str, tuple[str, ...]], tuple[list[str], list[str], list[int]]] = {}
    set_numbers: dict[str, int] = {}
    for index, gap in enumerate(answer["gaps"]):
        if gap["reason"] in notes:
            continue
        # A set handle in a subject is minted per run: a repeat press names the same gap, so
        # the record's identity (and the person's text) numbers the answer's sets in order
        # ("lead set 1"), never their handles.
        subject = _SET_HANDLE_TEXT.sub(
            lambda m: f"lead set {set_numbers.setdefault(m.group(0), len(set_numbers) + 1)}",
            gap["subject"])
        code, who = gap["reason"].upper(), list(gap["who_can_answer"])
        citations = cited(gap)
        if partial is not None and not citations:
            codes, _, indexes = unread.setdefault((subject, tuple(who)), ([], [], []))
            codes.append(code)
            indexes.append(index)
            out.append({"action": FOLLOW_UP_ACTION, "subject": subject, "gaps": [index],
                        "outcome": "not_recorded", "partial": partial})
            continue
        codes, handles, indexes = groups.setdefault((subject, tuple(who)), ([], [], []))
        codes.append(code)
        indexes.append(index)
        handles.extend(h for h in citations if h not in handles)
        tail = f": {code}"
        missing = f"{subject} of {about}"[:MAX_MISSING - len(tail)] + tail
        if missing in known:
            out.append({"action": FOLLOW_UP_ACTION, "subject": subject, "gaps": [index],
                        "outcome": "exists"})
            continue
        known.add(missing)
        state = gap["state"] if gap["state"] in _FOLLOW_UP_STATES else "unknown"
        why = gap.get("detail") or code
        if gap["state"] == "absent_in_source":
            why += " (reported absent in source; recorded unknown: no absence proof is run here)"
        execute(FOLLOW_UP_ACTION, subject, [index], {
            "missing": missing, "why": why[:1900], "who_can_answer": who, "state": state,
            **({"citations": citations[:MAX_FOLLOW_UP_CITATIONS]} if citations else {})})

    def worded(subject: str, who: tuple[str, ...], codes: list[str]) -> str | None:
        return next((text for text in (draft(f"{subject} of {about}", who, codes)
                                       for draft in drafts) if text), None)

    for (subject, who), (codes, handles, indexes) in groups.items():
        statement = worded(subject, who, codes)
        if statement is None:
            continue
        if statement in noted:
            out.append({"action": DRAFT_ACTION, "subject": subject, "gaps": indexes,
                        "outcome": "exists"})
            continue
        noted.add(statement)
        done = record_draft(path, actor, case_id, statement, handles, session=session)
        out.append({"action": DRAFT_ACTION, "subject": subject, "gaps": indexes,
                    **({key: done[key] for key in ("outcome", "refused")}
                       if done["outcome"] == "refused" else done)})
    for (subject, who), (codes, _, indexes) in unread.items():
        if worded(subject, who, codes) is not None:
            out.append({"action": DRAFT_ACTION, "subject": subject, "gaps": indexes,
                        "outcome": "not_recorded", "partial": partial})
    return out
