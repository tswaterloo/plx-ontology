"""Live check of the console question box's one path (``workspace.console_question_one_path``):
a two-part question about one purchase order by its number ("Why is <number> late, and
what is missing?") through ``operator_ui.objective_box.answer`` (the only path the box
posts, ``POST /api/objective``) on the object workspace the console builds for one named
reader of the persistent host (``scripts.eval_real_agent.ask_workspace``), with a headless
Claude Code planner. The browser routes nothing: the server answers each part a plan or
the record's page covers, and the agent runs beside it.

The order is one the reader's own home inbox shows a gap on (``gap_inbox``, through
``scripts.eval_flagship_path.home_inbox``), whose number the reader's ``plx.find`` resolves
to exactly that one record (``objective_runner.find_roots``): never chosen by value.

Pass: both parts asked (``late``, ``missing``), each answered by the server for the order
(none left to the agent), each answer exactly what ``GET /api/investigate`` /
``GET /api/object`` show the same reader, and the agent called. Preflight is
``scripts.eval_objective_box``'s (each refusal blocks with its code; ``no_employee_identity``
waits on a person naming a reader); no order the reader may read to ask about is blocked
``no_readable_order``. Stdout is one JSON object of counts and enums only: no question
text, order number, verdict text, id or handle leaves the run. Exit 0 (pass or blocked),
3 when a fail reason is set. Evidence: live-unverified until a passing result on the live
host; a keyless test drives ``main`` on the fixture world with a scripted planner
(injected transport, never live).
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
import os
from typing import Any, Callable, Mapping, Sequence

QUESTION = "Why is {number} late, and what is missing?"
PARTS = ("late", "missing")
ORDER_TYPE = "PurchaseOrder"
MAX_PICKS = 20
ITEM_ERRORS = (ValueError, LookupError, PermissionError, OSError)


def _report(blocked: str | None, measured: Mapping[str, Any] | None) -> dict[str, Any]:
    measured = dict(measured or {})
    fail = list(measured.pop("fail_reasons", [])) if blocked is None else []
    return {"adapter_status": "live-unverified", "values_published": False,
            "blocked": blocked,
            "console_question": {"ran": 1 if measured else 0, **measured,
                                 "fail_reasons": fail}}


def pick_order(workspace: Any, as_of: date) -> str | None:
    """The question naming the first order of the reader's home gaps whose number the
    reader's ``plx.find`` resolves to that one order (``MAX_PICKS`` tried at most)."""
    from operating import objective_runner
    from scripts.eval_flagship_path import home_inbox

    home = home_inbox(workspace, as_of)
    tried: set[str] = set()
    for gap in home.get("gaps") or ():
        object_id = gap.get("object_id")
        if gap.get("object_type") != ORDER_TYPE or not isinstance(object_id, str) \
                or object_id in tried:
            continue
        tried.add(object_id)
        if len(tried) > MAX_PICKS:
            break
        with workspace._request():
            card = workspace._call("plx.object", {"object_id": object_id})
            display = (card.get("object") or {}).get("display") or {}
            number = display.get("untrusted_text") if isinstance(display, Mapping) else None
            if not isinstance(number, str) or not number.strip():
                continue
            question = QUESTION.format(number=number.strip())
            found = objective_runner.find_roots(
                question, lambda arguments: workspace._call("plx.find", arguments))
        if [root["object_id"] for root in found["roots"]] == [object_id] \
                and not found["ambiguous"]:
            return question
    return None


def measure(workspace: Any, question: str) -> dict[str, Any]:
    """The question through the box, each server part checked against the reader's own
    ``investigate`` / ``workspace`` answer for the same record."""
    from operator_ui import objective_box

    try:
        out = objective_box.answer(workspace, {"objective": question, "mode": "investigate"})
    except ITEM_ERRORS as error:
        return {"error": type(error).__name__, "fail_reasons": ["box_refused"]}
    parts = out.get("parts") or {}
    asked = list(parts.get("asked") or [])
    left = list(parts.get("left_to_agent") or [])
    answered = [entry for entry in out.get("answers") or [] if entry.get("parts")]
    states: Counter = Counter()
    covered = matches = 0
    for entry in answered:
        page = None
        for part in entry["parts"]:
            covered += 1
            states[str((part.get("answer") or {}).get("state"))] += 1
            if part.get("family"):
                server = workspace.investigate(part["family"], entry["object_id"])
                same = (server.get("verdict") == part.get("answer")
                        and (server.get("explanation") or {}).get("why") == part.get("why"))
            else:
                page = page if page is not None else (
                    workspace.workspace(entry["object_id"]).get("answers") or {})
                same = part.get("answer") == {**(page.get(part["part"]) or {}),
                                              "subject": page.get("subject")}
            matches += bool(same)
    unanswered = [part for part in asked if part not in left
                  and not any(p["part"] == part for e in answered for p in e["parts"])]
    fail = ([] if parts.get("agent_called") else ["agent_not_called"]) \
        + ([] if asked == list(PARTS) else ["parts_not_asked"]) \
        + ([] if answered else ["no_root_answered"]) \
        + (["part_left_to_agent"] if left else []) \
        + (["verdict_differs_from_server"] if matches != covered else []) \
        + (["unanswered_part"] if unanswered else [])
    return {"parts_asked": ",".join(asked), "parts_left_to_agent": left,
            "agent_called": bool(parts.get("agent_called")),
            "agent_outcome": (out.get("answer") or {}).get("outcome"),
            "roots_answered": len(answered), "parts_covered": covered,
            "matches_server": matches, "unanswered_parts": len(unanswered),
            "verdict_states": dict(sorted(states.items())),
            "roots_not_answered": parts.get("roots_not_answered", 0),
            "fail_reasons": fail}


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         make_workspace: Callable[[], tuple[Any, Any]] | None = None,
         as_of: date | None = None) -> int:
    """``make_workspace`` (tests only): ``(closer, workspace)`` in place of the live host's."""
    from scripts.eval_objective_box import _live_workspace

    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_console_question",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--planner-timeout", type=int, default=120)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--host-config")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    def emit(report: dict[str, Any]) -> int:
        print(json.dumps(report, sort_keys=True))
        return 3 if report["console_question"]["fail_reasons"] else 0

    if make_workspace is None:
        opened, blocked = _live_workspace(environ, args)
        if blocked:
            return emit(_report(blocked, None))
    else:
        opened = make_workspace()
    closer, workspace = opened
    try:
        question = pick_order(workspace, as_of or date.today())
        if question is None:
            return emit(_report("no_readable_order", None))
        return emit(_report(None, measure(workspace, question)))
    finally:
        try:
            workspace.close()
        finally:
            closer.close()


if __name__ == "__main__":
    raise SystemExit(main())
