"""Live check of Home's objective box (``workspace.objective_box``): three free-form
objectives, no object id and no case id, run through ``operator_ui.objective_box.answer``
(the path ``POST /api/objective`` serves) on the object workspace the console builds for
one named reader of the persistent host (``scripts.eval_real_agent.ask_workspace``: the
reader's own session start-up, its case runtime and projection store), with a headless
Claude Code planner (``operating.agent_loop.ClaudeCodePlanner``, as ``case_console --ask``
wires it). Each objective must end in a cited answer or honest gaps: ``cited`` (at least one
claim the reader may see, every citation openable), ``honest_gaps`` (no claim, but the
gaps say what is missing), else ``unanswered``.

Preflight, in ``eval_real_agent.live_part``'s order, before any read or session: the
``claude`` binary, the reader identity (``PLX_EVAL_READER``), the session secret, the host
state and config, the host's acceptance of the reader, its projection store. A missing one
is ``blocked`` with its code (``no_employee_identity`` waits on a person naming a reader).
A run where no objective reached the planner (``every_objective_failed``) is blocked too.
Stdout is one JSON object of counts and enums only: no objective text, claim text, id or
handle leaves the run. Exit 0 (pass or blocked), 3 when an objective went unanswered.
Evidence: live-unverified until a passing result on the live host; a keyless test drives
``main`` on the fixture world with a scripted planner (injected transport, never live).
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping, Sequence

# Generic objectives an employee would type: no case id, no record id.
OBJECTIVES = (
    {"objective": "Which of our open purchase orders are late, and why?",
     "mode": "investigate"},
    {"objective": "We want to air-ship our next open order to its customer by the end of "
                  "next month. Which inputs (landed cost, lead times, supplier quotes) do "
                  "our records already quote, and what must a person confirm?",
     "mode": "scenario"},
    {"objective": "What is the status of our most recent assembly order, and what is "
                  "blocking it?", "mode": "investigate"},
)
ITEM_ERRORS = (ValueError, LookupError, PermissionError, OSError)


def _report(blocked: str | None, items: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = Counter(item["verdict"] for item in items)
    fail_reasons = (["unanswered"] if verdicts["unanswered"] else []) if blocked is None else []
    return {"adapter_status": "live-unverified", "values_published": False,
            "blocked": blocked,
            "objective_box": {"objectives": len(OBJECTIVES), "ran": len(items),
                              "cited": verdicts["cited"],
                              "honest_gaps": verdicts["honest_gaps"],
                              "unanswered": verdicts["unanswered"], "items": items,
                              "fail_reasons": fail_reasons}}


def measure(objects: Any, objectives: Sequence[Mapping[str, Any]] = OBJECTIVES
            ) -> list[dict[str, Any]]:
    """Each objective's verdict and counts through the box (``objective_box.answer``)."""
    from operator_ui import objective_box

    items: list[dict[str, Any]] = []
    for request in objectives:
        try:
            out = objective_box.answer(objects, dict(request))
        except ITEM_ERRORS as error:
            items.append({"mode": request.get("mode"), "verdict": "unanswered",
                          "error": type(error).__name__, "outcome": None})
            continue
        answer = out["answer"]
        claims = answer["claims"]
        openable = all(cite["openable"] for claim in claims for cite in claim["citations"])
        verdict = ("cited" if claims and openable else
                   "honest_gaps" if not claims and out["gaps"] else "unanswered")
        scenario = out.get("scenario") or {}
        items.append({
            "mode": out["mode"], "verdict": verdict, "outcome": answer["outcome"],
            "reason": answer["reason"], "cited_claims": len(claims),
            "citations_openable": openable, "unsupported": len(answer["unsupported"]),
            "unknowns": len(answer["unknowns"]), "gaps": len(out["gaps"]),
            "roots": dict(Counter(root["found_by"] for root in out["roots"])),
            "ambiguous": len(out["ambiguous"]), "follow_ups": len(out["follow_ups"]),
            "steps": answer["steps"], "tool_calls": len(answer["tool_calls"]),
            **({"inputs": dict(Counter(item["state"] for item in scenario["inputs"])),
                "must_confirm": len(scenario["must_confirm"])} if scenario else {})})
    return items


def _planner_reached(item: Mapping[str, Any]) -> bool:
    reason = str(item.get("reason") or "")
    return item.get("outcome") is not None and not (
        item.get("outcome") == "escalated" and reason.startswith("planner"))


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         make_workspace: Callable[[], tuple[Any, Any]] | None = None) -> int:
    """``make_workspace`` (tests only): ``(closer, workspace)`` in place of the live host's."""
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_objective_box",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--planner-timeout", type=int, default=120)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--host-config")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    def emit(report: dict[str, Any]) -> int:
        print(json.dumps(report, sort_keys=True))
        return 3 if report["objective_box"]["fail_reasons"] else 0

    if make_workspace is None:
        opened, blocked = _live_workspace(environ, args)
        if blocked:
            return emit(_report(blocked, []))
    else:
        opened = make_workspace()
    closer, workspace = opened
    try:
        items = measure(workspace)
    finally:
        try:
            workspace.close()
        finally:
            closer.close()
    if not any(_planner_reached(item) for item in items):
        return emit(_report("every_objective_failed", items))
    return emit(_report(None, items))


def _live_workspace(environ: Mapping[str, str], args: argparse.Namespace
                    ) -> tuple[tuple[Any, Any] | None, str | None]:
    from operating import agent_loop
    from operating.session_identity import session_secret
    from scripts import eval_real_agent as real

    if shutil.which("claude") is None:
        return None, "binary_not_found"
    identity, problem = real.reader_identity(environ, real.READER_ENV)
    if identity is None:
        return None, problem
    secret = session_secret(environ)
    state = Path(environ.get(real.STATE_ENV) or "")
    config = Path(args.host_config or environ.get(real.HOST_CONFIG_ENV) or "")
    missing = ("session_secret_missing" if not secret else
               "host_state_missing" if not environ.get(real.STATE_ENV) or not state.is_dir()
               else "host_config_missing" if not config.name or not config.is_file() else None)
    if missing:
        return None, missing
    host_config = json.loads(config.read_text(encoding="utf-8"))
    extra = ({name: environ[name] for name in real.GRAPH_ENV if environ.get(name)}
             if host_config.get("entra_directory") else {})
    host = real.LiveHost(state, config, identity, secret,
                         tenant=str(host_config.get("tenant") or "plx"), extra_env=extra)
    refused = real.reader_refusal(host)
    if refused:
        return None, refused
    refused_store = real.store_refusal(state)
    if refused_store is not None:
        return None, refused_store[0]
    try:
        session, workspace, _ = real.ask_workspace(
            host, agent_loop.ClaudeCodePlanner(timeout=args.planner_timeout),
            agent_loop.Bounds(max_steps=args.max_steps))
    except real.runner.RunnerError as error:
        return None, str(error).split(":", 1)[0]
    return (session, workspace), None


if __name__ == "__main__":
    raise SystemExit(main())
