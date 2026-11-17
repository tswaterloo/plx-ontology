#!/usr/bin/env python3
"""Which layers each source system reaches in the served store (CAP-L220).

USAGE
    python3 tools/system_layers.py [--dsn DSN] [--inbox GAP_INBOX] [--system NAME]...
                                   [--sample N] [--json]

A system's records exist once projected; they are useful only when the other layers reach
them too. Per source system (the family of each binding: ``bc.accounts`` -> ``bc``,
``gen.teams.chat_messages`` -> ``teams``) and per (binding, object type) with current
records, a seeded sample of current records (``--sample``, by md5 of the object id, so a
rerun reads the same records) is checked for:

- firm: at least one current firm link either way (a tier before ``lead`` in
  ``ontology.store.TIER_ORDER``);
- lead: at least one current ``lead`` link either way (mentions waiting to be firmed);
- search: at least one ``search.chunks`` row of its own (its text is searchable);
- readable: an audience key (a record without one opens for no reader, so no page);
- alerts: gaps in the gap inbox whose subject or a citation is a record of the binding
  (open and resolved), over all its records, not the sample. Judged per system: most
  types hold no alert by nature, a system none of whose records ever had one is the gap.

A current record is one its source holds: not missing from it and no removal in force
(``ontology.object_removals``, as ``ontology.store.removed_ids_sql`` at latest knowledge).
A link probe reads at most ``PROBE_CAP`` links per end of a record (a hub holds millions);
a record whose probe found nothing within a full cap is undecided, never "no link".
A record layer no sampled record of a type reaches, none undecided, is a gap of that
type. A layer that cannot be read
(no ``search.chunks``, no inbox given or readable) or decided (only undecided records) is
``null``: unknown, never 0, and the check fails on it as on a gap. Read only (a read-only session with a statement timeout;
the inbox opened ``mode=ro``); counts only, never a value. Exit 0 when no gap and nothing
unknown, 1 otherwise, 2 on a bad argument (an unknown ``--system`` included).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ontology.store import TIER_ORDER  # noqa: E402

FIRM_TIERS = TIER_ORDER[:TIER_ORDER.index("lead")]
RECORD_LAYERS = ("firm", "lead", "search", "readable")
SAMPLE = 200
PROBE_CAP = 20000
STATEMENT_TIMEOUT = "300s"
# A record its source holds at latest knowledge (``ontology.store.removed_ids_sql``: a
# removal with an end time that cannot be read stays in force).
CURRENT = ("o.missing_from_source_since IS NULL AND NOT EXISTS (SELECT 1 FROM "
           "ontology.object_removals rm WHERE rm.object_id = o.object_id AND (rm.until IS NULL "
           "OR rm.until !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'))")


def system_of(binding_id: str) -> str:
    """The source system of a binding: its first segment, the second under ``gen.``."""
    parts = binding_id.split(".")
    return parts[1] if parts[0] == "gen" and len(parts) > 2 else parts[0]


def binding_of(object_id: str | None) -> str | None:
    """The binding of an object id (``<Type>:<binding>:<key>``); None when not of that form."""
    parts = (object_id or "").split(":", 2)
    return parts[1] if len(parts) == 3 and parts[1] else None


def bindings(conn: Any) -> list[tuple[str, str, int]]:
    """[(binding, object type, current records)] of every binding with a current record."""
    return [(b, t, int(n)) for b, t, n in conn.execute(
        "SELECT o.binding_id, o.object_type, count(*) FROM ontology.objects o "
        f"WHERE {CURRENT} GROUP BY 1, 2 ORDER BY 1, 2").fetchall()]


def _exists(conn: Any, ids: Sequence[str], where: str, params: Sequence[Any]
            ) -> int | None:
    """How many of ``ids`` have a current link (either end) matching ``where`` (on ``x``),
    each end read up to ``PROBE_CAP`` links. None when none has one but some record's
    probe stopped at the cap (undecided, never a gap)."""
    def capped(end: str, cols: str) -> str:
        return (f"(SELECT {cols} FROM ontology.link_rows l WHERE l.{end} = i "
                f"LIMIT {PROBE_CAP})")
    found = " OR ".join(f"EXISTS (SELECT 1 FROM {capped(end, 'l.tier, l.superseded_at')} x "
                        f"WHERE x.superseded_at IS NULL AND {where})"
                        for end in ("from_object", "to_object"))
    full = " OR ".join(f"(SELECT count(*) FROM {capped(end, '1')} y) >= {PROBE_CAP}"
                       for end in ("from_object", "to_object"))
    hit, undecided = conn.execute(
        f"SELECT count(*) FILTER (WHERE f), count(*) FILTER (WHERE NOT f AND ({full})) "
        f"FROM (SELECT i, ({found}) AS f FROM unnest(%s::text[]) AS i OFFSET 0) q",
        (*params, *params, list(ids))).fetchone()
    return None if not hit and undecided else int(hit)


def search_readable(conn: Any) -> bool:
    return conn.execute("SELECT to_regclass('search.chunks') IS NOT NULL").fetchone()[0]


def sample(conn: Any, binding: str, object_type: str, n: int, searchable: bool
           ) -> dict[str, Any]:
    """The sampled shares of one (binding, type): sampled count and records reaching each
    link/search/readable layer (search None when the index cannot be read)."""
    rows = conn.execute(
        "SELECT o.object_id, coalesce(o.audience_key, '') <> '' FROM ontology.objects o "
        f"WHERE o.binding_id = %s AND o.object_type = %s AND {CURRENT} "
        "ORDER BY md5(o.object_id) LIMIT %s", (binding, object_type, n)).fetchall()
    ids = [r[0] for r in rows]
    out: dict[str, Any] = {"sampled": len(ids),
                           "readable": sum(1 for r in rows if r[1]),
                           "firm": _exists(conn, ids, "x.tier = ANY(%s)", (list(FIRM_TIERS),)),
                           "lead": _exists(conn, ids, "x.tier = %s", ("lead",)),
                           "search": None}
    if searchable:
        out["search"] = int(conn.execute(
            "SELECT count(*) FROM unnest(%s::text[]) AS i WHERE EXISTS "
            "(SELECT 1 FROM search.chunks c WHERE c.object_id = i)", (ids,)).fetchone()[0])
    return out


def alerts(inbox: str | Path | None) -> dict[str, dict[str, int]] | None:
    """{binding: {"open", "resolved"}}: distinct gaps whose subject or a citation is a
    record of the binding. None when no inbox is given or it cannot be read."""
    if not inbox or not Path(inbox).is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(inbox).resolve()}?mode=ro", uri=True, timeout=30)
        try:
            rows = conn.execute(
                "SELECT g.gap_id, g.state, g.object_id FROM gaps g UNION "
                "SELECT g.gap_id, g.state, c.object_id FROM gaps g "
                "JOIN gap_cites c ON c.gap_id = g.gap_id WHERE c.object_id IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    seen: dict[str, dict[str, set[str]]] = {}
    for gap_id, state, object_id in rows:
        binding = binding_of(object_id)
        if binding is not None:
            seen.setdefault(binding, {}).setdefault(state, set()).add(gap_id)
    return {b: {s: len(seen[b].get(s, ())) for s in ("open", "resolved")} for b in seen}


def check(conn: Any, *, inbox: str | Path | None = None, systems: Iterable[str] = (),
          n: int = SAMPLE) -> dict[str, Any]:
    """The report: per system, its (binding, type) rows with their layer counts, and the
    gaps (a layer no sampled record reaches) and unknowns (a layer that could not be read).
    An asked system with no current record is a ValueError (unknown, never an empty pass)."""
    if n < 1:
        raise ValueError("sample must be at least 1")
    asked = set(systems)
    rows = [r for r in bindings(conn) if not asked or system_of(r[0]) in asked]
    missing = asked - {system_of(b) for b, _, _ in rows}
    if missing:
        raise ValueError(f"no current records for system(s): {', '.join(sorted(missing))}")
    searchable = search_readable(conn)
    gap_counts = alerts(inbox)
    report: dict[str, Any] = {"sample": n, "firm_tiers": list(FIRM_TIERS), "systems": {},
                              "gaps": [], "unknown": []}
    for binding, object_type, current in rows:
        row: dict[str, Any] = {"binding": binding, "type": object_type, "current": current,
                               **sample(conn, binding, object_type, n, searchable)}
        row["alerts"] = None if gap_counts is None else gap_counts.get(
            binding, {"open": 0, "resolved": 0})
        report["systems"].setdefault(system_of(binding), []).append(row)
        for layer in RECORD_LAYERS:
            where = {"system": system_of(binding), "binding": binding, "type": object_type,
                     "layer": layer}
            if row[layer] is None:
                report["unknown"].append(where)
            elif row[layer] == 0:
                report["gaps"].append(where)
    for system in sorted(report["systems"]):
        where = {"system": system, "binding": None, "type": None, "layer": "alerts"}
        if gap_counts is None:
            report["unknown"].append(where)
        elif not any(sum(r["alerts"].values()) for r in report["systems"][system]):
            report["gaps"].append(where)
    return report


def _pct(part: int | None, whole: int) -> str:
    return "?" if part is None else f"{100 * part / whole:.0f}%" if whole else "-"


def text(report: Mapping[str, Any]) -> str:
    lines = [f"sample {report['sample']} per (binding, type); firm = "
             f"{', '.join(report['firm_tiers'])}; ? = layer unreadable"]
    for system, rows in sorted(report["systems"].items()):
        lines.append(f"\n## {system}")
        lines.append("binding | type | current | firm | lead | search | readable | alerts open/resolved")
        for r in rows:
            a = r["alerts"]
            lines.append(" | ".join([
                r["binding"], r["type"], str(r["current"]),
                *(_pct(r[k], r["sampled"]) for k in ("firm", "lead", "search", "readable")),
                "?" if a is None else f"{a['open']}/{a['resolved']}"]))
    lines.append(f"\ngaps {len(report['gaps'])}, unknown {len(report['unknown'])}")
    return "\n".join(lines)


def connect(dsn: str) -> Any:
    import psycopg
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    conn.execute("SET default_transaction_read_only = on")
    conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
    return conn


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg.config import dsn as default_dsn
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dsn", default=None, help="served store (default PLX_PG_DSN or local plx)")
    p.add_argument("--inbox", default=None, help="gap inbox sqlite (alerts layer)")
    p.add_argument("--system", action="append", default=[], help="only this system")
    p.add_argument("--sample", type=int, default=SAMPLE)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    with connect(args.dsn or default_dsn()) as conn:
        try:
            report = check(conn, inbox=args.inbox, systems=args.system, n=args.sample)
        except ValueError as exc:
            print(f"system_layers: {exc}", file=sys.stderr)
            return 2
    print(json.dumps(report, indent=1, sort_keys=True) if args.json else text(report))
    return 1 if report["gaps"] or report["unknown"] else 0


if __name__ == "__main__":
    sys.exit(main())
