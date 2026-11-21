"""plx.insight: the simple Ask's read of a built-in Insights tile exactly as the Insights page
opens it for the person asking (``backend.insight_result``: the tile withheld when it is not
for them, its rows through ``insights.visible_rows`` with their Team scope), so an answer about
what a tile counts matches the tile. Off unless ``PLX_ASK_INSIGHT_TOOL=1``: its after-switch
eval decides."""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from operating.agent_session import LocalTool
from operating.ontology_tools_sql import _cell

NAME = "plx.insight"
FLAG = "PLX_ASK_INSIGHT_TOOL"
SPEC = {
    "name": NAME,
    "description": (
        "Read a built-in Insights tile exactly as the Insights page shows it to this person "
        "(their access and Team view applied; rows it hides never come back). Use it, not a count of your own, when the question is what a tile "
        "measures: colleagues_waiting / team_waiting (who is waiting on whom), "
        "team_reply_time, team_output, waiting_on_others, planner_open, leader_scorecard, "
        "org_span. A tile not for this person is refused (insight_withheld): say so, never "
        "work around it; one that cannot be read now (insight_unreadable): count it with "
        "plx.sql. plx.sql {\"describe\": true} lists the other tiles."),
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["id"],
                    "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 64}}},
    "annotations": {"readOnlyHint": True},
}


def tools(backend: Any, reader: Any) -> list[Any]:
    """``plx.insight`` bound to ``reader``; none while the flag is off or the backend has no
    Insights."""
    if os.environ.get(FLAG) != "1" or not callable(getattr(backend, "insight_result", None)):
        return []
    return [LocalTool(spec=SPEC, call=lambda arguments: read(backend, reader, arguments))]


def read(backend: Any, reader: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from . import insights
    iid = str((arguments or {}).get("id") or "")
    ids = sorted(s.id for s in insights.builtin())
    if iid not in ids:
        return {"ok": False, "error": "not_found",
                "detail": "a built-in Insights tile id: " + ", ".join(ids)}
    try:
        got = backend.insight_result(reader, iid)
    except insights.Refused as refused:
        return {"ok": False, "error": str(getattr(refused, "code", "") or "refused"),
                "detail": str(refused)[:300]}
    except Exception as error:  # noqa: BLE001 - unread now: the Ask counts with plx.sql
        # where it failed (file:line of the last frames, never a value): 9 of 15 calls
        # failed so on a loaded Spark (11-21), not reproduced alone
        import logging
        import traceback
        where = [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}"
                 for f in traceback.extract_tb(error.__traceback__)[-4:]]
        logging.getLogger("operator_api.ask").warning(json.dumps(
            {"insight_unread": type(error).__name__, "where": where}))
        return {"ok": False, "error": "insight_unreadable",
                "detail": "the tile could not be read now: count it with plx.sql"}
    return {"ok": True, "insight": iid, "title": got.title, "columns": list(got.columns),
            # plx.sql's row shape, so the Ask's figure gate and citations read it as one
            "rows": [{"cells": [_cell(c)[0] for c in row]} for row in got.rows],
            "row_count": got.row_count, "truncated": got.truncated, "as_of": got.as_of}
