"""What each system created, changed and removed per day (Ask 14.1 h10, plan D9): the tables
``biz.daily_changes_objects`` and ``biz.daily_changes`` (migration 0070) and the guarded view
``biz_sql.daily_changes``.

Two steps, both set-based, every run:

1. ``advance``: the object versions after the watermark (``biz.daily_changes_mark.ver``) are
   counted per (day, object) into ``daily_changes_objects`` in chunks of ``CHUNK`` versions
   (about 20 s each on 1M, measured 11-23 on the train copy), each chunk's counts and the
   watermark in one transaction, so a killed run repeats nothing and loses nothing. A version
   is a *creation* when the object has no earlier version, else a *change*; its day is the
   company's calendar day (``America/New_York``) of ``recorded_at``. ``removed`` is recomputed
   whole from ``ontology.object_removals``. No permission is kept at this step.
2. ``regroup``: the per-object counts are grouped into the served table by what each object IS
   NOW: its system (the first part of its binding: ``gen.sage.ar_pjd`` -> ``sage``), type,
   audience key, the markings of its current version, and whether it is in the people
   exclusion's set, and each group is given its access class (``access_classes``, exactly as a
   business view row; an excluded person's record adds the owner key). Only the rows that
   differ from the served table are deleted and inserted, in one transaction. So a record
   whose audience narrowed, whose marking tightened or whose owner was added to the exclusion
   set moves to its new class at the next run, and its counts leave the readers of the old
   class. A version whose object row is missing is counted as system ``unknown`` under the
   owner-only class and reported (``orphans``), never dropped.

The guarded view shows a reader the groups of the classes the reader holds
(``door.view_ddl``): there is no audience rule of this view's own. A run goes to the head; only
a hung statement ends it (``statement_timeout``).

``python3 -m operating.pg.daily_changes run``; started by ``plx-daily-changes.service`` after
each business views refresh."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from types import SimpleNamespace
from typing import Any, Sequence

TABLE = "daily_changes"
OBJECTS = "daily_changes_objects"
MARK = "daily_changes_mark"
CHUNK = 200_000
REGROUP_CHUNK = 100_000
ZONE = "America/New_York"
DOC = ("What each system created, changed and removed per day, by kind of record (one row per "
       "day, system, object type and kind of access: SUM the counts over the rows you read to "
       "get a day's total). A reader sees the changes of the records the reader may read. "
       "created = records first recorded that day, changed = a new version of a record "
       "recorded before, removed = records that left their source that day. Day = the "
       "company's calendar day.")
COLS = (("day", "date", "the company's calendar day (America/New_York)"),
        ("system", "text", "the source system of the records (sage, portal, bc, graph_mail, "
         "teams, sharepoint, mission_control, meetings; unknown: an object the store no longer "
         "holds)"),
        ("object_type", "text", "the kind of record (its type in the ontology)"),
        ("created", "integer", "records first recorded that day"),
        ("changed", "integer", "records that got a new version that day (not their first)"),
        ("removed", "integer", "records that left their source that day"))

_ADVANCE = f"""
INSERT INTO biz.{OBJECTS} (day, object_id, created, changed)
SELECT (v.recorded_at::timestamptz AT TIME ZONE '{ZONE}')::date, v.object_id,
       count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM ontology.object_versions p
                                          WHERE p.object_id = v.object_id AND p.ver < v.ver)),
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ontology.object_versions p
                                      WHERE p.object_id = v.object_id AND p.ver < v.ver))
