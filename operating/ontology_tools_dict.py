"""One-call record-type dictionary read (Ask spec 20 G9 / S13, A6): for one record type
(``plx.sql {"describe": "<Type>"}``) or every captured record type in one read
(``plx.sql {"describe": "dictionary"}``), returns the type's declared date key, amount
key(s), party key(s) and live links with counts as this reader may read them.

Derived strictly from the ontology catalog (``record_types``) and the reader's own
guarded ``records`` / ``record_links`` views (no keyword lists, no shape guessing)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping, Sequence

from operating import result_profile
from operating.entity_resolution import PARTY_TYPES
from operating.ontology_tools_identity import ToolError
from operating.pg import generic_views as gv

_LOG = logging.getLogger(__name__)

COLUMNS = ["type", "name", "systems", "tables", "records", "date_key", "date_role",
           "amount_key", "party_key", "links_with_counts"]
VIEWS = ["record_types", "records", "record_links"]


def _json_val(value: Any, *, field: str = "", type_name: str = "") -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            _LOG.warning("malformed catalog JSON for %s.%s: %s",
                         type_name or "?", field or "?", exc)
            raise
    return value


def _is_party_type(type_name: Any) -> bool:
    if not isinstance(type_name, str) or not type_name:
        return False
    return type_name.strip().lower() in PARTY_TYPES


def _declared_link_types(entry: Mapping[str, Any]) -> list[str]:
    """Distinct declared ``link_type`` names touching this catalog row."""
    tname = str(entry.get("type") or "")
    names: list[str] = []
    seen: set[str] = set()
    for key in ("links_out", "links_in"):
        raw = _json_val(entry.get(key), field=key, type_name=tname) or []
        if isinstance(raw, list):
            for lk in raw:
                lt = lk.get("link_type") if isinstance(lk, Mapping) else None
                if isinstance(lt, str) and lt and lt not in seen:
                    seen.add(lt)
                    names.append(lt)
    for lt in entry.get("identity_links") or ():
        if isinstance(lt, str) and lt and lt not in seen:
            seen.add(lt)
            names.append(lt)
    return names


def type_keys(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Extract declared date, amount and party keys from one ``record_types`` row."""
    tname = str(entry.get("type") or "")
    fields = _json_val(entry.get("fields"), field="fields", type_name=tname) or []
    if not isinstance(fields, list):
        fields = []
    field_names = {str(f.get("name")) for f in fields if isinstance(f, Mapping) and f.get("name")}
    dates_meta = _json_val(entry.get("dates"), field="dates", type_name=tname) or []
    if not isinstance(dates_meta, list):
        dates_meta = []
    date_fields = list(entry.get("date_fields") or ())

    # Date key: prefer the first declared record-role date from record_types.dates,
    # falling back to date_fields[0] or the first schema date-kind field.
    date_key: str | None = None
    date_role: str | None = None
    date_keys: list[dict[str, Any]] = []
    for d in dates_meta:
        if not isinstance(d, Mapping) or not d.get("field"):
            continue
        item = {"field": str(d["field"]), "role": d.get("role"), "means": d.get("means"),
                "dated_share": d.get("dated_share")}
        date_keys.append(item)
        if date_key is None and d.get("role") == "record":
            date_key, date_role = item["field"], "record"
    if date_key is None and date_keys:
        date_key, date_role = date_keys[0]["field"], date_keys[0].get("role")
    if date_key is None and date_fields:
        date_key = str(date_fields[0])
    seen_dates = {d["field"] for d in date_keys}
    for f in fields:
        if isinstance(f, Mapping) and f.get("kind") in gv.DATE_KINDS:
            fn = str(f.get("name") or "")
            if fn and fn not in seen_dates:
                seen_dates.add(fn)
                date_keys.append({"field": fn, "role": None, "means": f.get("description")})
                if date_key is None:
                    date_key = fn

    # Amount keys: declared money fields first, then other declared numeric/quantity fields.
    money_keys: list[dict[str, Any]] = []
    other_num_keys: list[dict[str, Any]] = []
    for f in fields:
        if not isinstance(f, Mapping):
            continue
        kind = f.get("kind")
        fn = str(f.get("name") or "")
        if not fn or kind not in gv.NUMERIC_KINDS:
            continue
        info: dict[str, Any] = {"field": fn, "kind": kind}
        if f"{fn}_currency" in field_names:
            info["currency_field"] = f"{fn}_currency"
        if f"{fn}_unit" in field_names:
            info["unit_field"] = f"{fn}_unit"
        if kind == "money":
            money_keys.append(info)
        else:
            other_num_keys.append(info)
    amount_keys = [*money_keys, *other_num_keys]
    amount_key = amount_keys[0]["field"] if amount_keys else None

    # Party keys: when this type is itself a party type (PARTY_TYPES), lead with its own
    # label_fields, then declared links whose other end is a party type.
    party_keys: list[dict[str, Any]] = []
    label_fields = list(entry.get("label_fields") or ())
    if _is_party_type(tname):
        for lf in label_fields:
            party_keys.append({"via": "self_label", "field": str(lf), "party_type": tname})
    links_out = _json_val(entry.get("links_out"), field="links_out", type_name=tname) or []
    links_in = _json_val(entry.get("links_in"), field="links_in", type_name=tname) or []
    for lk in links_out if isinstance(links_out, list) else ():
        if isinstance(lk, Mapping) and _is_party_type(lk.get("to")):
            party_keys.append({"via": "link_out", "link_type": lk.get("link_type"),
                               "party_type": lk.get("to")})
    for lk in links_in if isinstance(links_in, list) else ():
        if isinstance(lk, Mapping) and _is_party_type(lk.get("from")):
            party_keys.append({"via": "link_in", "link_type": lk.get("link_type"),
                               "party_type": lk.get("from")})
    party_key: str | None = None
    if party_keys:
        first = party_keys[0]
        if first.get("via") == "link_in" and first.get("link_type"):
            party_key = f"{first['link_type']} <- {first['party_type']}"
        elif first.get("link_type"):
            party_key = f"{first['link_type']} -> {first['party_type']}"
        else:
            party_key = str(first.get("field") or tname)

    return {
        "type": tname,
        "name": entry.get("name"),
        "systems": list(entry.get("systems") or ()),
        "tables": list(entry.get("tables") or ()),
        "label_keys": label_fields,
        "date_key": date_key,
        "date_role": date_role,
        "date_keys": date_keys,
        "amount_key": amount_key,
        "amount_keys": amount_keys,
        "party_key": party_key,
        "party_keys": party_keys,
    }


