"""A catalogue measure over records no typed business view holds yet (Receipt, Shipment,
AssemblyOrder, the Sage GL lines ...): ASK-GENIE 17.4 G2, master plan G2.

A measure entry names ``source``: ``{"type": <record type>, "columns": {<name>: <how>}}``.
The measure's statement is then made over one derived table built here from the door's
``records`` view (so the reader's door decides every row, as for any statement), never over
a business view. The columns are the entry's own definition, stated in the catalogue:

* ``{"field": f, "kind": "text"}``: the field's text, trimmed, blank = NULL;
* ``{"field": f, "kind": "number"}``: the field as a number; a cell that is not one reads
  NULL (counted by the statement as an unread cell, never coerced, never an error);
* ``{"field": f, "kind": "date"}``: the field's first ten characters as a day when it has
  the shape of one, else NULL;
* ``{"kind": "declared_date"}``: the record's declared day (``records.record_date``, which the
  door reads as the declaration's day: biz.record_dates, ASK12), valid only for a type whose
  declaration names a field that says when the thing happened (``declared_field``);
* ``{"link": "<link type>", "to": "<business view>", "column": c, "as": <fn>}``: a column of
  the business record this one links to (min of it over its links).

``derived``: columns computed from the others (plain SQL over the names above). Fill of every
column is measured on the data (``operating/source_health.py``) and a measure's ``requires``
lines read it: a thin column reads "not measurable" with its fill, never a number.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

DECLARATIONS = Path(__file__).resolve().parent / "pg" / "date_declarations.json"
_NAME = re.compile(r"^[a-z][a-z_0-9]{0,40}$")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z_0-9]{0,60}$")
_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,60}$")
_LINK = re.compile(r"^[A-Za-z]+\.[a-z_]+$")
_VIEW = re.compile(r"^[a-z_]+$")
KINDS = ("text", "number", "date", "declared_date")
NUMBER = r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$"
DAY = r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"


class SourceError(ValueError):
    """A source definition that cannot be made into a statement: refused when the catalogue
    loads, never guessed."""


@lru_cache(maxsize=1)
def declarations() -> Mapping[str, Any]:
    return json.loads(DECLARATIONS.read_text(encoding="utf-8"))["types"]


def declared_field(rtype: str) -> str:
    """The field the type's date declaration names (what its day says when it happened), or
    "" when the type has no declaration."""
    dates = (declarations().get(rtype) or {}).get("dates") or []
    return str(dates[0].get("field") or "") if dates else ""


def columns_of(m: Mapping[str, Any]) -> list[str]:
    src = m["source"]
    return [*src["columns"], *(src.get("derived") or {})]


def check(m: Mapping[str, Any]) -> None:
    """Refuse (``SourceError``) a source that is not a plain, declared one."""
    mid, src = m.get("id"), m.get("source")
    if not isinstance(src, Mapping) or not _TYPE.match(str(src.get("type", ""))):
        raise SourceError(f"measure {mid}: source needs a record type")
    cols = src.get("columns")
    if not isinstance(cols, Mapping) or not cols:
        raise SourceError(f"measure {mid}: source has no columns")
    for name, how in cols.items():
        if not _NAME.match(name) or not isinstance(how, Mapping):
            raise SourceError(f"measure {mid}: column {name!r} is not plain")
        if "link" in how:
            if not (_LINK.match(str(how["link"])) and _VIEW.match(str(how.get("to", "")))
                    and _NAME.match(str(how.get("column", ""))) and how.get("as") in ("min", "max")):
                raise SourceError(f"measure {mid}: link column {name!r} is not plain")
            continue
        kind = how.get("kind")
        if kind not in KINDS:
            raise SourceError(f"measure {mid}: column {name!r} kind {kind!r} is not one of {KINDS}")
        if kind == "declared_date":
            if not declared_field(src["type"]):
                raise SourceError(f"measure {mid}: {src['type']} has no date declaration "
                                  "(ASK12): its day is not a stated one, name a field instead")
        elif not _FIELD.match(str(how.get("field", ""))):
            raise SourceError(f"measure {mid}: column {name!r} names no field")
    for name, expr in (src.get("derived") or {}).items():
        if not _NAME.match(name) or name in cols or not isinstance(expr, str) \
                or ";" in expr or "--" in expr:
            raise SourceError(f"measure {mid}: derived column {name!r} is not plain")
    known = set(columns_of(m))
    for field in ("per", "date"):
        if m.get(field) and m[field] not in known:
            raise SourceError(f"measure {mid}: {field} {m[field]!r} is not a source column")
    if m.get("date"):
        how = cols.get(m["date"]) or {}
        if how.get("kind") not in ("date", "declared_date") and m["date"] not in (src.get("derived") or {}):
            raise SourceError(f"measure {mid}: date {m['date']!r} is not a date column")
    for d in m.get("dimensions") or ():
        if d not in known:
            raise SourceError(f"measure {mid}: dimension {d!r} is not a source column")


def _cell(how: Mapping[str, Any], r: str = "r.") -> str:
    kind = how["kind"]
    if kind == "declared_date":
        return f"CASE WHEN {r}record_date ~ '{DAY}' THEN substr({r}record_date, 1, 10)::date END"
    f = f"{r}fields->>'{how['field']}'"
    if kind == "text":
        return f"NULLIF(btrim({f}), '')"
    if kind == "number":
        return f"CASE WHEN btrim({f}) ~ '{NUMBER}' THEN btrim({f})::numeric END"
    return f"CASE WHEN {f} ~ '{DAY}' THEN substr({f}, 1, 10)::date END"


def from_sql(m: Mapping[str, Any], records: str = "records", links: str = "record_links") -> str:
    """The derived table of measure ``m``: one row per record of its type, ``handle`` and
    each column; ``records``/``links`` name the relations read (the door's by default)."""
    src = m["source"]
    cells, joins = ["r.handle"], []
    for name, how in src["columns"].items():
        if "link" in how:
            joins.append(f" LEFT JOIN LATERAL (SELECT {how['as']}(t.{how['column']}) AS {name} "
                         f"FROM {links} l JOIN {how['to']} t ON t.handle = l.to_handle WHERE "
                         f"l.from_handle = r.handle AND l.link_type = '{how['link']}') j_{name} ON true")
            cells.append(f"j_{name}.{name}")
        else:
            cells.append(f"{_cell(how)} AS {name}")
    base = (f"SELECT {', '.join(cells)} FROM {records} r" + "".join(joins)
            + f" WHERE r.type = '{src['type']}'")
    derived = src.get("derived") or {}
    alias = f"src_{m['id']}"
    if not derived:
        return f"({base}) AS {alias}"
    return (f"(SELECT b.*, " + ", ".join(f"({e}) AS {n}" for n, e in derived.items())
            + f" FROM ({base}) b) AS {alias}")


def is_derived(view: str) -> bool:
    """Whether a statement's FROM is a derived source table (its date columns are days)."""
    return view.startswith("(")