FROM ontology.object_versions v WHERE v.ver > %(lo)s AND v.ver <= %(hi)s GROUP BY 1, 2
ON CONFLICT (day, object_id) DO UPDATE SET created = biz.{OBJECTS}.created + excluded.created,
                                           changed = biz.{OBJECTS}.changed + excluded.changed"""

_REMOVED = f"""
INSERT INTO biz.{OBJECTS} (day, object_id, removed)
SELECT (r.since::timestamptz AT TIME ZONE '{ZONE}')::date, r.object_id, count(*)
FROM ontology.object_removals r WHERE r.since IS NOT NULL GROUP BY 1, 2
ON CONFLICT (day, object_id) DO UPDATE SET removed = excluded.removed"""

# the groups of one bounded (day, object_id) slice of daily_changes_objects, aggregated by
# what each object is NOW (an object the store lacks: the owner's). Slicing keeps every join
# and hash aggregate well under temp_file_limit (4 GB).
_SLICE_GROUPS = """
INSERT INTO dc_part
SELECT s.day,
       coalesce(split_part(regexp_replace(o.binding_id, '^gen[.]', ''), '.', 1), 'unknown') AS system,
       coalesce(o.object_type, 'unknown') AS object_type,
       CASE WHEN o.object_id IS NULL THEN %(owner)s ELSE coalesce(o.audience_key, '') END AS audience_key,
       coalesce(cv.markings, '[]') AS markings,
       (o.object_id IS NULL OR EXISTS (SELECT 1 FROM ontology.people_excluded_objects pe
                                       WHERE pe.object_id = s.object_id)) AS owner_only,
       sum(s.created)::bigint AS created, sum(s.changed)::bigint AS changed,
       sum(s.removed)::bigint AS removed,
       count(*) FILTER (WHERE o.object_id IS NULL)::bigint AS orphan_rows
FROM dc_slice s LEFT JOIN ontology.objects o ON o.object_id = s.object_id
LEFT JOIN LATERAL (SELECT v.markings FROM ontology.object_versions v
                   WHERE v.object_id = s.object_id AND v.superseded_at IS NULL LIMIT 1) cv ON true
