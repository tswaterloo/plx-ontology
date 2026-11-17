"""Remove from the Postgres ontology the ETL copies a build no longer keeps (CC-NY-2).

The Portal ETL re-inserts rows of some relations under a fresh id on every run and the
old id leaves the source (``ontology.project.ETL_REINSERTED``). Until 11-14 the projection
kept, per natural key, one copy still in the source AND one removed copy, so every
re-inserted sales order line was served twice: measured on the host mirror on 11-14, 659
removed line copies whose natural key a present copy holds, a repeated line in 263 of the
702 Portal app orders (the audit's "44% of app orders carry duplicate lines"). The
projector now keeps the present copy and drops the removed ones
(``Projector._collapse_reinserted``), and a delta layer owns a dropped copy
(``ontology.delta.finish_store``), but generation 12 is the last full build: the copies
it already serves stay until they are removed in place. This is that removal.

``plan`` reads the mirror capture store read-only (``row_current``, one relation at a
time) and computes, with the projector's own natural key (``project._natural_key``) and
keep order, every removed copy whose natural key a present copy holds under the same
audience: (object id to drop, object id kept). ``apply`` then, in one transaction under the
delta applier's lock (``pg_advisory_xact_lock(hashtext('ontology_delta'))``):

* keeps only the pairs Postgres agrees with: the dropped object is of the binding and is
  missing from the source there, the kept object is served, present and of the same
  audience; every other pair is skipped and counted by reason (fail closed);
* copies every row it will delete into ``etl_copies_undo.<table>_<run>`` (the undo);
* deletes the dropped objects' rows dependents first (``ontology_delta._DELETE``'s order),
  every link with an end at one of them included (a whole-store step's too: no link may
  point at an object that no longer exists).

Without ``commit`` it rolls back and reports what it would delete. Run (applied later,
after review):
``python3 -m operating.pg.etl_copies --mirror /data/plx-state/case-runtime/source-mirror.sqlite``
then the same with ``--commit``.

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_RELATIONS = ("public.app_sales_order_line",)
UNDO_SCHEMA = "etl_copies_undo"


def bindings(relations: Iterable[str]) -> dict[str, list[tuple[str, str]]]:
    """relation -> (object type, binding id) of each Portal binding keyed on ``id`` over
    it; a relation the ETL does not re-insert is refused (``ValueError``)."""
    from ontology.project import ETL_REINSERTED
    from ontology.schema import load_schema
    known = ETL_REINSERTED.get("portal_api", {})
    wanted = list(relations)
    unknown = [relation for relation in wanted if relation not in known]
    if unknown:
        raise ValueError("not an ETL re-inserted relation: " + ", ".join(unknown))
    out: dict[str, list[tuple[str, str]]] = {relation: [] for relation in wanted}
    for type_id, spec in load_schema().object_types.items():
        for binding in spec.get("bindings") or ():
            if binding.get("source") == "portal_api" and binding.get("relation") in out \
                    and binding.get("key") == ["id"]:
                out[binding["relation"]].append((type_id, binding["id"]))
    return out


def plan(mirror: sqlite3.Connection, relations: Sequence[str] = DEFAULT_RELATIONS
         ) -> dict[str, Any]:
    """The removed copies to drop, per relation: ``pairs`` [(drop object id, kept object
    id, binding id)], and counts. Reads ``row_current`` only."""
    from ontology.project import ETL_REINSERTED, _natural_key, object_id_of
    minted_of = ETL_REINSERTED["portal_api"]
    out: dict[str, Any] = {"pairs": [], "relations": {}}
    for relation, bound in bindings(relations).items():
        minted = minted_of[relation]
        present: dict[str, tuple[str, str]] = {}     # natural -> (keep order, key id)
        removed: dict[str, list[str]] = {}
        runs = rows = 0
        for key_json, row_json, run_length, missing in mirror.execute(
                "SELECT key_json, row_json, run_length, missing_from_source_since "
                "FROM row_current WHERE relation = ?", (relation,)):
            rows += 1
            if run_length != 1:
                runs += 1          # a key with a run is not one record: never touched
                continue
            key = json.loads(key_json)
            row = json.loads(row_json)
            if not isinstance(key, Mapping) or set(key) != {"id"}:
                runs += 1
                continue
            natural, rank = _natural_key(row, minted, key_json)
            if natural.startswith("record:"):
                continue           # not comparable: never collapsed by the projector
            if missing is None:
                held = present.get(natural)
                mine = (rank, str(key["id"]))
                if held is None or mine < held:
                    present[natural] = mine
            else:
                removed.setdefault(natural, []).append(str(key["id"]))
        pairs = 0
        for natural, ids in removed.items():
            kept = present.get(natural)
            if kept is None:
                continue           # the row left the source: one removed object stays
            for type_id, binding_id in bound:
                keep = object_id_of(type_id, binding_id, {"id": kept[1]})
                for drop in ids:
                    out["pairs"].append((object_id_of(type_id, binding_id, {"id": drop}),
                                         keep, binding_id))
                    pairs += 1
        out["relations"][relation] = {
            "rows": rows, "skipped_not_one_record": runs, "present_records": len(present),
            "removed_records": len(removed), "removed_copies_of_present": pairs,
            "bindings": [binding_id for _type, binding_id in bound]}
    return out


def apply(dsn: str, pairs: Sequence[tuple[str, str, str]], *, commit: bool = False,
          run: str | None = None) -> dict[str, Any]:
    """Delete the dropped copies of ``pairs`` that Postgres agrees with (module
    docstring); with ``commit`` False, roll back and report only."""
    import psycopg
    from operating.pg.ontology_delta import _DELETE, _OWNED_PG
    run = run or time.strftime("%Y%m%d%H%M%S", time.gmtime())
    if not run.isalnum():
        raise ValueError("run must be letters and digits")
    report: dict[str, Any] = {"run": run, "pairs": len(pairs), "commit": commit}
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))")
        cur.execute("CREATE TEMP TABLE pairs (drop_id text NOT NULL, keep_id text NOT NULL, "
                    "binding_id text NOT NULL, PRIMARY KEY (drop_id, keep_id, binding_id)) "
                    "ON COMMIT DROP")
        with cur.copy("COPY pairs (drop_id, keep_id, binding_id) FROM STDIN") as copy:
            for drop, keep, binding in dict.fromkeys(pairs):
                copy.write_row((drop, keep, binding))
        cur.execute("ANALYZE pairs")
        cur.execute(
            """SELECT CASE
                 WHEN d.object_id IS NULL THEN 'drop_not_served'
                 WHEN d.binding_id <> p.binding_id THEN 'drop_other_binding'
                 WHEN d.missing_from_source_since IS NULL AND NOT EXISTS (
                     SELECT 1 FROM ontology.object_removals r
                     WHERE r.object_id = d.object_id AND r.until IS NULL)
                   THEN 'drop_present_in_postgres'
                 WHEN k.object_id IS NULL THEN 'keep_not_served'
                 WHEN k.missing_from_source_since IS NOT NULL THEN 'keep_missing_from_source'
                 WHEN k.audience_key <> d.audience_key THEN 'keep_other_audience'
                 ELSE 'drop' END AS verdict, p.drop_id
               FROM pairs p
               LEFT JOIN ontology.objects d ON d.object_id = p.drop_id
               LEFT JOIN ontology.objects k ON k.object_id = p.keep_id""")
        verdicts: dict[str, int] = {}
        agreed, refused = set(), set()
        for verdict, drop in cur.fetchall():
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            (agreed if verdict == "drop" else refused).add(drop)
        # a copy any of its pairs refuses is never dropped
        owned = sorted(agreed - refused)
        report["dropped_objects"] = len(owned)
        report["verdicts"] = dict(sorted(verdicts.items()))
        cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) ON COMMIT DROP")
        with cur.copy("COPY owned (object_id) FROM STDIN") as copy:
            for oid in owned:
                copy.write_row((oid,))
        cur.execute("ANALYZE owned")
        # every link with an end at a dropped object, whoever wrote it
        deletes = [(table, where) for table, where in _DELETE if table != "link_rows"]
        at = deletes.index(next(d for d in deletes if d[0] == "value_cells"))
        deletes[at:at] = [("link_rows", f"from_object IN {_OWNED_PG}"),
                          ("link_rows", f"to_object IN {_OWNED_PG}")]
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {UNDO_SCHEMA}")
        undone: set[str] = set()
        deleted: dict[str, int] = {}
        for table, where in deletes:
            undo = f"{UNDO_SCHEMA}.{table}_{run}"
            if table in undone:
                cur.execute(f"INSERT INTO {undo} SELECT * FROM ontology.{table} WHERE {where}")
            else:
                cur.execute(f"CREATE TABLE {undo} AS SELECT * FROM ontology.{table} "
                            f"WHERE {where}")
                undone.add(table)
            cur.execute(f"DELETE FROM ontology.{table} WHERE {where}")
            deleted[table] = deleted.get(table, 0) + cur.rowcount
        report["deleted"] = deleted
        report["undo_schema"] = UNDO_SCHEMA if commit else None
        if commit:
            pg.commit()
        else:
            pg.rollback()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.etl_copies")
    parser.add_argument("--mirror", required=True, help="the mirror capture store (read only)")
    parser.add_argument("--relation", action="append", default=None,
                        help=f"an ETL re-inserted relation (default {DEFAULT_RELATIONS[0]})")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--plan-only", action="store_true", help="read the mirror only")
    parser.add_argument("--commit", action="store_true",
                        help="delete (default: report what would be deleted, roll back)")
    args = parser.parse_args(argv)
    mirror = sqlite3.connect(f"file:{args.mirror}?mode=ro", uri=True, timeout=30)
    try:
        planned = plan(mirror, tuple(args.relation or DEFAULT_RELATIONS))
    finally:
        mirror.close()
    out: dict[str, Any] = {"plan": planned["relations"]}
    if not args.plan_only:
        from operating.pg import config
        out["apply"] = apply(args.dsn or config.dsn(), planned["pairs"], commit=args.commit)
    print(json.dumps(out, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
