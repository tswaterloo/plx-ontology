"""The console's home view: the signed-in employee's open cases and open gaps, and the gaps
the scheduled engine found (``operating.proactive_engine``'s persisted inbox, read here, never
scanned on the request).

Everything is read for the request session's actor: the case store shows a case only to its
audience and withholds a gap whose citations that reader cannot resolve (counted, its words
never shown); the inbox is trimmed by the same actor's grant and per-item decisions. Read-only: a detected
gap becomes a record only when the person sends its follow-up.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from operating.cases import WITHHELD, CaseIntegrityError
from ontology.types import ValidationError
from operator_ui import objective_box
from operator_ui.case_plain import plain_gap, question_root_types

MAX_HOME_CASES = 50
GAP_FIELDS = ("id", "missing", "why", "who_can_answer", "state", "at", "citations")


def home(case_workspace: Any, objects: Any) -> dict[str, Any]:
    """``{"cases": ..., "proactive": ..., "objective": ...}``; each part says
    ``not_configured`` when its workspace is absent for this session and never fails the
    other (``objective``: whether the objective box runs, ``operator_ui.objective_box``)."""
    return {"cases": _open_cases(case_workspace), "proactive": _found(objects),
            "objective": objective_box.served(objects)}


def _open_cases(workspace: Any) -> dict[str, Any]:
    if workspace is None:
        return {"status": "not_configured"}
    try:
        listed = workspace.list_cases(status="open", limit=MAX_HOME_CASES)["cases"]
    except (ValueError, sqlite3.Error) as error:
        return {"status": "unreadable", "error": type(error).__name__}
    cases: list[dict[str, Any]] = []
    unreadable = 0
    for case in listed:
        try:
            view = workspace.runtime.cases.get(workspace.actor, case["case_id"])
        except (LookupError, ValidationError, CaseIntegrityError, sqlite3.Error):
            unreadable += 1
            continue
        gaps = view.get("gaps") or []
        roots = question_root_types(case.get("question"))
        cases.append({**{key: case[key] for key in ("case_id", "question", "opened_at",
                                                    "updated_at", "events")},
                      "gaps": [{**{key: gap.get(key) for key in GAP_FIELDS},
                                "plain": plain_gap(gap, roots)}
                               for gap in gaps if gap.get("content") != WITHHELD],
                      "gaps_withheld": sum(1 for gap in gaps if gap.get("content") == WITHHELD)})
    return {"status": "read", "cases": cases, "open_cases": len(cases),
            "open_gaps": sum(len(case["gaps"]) for case in cases),
            "gaps_withheld": sum(case["gaps_withheld"] for case in cases),
            "cases_unreadable": unreadable, "max_cases": MAX_HOME_CASES,
            "cases_listing_full": len(listed) >= MAX_HOME_CASES}


def _found(objects: Any) -> dict[str, Any]:
    if objects is None:
        return {"status": "not_configured"}
    try:
        return objects.gap_inbox()
    except PermissionError:
        return {"status": "refused", "error": "permission_denied", "gaps": []}
    except Exception as error:   # noqa: BLE001 - the inbox fails alone, typed, cases still serve
        return {"status": "unreadable", "error": type(error).__name__, "gaps": []}
