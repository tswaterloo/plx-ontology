"""The generic door's tables (ARCH addendum 9 + part 2 s.14, DOOR v2): ``<biz>.records``, one
narrow row per current record of every type the comms tables do not own, and
``<biz>.record_links``, one row per live link between two such records, built inside
Postgres in bounded batches (no record is loaded into Python) and refreshed in place.

``records`` (one row per record; ``rid`` = the store's ``objects.row_no``):
  type, system, source (binding), handle, record_id, label, record_date (YYYY-MM-DD text),
  first_seen, changed_at, ``fields`` (jsonb: per property the value the schema's precedence
  ranks first, numbers as numbers, ``<field>_currency`` / ``<field>_unit`` beside a money or
  quantity value), and the permission columns decided at build:
  * ``access_class``: the record's (audience key, current versions' markings) pattern in
    ``access_classes``; a record of a system that is not a business system (citation scheme
    other than ``api``/``bc``/``mc``: SharePoint lists, Planner, records read out of mail)
    also carries ``FULL_KEY`` (a full reader's only, as plx.table shows such records to no
    staff reader);
  * a field whose first-ranked cell is marked beyond the record's own markings joins the
    record's markings in its class: a reader lacking that marking reads no row of the
    record (``col_class`` stays 0); the label and record date are read from unmarked cells;
  * ``ver`` / ``biz``: the record's newest current version and whether its system is a
    business system. The people exclusion decides versions only up to its ``covered_ver``:
    the views serve a record of any other system only once ``ver`` is at or below it (the
    communications tables' rule; no decided set: none), so a record read out of mail an
    excluded person sent is never shown before the set decided it;
  * ``ai_ok``: false when any cell of the current version, the handle, binding or native
    key names Freightcom (the owner's no-AI rule; regex once here, never per query).
  Never in the table: records whose source no longer holds them, the excluded people's own
  records (``ontology.people_excluded_objects``: no set built for the store = no record of
  a non-business system at all), PLX's own issued documents (``own_issued_documents``) and
  the types ``LEFT_OUT`` (mail, attachments, files, Teams, meetings: the comms tables').

``record_links``: (from_rid, link_type, to_rid, tier) for live links at a shown tier between
two records of ``records`` (no never-authority end); its ``access_class`` is the union of
both ends' patterns, the link's own markings and, when the link was learned from anything
but a business system or its from record (evidence handles), ``FULL_KEY``: one class test
says both ends and the link are the reader's. Its ``ver`` is the newest of both ends' and its
evidence's, ``biz`` only when both ends are and no such evidence exists, ``ev_ids`` the
evidence's records (lineage ``read_ids``: the people exclusion drops the link with any).

  python3 -m operating.pg.generic_records build [--schema S] [--workers N]
  python3 -m operating.pg.generic_records refresh [--schema S]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import sys
import time
from typing import Any, Mapping, Sequence

from operating.pg import access_classes as ac
from operating.pg import business_views as bv
from operating.pg import generic_views as gv

ONT = "ontology"
BATCH = 50_000           # store rows (objects.row_no / link_rows.row_no) per statement
# Disk: a batch's temp tables take about 4 GB at 100k rows (8 workers took /data from 56 to
# 22 GB free, 11-18 7:20 PM ET); each batch first waits for the floor plus every worker's
# share free (``room``), and gives up, failing the build (the served tables kept), when it
# does not come back. The capture must never run short.
MIN_FREE_GB_DEFAULT = 35.0
BATCH_GB = 3.0           # one worker's temp tables at BATCH rows, with headroom
ROOM_WAIT_SECONDS = 600.0
# The comms tables own these (operating.pg.business_comms and its successors).
LEFT_OUT = ("Email", "Attachment", "SharePointFile", "ContainerMember", "CalendarEvent",
            "Meeting", "MeetingTranscript", "TeamsChat", "TeamsChannel",
            "TeamsChannelMessage", "TeamsChatMessage")
UNMARKED = "[]"

RECORDS_DDL = """CREATE TABLE {s}.{t} (
    rid bigint PRIMARY KEY, record_id text NOT NULL, type text NOT NULL, system text NOT NULL,
    source text NOT NULL, handle text NOT NULL, label text, record_date text,
    first_seen text, changed_at text, access_class integer NOT NULL,
    col_class integer NOT NULL DEFAULT 0, ai_ok boolean NOT NULL, link_ok boolean NOT NULL,
    fields jsonb NOT NULL, ver bigint NOT NULL, biz boolean NOT NULL)"""
LINKS_DDL = """CREATE TABLE {s}.{t} (
    from_rid bigint NOT NULL, link_type text NOT NULL, to_rid bigint NOT NULL,
    tier text NOT NULL, from_id text NOT NULL, from_type text NOT NULL, from_handle text,
    to_id text NOT NULL, to_type text NOT NULL, to_handle text, handle text NOT NULL UNIQUE,
    access_class integer NOT NULL, col_class integer NOT NULL DEFAULT 0,
    ai_ok boolean NOT NULL, ver bigint NOT NULL, biz boolean NOT NULL,
    ev_ids text[] NOT NULL DEFAULT '{{}}')"""
# the generic views' columns (``door.view_ddl`` adds handle and withheld)
RECORD_COLS = ("record_id", "type", "system", "source", "label", "record_date", "first_seen",
               "changed_at", "fields")
LINK_COLS = ("from_id", "from_type", "from_handle", "link_type", "to_id", "to_type",
             "to_handle", "tier")


def _lit(text: str) -> str:
    return gv._lit(text)


def _arr(values: Sequence[str]) -> str:
    return gv._arr(values)


def removed_sql(o: str, now: str) -> str:
    """plx.table's removal rule (``ontology_tools_table._held``) at ``now``: a missing mark
    with no removal row, or a removal row in force; an unreadable time keeps it out."""
    at = f"{ONT}.sq_julianday({now})"
    since = f"COALESCE(rm.since, {o}.missing_from_source_since)"
    return (f"(({o}.missing_from_source_since IS NOT NULL AND COALESCE({ONT}.sq_julianday("
            f"{o}.missing_from_source_since) <= {at}, true) AND NOT EXISTS (SELECT 1 FROM "
            f"{ONT}.object_removals rm WHERE rm.object_id = {o}.object_id)) OR EXISTS ("
            f"SELECT 1 FROM {ONT}.object_removals rm WHERE rm.object_id = {o}.object_id AND "
            f"{since} IS NOT NULL AND COALESCE({ONT}.sq_julianday({since}) <= {at}, true) AND "
            f"(rm.until IS NULL OR COALESCE({ONT}.sq_julianday(rm.until) > {at}, true))))")


def _now() -> str:
    """The store's latest time (``ontology.store.LATEST_SQL``): a record its source no longer
    holds is out whenever its mark was set (the catalog's and the comms tables' rule)."""
    from ontology.store import LATEST_SQL
    return LATEST_SQL


def _stage(pg: Any, where: str, excluded_ready: bool) -> None:
    """``pg_temp.gr_b``: the records of ``where`` (over ``o``) that belong in ``records``,
    each with its newest current version, its class pattern and flags; ``gr_c``: per record
    and property the first-ranked cell (and every cell's Freightcom test)."""
    from integrations.image_ocr_gpu import FREIGHTCOM
    pat = _lit(FREIGHTCOM.pattern)
    business = _arr(bv.BUSINESS_SCHEMES)
    # no exclusion set for the store: records of non-business systems stay out (fail closed)
    item_ok = "true" if excluded_ready else "false"
    pg.execute("DROP TABLE IF EXISTS pg_temp.gr_b, pg_temp.gr_c, pg_temp.gr_f")
    pg.execute(
        "CREATE TEMP TABLE gr_b AS SELECT o.row_no AS rid, o.object_id, o.object_type, "
        "o.binding_id, o.first_recorded_at, v.ver, v.l1_handle, v.recorded_at, "
        f"(split_part(v.l1_handle, '://', 1) = ANY({business})) AS biz, "
        f"CASE WHEN split_part(v.l1_handle, '://', 1) = ANY({business}) THEN "
        f"ARRAY[o.audience_key] ELSE (SELECT array_agg(k ORDER BY k) FROM unnest(ARRAY["
        f"o.audience_key, {_lit(ac.FULL_KEY)}]) k) END AS keys, "
        f"ARRAY(SELECT DISTINCT w.markings FROM {ONT}.object_versions w WHERE w.object_id = "
        "o.object_id AND w.superseded_at IS NULL ORDER BY 1) AS marks, "
        f"NOT (COALESCE(v.lineage, '') = ANY({_arr(bv.NEVER_AUTHORITY_LINEAGES)})) AS link_ok, "
        f"NOT (concat_ws(' ', v.l1_handle, o.binding_id, o.native_key) ~* {pat}) AS ai_ok "
        f"FROM {ONT}.objects o CROSS JOIN LATERAL (SELECT * FROM {ONT}.object_versions v "
        "WHERE v.object_id = o.object_id AND v.superseded_at IS NULL ORDER BY v.ver DESC "
        f"LIMIT 1) v WHERE ({where}) AND NOT (o.object_type = ANY({_arr(LEFT_OUT)})) "
        f"AND NOT {removed_sql('o', _now())} "
        f"AND NOT EXISTS (SELECT 1 FROM {ONT}.people_excluded_objects peo WHERE "
        "peo.object_id = o.object_id) "
        f"AND NOT EXISTS (SELECT 1 FROM {ONT}.own_issued_documents oi WHERE "
        "oi.object_id = o.object_id) "
        f"AND (split_part(v.l1_handle, '://', 1) = ANY({business}) OR {item_ok})")
    pg.execute(
        "CREATE TEMP TABLE gr_c AS SELECT DISTINCT ON (b.rid, f.property) b.rid, f.property, "
        "c.value, c.value_num, c.currency, k.unit, k.value_type, k.markings, "
        # a cell marked as the record itself is no more than the record's class
        f"(k.markings = {_lit(UNMARKED)} OR k.markings = ANY(b.marks)) AS free, "
        f"bool_or(c.value ~* {pat}) OVER (PARTITION BY b.rid) AS fc "
        f"FROM gr_b b JOIN {ONT}.value_sources s ON s.ver = b.ver AND s.superseded_at IS NULL "
        f"JOIN {ONT}.value_cells c ON c.src = s.src JOIN {ONT}.value_fields f ON "
        f"f.field = c.field JOIN {ONT}.value_kinds k ON k.kind = c.kind "
        "ORDER BY b.rid, f.property, k.authority_rank NULLS LAST, c.field, c.value")
    nums = _arr(gv.NUMERIC_KINDS)
    pg.execute(
        "CREATE TEMP TABLE gr_f AS SELECT rid, bool_or(fc) AS fc, "
        "jsonb_object_agg(property, CASE WHEN value_type = ANY(" + nums + ") AND value_num "
        "IS NOT NULL THEN to_jsonb(value_num) ELSE to_jsonb(value) END) "
        "|| COALESCE(jsonb_object_agg(property || '_currency', currency) FILTER (WHERE "
        "currency IS NOT NULL), '{}') || COALESCE(jsonb_object_agg(property || '_unit', "
        "unit) FILTER (WHERE unit IS NOT NULL), '{}') AS fields, "
        "jsonb_object_agg(property, markings) FILTER (WHERE NOT free) AS field_marks, "
        "ARRAY(SELECT DISTINCT m FROM unnest(array_agg(markings) FILTER (WHERE NOT free)) m "
        "ORDER BY 1) AS col_marks FROM gr_c GROUP BY rid")
    pg.execute("CREATE INDEX ON gr_c (rid)")
    pg.execute("ANALYZE gr_b")
    pg.execute("ANALYZE gr_c")
    pg.execute("ANALYZE gr_f")


DAY = ("CASE WHEN c.value ~ '^[12][0-9]{3}-[01][0-9]-[0-3][0-9]' THEN left(c.value, 10) "
       "WHEN c.value ~ '^[12][0-9]{3}[01][0-9][0-3][0-9]$' THEN substr(c.value, 1, 4) "
       "|| '-' || substr(c.value, 5, 2) || '-' || substr(c.value, 7, 2) END")


def _first_unmarked(col: str, expr: str, cond: str) -> str:
    """``expr`` over the record's first-ranked unmarked cell of the first of the catalog's
    ``col`` fields (in their declared order) that has one meeting ``cond``."""
    return (f"(SELECT {expr} FROM gr_c c WHERE c.rid = b.rid AND c.free "
            f"AND c.value IS NOT NULL AND c.property = ANY(rt.{col}) AND "
            f"{cond} ORDER BY array_position(rt.{col}, c.property) LIMIT 1)")


def _insert(pg: Any, schema: str, table: str) -> int:
    """Classes for the staged patterns, then the staged rows into ``<schema>.<table>``."""
    # a field marked beyond the record joins the record's own markings: a reader lacking
    # it reads no row of the record (the comms tables' rule: a marked cell hides the row)
    pg.execute("DROP TABLE IF EXISTS pg_temp.gr_k")
    pg.execute("CREATE TEMP TABLE gr_k AS SELECT b.rid, b.keys, ARRAY(SELECT DISTINCT m FROM "
               "unnest(b.marks || COALESCE(f.col_marks, '{}'::text[]) || "
               f"ARRAY[{_lit(UNMARKED)}]) m ORDER BY 1) AS marks FROM gr_b b LEFT JOIN gr_f f "
               "ON f.rid = b.rid")
    with pg.transaction():   # short: a build's parallel batches only wait on the ids
        ac.assign(pg, schema, "SELECT keys, marks FROM gr_k")
    label = _first_unmarked("label_fields", "c.value", "true")
    record_date = _first_unmarked("date_fields", DAY, f"({DAY}) IS NOT NULL")
    cur = pg.execute(
        f"INSERT INTO {schema}.{table} (rid, record_id, type, system, source, handle, label, "
        "record_date, first_seen, changed_at, access_class, col_class, ai_ok, link_ok, "
        "fields, ver, biz) SELECT b.rid, b.object_id, b.object_type, "
        f"{gv.system_sql('b.binding_id')}, b.binding_id, b.l1_handle, {label}, "
        f"{record_date}, b.first_recorded_at, b.recorded_at, k.id, 0, "
        "b.ai_ok AND NOT COALESCE(f.fc, false), b.link_ok, COALESCE(f.fields, '{}'), "
        "b.ver, b.biz "
        f"FROM gr_b b JOIN gr_k gk ON gk.rid = b.rid JOIN {schema}.access_classes k ON "
        "k.audience_keys = gk.keys AND k.rec_marks = gk.marks AND k.doc_marks = '{}' AND "
        f"NOT k.blocked LEFT JOIN gr_f f ON f.rid = b.rid "
        f"LEFT JOIN {schema}.record_types rt ON rt.type = b.object_type")
    return cur.rowcount


def _link_insert(pg: Any, schema: str, records: str, table: str, where: str) -> int:
    """Live links of ``where`` (over ``l``, ``ontology.link_rows``) between two rows of
    ``records``, each with its class (both ends, its markings, mail evidence), its version
    and its evidence records.

    Evidence (``evidence_handles``) other than a business system's or the from record's
    own handle (mail, Teams, SharePoint, Planner...) makes the link a full reader's only
    (``FULL_KEY``) and not a business link (``biz`` false: the views serve it only once the
    people exclusion decided its newest version, ``ver``); its records are the link's
    ``ev_ids`` (``lineage.read_ids``: the exclusion drops the link with any of them,
    ``door.exclude``). A link with evidence from an excluded person's record, or evidence
    the store cannot name as a record (undecidable), is left out (fail closed); a page or
    attachment handle names its message's or file's record (live sample 2026-11-18, read
    only: 0 of 14,771 such evidence handles unnamed so; 434 without the fragment rule)."""
    business = _arr(bv.BUSINESS_SCHEMES)
    ev = f"COALESCE(l.evidence_handles, {ONT}.link_text(l.evidence_id, l.slots, 1, l.ek))"
    handles = (f"json_array_elements_text(CASE WHEN {ev} IS NULL OR {ev} = '' THEN "
               f"'[]'::json ELSE ({ev})::json END) h(x)")
    evidence = (
        "SELECT count(h.x) AS n, count(h.x) FILTER (WHERE ov.object_id IS NULL) AS lost, "
        "COALESCE(array_agg(DISTINCT ov.object_id ORDER BY ov.object_id) FILTER (WHERE "
        "ov.object_id IS NOT NULL), '{}'::text[]) AS ids, max(ov.ver) AS ver "
        # a page or attachment of a message or file (``...#page=1``) is its record's
        f"FROM {handles} LEFT JOIN {ONT}.object_versions ov ON ov.l1_handle IN (h.x, "
        "split_part(h.x, '#', 1)) "
        f"WHERE split_part(h.x, '://', 1) <> ALL({business}) AND h.x <> f.handle")
    pg.execute("DROP TABLE IF EXISTS pg_temp.gr_l")
    pg.execute(
        "CREATE TEMP TABLE gr_l AS SELECT f.rid AS from_rid, l.link_type, t.rid AS to_rid, "
        "l.tier, f.record_id AS from_id, f.type AS from_type, f.handle AS from_handle, "
        "t.record_id AS to_id, t.type AS to_type, t.handle AS to_handle, "
        "f.ai_ok AND t.ai_ok AS ai_ok, ARRAY(SELECT DISTINCT k FROM unnest(fc.audience_keys "
        "|| tc.audience_keys || CASE WHEN e.n > 0 THEN "
        f"ARRAY[{_lit(ac.FULL_KEY)}] ELSE '{{}}'::text[] END) k "
        "ORDER BY 1) AS keys, ARRAY(SELECT DISTINCT m FROM unnest(fc.rec_marks || "
        "tc.rec_marks || ARRAY[l.markings]) m ORDER BY 1) AS marks, "
        "greatest(f.ver, t.ver, COALESCE(e.ver, 0)) AS ver, "
        "f.biz AND t.biz AND e.n = 0 AS biz, e.ids AS ev_ids "
        f"FROM {ONT}.link_rows l JOIN {schema}.{records} f ON f.record_id = l.from_object "
        f"JOIN {schema}.{records} t ON t.record_id = l.to_object "
        f"JOIN {schema}.access_classes fc ON fc.id = f.access_class "
        f"JOIN {schema}.access_classes tc ON tc.id = t.access_class "
        f"CROSS JOIN LATERAL ({evidence}) e "
        f"WHERE ({where}) AND l.superseded_at IS NULL AND l.tier = ANY({_arr(gv.LINK_TIERS)}) "
        "AND f.link_ok AND t.link_ok AND e.lost = 0 AND NOT EXISTS (SELECT 1 FROM "
        f"unnest(e.ids) i JOIN {ONT}.people_excluded_objects peo ON peo.object_id = i)")
    with pg.transaction():
        ac.assign(pg, schema, "SELECT keys, marks FROM gr_l")
    return pg.execute(
        f"INSERT INTO {schema}.{table} (from_rid, link_type, to_rid, tier, from_id, from_type, "
        "from_handle, to_id, to_type, to_handle, handle, access_class, col_class, ai_ok, ver, "
        "biz, ev_ids) "
        "SELECT DISTINCT ON (g.from_rid, g.link_type, g.to_rid) g.from_rid, g.link_type, "
        "g.to_rid, g.tier, g.from_id, g.from_type, "
        "g.from_handle, g.to_id, g.to_type, g.to_handle, 'link:' || g.from_rid || '/' || "
        "g.link_type || '/' || g.to_rid, k.id, 0, g.ai_ok, g.ver, g.biz, g.ev_ids "
        f"FROM gr_l g JOIN {schema}.access_classes k ON k.audience_keys = g.keys AND "
        "k.rec_marks = g.marks AND k.doc_marks = '{}' AND NOT k.blocked "
        # one row per (from, link type, to): the best tier's link row (its own class)
        "ORDER BY g.from_rid, g.link_type, g.to_rid, array_position("
        f"{_arr(gv.LINK_TIERS)}, g.tier), k.id ON CONFLICT (handle) DO NOTHING").rowcount


def excluded_ready(pg: Any) -> bool:
    """Whether the store's people-exclusion set is built (its state row says so)."""
    try:
        row = pg.execute(f"SELECT exclusion FROM {ONT}.people_excluded_state").fetchone()
    except Exception:  # noqa: BLE001 - no state table: no set
        return False
    return bool(row and row[0])


def _marks(pg: Any) -> dict[str, int]:
    ver, link, row = pg.execute(
        f"SELECT (SELECT max(ver) FROM {ONT}.object_versions), (SELECT max(row_no) FROM "
        f"{ONT}.link_rows), (SELECT max(row_no) FROM {ONT}.objects)").fetchone()
    return {"ver": int(ver or 0), "link": int(link or 0), "row": int(row or 0)}


def min_free_bytes() -> int:
    """``PLX_RECORDS_MIN_FREE_GB`` (else ``MIN_FREE_GB_DEFAULT``) in bytes; a value that
    is not a number of GB is refused, never read as no floor."""
    import os
    raw = os.environ.get("PLX_RECORDS_MIN_FREE_GB", "").strip()
    try:
        gb = float(raw) if raw else MIN_FREE_GB_DEFAULT
    except ValueError:
        gb = -1.0
    if not gb >= 0:
        raise RuntimeError(f"PLX_RECORDS_MIN_FREE_GB must be a number of GB, not {raw!r}")
    return int(gb * 1024 ** 3)


def room(pg: Any, workers: int, *, wait: float = ROOM_WAIT_SECONDS,
         sleep: Any = None, free: Any = None) -> None:
    """Before a batch: wait until the database's disk has the floor plus ``workers``
    batches' temp tables free; still short after ``wait`` seconds (or its free space
    unreadable): raise (the batch's transaction never starts; a build keeps the served
    tables)."""
    import time as _time
    from operating.pg import business_comms
    sleep = sleep or _time.sleep
    free = free or business_comms.free_bytes
    path = business_comms.disk_path(pg)
    need = min_free_bytes() + int(max(1, workers) * BATCH_GB * 1024 ** 3)
    deadline = _time.monotonic() + wait
    while True:
        try:
            have = free(path)
        except OSError as exc:
            raise RuntimeError(f"records: the free space of {path} cannot be read "
                               f"({exc}); stopped") from exc
        if have >= need:
            return
        if _time.monotonic() >= deadline:
            gb = 1024 ** 3
            raise RuntimeError(f"records: {have / gb:.1f} GB free on {path}, below the "
                               f"{need / gb:.0f} GB a batch needs (PLX_RECORDS_MIN_FREE_GB "
                               f"floor + {max(1, workers)} batches); stopped")
        sleep(15)


def _batches(dsn: str, statements: Sequence[Any], workers: int, setup: Any) -> int:
    """Run ``statements`` (callables taking a connection, each its own transaction) on
    ``workers`` connections; the first failure stops the build."""
    import psycopg
    total = 0

    def run(chunk: Sequence[Any]) -> int:
        n = 0
        with psycopg.connect(dsn, autocommit=True) as pg:
            setup(pg)
            for step in chunk:
                n += step(pg)
        return n

    parts = [statements[i::workers] for i in range(max(1, workers))]
    with cf.ThreadPoolExecutor(max_workers=len(parts)) as pool:
        for got in pool.map(run, parts):
            total += got
    return total


def _session(pg: Any) -> None:
    pg.execute("SET work_mem = '256MB'")
    pg.execute("SET search_path = ontology, public")


def covered_types() -> list[str]:
    """The types a business table holds one row per record of (its first read type,
    ``business_views.View.reads[0]``: the curated tables' and the communications tables'):
    ``build(uncovered=True)`` leaves them out, with the raw relational mirrors (``Rel...``),
    for a first build that must stay small (disk); a full build holds every type. A type a
    curated table only reads from (shipments for an order's status, a supplier's
    confirmation for a PO's promise) is not one: its records are in ``records``."""
    return sorted({v.reads[0] for v in bv.VIEWS.values()
                   if v.reads and getattr(v, "covers", True)})


def type_choice(uncovered: bool, mirrors: bool,
                skip_prefixes: Sequence[str] = ()) -> dict[str, Any]:
    """A build's type choice (kept in its notes: the refresh follows the same types).
    ``skip_prefixes``: type name prefixes left out besides (``RelSage``: Sage's 7.5M mirror
    records, too many for the disk free on 11-18). A prefix is letters and digits only."""
    bad = [p for p in skip_prefixes if not (isinstance(p, str) and p.isalnum())]
    if bad:
        raise ValueError(f"a type prefix is letters and digits only: {bad!r}")
    return {"skip_types": covered_types() if uncovered else [],
            "skip_mirrors": bool(uncovered) and not mirrors,
            "skip_prefixes": sorted(set(skip_prefixes))}


def types_sql(notes: Mapping[str, Any]) -> str:
    """The record filter (over ``o``) of a build's type choice (its ``notes``)."""
    skip = list(notes.get("skip_types") or ())
    out = f"NOT (o.object_type = ANY({_arr(skip)}))" if skip else "true"
    if notes.get("skip_mirrors"):
        out += " AND o.object_type NOT LIKE 'Rel%'"
    for prefix in notes.get("skip_prefixes") or ():
        if not (isinstance(prefix, str) and prefix.isalnum()):
            return "false"   # a choice not made here: no record (fail closed)
        out += f" AND o.object_type NOT LIKE '{prefix}%'"
    return out


def door_side(pg: Any, s: str, touched: Mapping[str, str] | None = None) -> None:
    """The door's side tables for the generic tables: their ``lineage`` (each row's own
    records: the people exclusion drops a row naming one, ``door.exclude``; a handle two
    records share names both) and ``class_counts``. ``touched``: per view, a query of the
    handles whose rows changed (a refresh: only those are decided again); else whole. In
    the caller's transaction."""
    from operating.pg import door
    for view, ids, read in (("records", "ARRAY[record_id]", "'{}'::text[]"),
                            ("record_links", "ARRAY[from_id, to_id]", "ev_ids")):
        if touched is None:
            pg.execute(f"DELETE FROM {s}.lineage WHERE view = %s", (view,))
            only = ""
        else:
            pg.execute(f"DELETE FROM {s}.lineage WHERE view = %s AND handle IN "
                       f"({touched[view]})", (view,))
            only = f"WHERE handle IN ({touched[view]})"
        # a link's evidence records are read ids: the exclusion drops it with any of them
        pg.execute(f"INSERT INTO {s}.lineage (view, handle, object_ids, read_ids) SELECT %s, "
                   f"handle, array_agg(DISTINCT x ORDER BY x), COALESCE(array_agg(DISTINCT y ORDER "
                   "BY y) FILTER (WHERE y IS NOT NULL), '{}'::text[]) "
                   f"FROM {s}.{view} LEFT JOIN LATERAL unnest({read}) y ON true, "
                   f"unnest({ids}) x {only} GROUP BY handle", (view,))
        door.recount(pg, s, view)


def build(dsn: str, schema: str | None = None, workers: int = 4, *,
          uncovered: bool = False, mirrors: bool = False,
          skip_prefixes: Sequence[str] = ()) -> dict[str, Any]:
    """Build ``records`` and ``record_links`` beside the live ones, index them, then swap
    them in with the guarded views re-made (one transaction: readers see old or new).
    ``uncovered``: the types no curated table holds; ``mirrors`` with it: the raw relational
    mirrors (``Rel...``, 184 types and 7.8M records on 11-18, Sage's history among them)
    too, else they are left out (a first build that must stay small)."""
    import psycopg
    s = schema or bv.SCHEMA
    started = time.monotonic()
    choice = type_choice(uncovered, mirrors, skip_prefixes)
    which = types_sql(choice)
    with psycopg.connect(dsn, autocommit=True) as pg:
        ac.ensure(pg, s)
        pg.execute(gv.CATALOG_DDL.format(s=s))
        got = _marks(pg)
        ready = excluded_ready(pg)
        for t in ("records_new", "record_links_new"):
            pg.execute(f"DROP TABLE IF EXISTS {s}.{t}")
        pg.execute(RECORDS_DDL.format(s=s, t="records_new"))
        pg.execute(LINKS_DDL.format(s=s, t="record_links_new"))

    def rec_step(lo: int) -> Any:
        def step(pg: Any) -> int:
            pg.execute("DISCARD TEMP")   # the last batch's temp tables, before the check
            room(pg, workers)
            _stage(pg, f"o.row_no > {lo} AND o.row_no <= {lo + BATCH} AND {which}", ready)
            return _insert(pg, s, "records_new")
        return step

    rows = _batches(dsn, [rec_step(lo) for lo in range(0, got["row"], BATCH)], workers,
                    _session)
    with psycopg.connect(dsn, autocommit=True) as pg:
        # a record the delta wrote again under a new store row while the batches ran was
        # read twice (old and new row, each by its own batch): its newest row stays (11-18
        # live: Person records rewritten every cycle failed the unique index below)
        pg.execute(f"DELETE FROM {s}.records_new a USING {s}.records_new b WHERE "
                   "a.record_id = b.record_id AND a.rid < b.rid")
        pg.execute(f"CREATE UNIQUE INDEX ON {s}.records_new (record_id)")
        pg.execute(f"ANALYZE {s}.records_new")

    def link_step(lo: int) -> Any:
        def step(pg: Any) -> int:
            room(pg, workers)
            with pg.transaction():
                return _link_insert(pg, s, "records_new", "record_links_new",
                                    f"l.row_no > {lo} AND l.row_no <= {lo + BATCH}")
        return step

    links = _batches(dsn, [link_step(lo) for lo in range(0, got["link"], BATCH)], workers,
                     _session)
    with psycopg.connect(dsn, autocommit=True) as pg:
        index(pg, s, "records_new", "record_links_new")
        swap(pg, s, got, {"records": rows, "record_links": links,
                          "seconds": round(time.monotonic() - started, 1),
                          "people_excluded_ready": ready, **choice})
    return {"records": rows, "record_links": links, "marks": got,
            "seconds": round(time.monotonic() - started, 1)}


def index(pg: Any, s: str, records: str, links: str) -> None:
    for cols in ("type, access_class) INCLUDE (ai_ok, record_id", "handle", "type, record_date",
                 "source"):
        pg.execute(f"CREATE INDEX ON {s}.{records} ({cols})")
    # a record found by its name or number (``label ILIKE '%...%'``) without a scan
    if pg.execute("SELECT 1 FROM pg_opclass WHERE opcname = 'gin_trgm_ops'").fetchone():
        pg.execute(f"CREATE INDEX ON {s}.{records} USING gin (label gin_trgm_ops)")
    for cols in ("from_rid, link_type", "to_rid, link_type", "link_type"):
        pg.execute(f"CREATE INDEX ON {s}.{links} ({cols})")
    pg.execute(f"ANALYZE {s}.{records}")
    pg.execute(f"ANALYZE {s}.{links}")


def swap(pg: Any, s: str, got: Mapping[str, int], notes: Mapping[str, Any]) -> None:
    """The new tables in place of the old in one transaction, the guarded views re-made
    over them (a view follows its table, so it is dropped and made again)."""
    with pg.transaction():
        dst = gv.sql_views.guarded_schema(s)
        if pg.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is not None:
            for name in ("record_links", "records"):
                gv.sql_views.drop(pg, name, s)
        for old, new in (("records", "records_new"), ("record_links", "record_links_new")):
            pg.execute(f"DROP TABLE IF EXISTS {s}.{old}")
            pg.execute(f"ALTER TABLE {s}.{new} RENAME TO {old}")
        _state(pg, s, got, notes)
        door_side(pg, s)
        if pg.execute("SELECT to_regclass(%s)", (f"{s}.record_types",)).fetchone()[0]:
            gv.mark_tables(pg, s)
        if pg.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is not None:
            gv.make_views(pg, s)


def _state(pg: Any, s: str, got: Mapping[str, int], notes: Mapping[str, Any]) -> None:
    pg.execute(f"CREATE TABLE IF NOT EXISTS {s}.state (view text PRIMARY KEY, "
               "built_at text NOT NULL, digest text, rows bigint NOT NULL, "
               "seconds double precision, notes jsonb NOT NULL DEFAULT '{}')")
    rows = pg.execute(f"SELECT count(*) FROM {s}.records").fetchone()[0]
    pg.execute(f"INSERT INTO {s}.state (view, built_at, rows, notes) VALUES ('records', %s, "
               "%s, %s) ON CONFLICT (view) DO UPDATE SET built_at = EXCLUDED.built_at, rows = "
               "EXCLUDED.rows, notes = EXCLUDED.notes",
               (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), rows,
                json.dumps({**notes, "marks": dict(got)})))


