"""The built-in Insights tiles as ready statements for ``plx.sql`` describe: each tile is
one statement ``plx.sql`` reads as the person looking, so an Ask about what a tile counts
(waiting, reply time, workload, approvals, decisions) can count it the way the tile does.
Definitions only: the statement still runs through the reader's door like any other."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from operating.ontology_tools_identity import ToolError
from operating.pg import business_views as bv

INSIGHT = "insight:"
FILE = Path(__file__).resolve().parents[1] / "operator_api" / "insights_builtin.json"


def tiles() -> dict[str, Mapping[str, Any]]:
    """The built-in Insights tiles: each one statement ``plx.sql`` reads as the person
    looking, so an Ask about the same thing can count it the way the tile does."""
    try:
        raw = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = raw.get("insights", []) if isinstance(raw, dict) else raw
    # a tile with a person column or a rollup is more than its statement: the Insights page
    # drops rows after the query (insights.visible_rows), which plx.sql does not, so those
    # are never handed out
    return {str(e["id"]): e for e in raw if isinstance(e, dict) and e.get("id") and e.get("sql")
            and not e.get("person") and not e.get("rollup")}


def listed() -> dict[str, Any]:
    got = tiles()
    if not got:
        return {}
    return {"insights": {k: str(e.get("title") or k) for k, e in got.items()},
            "insights_more": "plx.sql {\"describe\": \"insight:<id>\"}: that Insights tile's "
                             "statement; to answer what a tile measures (waiting, reply time, "
                             "workload, approvals, decisions), run it (or wrap it in WITH ... "
                             "SELECT) rather than writing a new count, so the answer matches "
                             "the tile"}


def described(what: str) -> dict[str, Any]:
    got = tiles()
    tile = got.get(what[len(INSIGHT):])
    if tile is None:
        raise ToolError("unknown_view", f"no insight {what[:64]!r}",
                        "insights: " + ", ".join(sorted(got)))
    return {"insight": tile["id"], "title": str(tile.get("title") or ""),
            "about": str(tile.get("about") or ""),
            "sql": str(tile["sql"]).replace("{business_tz}", bv.BUSINESS_TZ)}
