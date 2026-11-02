"""Price history: an item's purchase prices over time and their trend, from the records
that state a price paid or ordered, never an item card's or a supplier list's cost.

The engine of the ``plx.investigate`` plan ``ontology/plans/price_history.json`` (family
F30, evidence kind ``compute``). The plan walks the item and its same-record twins to the
lines that bought it and their headers, each read through ``plx.object`` as the
investigating reader may read it, so a line, a header or a price the reader may not see
never reaches this engine. ``engine_args``:

``{"series": [{"id", "label", "price", "header_link", "date" | "header_handle" +
"header_date_field", "status"?, "status_in"? | "status_not_in"?}, ...]}``

* ``price``: the property item of each line's unit price (one per line, its unit and
  currency as the projection reads them);
* ``header_link``: the line-to-header link type (each line has exactly one header);
* ``date``: the property item of the header's date, or ``header_handle`` (a property item
  of the header whose citation is the header's own record) and ``header_date_field`` (a
  row field of that record, re-read for the reader through ``resolver``: a header the
  reader cannot re-read gives that line no date);
* ``status`` with ``status_in`` (only these, e.g. posted invoices) or ``status_not_in``
  (never these, e.g. cancelled or draft orders), compared case-folded.

Each series in the order the plan lists them (its precedence: a price paid before a price
ordered) is the item's dated prices; its trend is the last dated price against the one
before it on an earlier day: ``up``, ``down`` or ``flat``, with the change in percent
(Decimal, half-up to 0.01). A day with two different prices, a last and previous price in
two units or two currencies, a single dated price, a line with no readable header or date,
or a run the step budget cut, gives no direction (a typed gap): never a guess. Entries
(names under the compute item's ``name``): ``<name>_<id>_last_price`` and
``_previous_price`` (each carrying the line's own citation, unit and currency, so the
answer cites the line), ``_direction``, ``_change_percent`` and ``_history`` (the dated
prices, oldest first, in its steps); ``<name>`` is the direction of the first series that
has one. No price is converted or added across units or currencies.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import re
from typing import Any, Callable, Mapping, Sequence

ENGINE = "price_history"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
_LINK_TYPE = re.compile(r"^[A-Z][A-Za-z0-9]*\.[a-z][a-z0-9_]*$")
_FIELD = re.compile(r"^row\.[A-Za-z_][A-Za-z0-9_]*$")
SERIES_KEYS = frozenset({"id", "label", "price", "header_link", "date", "header_handle",
                         "header_date_field", "status", "status_in", "status_not_in"})
MAX_SERIES = 4
MAX_LISTED = 30          # dated prices listed in a history entry's steps
MAX_STEP_CHARS = 300
CENT = Decimal("0.01")

Resolver = Callable[[str, Any], Any]


# --------------------------------------------------------------------------- check

def plan_check(args: Any, doc: Mapping[str, Any], index: int, schema: Any) -> list[str]:
    """Problems with ``price_history`` ``engine_args`` (module doc)."""
    if not isinstance(args, Mapping):
        return ["engine_args must be an object"]
    out = [f"unknown engine argument {k}" for k in set(args) - {"series"}]
    series = args.get("series")
    if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
        return out + [f"series must list 1 to {MAX_SERIES} series"]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    declared = getattr(schema, "link_types", None) or {}
    seen: set[str] = set()
    for spec in series:
        if not isinstance(spec, Mapping):
            out.append("each series must be an object")
            continue
        sid = spec.get("id")
        label = f"series {sid}"
        out += [f"{label}: unknown key {k}" for k in set(spec) - SERIES_KEYS]
        if not isinstance(sid, str) or not _NAME.match(sid) or sid in seen:
            out.append(f"{label}: id must be a short unique snake_case name")
        seen.add(str(sid))
        if not isinstance(spec.get("label"), str) or not spec["label"].strip():
            out.append(f"{label}: label must be text")
        for key in ("price", *(k for k in ("date", "header_handle", "status") if k in spec)):
            item = items.get(spec.get(key))
            if item is None or item.get("kind") != "property":
                out.append(f"{label}: {key} must name a property item listed before it")
        link = spec.get("header_link")
        if not isinstance(link, str) or not _LINK_TYPE.match(link):
            out.append(f"{label}: header_link must be a link type")
        elif declared and link not in declared:
            out.append(f"{label}: {link} is not a declared link type")
        if ("date" in spec) == ("header_handle" in spec):
            out.append(f"{label}: give date or header_handle, not both or neither")
        if ("header_handle" in spec) != ("header_date_field" in spec):
            out.append(f"{label}: header_handle and header_date_field go together")
        if "header_date_field" in spec and not (isinstance(spec["header_date_field"], str)
                                                and _FIELD.match(spec["header_date_field"])):
            out.append(f"{label}: header_date_field must be a row field path")
        filters = [k for k in ("status_in", "status_not_in") if k in spec]
        if len(filters) > 1 or bool(filters) != ("status" in spec):
            out.append(f"{label}: status takes exactly one of status_in or status_not_in")
        for key in filters:
            if not (isinstance(spec[key], list) and spec[key]
                    and all(isinstance(s, str) and s.strip() for s in spec[key])):
                out.append(f"{label}: {key} must list statuses")
    return out


# --------------------------------------------------------------------------- run

def _text(value: Any) -> Any:
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value.get("untrusted_text")
    return value


def _decimal(value: Any) -> Decimal | None:
    value = _text(value)
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _day(value: Any) -> str | None:
    value = _text(value)
    if not isinstance(value, str) or not _DAY.match(value.strip()):
        return None
    return value.strip()[:10]


def _unit(entry: Mapping[str, Any]) -> str | None:
    from ontology.units import unit_token
    unit = entry.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        return None
    return unit_token(unit) or unit.strip()


def _reread(resolver: Resolver | None, actor: Any, handle: Any, path: str) -> Any:
    """The cited record's field re-read for the actor; None when it cannot be read."""
    from operating.cases import _field
    if resolver is None or not isinstance(handle, str):
        return None
    try:
        view = resolver(handle, actor)
        return _field(view, path) if isinstance(view, Mapping) else None
    except Exception:  # noqa: BLE001 - unreadable for this reader: fails closed
        return None


