"""plx.insight: the simple Ask's read of a built-in Insights tile exactly as the Insights page
opens it for the person asking (``backend.insight_result``: the tile withheld when it is not
for them, its rows through ``insights.visible_rows`` with their Team scope), so an answer about
what a tile counts matches the tile. Off unless ``PLX_ASK_INSIGHT_TOOL=1``: its after-switch
eval decides."""
from __future__ import annotations

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
        "(their access and Team view applied; rows it hides from them never come back): its "
        "columns and rows. Use it, not a count of your own, when the question is what a tile "
        "measures: colleagues_waiting / team_waiting (who is waiting on whom), "
        "team_reply_time, team_output, waiting_on_others, planner_open, leader_scorecard, "
        "org_span. A tile not for this person is refused (insight_withheld): say so, never "
        "work around it. plx.sql {\"describe\": true} lists the other tiles."),
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
    return {"ok": True, "insight": iid, "title": got.title, "columns": list(got.columns),
            "rows": [[_cell(c)[0] for c in row] for row in got.rows],
            "row_count": got.row_count, "truncated": got.truncated, "as_of": got.as_of}
