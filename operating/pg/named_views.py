"""Every view that the measure registry or the Ask catalog names must exist (speed specs
P2/P6, owner 11-23: the readings landing dropped ``biz_sql.comms_findings`` and nothing said
so until it was made again by hand).

Two lists, each read from where it is defined (no copy here):

* the measure registry (``operating.measures``): each measure's ``view`` is read by the
  measure builder in the business schema (``biz``; not a view derived from the guarded
  comms views, which exists only in the guarded schema) and, as a statement through the
  door, in the guarded schema (``biz_sql``);
* the Ask catalog (``operating.ontology_tools_sql_catalog.view_names``): every view the model
  is told it may query, read in the guarded schema.

``check`` returns what is missing, per schema and per name, with who names it. The refresh
runs it after its last step (the result is in its output and on stderr; it never fails the
refresh) and ``alarm_checks`` (the ops alarm, ``view_state.alarm_checks``) runs it on every
ops cycle: ``views_named_exist`` breaches while any named view is missing, and is
``unknown`` (said) while the guarded schema itself is not installed (migration 0042).
"""
from __future__ import annotations

import json
import sys
from typing import Any

CHECK = "views_named_exist"


def named(schema: str = "biz") -> dict[str, dict[str, list[str]]]:
    """``{schema: {view: [who names it]}}`` for the business schema and the guarded one."""
    from operating import measures
    from operating import ontology_tools_sql_catalog as catalog
    from operating.pg import sql_views
    guarded = sql_views.guarded_schema(schema)
    out: dict[str, dict[str, list[str]]] = {schema: {}, guarded: {}}
    for mid, m in sorted(measures.measures().items()):
        # a measure over records (``source``) names the door's records and links, and the
        # business views its link columns read; the view the entry's "view" label shows is no relation
        named = ["records", "record_links"] + [h["to"] for h in m["source"]["columns"].values()
                                                if "link" in h] if m.get("source") else [m["view"]]
        for view in named:
            for s in (schema, guarded):
                if s == schema and view in catalog.COMMS_DERIVED:
                    continue   # a view over the guarded comms views: it lives only there
                out[s].setdefault(view, []).append(f"measure {mid}")
    for name in catalog.view_names():
        out[guarded].setdefault(name, []).append("Ask catalog")
    return out


def check(conn: Any, schema: str = "biz") -> dict[str, Any]:
    """``{"missing": {schema: {view: [who]}}, "named": n, "installed": bool}``; ``installed``
    False when the guarded schema does not exist (nothing of it can be checked: said)."""
    from operating.pg import sql_views
    guarded = sql_views.guarded_schema(schema)
    want = named(schema)
    installed = conn.execute("SELECT to_regnamespace(%s)", (guarded,)).fetchone()[0] is not None
    missing: dict[str, dict[str, list[str]]] = {}
    for s, views in want.items():
        if s == guarded and not installed:
            continue
        rows = conn.execute("SELECT name FROM unnest(%s::text[]) name WHERE "
                            "to_regclass(%s || '.' || name) IS NULL",
                            (sorted(views), s)).fetchall()
        gone = {r[0]: views[r[0]] for r in rows}
        if gone:
            missing[s] = gone
    return {"missing": missing, "installed": installed,
            "named": sum(len(v) for v in want.values())}


def said(result: dict[str, Any]) -> str:
    """The missing views in one line (``schema.view`` and who names it)."""
    return "; ".join(f"{s}.{v} ({', '.join(who[:2])}{'...' if len(who) > 2 else ''})"
                     for s, views in sorted(result["missing"].items())
                     for v, who in sorted(views.items()))


def alarm_checks(conn: Any, schema: str = "biz") -> list[dict[str, Any]]:
    """The one ops check (``views_named_exist``); a failure to read is an ``unknown`` that
    says so, never an ``ok``."""
    from operating.ops_health import _check
    try:
        got = check(conn, schema)
    except Exception as exc:  # noqa: BLE001 - said as unknown with the error type
        return [_check(CHECK, None, 0, None, "views", f"not checked: {type(exc).__name__}")]
    count = sum(len(v) for v in got["missing"].values())
    if count:
        from operating.pg.view_state import _said
        return [_check(CHECK, count, 0, True, "views", _said("missing: " + said(got)))]
    if not got["installed"]:   # the guarded names could not be checked: unknown, said
        return [_check(CHECK, None, 0, None, "views",
                       "guarded schema not installed (migration 0042): its names not checked")]
    return [_check(CHECK, 0, 0, False, "views", f"{got['named']} names, all present")]


def report(conn: Any, schema: str = "biz") -> dict[str, Any] | None:
    """For the refresh: the missing views (also on stderr), or None when none is missing."""
    got = check(conn, schema)
    if not got["missing"]:
        return None
    print(json.dumps({"views_named_missing": got["missing"]}), file=sys.stderr, flush=True)
    return got["missing"]