class _Point:
    __slots__ = ("day", "price", "unit", "currency", "entry", "line", "header")

    def __init__(self, day: str, price: Decimal, entry: Mapping[str, Any], line: str,
                 header: str) -> None:
        self.day, self.price, self.entry, self.line, self.header = (day, price, entry, line,
                                                                    header)
        self.unit, self.currency = _unit(entry), entry.get("currency")

    def said(self) -> str:
        priced = " ".join(x for x in (_plain(self.price), self.unit, self.currency) if x)
        return f"{self.day}: {priced} (entry {self.entry['name']})"


def _first_by_object(entries: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        oid = entry.get("object_id")
        if isinstance(oid, str) and entry.get("value") is not None and oid not in out:
            out[oid] = entry
    return out


def _gap(state: str, reason: str, subject: str, detail: str,
         concerns: str | None = None) -> dict[str, Any]:
    gap = {"state": state, "reason": reason, "subject": subject[:700],
           "detail": f"{reason}: {detail}"[:600]}
    if concerns:
        gap["concerns"] = concerns[:64]
    return gap


def _points(spec: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
            headers: Mapping[tuple[str, str], set[str]], resolver: Resolver | None,
            actor: Any) -> tuple[list[_Point], dict[str, int]]:
    """The series' dated prices and how many lines were left out, by why."""
    left: dict[str, int] = {}
    statuses = _first_by_object(evidence.get(spec.get("status"), ())) if "status" in spec \
        else {}
    dates = _first_by_object(evidence.get(spec["date"], ())) if "date" in spec else {}
    handles = _first_by_object(evidence.get(spec["header_handle"], ())) \
        if "header_handle" in spec else {}
    allowed = {s.casefold() for s in spec.get("status_in", ())}
    barred = {s.casefold() for s in spec.get("status_not_in", ())}
    points, seen = [], set()
    for entry in evidence.get(spec["price"], ()):
        line = entry.get("object_id")
        if not isinstance(line, str) or line in seen or entry.get("value") is None:
            continue
        seen.add(line)
        price = _decimal(entry["value"])
        if price is None or price <= 0:
            left["no_price"] = left.get("no_price", 0) + 1
            continue
        owners = headers.get((spec["header_link"], line), set())
        if len(owners) != 1:
            left["header_unknown"] = left.get("header_unknown", 0) + 1
            continue
        header = next(iter(owners))
        if "status" in spec:
            status = _text((statuses.get(header) or {}).get("value"))
            status = status.strip().casefold() if isinstance(status, str) else None
            if (allowed and status not in allowed) or (barred and status in barred):
                left["status_excluded"] = left.get("status_excluded", 0) + 1
                continue
        if "date" in spec:
            day = _day((dates.get(header) or {}).get("value"))
        else:
            cite = (handles.get(header) or {}).get("citation") or {}
            day = _day(_reread(resolver, actor, cite.get("l1_handle"),
                               spec["header_date_field"]))
        if day is None:
            left["date_unknown"] = left.get("date_unknown", 0) + 1
            continue
        points.append(_Point(day, price, entry, line, header))
    points.sort(key=lambda p: (p.day, p.line))
    return points, left


def _price_entry(name: str, point: _Point, role: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name[:64], "kind": "computed", "value": _plain(point.price),
        "value_type": "money", "inputs": [point.entry["name"]], "basis": [point.line],
        "steps": [f"the {role} dated price: {point.said()}; header {point.header}"[
            :MAX_STEP_CHARS]], "check": point.entry.get("check", "not_checked")}
    citation = point.entry.get("citation")
    if isinstance(citation, Mapping):
        entry["citation"] = dict(citation)
    for key in ("unit", "currency"):
        if point.entry.get(key) is not None:
            entry[key] = point.entry[key]
    return entry


def _series(spec: Mapping[str, Any], name: str, points: list[_Point],
            left: Mapping[str, int], complete: bool, lacked: Sequence[str]
            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """(entries, gaps, direction) of one series."""
    base = f"{name}_{spec['id']}"
    label = spec["label"]
    gaps: list[dict[str, Any]] = []
    if lacked and not points:
        gaps.append(_gap("inaccessible", "marking_withheld", f"{label} prices",
                         f"the price carries the {', '.join(lacked)} marking, which this "
                         "reader does not hold; whether the item has such prices is not "
                         "shown", f"{base}_direction"))
        return [], gaps, None
    if not points:
        return [], ([_gap("unknown", "dated_price_unknown", f"{label} prices",
                          f"{sum(left.values())} line(s) were reached but none has a "
                          "readable price and date (" + ", ".join(
                              f"{k} {v}" for k, v in sorted(left.items())) + ")",
                          f"{base}_direction")] if left else []), None
    by_day: dict[str, list[_Point]] = {}
    for point in points:
        by_day.setdefault(point.day, []).append(point)
    days = sorted(by_day)
    history = {"name": f"{base}_history"[:64], "kind": "computed", "value": len(points),
               "inputs": [p.entry["name"] for p in points][-50:],
               "basis": [p.line for p in points][-100:],
               "steps": [f"{len(points)} dated price(s) on {label} lines, {days[0]} to "
                         f"{days[-1]}, oldest first"[:MAX_STEP_CHARS]]
               + [p.said()[:MAX_STEP_CHARS] for p in points[-MAX_LISTED:]],
               "check": "not_applicable"}
    if len(points) > MAX_LISTED:
        history["steps"].insert(1, f"the {MAX_LISTED} latest are listed")
    entries = [history]
    direction_name = f"{base}_direction"[:64]
    why: tuple[str, str] | None = None

    def one(day: str) -> _Point | None:
        kinds = {(p.price, p.unit, p.currency) for p in by_day[day]}
        return by_day[day][-1] if len(kinds) == 1 else None
    last = one(days[-1])
    previous = one(days[-2]) if len(days) > 1 else None
    if last is not None:
        entries.append(_price_entry(f"{base}_last_price", last, "last"))
    if previous is not None:
        entries.append(_price_entry(f"{base}_previous_price", previous, "previous"))
    if not complete:
        why = ("price_history_truncated", "the plan's step budget or a read cap cut the run, "
               "so a later price may not have been read")
    elif left.get("header_unknown") or left.get("date_unknown"):
        why = ("price_history_incomplete", f"{left.get('header_unknown', 0) + left.get('date_unknown', 0)} "
               "priced line(s) have no readable header or date, so one may be later than "
               "those dated")
    elif last is None or (len(days) > 1 and previous is None):
        why = ("several_prices_one_day", "a day of the last two has two different prices "
               "(or units), so neither is the price of that day")
    elif len(days) < 2:
        why = ("single_dated_price", f"only {days[-1]} has a price, so there is nothing "
               "to compare it with")
    elif last.unit != previous.unit or last.currency != previous.currency:
        why = ("price_basis_differs", f"the last price is per {last.unit or 'an unknown unit'}"
               f" in {last.currency or 'an unknown currency'}, the previous per "
               f"{previous.unit or 'an unknown unit'} in "
               f"{previous.currency or 'an unknown currency'}: never compared or converted")
    direction: str | None = None
    if why is None:
        assert last is not None and previous is not None
        direction = ("up" if last.price > previous.price
                     else "down" if last.price < previous.price else "flat")
        change = ((last.price - previous.price) / previous.price * 100).quantize(
            CENT, rounding=ROUND_HALF_UP)
        steps = [f"last: {last.said()}", f"previous: {previous.said()}",
                 f"change: ({_plain(last.price)} - {_plain(previous.price)}) / "
                 f"{_plain(previous.price)} x 100 = {_plain(change)}%"]
        if previous.price and (last.price / previous.price > 10
                               or previous.price / last.price > 10):
            steps.append("the two prices differ more than tenfold: check that both are per "
                         "the same quantity before relying on the direction")
        inputs = [f"{base}_last_price"[:64], f"{base}_previous_price"[:64]]
        entries.append({"name": direction_name, "kind": "computed", "value": direction,
                        "inputs": inputs, "basis": [last.line, previous.line],
                        "steps": [s[:MAX_STEP_CHARS] for s in steps],
                        "check": "not_applicable"})
        entries.append({"name": f"{base}_change_percent"[:64], "kind": "computed",
                        "value": _plain(change), "value_type": "decimal", "inputs": inputs,
                        "basis": [last.line, previous.line], "steps": steps[2:3],
                        "check": "not_applicable"})
    else:
        reason, detail = why
        gaps.append(_gap("unknown", reason, f"{label} price trend", detail, direction_name))
        entries.append({"name": direction_name, "kind": "computed", "value": None,
                        "inputs": [], "steps": [f"no direction: {detail}"[:MAX_STEP_CHARS]],
                        "check": "not_applicable"})
    return entries, gaps, direction


def plan_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Resolver | None, actor: Any, complete: bool,
             covers: Callable[[str], bool],
             markings_lacked: Callable[[str], list[str]] | None = None) -> dict[str, Any]:
    """The item's price series and trends (module doc)."""
    headers: dict[tuple[str, str], set[str]] = {}
    for edge in edges:
        if isinstance(edge, Mapping) and isinstance(edge.get("from"), str) \
                and isinstance(edge.get("to"), str):
            headers.setdefault((str(edge.get("link_type")), edge["from"]), set()).add(
                edge["to"])
    entries: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    reasons: list[str] = []
    chosen: tuple[Mapping[str, Any], str] | None = None
    for spec in args["series"]:
        points, left = _points(spec, evidence, headers, resolver, actor)
        lacked = markings_lacked(spec["price"]) if markings_lacked is not None else []
        found, more, direction = _series(spec, name, points, left, complete, lacked)
        entries += found
        gaps += more
        if direction is not None and chosen is None:
            chosen = (spec, direction)
    if chosen is None:
        reasons.append("no_price_trend")
        if not entries and not gaps:
            gaps.append(_gap("unknown", "no_purchase_price_found", "purchase price history",
                             "no line this reader may read prices the item (posted "
                             "receipts, posted invoices or orders); this is not proof that "
                             "it was never bought", name))
        entries.append({"name": name, "kind": "computed", "value": None, "inputs": [],
                        "steps": ["no series has a direction this reader may be shown"],
                        "check": "not_applicable"})
    else:
        spec, direction = chosen
        base = f"{name}_{spec['id']}"
        entries.append({"name": name, "kind": "computed", "value": direction,
                        "inputs": [f"{base}_last_price"[:64], f"{base}_previous_price"[:64]],
                        "steps": [f"the trend of the {spec['label']} (the first series, in "
                                  "the plan's order, that has one): entry "
                                  f"{base}_direction; state it in words beside the two "
                                  "prices it compares"][:1],
                        "check": "not_applicable"})
    return {"entries": entries, "gaps": gaps, "reasons": reasons}


PLAN_ENGINES = {ENGINE: {"check": plan_check, "run": plan_run}}

__all__ = ["ENGINE", "PLAN_ENGINES", "plan_check", "plan_run"]
