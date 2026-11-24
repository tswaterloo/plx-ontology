"""The fill contract (master plan G5): every measure declares the columns it reads and the
least share of rows that must hold a value (``requires: [{column, min_fill}]``); the newest
column health (exported from the standby by
``python3 -m operating.pg.column_health export``) says what the columns hold. A measure on a
column that is missing, unmeasured or below its fill is not measurable: ``problems`` names
it with the fill, and a read of that measure says so instead of a number
(``measurable``)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

HEALTH = Path(__file__).resolve().parents[1] / "devloop" / "column-health.json"


def health(path: Path = HEALTH) -> dict[str, dict[str, dict[str, Any]]]:
    return json.loads(path.read_text(encoding="utf-8"))


def problems(m: Mapping[str, Any], got: Mapping[str, Mapping[str, Mapping[str, Any]]]
             ) -> list[str]:
    """Why measure ``m`` is not measurable on ``got`` (empty: it is)."""
    req = m.get("requires")
    if not isinstance(req, list) or not req:
        return [f"{m['id']}: declares no requires"]
    cols = got.get(m["view"])
    if cols is None:
        return [f"{m['id']}: {m['view']} has no column health"]
    out = []
    for r in req:
        c, need = r.get("column"), r.get("min_fill")
        if not isinstance(need, (int, float)) or not 0 < need <= 1:
            out.append(f"{m['id']}: {c} min_fill {need!r} is not a share in (0, 1]")
            continue
        h = cols.get(c)
        if h is None:
            out.append(f"{m['id']}: {m['view']}.{c} is not a column")
        elif h.get("fill") is None:
            out.append(f"{m['id']}: {m['view']}.{c} has no measured fill")
        elif h["fill"] < need:
            out.append(f"{m['id']}: {m['view']}.{c} filled on {h['fill']:.0%}, needs {need:.0%}")
    return out


def measurable(m: Mapping[str, Any], got: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``{"status": "ok"}`` or ``{"status": "unknown", "why": "not measurable: ..."}``."""
    why = problems(m, health() if got is None else got)
    return {"status": "ok"} if not why else {"status": "unknown",
                                            "why": "not measurable: " + "; ".join(why)}
