"""Home's objective box: one free-form objective, no object id, run by the employee agent
loop for the console's reader (``operating.objective_runner``).

``answer`` runs it through the object workspace's own paths, so it reads, cites and
withholds exactly as the object Ask does: the reader's ``plx.find`` / ``plx.object`` find
and verify the roots, the workspace's agent server and planner run the loop, the
workspace's grant decides which claims are shown (``_shown_claims``) and which citations
open, and the answer is shaped as the Ask's (``_answer``). Follow-ups come back proposed,
shaped for ``POST /api/object-follow-up``; the box records none (the person proposes).

The box is the console's one question path (``POST /api/objective``): beside the agent's
run, each record the objective names by its identifier (at most ``MAX_ANSWER_ROOTS``)
carries the server's own answer for every part the objective asks
(``objective_runner.asked_parts``): a plan family's verdict, exactly as
``GET /api/investigate`` shows it this reader, or the record page's answer, exactly as
``GET /api/object`` shows it, each with its "why". A part no plan or page answer covers
for a root, and every part when no identifier names a root, is left to the agent
(``parts.left_to_agent``); the agent always runs.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from operating import objective_runner, po_status
from operating.explanations import explain, words

MAX_ANSWER_ROOTS = 3

NOTE = ("The agent found the records this objective concerns and read them as you; each "
        "claim cites only records you may open. Gaps are what no record you may read "
        "answers. Follow-ups are proposed only: nothing is recorded until you propose one.")


def _today() -> date:
    """The business day (ET) a run judges lateness on, never the host's local day."""
    return po_status.business_today()


def served(objects: Any) -> dict[str, Any]:
    """Whether this console runs objectives (Home's ``objective`` block)."""
    if objects is None:
        return {"served": False, "reason": "not_configured"}
    if not objects._ask_served():
        return {"served": False, "reason": "no_agent_planner"}
    return {"served": True, "modes": list(objective_runner.MODES),
            "max_chars": objective_runner.MAX_OBJECTIVE,
            "follow_ups": objects.follow_ups is not None}


def answer(objects: Any, request: Any, *, as_of: date | None = None) -> dict[str, Any]:
    """The objective's cited answer, roots, gaps and proposed follow-ups for the reader
    (``LookupError`` when the console runs no agent; ``ValueError`` for a bad request)."""
    from operating import agent_loop

    if objects is None or not objects._ask_served():
        raise LookupError("objective_not_served")
    objective_runner.check(request)
    with objects._request():
        base = objects._base()
        ran = objective_runner.run(
            request,
            find=lambda arguments: objects._call("plx.find", arguments),
            read=lambda object_id: objects._call("plx.object", {"object_id": object_id}),
            server=objects.access.agent_server(), planner=objects.planner,
            bounds=objects.ask_bounds or agent_loop.Bounds(),
            withholds=lambda handles: any(marker is not None for marker
                                          in objects.access.decide(handles).values()),
            shown=objects._shown_claims, as_of=as_of or _today())
        shown = objects._answer({"base": base, "object_id": None,
                                 "question": ran["objective"], "outcome": ran["outcome"],
                                 "reason": ran["reason"], "claims": ran["claims"],
                                 "withheld": ran["withheld"], "loop": ran["loop"]})
        out = {**base, "note": NOTE,
               **{key: ran[key] for key in ("objective", "mode", "as_of", "target", "roots",
                                            "ambiguous", "unfound", "gaps", "follow_ups")},
               "answer": shown["answer"],
               "follow_ups_served": objects.follow_ups is not None}
        out["answers"], out["parts"] = _part_answers(objects, ran)
        if "scenario" in ran:
            scenario = ran["scenario"]
            out["scenario"] = {
                **scenario,
                "inputs": [{**item, "openable": objects._can_open(item["citation"])}
                           for item in scenario["inputs"]]}
        return out


def _not_run(why: str, subject: Any = None) -> dict[str, Any]:
    return {"state": "not_established", "text": f"Not established: {why}",
            "subject": subject, "facts": [], "follow_ups": []}


def _plan_part(objects: Any, family: str, root: str) -> tuple[dict[str, Any], list]:
    """``GET /api/investigate``'s verdict and why, in the box's own request."""
    from operator_ui.object_workspace import investigation_verdict

    try:
        out = objects._investigate(family, root)
    except (KeyError, ValueError, LookupError) as error:
        code = words(str(error.args[0]) if error.args else "") or "no answer"
        return _not_run(f"the plan did not run ({code})"), []
    except Exception:  # one part's failure never costs the agent's answer: fail closed
        return _not_run("the plan did not run"), []
    try:
        out["verdict"] = objects._openable(investigation_verdict(family, out))
        shown = explain(out, objects.access.decide)
        return shown["verdict"], (shown.get("explanation") or {}).get("why") or []
    except Exception:  # shaping fails closed too
        return _not_run("the plan did not run"), []


def _page(objects: Any, root: str) -> tuple[dict[str, Any] | None, list]:
    """``GET /api/object``'s answers and why, in the box's own request."""
    try:
        page = explain(objects._workspace(root))
    except Exception:  # as _plan_part: the part reads "Not established", the box answers
        return None, []
    return page.get("answers"), (page.get("explanation") or {}).get("why") or []


def _part_answers(objects: Any, ran: dict[str, Any]) -> tuple[list, dict[str, Any]]:
    """The server's answer per asked part for each root found by identifier, and which
    parts are left to the agent (module doc). Fails closed: a part a root's kind has no
    plan or page answer for is left to the agent, never worded here."""
    from operator_ui.object_workspace import page_answers_for, plans_for

    asked = objective_runner.asked_parts(ran["objective"])
    roots = [root for root in ran["roots"] if root.get("found_by") == "identifier"
             and isinstance(root.get("object_id"), str)]
    left = set(asked) if not roots or len(roots) > MAX_ANSWER_ROOTS else set()
    answers = []
    for root in roots[:MAX_ANSWER_ROOTS]:
        object_id, kind = root["object_id"], root.get("object_type")
        families = {plan["family"] for plan in plans_for(kind)} if kind else set()
        pages = set(page_answers_for(kind)) if kind else set()
        page: tuple[dict[str, Any] | None, list] | None = None
        parts = []
        for part in asked:
            family = objective_runner.PART_PLANS.get(part)
            if family is not None and family in families:
                said, why = _plan_part(objects, family, object_id)
                parts.append({"part": part, "family": family, "answer": said, "why": why})
            elif part in objective_runner.PAGE_PARTS and part in pages:
                page = page if page is not None else _page(objects, object_id)
                shown, why = page
                said = ({**shown[part], "subject": shown.get("subject")}
                        if isinstance(shown, dict) and isinstance(shown.get(part), dict)
                        else _not_run("this record's page answered nothing for it"))
                parts.append({"part": part, "answer": said, "why": why})
            else:
                left.add(part)
        answers.append({"object_id": object_id, "object_type": kind, "parts": parts})
    return answers, {"asked": asked, "left_to_agent": [p for p in asked if p in left],
                     "agent_called": ran.get("loop") is not None,
                     "roots_not_answered": max(0, len(roots) - MAX_ANSWER_ROOTS)}