def _link_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (-int(item["count"]), str(item["direction"]), str(item["link_type"] or ""),
            str(item["other_type"] or ""), str(item["tier"] or ""))


def link_counts_for_type(pg: Any, type_name: str,
                         link_types: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Every link type touching ``type_name`` in this reader's guarded ``record_links``,
    with direction, far type, tier and exact count.

    Filters on ``link_type = ANY(%s)`` when the catalog declares link types for
    ``type_name`` so Postgres uses the ``(link_type)`` btree index in a single pass
    instead of scanning ``record_links`` twice."""
    if pg.execute("SELECT to_regclass('record_links')").fetchone()[0] is None:
        return []
    declared = [str(lt) for lt in (link_types or ()) if lt]
    if declared:
        rows = pg.execute(
            "SELECT from_type, link_type, to_type, tier, count(*) AS n "
            "FROM record_links "
            "WHERE link_type = ANY(%s) AND (from_type = %s OR to_type = %s) "
            "GROUP BY 1, 2, 3, 4",
            (declared, type_name, type_name),
        ).fetchall()
    else:
        rows = pg.execute(
            "SELECT from_type, link_type, to_type, tier, count(*) AS n "
            "FROM record_links "
            "WHERE from_type = %s OR to_type = %s "
            "GROUP BY 1, 2, 3, 4",
            (type_name, type_name),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for ft, lt, tt, tier, n in rows:
        cnt = int(n)
        if ft == type_name:
            out.append({"direction": "out", "link_type": lt, "other_type": tt,
                        "tier": tier, "count": cnt})
        if tt == type_name:
            out.append({"direction": "in", "link_type": lt, "other_type": ft,
                        "tier": tier, "count": cnt})
    out.sort(key=_link_sort_key)
    return out


def for_type(pg: Any, entry: Mapping[str, Any], total: int) -> dict[str, Any]:
    """One-call dictionary summary for a single record type."""
    tname = str(entry.get("type") or "")
    return {
        **type_keys(entry),
        "records": int(total),
        "links_with_counts": link_counts_for_type(pg, tname,
                                                  link_types=_declared_link_types(entry)),
    }


def all_types(tool: Any, conn: Any, scope: Any, actor: Any) -> dict[str, Any]:
    """``plx.sql {"describe": "dictionary"}``: every captured record type's date key,
    amount key, party key, record count and links with counts in ONE scoped read, stored
    in ``RESULTS`` as a standard pageable ``sql://`` result."""
    from operating import ontology_tools_sql as sql_mod
    started = time.monotonic()
    door = sql_mod.Door(sql_mod.reader_grant(scope, full=tool._full(actor)))

    def work(pg: Any) -> tuple[list[dict[str, Any]], dict[str, int],
                               dict[str, list[dict[str, Any]]], str | None, dict[str, Any]]:
        refused = [v for v in VIEWS if v in door.decided["refused"]]
        if refused:
            raise ToolError("not_ready", f"{', '.join(refused)} cannot be read here yet",
                            "use plx.search")
        if pg.execute("SELECT to_regclass('record_types')").fetchone()[0] is None:
            raise ToolError("not_ready", "record_types is not built yet")
        sql_mod._costly_slot(pg)
        cur = pg.execute("SELECT * FROM record_types ORDER BY type")
        cols = [d.name for d in cur.description]
        entries = [dict(zip(cols, r)) for r in cur.fetchall()]
        counts: dict[str, int] = {}
        if pg.execute("SELECT to_regclass('records')").fetchone()[0] is not None:
            for t, n in pg.execute("SELECT type, count(*) FROM records GROUP BY 1").fetchall():
                counts[str(t)] = int(n)
        links_by: dict[str, list[dict[str, Any]]] = {}
        if pg.execute("SELECT to_regclass('record_links')").fetchone()[0] is not None:
            lrows = pg.execute(
                "SELECT from_type, link_type, to_type, tier, count(*) AS n "
                "FROM record_links GROUP BY 1, 2, 3, 4"
            ).fetchall()
            for ft, lt, tt, tier, n in lrows:
                cnt = int(n)
                links_by.setdefault(str(ft), []).append(
                    {"direction": "out", "link_type": lt, "other_type": tt,
                     "tier": tier, "count": cnt})
                links_by.setdefault(str(tt), []).append(
                    {"direction": "in", "link_type": lt, "other_type": ft,
                     "tier": tier, "count": cnt})
            for items in links_by.values():
                items.sort(key=_link_sort_key)
        built = sql_mod._built(pg, VIEWS)
        read_at = sql_mod.data_as_of(pg)
        return entries, counts, links_by, built, read_at

    while True:
        try:
            entries, counts, links_by, built, read_at = tool._scoped(conn, door, work)
            break
        except sql_mod._NoSlot:
            time.sleep(sql_mod.FULL_COSTLY_POLL_SECONDS)

    rows: list[list[Any]] = []
    for entry in entries:
        info = type_keys(entry)
        tname = info["type"]
        rows.append([
            tname,
            info["name"],
            info["systems"],
            info["tables"],
            counts.get(tname, 0),
            info["date_key"],
            info["date_role"],
            info["amount_key"],
            info["party_key"],
            json.dumps(links_by.get(tname, []), ensure_ascii=False, separators=(",", ":")),
        ])
    cats = [result_profile.OTHER] * len(COLUMNS)
    cats[4] = result_profile.NUMBER
    profile = result_profile.from_rows(COLUMNS, rows, cats, [(None, None)] * len(COLUMNS))
    got = {
        "cols": COLUMNS,
        "rows": rows,
        "stopped": False,
        "built": built,
        "profile": profile,
        "sources": [],
        "views": VIEWS,
        "read_at": read_at,
        "withheld": {},
    }
    out = tool._present("describe dictionary", got, actor, started)
    out["scope"] = (f"dictionary: {len(rows)} record types with date_key, amount_key, "
                    f"party_key and links_with_counts as this reader may read them; "
                    f"{len(out.get('rows') or ())} of {len(rows)} rows shown")
    return out