GROUP BY 1, 2, 3, 4, 5, 6"""


def handle_of(day: Any, system: str, kind: str, audience: str, marks: str, only: bool) -> str:
    key = f"{day}|{system}|{kind}|{audience}|{marks}|{only}"
    return f"daily_changes:{day}:{system}:{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def _mark(write: Any) -> tuple[int, int | None]:
    write.execute(f"INSERT INTO biz.{MARK} (id, ver) VALUES (1, 0) ON CONFLICT DO NOTHING")
    return write.execute(f"SELECT ver, head FROM biz.{MARK} WHERE id = 1").fetchone()


def advance(write: Any, chunk: int = CHUNK) -> dict[str, Any]:
    """One chunk of versions counted per (day, object), its watermark moved in the same
    transaction. ``{"through": ver, "head": ver, "versions": n}``; ``versions`` 0: caught up."""
    with write.transaction():
        lo, _ = _mark(write)
        head = write.execute("SELECT max(ver) FROM ontology.object_versions").fetchone()[0] or 0
        hi_row = write.execute("SELECT ver FROM ontology.object_versions WHERE ver > %s "
                               "ORDER BY ver OFFSET %s LIMIT 1", (lo, chunk - 1)).fetchone()
        hi = hi_row[0] if hi_row else head
        if hi <= lo:
            write.execute(f"UPDATE biz.{MARK} SET head = %s, updated_at = now() WHERE id = 1",
                          (head,))
            return {"through": lo, "head": head, "versions": 0}
        write.execute(_ADVANCE, {"lo": lo, "hi": hi})
        n = write.execute("SELECT count(*) FROM ontology.object_versions WHERE ver > %s AND "
                          "ver <= %s", (lo, hi)).fetchone()[0]
        write.execute(f"UPDATE biz.{MARK} SET ver = %s, head = %s, updated_at = now() "
                      "WHERE id = 1", (hi, head))
        return {"through": hi, "head": head, "versions": n}


def refresh_removed(write: Any) -> int:
    """``removed`` per (day, object) recomputed whole from ``ontology.object_removals``."""
    with write.transaction():
        write.execute(f"UPDATE biz.{OBJECTS} SET removed = 0 WHERE removed <> 0")
        write.execute(_REMOVED)
        return write.execute(f"SELECT count(*) FROM biz.{OBJECTS} WHERE removed > 0"
                             ).fetchone()[0]


def regroup(write: Any, regroup_chunk: int = REGROUP_CHUNK) -> dict[str, Any]:
    """The served table from the objects as they are now (module doc, step 2): rows that
    differ are deleted and inserted, classes decided from the current audience, markings and
    exclusion set. Walks ``daily_changes_objects`` in PK-ordered slices of ``regroup_chunk``
    rows so no join or sort spills past ``temp_file_limit``. Returns the counts of rows kept,
    removed and added and the orphans."""
    from operating.pg import business_views_access as acc
    from operating.pg.access_classes import OWNER_KEY
    with write.transaction():
        write.execute(
            "CREATE TEMP TABLE dc_slice (day date, object_id text, created integer, "
            "changed integer, removed integer) ON COMMIT DROP")
        write.execute(
            "CREATE TEMP TABLE dc_part (day date, system text, object_type text, "
            "audience_key text, markings text, owner_only boolean, created bigint, "
            "changed bigint, removed bigint, orphan_rows bigint) ON COMMIT DROP")
        cursor: tuple[Any, str] | None = None
        while True:
            write.execute("TRUNCATE dc_slice")
            if cursor is None:
                inserted = write.execute(
                    f"INSERT INTO dc_slice SELECT day, object_id, created, changed, removed "
                    f"FROM biz.{OBJECTS} ORDER BY day, object_id LIMIT %s",
                    (regroup_chunk,)).rowcount
            else:
                inserted = write.execute(
                    f"INSERT INTO dc_slice SELECT day, object_id, created, changed, removed "
                    f"FROM biz.{OBJECTS} WHERE (day, object_id) > (%s, %s) "
                    f"ORDER BY day, object_id LIMIT %s",
                    (cursor[0], cursor[1], regroup_chunk)).rowcount
            if not inserted:
                break
            cursor = write.execute(
                "SELECT day, object_id FROM dc_slice ORDER BY day DESC, object_id DESC LIMIT 1"
            ).fetchone()
            write.execute("ANALYZE dc_slice")
            write.execute(_SLICE_GROUPS, {"owner": OWNER_KEY})
            if inserted < regroup_chunk:
                break
        orphans = int(write.execute(
            "SELECT coalesce(sum(orphan_rows), 0) FROM dc_part").fetchone()[0])
        write.execute(
            "CREATE TEMP TABLE dc_new ON COMMIT DROP AS "
            "SELECT day, system, object_type, audience_key, markings, owner_only, "
            "sum(created)::bigint AS created, sum(changed)::bigint AS changed, "
            "sum(removed)::bigint AS removed FROM dc_part GROUP BY 1, 2, 3, 4, 5, 6")
        pats = write.execute("SELECT DISTINCT audience_key, markings, owner_only FROM dc_new"
                             ).fetchall()
        raw = {}
        for aud, marks, only in pats:
            keys = {aud, *([OWNER_KEY] if only else [])}
            raw[(aud, marks, only)] = (tuple(sorted(k for k in keys if k)),
                                       tuple(sorted({marks, "[]"})), (), not aud)
        ids = acc.access_ids(write, raw.values())
        write.execute("CREATE TEMP TABLE dc_pat (audience_key text, markings text, "
                      "owner_only boolean, access_class integer) ON COMMIT DROP")
        with write.cursor() as cur:
            cur.executemany("INSERT INTO dc_pat VALUES (%s, %s, %s, %s)",
                            [(a, m, o, ids[acc._key(p)]) for (a, m, o), p in raw.items()])
        write.execute("CREATE TEMP TABLE dc_full ON COMMIT DROP AS SELECT n.*, p.access_class, "
                      "0 AS col_class, "
                      "'daily_changes:' || n.day || ':' || n.system || ':' || "
                      "substr(md5(concat_ws('|', n.day, n.system, n.object_type, n.audience_key, "
                      "n.markings, n.owner_only)), 1, 16) AS handle FROM dc_new n JOIN dc_pat p "
                      "USING (audience_key, markings, owner_only)")
        same = ("d.day = n.day AND d.system = n.system AND d.object_type = n.object_type AND "
                "d.audience_key = n.audience_key AND d.markings = n.markings AND "
                "d.owner_only = n.owner_only AND d.created = n.created AND "
                "d.changed = n.changed AND d.removed = n.removed AND "
                "d.access_class = n.access_class")
        from operating.pg import door
        revoked = (door.present(write, "biz")
                   and door.classes_changed(write, "biz", TABLE, "dc_full"))
        if revoked:
            door.ensure(write, "biz")
        gone = write.execute(f"DELETE FROM biz.{TABLE} d WHERE NOT EXISTS (SELECT 1 FROM "
                             f"dc_full n WHERE {same})").rowcount
        added = write.execute(
            f"INSERT INTO biz.{TABLE} (day, system, object_type, audience_key, markings, "
            "owner_only, created, changed, removed, access_class, handle) SELECT n.day, "
            "n.system, n.object_type, n.audience_key, n.markings, n.owner_only, n.created, "
            f"n.changed, n.removed, n.access_class, n.handle FROM dc_full n WHERE NOT EXISTS "
            f"(SELECT 1 FROM biz.{TABLE} d WHERE {same})").rowcount
        total = write.execute(f"SELECT count(*) FROM biz.{TABLE}").fetchone()[0]
        write.execute("DELETE FROM biz.class_counts WHERE view = %s", (TABLE,))
        write.execute("INSERT INTO biz.class_counts (view, access_class, col_class, rows) "
                      f"SELECT %s, access_class, col_class, count(*) FROM biz.{TABLE} "
                      "GROUP BY 2, 3", (TABLE,))
        if revoked:
            door.bump_revoked(write, "biz")
        write.execute(f"UPDATE biz.{MARK} SET orphans = %s WHERE id = 1", (orphans,))
        return {"groups": total, "removed_rows": gone, "added_rows": added, "orphans": orphans}


def _view() -> Any:
    from operating.pg.business_views import Col
    return SimpleNamespace(name=TABLE, doc=DOC, cols=[Col(*c) for c in COLS], fallback=None)


def ensure_view(write: Any) -> bool:
    """The guarded view ``<biz>_sql.daily_changes`` (door v2: the reader's held classes);
    False when the guarded schema is not installed (said by the caller)."""
    from operating.pg import door, sql_views
    dst = sql_views.guarded_schema()
    if write.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is None:
        return False
    with write.transaction():
        made = sql_views.put(write, f"{dst}.{TABLE}", door.view_ddl(write, _view(), "biz", dst))
        sql_views._grant(write, (sql_views.ROLE,), f"SELECT ON {dst}.{TABLE}")
        if made:
            door._bump(write, "biz")
    return True


def run(write: Any, chunk: int = CHUNK, regroup_chunk: int = REGROUP_CHUNK) -> dict[str, Any]:
    """Advance to the head, recompute the removals and the groups, make the guarded view."""
    out: dict[str, Any] = {"versions": 0, "chunks": 0}
    while True:
        got = advance(write, chunk)
        out.update(through_ver=got["through"], head_ver=got["head"])
        out["versions"] += got["versions"]
        out["chunks"] += 1
        if not got["versions"] or got["through"] >= got["head"]:
            break
    out["removal_objects"] = refresh_removed(write)
    out.update(regroup(write, regroup_chunk=regroup_chunk))
    out["view"] = ensure_view(write)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="daily_changes")
    ap.add_argument("command", choices=("run",))
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    import psycopg
    from operating.pg import config
    with psycopg.connect(args.dsn or config.dsn(), autocommit=True) as write:
        write.execute("SET statement_timeout = 1800000")
        try:
            got = run(write)
        except psycopg.Error as exc:
            print(json.dumps({"daily_changes_failed": f"{type(exc).__name__}: {exc}"}),
                  file=sys.stderr)
            return 1
    print(json.dumps(got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
