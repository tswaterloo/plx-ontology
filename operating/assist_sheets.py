"""Entry sheets (ASSIST specs 2.8, class C: a system we only read, the person keys the entry).

``sheet(routine, trigger_row, records)`` prepares the entry's fields from what the routine's
``field_sources`` name: ``trigger.<col>`` (a column of the trigger row), ``record.<view>.<col>`` (a
column of a linked record's row), ``constant`` (its value) or ``person`` (left for the person: value
None, shown as "you fill this"). ``sure`` is true only when every act the source was mined on had
that very value (``equal_share == 1``); a field whose source is absent is ``person``.
No field source can be mined from the action log today (its ``fields`` hold only the records list:
open decision AS8), so a routine carries no ``field_sources`` yet and every field is the person's;
the sheet then says so instead of filling guesses.

``compare(prepared, captured)`` is the exact match after trimming (numbers as numbers, dates as
dates). ``verify(pg, ...)`` looks up the captured entry for each open sheet (``captured(record,
kind) -> row | None``), writes ``sheet_matched`` / ``sheet_mismatch`` to the journal with the
differing fields, and never counts an entry that was never keyed.
Evidence: isolated-fixture (``operating/tests/test_assist_sheets.py``); live-unverified."""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional, Sequence

SCHEMA = "assist"


def _pick(src: Mapping[str, Any], trigger_row: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]],
          field: str) -> tuple[Any, str]:
    how = str(src.get("from") or "person")
    if how.startswith("trigger."):
        col = how.split(".", 1)[1]
        return trigger_row.get(col), (how if col in trigger_row else "person")
    if how.startswith("record."):
        _r, view, col = how.split(".", 2)
        row = records.get(view) or {}
        return row.get(col), (how if col in row else "person")
    if how == "constant":
        return src.get("value"), "constant"
    return None, "person"


def sheet(routine: Mapping[str, Any], trigger_row: Mapping[str, Any],
          records: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict[str, Any]:
    """``{"system", "kind", "fields": [{"name", "value", "source", "sure"}], "order": [...], "ready": n,
    "for_you": n}``; ``order`` is the entry screen's order (``field_sources`` order as mined)."""
    sources = routine.get("field_sources") or {}
    fields = []
    for name, src in sources.items():
        value, how = _pick(src, trigger_row, records or {}, name)
        fields.append({"name": name, "value": value, "source": how,
                       "sure": how != "person" and value is not None and float(src.get("equal_share") or 0) == 1.0})
    ready = sum(1 for f in fields if f["source"] != "person" and f["value"] is not None)
    return {"system": routine.get("system", ""), "kind": routine.get("kind", ""), "fields": fields,
            "order": [f["name"] for f in fields], "ready": ready, "for_you": len(fields) - ready,
            "note": "" if fields else "no field could be prepared: every field is for you"}


def _when(x: datetime) -> str:
    return x.date().isoformat() if x.time() == datetime.min.time() and x.tzinfo is None else x.isoformat()


def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, datetime):
        return _when(v)
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        pass
    try:
        return _when(datetime.fromisoformat(s))
    except ValueError:
        return s


def compare(prepared: Sequence[Mapping[str, Any]], captured: Mapping[str, Any]) -> dict[str, Any]:
    """Field by field over the PREPARED fields that held a value: ``{"matched": bool, "differ":
    [{"name", "prepared", "captured"}], "checked": n}``. A field left to the person is not compared."""
    differ, checked = [], 0
    for f in prepared:
        if f.get("source") == "person" or f.get("value") is None:
            continue
        checked += 1
        got = captured.get(f["name"])
        if _norm(f["value"]) != _norm(got):
            differ.append({"name": f["name"], "prepared": f["value"], "captured": got})
    return {"matched": checked > 0 and not differ, "differ": differ, "checked": checked}


_OPEN = f"""
SELECT m.matter_id, m.owner_person, m.sources->'records'->>0, m.sources->>'act_kind',
       m.plan->'prepared'->'sheet', m.plan->'prepared'->>'system'
FROM {SCHEMA}.matters m
WHERE m.kind = 'routine' AND m.state IN ('open', 'unverified') AND m.plan->'prepared'->'sheet' IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.write_journal j WHERE j.matter_id = m.matter_id
                  AND j.event IN ('sheet_matched', 'sheet_mismatch'))"""


def verify(pg: Any, captured: Callable[[str, str], Optional[Mapping[str, Any]]]) -> dict[str, int]:
    """For each open sheet with no verdict yet: ``captured(record, kind)`` is the entry as the
    system now holds it. Found -> ``sheet_matched`` when every prepared field is equal, else
    ``sheet_mismatch`` with the differing fields; not found -> nothing is written and the sheet
    stays open (an entry never keyed is never counted as matched)."""
    out = {"open": 0, "matched": 0, "mismatch": 0, "not_keyed": 0, "nothing_prepared": 0}
    for mid, person, rec, kind, sh, system in pg.execute(_OPEN).fetchall():
        out["open"] += 1
        row = captured(rec, kind)
        if row is None:
            out["not_keyed"] += 1
            continue
        got = compare((sh or {}).get("fields") or [], row)
        if got["checked"] == 0:         # no prepared field held a value: there is nothing to match or miss
            out["nothing_prepared"] += 1
            continue
        event = "sheet_matched" if got["matched"] else "sheet_mismatch"
        out["matched" if got["matched"] else "mismatch"] += 1
        pg.execute(f"INSERT INTO {SCHEMA}.write_journal (prepared_id, person, origin, matter_id, system, operation, "
                   "class, event, target, sources) VALUES (%s, %s, 'assist', %s, %s, %s, 'C', %s, %s, %s)",
                   (f"sheet:{mid}", person, mid, system or (sh or {}).get("system") or "", kind, event,
                    json.dumps({"record": rec}), json.dumps({"differ": got["differ"], "checked": got["checked"]})))
    return out