def refresh(dsn: str, schema: str | None = None, *, sweep: bool = False) -> dict[str, Any]:
    """Update the live tables in place, one transaction: records with a version recorded or
    a link changed since the last marks are read again; records removed from their source,
    newly excluded or found to be PLX's own documents since are dropped with their links.
    A record the store holds under a new row (deleted and written again: its ``rid`` moved)
    is dropped by its id before it is read again (11-18 live: the old row stayed and every
    refresh failed on the record id's unique index). ``sweep``: also every row whose store
    row is gone (a full scan, once a day: 2,516 such rows on 11-18)."""
    import psycopg
    s = schema or bv.SCHEMA
    with psycopg.connect(dsn, autocommit=True) as pg:
        _session(pg)
        (notes,) = pg.execute(f"SELECT notes FROM {s}.state WHERE view = 'records'").fetchone()
        since = (notes or {}).get("marks") or {}
        room(pg, 1)   # before the transaction: never waits holding its locks
        with pg.transaction():
            got = _marks(pg)
            lo_ver, lo_link = int(since.get("ver") or 0), int(since.get("link") or 0)
            pg.execute("DROP TABLE IF EXISTS pg_temp.gr_changed")
            pg.execute(
                "CREATE TEMP TABLE gr_changed AS SELECT DISTINCT o.row_no AS rid FROM "
                f"{ONT}.object_versions v JOIN {ONT}.objects o ON o.object_id = v.object_id "
                "WHERE v.ver > %s AND v.ver <= %s UNION SELECT o.row_no FROM "
                f"{ONT}.objects o WHERE o.row_no > %s", (lo_ver, got["ver"],
                                                        int(since.get("row") or 0)))
            pg.execute("DROP TABLE IF EXISTS pg_temp.gr_gone")
            pg.execute(
                "CREATE TEMP TABLE gr_gone AS WITH d AS (DELETE FROM " + s + ".records r "
                "WHERE r.rid IN (SELECT rid FROM gr_changed) "
                f"OR r.record_id IN (SELECT o.object_id FROM {ONT}.objects o WHERE o.row_no "
                "IN (SELECT rid FROM gr_changed)) "
                + (f"OR NOT EXISTS (SELECT 1 FROM {ONT}.objects o WHERE o.row_no = r.rid) "
                   if sweep else "")
                + f"OR EXISTS (SELECT 1 FROM {ONT}.people_excluded_objects p WHERE "
                "p.object_id = r.record_id) OR EXISTS (SELECT 1 FROM "
                f"{ONT}.own_issued_documents oi WHERE oi.object_id = r.record_id) OR "
                f"EXISTS (SELECT 1 FROM {ONT}.objects o WHERE o.row_no = r.rid AND "
                f"{removed_sql('o', _now())}) RETURNING r.rid, r.record_id, r.handle) SELECT * FROM d")
            gone = pg.execute("SELECT count(*) FROM gr_gone").fetchone()[0]
            _stage(pg, "o.row_no IN (SELECT rid FROM gr_changed) AND "
                   + types_sql(notes or {}), excluded_ready(pg))
            added = _insert(pg, s, "records")
            # links: every link of a record read again or dropped, and every link (from,
            # type, to) a new link row names, are dropped and read again from the store
            pg.execute("DROP TABLE IF EXISTS pg_temp.gr_lt, pg_temp.gr_lid")
            pg.execute(f"CREATE TEMP TABLE gr_lt AS SELECT DISTINCT from_object, link_type, "
                       f"to_object FROM {ONT}.link_rows WHERE row_no > %s AND row_no <= %s",
                       (lo_link, got["link"]))
            # a link the store no longer holds live at its tier (deleted or superseded with
            # no new row): dropped, and read again from whatever live rows it has left
            pg.execute(
                "INSERT INTO gr_lt SELECT l.from_id, l.link_type, l.to_id FROM "
                f"{s}.record_links l WHERE NOT EXISTS (SELECT 1 FROM {ONT}.link_rows r WHERE "
                "r.from_object = l.from_id AND r.link_type = l.link_type AND r.to_object = "
                "l.to_id AND r.superseded_at IS NULL AND r.tier = l.tier)")
            pg.execute("DROP TABLE IF EXISTS pg_temp.gr_ldel")
            pg.execute(
                "CREATE TEMP TABLE gr_ldel AS WITH d AS ("
                f"DELETE FROM {s}.record_links l WHERE l.from_rid IN (SELECT rid FROM "
                "gr_gone) OR l.to_rid IN (SELECT rid FROM gr_gone) OR EXISTS (SELECT 1 FROM "
                f"gr_lt t JOIN {s}.records f ON f.record_id = t.from_object JOIN {s}.records "
                "x ON x.record_id = t.to_object WHERE f.rid = l.from_rid AND x.rid = "
                "l.to_rid AND t.link_type = l.link_type) RETURNING l.handle) SELECT * FROM d")
            pg.execute(
                f"CREATE TEMP TABLE gr_lid AS SELECT l.link_id, l.link_type FROM "
                f"{ONT}.link_rows l JOIN gr_lt t ON t.from_object = l.from_object AND "
                "t.link_type = l.link_type AND t.to_object = l.to_object UNION SELECT "
                f"l.link_id, l.link_type FROM {ONT}.link_rows l JOIN gr_gone g ON "
                "g.record_id = l.from_object UNION SELECT l.link_id, l.link_type FROM "
                f"{ONT}.link_rows l JOIN gr_gone g ON g.record_id = l.to_object")
            linked = _link_insert(pg, s, "records", "record_links",
                                  "(l.link_id, l.link_type) IN (SELECT link_id, link_type "
                                  "FROM gr_lid)")
            _state(pg, s, got, {**{k: v for k, v in (notes or {}).items() if k != "marks"},
                                "refreshed": {"dropped": gone, "read": added,
                                              "links_read": linked}})
            door_side(pg, s, {
                "records": "SELECT handle FROM gr_gone UNION SELECT handle FROM "
                           f"{s}.records WHERE rid IN (SELECT rid FROM gr_changed)",
                "record_links": "SELECT handle FROM gr_ldel UNION SELECT 'link:' || "
                                "from_rid || '/' || link_type || '/' || to_rid FROM gr_l"})
            gv.mark_tables(pg, s)
            from operating.pg import door
            door.follow(pg, s, "records")
            door.follow(pg, s, "record_links")
            door._bump(pg, s)
    return {"dropped": gone, "read": added, "links_read": linked, "marks": got}


