"""The fill contract (master plan G5): every measure declares the columns it reads and the
least share of rows that must hold a value (``requires: [{column, min_fill}]``); the newest
column health (exported from the standby by
``python3 -m operating.pg.column_health export``) says what the columns hold. A measure on a
column that is missing, unmeasured or below its fill is not measurable: ``problems`` names
it with the fill, and a read of that measure says so instead of a number
(``measurable``)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

HEALTH = Path(__file__).resolve().parents[1] / "devloop" / "column-health.json"
# the same measurement for the columns of a measure over records (``source``, exported by
# ``python3 -m operating.source_health export``): key ``source:<measure id>``
SOURCE_HEALTH = Path(__file__).resolve().parents[1] / "devloop" / "source-health.json"


def health(path: Path = HEALTH, source: Path = SOURCE_HEALTH) -> dict[str, dict[str, dict[str, Any]]]:
    got = json.loads(path.read_text(encoding="utf-8"))
    if source.exists():
        got.update(json.loads(source.read_text(encoding="utf-8")).get("sources", {}))
    return got


def key_of(m: Mapping[str, Any]) -> str:
    """Where measure ``m``'s columns are measured: its view, or its own source."""
    return f"source:{m['id']}" if m.get("source") else m["view"]


def problems(m: Mapping[str, Any], got: Mapping[str, Mapping[str, Mapping[str, Any]]]
             ) -> list[str]:
    """Why measure ``m`` is not measurable on ``got`` (empty: it is)."""
    req = m.get("requires")
    if not isinstance(req, list) or not req:
        return [f"{m['id']}: declares no requires"]
    key = key_of(m)
    cols = got.get(key)
    if cols is None:
        return [f"{m['id']}: {key} has no column health"]
    out = []
    for r in req:
        c, need = r.get("column"), r.get("min_fill")
        if not isinstance(need, (int, float)) or not 0 < need <= 1:
            out.append(f"{m['id']}: {c} min_fill {need!r} is not a share in (0, 1]")
            continue
        h = cols.get(c)
        if h is None:
            out.append(f"{m['id']}: {key}.{c} is not a column")
        elif h.get("fill") is None:
            out.append(f"{m['id']}: {key}.{c} has no measured fill")
        elif h["fill"] < need:
            out.append(f"{m['id']}: {key}.{c} filled on {h['fill']:.1%}, needs {need:.0%}")
    return out


def measurable(m: Mapping[str, Any], got: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``{"status": "ok"}`` or ``{"status": "unknown", "why": "not measurable: ..."}``."""
    why = problems(m, health() if got is None else got)
    return {"status": "ok"} if not why else {"status": "unknown",
                                            "why": "not measurable: " + "; ".join(why)}


def verdict(m: Mapping[str, Any]) -> tuple[str, str]:
    """(why not, note) for a read of measure ``m``: ``why not`` is plain words when it is not
    measurable on the newest exported health ("" when it is); ``note`` says the fill could not
    be checked (health unreadable: the read goes on and says so), "" otherwise."""
    try:
        got = health()
    except (OSError, ValueError) as exc:
        log.warning("fill contract: column health unreadable (%s)", exc)
        return "", f"the fill of its columns could not be checked ({type(exc).__name__})"
    why = problems(m, got)
    return ("not measurable: " + "; ".join(why) if why else ""), ""
