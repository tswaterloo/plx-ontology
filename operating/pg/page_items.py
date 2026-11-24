"""The served store's only write path (contract C2, speed specs 1.1.3): ``<biz>.page_items``, one
row per item a page shows, read only through the guarded view ``<biz>_page.page_items``
(``operator_api.served.read``).

A builder stages the rows a page kind should hold (``stage``: a table in the business schema with
the store's columns plus ``src_view``, the view of the business row each item was built from, and
``lin_ids``, record ids the item's lineage adds, the page's own record id for a record page) and
calls ``apply``, which makes the store equal the stage in ONE transaction, touching only the rows
that differ: a reader sees the old rows of a page or the new ones, never a mix; there is no swap,
no exclusive lock and no cold window, and an unchanged row is never rewritten.

PERMISSION. An item carries the access class, column class, ``ai_ok``, ``ver`` and ``biz`` of its
source row (never decided here or per reader) and a lineage row copied from the source row's
(``src_view``, ``src_handle``) plus ``lin_ids``, so the people exclusion makes it the owner's or
empties its payload exactly as it does the source row (``door.exclude`` on the stage before the
rows go in). An item whose source row has no lineage is refused with the whole apply: an item the
exclusion cannot follow is never served.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
from pathlib import Path
import re
import time
from typing import Any, Iterator, Mapping, Sequence

from operating.pg import business_views as bv

TABLE = "page_items"
REQUESTS = "page_requests"
MIGRATION = Path(__file__).with_name("migrations") / "0064_page_items.sql"
# the store's columns a stage holds (``stale_since`` is the store's own); the guarded view's
# columns are ``VIEW_COLS`` (the permission columns, handle, lineage stay behind the door)
COLS = ("page_kind", "page_key", "section", "seq", "payload", "access_class", "col_class",
        "ai_ok", "ver", "biz", "cellcol", "src_handle", "source_systems", "handle", "as_of",
        "dep_ver", "dep_ids", "code_fp")
VIEW_COLS = ("page_kind", "page_key", "section", "seq", "payload", "cellcol", "src_handle",
             "source_systems", "as_of", "stale_since")
STAGE_EXTRA = "src_view text NOT NULL, lin_ids text[] NOT NULL DEFAULT '{}'"
LOCK_TIMEOUT, STATEMENT_TIMEOUT = "5s", "600s"
_KIND = re.compile(r"[a-z][a-z0-9_:]{0,40}")


GRANTS = Path(__file__).with_name("migrations") / "0078_page_items_read_grants.sql"


def ensure(pg: Any, schema: str | None = None) -> None:
    """The store and its request table (migration 0064's text: one definition), in ``schema``,
    then the runners' read grant (migration 0078's text: plx_eval only; the API reads through the
    guarded view): 0064 revokes it, so replaying it alone left every batch runner without
    SELECT (KNOWN-GAPS O3)."""
    s = schema or bv.SCHEMA
    for path in (MIGRATION, GRANTS):
        pg.execute(path.read_text(encoding="utf-8").replace("biz_page", f"{s}_page")
                   .replace("biz.", f"{s}.").replace("pg_catalog, biz", f"pg_catalog, {s}"))


def stage(pg: Any, kind: str, schema: str | None = None) -> str:
    """A new empty stage for ``kind`` (an unlogged table in the business schema: the exclusion
    decides its rows where they are, ``door.exclude(staged=...)``), its qualified name."""
    if not _KIND.fullmatch(kind):
        raise ValueError(f"page kind {kind!r}")
    s = schema or bv.SCHEMA
    name = f"{s}.{TABLE}_stage_{kind.replace(':', '_')}"
    pg.execute(f"DROP TABLE IF EXISTS {name}")
    pg.execute(f"CREATE UNLOGGED TABLE {name} AS SELECT {', '.join(COLS)} FROM {s}.{TABLE} "
               "WITH NO DATA")
    pg.execute(f"ALTER TABLE {name} ADD COLUMN {STAGE_EXTRA.split(', ')[0]}, ADD COLUMN "
               f"{STAGE_EXTRA.split(', ')[1]}")
    return name


def _pairs(pg: Any, s: str) -> set[tuple[int, int]]:
    from operating.pg import door
    return {(a, c) for a, c in pg.execute(
        f"SELECT access_class, col_class FROM {s}.{door.COUNTS} WHERE view = %s", (TABLE,))}


def apply(pg: Any, kind: str, stage_table: str, *, keys: Sequence[str] | None, code_fp: str,
          job: str, schema: str | None = None, notes: Mapping[str, Any] | None = None
          ) -> dict[str, int]:
    """Make the items of ``kind`` equal the staged rows (``keys`` None: the stage is the whole
    kind; else only these page keys are replaced), in one transaction (module docstring). An
    item's handle is ``<kind>|<key>|<section>|<seq>`` (set here: one definition) and its code
    print ``code_fp``. ``notes``: kept in the kind's ``state`` row beside the counts.
    Returns {'staged', 'inserted', 'updated', 'deleted', 'unchanged', 'masked'}. The stage is
    dropped.

    MASKING. Every staged payload goes through ``card_text.mask_served`` here, once, before it is
    compared or stored (the one write path: every served payload, free text included, is stored
    masked; the read adds no cost: on a 200 KB body the rule takes ~27 ms, past the 3 ms the speed
    spec allows a read, 1.2 step 6). The rule is idempotent, so an unchanged item stays unchanged."""
    from operating.pg import door, generic_views as gv
    if not _KIND.fullmatch(kind):
        raise ValueError(f"page kind {kind!r}")
    s = schema or bv.SCHEMA
    started = time.monotonic()
    keys_arg = None if keys is None else list(keys)
    cols = ", ".join(COLS)
    with pg.transaction():
        pg.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        pg.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        pg.execute(f"UPDATE {stage_table} SET handle = page_kind || '|' || page_key || '|' || "
                   "section || '|' || seq, code_fp = %s", (code_fp,))
        masked = _mask_stage(pg, stage_table)
        staged, distinct, wrong = pg.execute(
            f"SELECT count(*), count(DISTINCT handle), count(*) FILTER (WHERE page_kind <> %s "
            f"OR (%s::text[] IS NOT NULL AND NOT page_key = ANY(%s::text[]))) FROM {stage_table}",
            (kind, keys_arg, keys_arg)).fetchone()
        if staged != distinct or wrong:
            raise RuntimeError(f"{stage_table}: {staged} rows, {distinct} handles, {wrong} "
                               f"outside kind {kind!r} / the keys given: not applied")
        found = pg.execute(f"SELECT count(*) FROM {stage_table} st JOIN {s}.lineage l ON "
                           "l.view = st.src_view AND l.handle = st.src_handle").fetchone()[0]
        if found != staged:   # an item the exclusion could not follow is never served
            raise RuntimeError(f"{staged} items staged but {found} with a lineage: not applied")
        # 1. each item's lineage: its source row's plus lin_ids; a cell's column only, a whole
        #    row's payload emptied by any column's excluded record (record_pages' rule)
        pg.execute(
            f"INSERT INTO {s}.lineage (view, handle, object_ids, read_ids, col_objects, cites, "
            "systems, copies) SELECT %s, st.handle, "
            "ARRAY(SELECT DISTINCT x FROM unnest(l.object_ids || st.lin_ids) x ORDER BY 1), "
            "l.read_ids, CASE WHEN st.cellcol IS NOT NULL THEN COALESCE((SELECT "
            "jsonb_build_object('payload', l.col_objects -> st.cellcol) WHERE "
            "l.col_objects ? st.cellcol), '{}'::jsonb) ELSE COALESCE((SELECT "
            "jsonb_build_object('payload', jsonb_agg(DISTINCT v)) FROM jsonb_each("
            "l.col_objects) e, jsonb_array_elements_text(e.value) v WHERE jsonb_typeof(e.value) "
            "= 'array' HAVING count(*) > 0), '{}'::jsonb) END, '{}'::jsonb, l.systems, l.copies "
            f"FROM {stage_table} st JOIN {s}.lineage l ON l.view = st.src_view AND "
            "l.handle = st.src_handle "
            "ON CONFLICT (view, handle) DO UPDATE SET object_ids = EXCLUDED.object_ids, "
            "read_ids = EXCLUDED.read_ids, col_objects = EXCLUDED.col_objects, "
            "systems = EXCLUDED.systems, copies = EXCLUDED.copies WHERE (lineage.object_ids, "
            "lineage.read_ids, lineage.col_objects, lineage.systems, lineage.copies) IS DISTINCT "
            "FROM (EXCLUDED.object_ids, EXCLUDED.read_ids, EXCLUDED.col_objects, "
            "EXCLUDED.systems, EXCLUDED.copies)", (TABLE,))
        # 2. the staged rows decided against the people exclusion where they are
        door.exclude(pg, s, only=TABLE, record=False, staged=(stage_table, f"{s}.lineage"))
        # 3. items of this kind (and these keys) the stage no longer has
        gone = [r[0] for r in pg.execute(
            f"DELETE FROM {s}.{TABLE} t WHERE t.page_kind = %s AND (%s::text[] IS NULL OR "
            "t.page_key = ANY(%s::text[])) AND NOT EXISTS (SELECT 1 FROM "
            f"{stage_table} st WHERE st.page_kind = t.page_kind AND st.page_key = t.page_key "
            "AND st.section = t.section AND st.seq = t.seq) RETURNING t.handle",
            (kind, keys_arg, keys_arg)).fetchall()]
        # 4. new and changed items; an unchanged one is not written
        cmp = ("payload", "access_class", "col_class", "ai_ok", "ver", "biz", "cellcol",
               "src_handle", "source_systems", "handle", "as_of", "dep_ver", "dep_ids", "code_fp")
        made = [r[0] for r in pg.execute(
            f"INSERT INTO {s}.{TABLE} AS t ({cols}) SELECT "
            + ", ".join(f"st.{c}" for c in COLS)
            + f" FROM {stage_table} st ON CONFLICT (page_kind, page_key, section, seq) DO UPDATE "
            "SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in cmp) + ", stale_since = NULL "
            "WHERE (" + ", ".join(f"t.{c}" for c in cmp) + ", t.stale_since) IS DISTINCT FROM ("
            + ", ".join(f"EXCLUDED.{c}" for c in cmp) + ", NULL) RETURNING (t.xmax = 0)",
            {"code_fp": code_fp}).fetchall()]
        # 5. the removed items' lineage (a handle the stage still has keeps its new row)
        if gone:
            pg.execute(f"DELETE FROM {s}.lineage l WHERE l.view = %s AND l.handle = ANY(%s) "
                       f"AND NOT EXISTS (SELECT 1 FROM {s}.{TABLE} t WHERE t.handle = l.handle)",
                       (TABLE, gone))
        pg.execute(f"DELETE FROM {s}.{REQUESTS} r WHERE r.page_kind = %s AND EXISTS (SELECT 1 "
                   f"FROM {stage_table} st WHERE st.page_kind = r.page_kind AND "
                   "st.page_key = r.page_key)", (kind,))
        # 6. the class counts; a class pair new to the store or gone from it: the door
        #    decides readers again (its stamp) and the view is made again for its masks
        before = _pairs(pg, s)
        door.recount(pg, s, TABLE)
        if _pairs(pg, s) != before:
            gv.make_views(pg, s, follow=False)
        inserted = sum(1 for x in made if x)
        out = {"staged": staged, "inserted": inserted, "updated": len(made) - inserted,
               "deleted": len(gone), "unchanged": staged - len(made), "masked": masked}
        pg.execute(
            f"INSERT INTO {s}.state (view, built_at, rows, seconds, notes) VALUES (%s, %s, %s, "
            "%s, %s) ON CONFLICT (view) DO UPDATE SET built_at = EXCLUDED.built_at, rows = "
            "EXCLUDED.rows, seconds = EXCLUDED.seconds, notes = EXCLUDED.notes",
            (state_view(kind), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             staged, round(time.monotonic() - started, 1),
             json.dumps({**(notes or {}), **out, "job": job, "code": code_fp,
                         "keys": None if keys_arg is None else len(keys_arg)})))
        pg.execute(f"DROP TABLE {stage_table}")
    return out


MASK_BATCH = 5000


def _mask_stage(pg: Any, stage_table: str) -> int:
    """Every staged payload through ``card_text.mask_served`` (card numbers with their security
    code and expiry, credential-shaped values); the number of payloads it changed. A payload the
    rule would make invalid JSON fails the apply (a masked page is still the page's JSON)."""
    from operating.card_text import mask_served
    changed: list[tuple[str, str, str, int, str]] = []   # (kind, key, section, seq, masked)
    with pg.cursor(name="page_items_mask") as cur:
        cur.itersize = MASK_BATCH
        cur.execute(f"SELECT page_kind, page_key, section, seq, payload::text FROM "
                    f"{stage_table} WHERE payload IS NOT NULL")
        for kind, key, section, seq, text in cur:
            shown = mask_served(text)
            if shown != text:
                changed.append((kind, key, section, seq, shown))
    for i in range(0, len(changed), MASK_BATCH):
        with pg.cursor() as cur:
            cur.executemany(f"UPDATE {stage_table} SET payload = %s::jsonb WHERE page_kind = %s "
                            "AND page_key = %s AND section = %s AND seq = %s",
                            [(shown, kind, key, section, seq)
                             for kind, key, section, seq, shown in changed[i:i + MASK_BATCH]])
    return len(changed)


def state_view(kind: str) -> str:
    """The ``state`` row of a kind's items (``views_as_of`` reads it: the page's as-of)."""
    return f"{TABLE}:{kind}"


def mark_stale(pg: Any, kind: str, keys: Sequence[str], schema: str | None = None) -> int:
    """Items of these pages await their builder: ``stale_since`` set where it is not."""
    s = schema or bv.SCHEMA
    return pg.execute(f"UPDATE {s}.{TABLE} SET stale_since = now() WHERE page_kind = %s AND "
                      "page_key = ANY(%s) AND stale_since IS NULL", (kind, list(keys))).rowcount


def code_fp(*functions: tuple[str, str]) -> str:
    """The digest of a builder's own functions ((module, name) pairs)."""
    from ontology.build_keys import code_digest
    digest = code_digest(list(functions))
    if not digest:
        raise RuntimeError(f"no code digest for {functions}")
    return digest[:32]


@contextlib.contextmanager
def room(pg: Any, gb: float, job: str) -> Iterator[Any]:
    """The WAL budget and the ONE disk budget (``operating.disk_budget``: its one floor) for a
    write of up to ``gb`` GB by ``job``, held while the block runs and released after. The disk
    asked is the database's own (``business_comms.disk_path``: on a Spark build copy its disk,
    on the box /data). Refused: ``disk_budget.Refused`` with the numbers (the caller says it and
    writes nothing)."""
    from operating import disk_budget
    from operating.pg import business_comms, wal_budget
    wal_budget.Budget.for_conn(pg, job=job, check_seconds=0).wait(
        max_seconds=wal_budget.max_wait_seconds())
    granted = disk_budget.reserve(job, gb, path=business_comms.disk_path(pg))
    try:
        yield granted
    finally:
        disk_budget.release(job)