def follow(dsn: str, day: dt.date, schema: str | None = None) -> dict[str, Any]:
    """The business views' refresh (``business_views.refresh``) keeps the generic door
    current: the catalog again on a new day (new types, fields and counts), then
    ``records``/``record_links`` in place by what changed. Nothing before they were first
    built (``build``)."""
    import psycopg
    s = schema or bv.SCHEMA
    with psycopg.connect(dsn, autocommit=True) as pg:
        if pg.execute("SELECT to_regclass(%s)", (f"{s}.state",)).fetchone()[0] is None:
            return {}
        built = dict(pg.execute(f"SELECT view, built_at FROM {s}.state WHERE view IN "
                                "('records', 'record_types')").fetchall())
    if "records" not in built:
        return {}
    out: dict[str, Any] = {}
    new_day = not str(built.get("record_types") or "").startswith(day.isoformat())
    if new_day:
        from ontology.schema import load_schema
        from operating.ontology_tools_identity import SchemaView
        out["catalog"] = gv.build_catalog(dsn, SchemaView.from_compiled(load_schema()), s)
    out["records"] = refresh(dsn, s, sweep=new_day)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.generic_records")
    ap.add_argument("command", choices=("build", "refresh"))
    ap.add_argument("--dsn")
    ap.add_argument("--schema")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--uncovered", action="store_true",
                    help="build: only the types no curated table reads, no Rel* mirrors")
    ap.add_argument("--mirrors", action="store_true",
                    help="build --uncovered: the Rel* raw relational mirrors too")
    ap.add_argument("--skip-prefix", action="append", default=[],
                    help="build: leave out the types whose name starts so (e.g. RelSage)")
    args = ap.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    if args.command == "build":
        from ontology.schema import load_schema
        from operating.ontology_tools_identity import SchemaView
        gv.build_catalog(dsn, SchemaView.from_compiled(load_schema()), args.schema)
        out = build(dsn, args.schema, args.workers, uncovered=args.uncovered,
                    mirrors=args.mirrors, skip_prefixes=args.skip_prefix)
    else:
        out = refresh(dsn, args.schema)
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
