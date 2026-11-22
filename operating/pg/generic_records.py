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
  * a field whose first-ranked cell is marked beyond the record's own markings is withheld
    from a reader lacking that marking, never the record: ``fields`` holds every field,
    ``fields_open`` only the fields marked no more than the record, and ``col_class`` marks
    ``fields`` with the union of those cells' markings (``door.view_ddl``: a reader not
    holding it reads ``fields_open`` in its place, with ``fields`` in ``withheld``). Owner
    rule 2026-11-17: staff read every business record, finance and pii values withheld (a
    Sage vendor's balances are finance-marked cells: the vendor was hidden whole, 6.58M of
    the 8.03M Sage records, 2026-11-19); the label and record date are read from unmarked
    cells;
  * ``ver`` / ``biz``: the record's newest current version and whether its system is a
    business system. The people exclusion decides versions only up to its ``covered_ver``:
    the views serve a record of any other system only once ``ver`` is at or below it (the
    communications tables' rule; no decided set: none), so a record read out of mail an
    excluded person sent is never shown before the set decided it;
  * ``ai_ok``: false when any cell of the current version, the handle, binding or native
    key names Freightcom (the owner's no-AI rule; regex once here, never per query).
  The excluded people's own records (``ontology.people_excluded_objects``) are the
  owner's alone (``OWNER_KEY`` among their keys); no set built
  for the store = no record of a non-business system served at all.
  Never in the table: records whose source no longer holds them, PLX's own issued
  documents (``own_issued_documents``) and
  the types ``LEFT_OUT`` (mail, chat messages, calendar events, transcripts: the comms
  tables'; attachments and files: the documents').

``record_links``: (from_rid, link_type, to_rid, tier) for live links at a shown tier between
two records of ``records`` (no never-authority end); its ``access_class`` is the union of
both ends' patterns, the link's own markings and, when the link was learned from anything
but a business system or its from record (evidence handles), ``FULL_KEY``: one class test
says both ends and the link are the reader's. Its ``ver`` is the newest of both ends' and its
evidence's, ``biz`` only when both ends are and no such evidence exists, ``ev_ids`` the
evidence's records (lineage ``read_ids``: evidence of the people set makes the link the
owner's alone, ``OWNER_KEY``).

  python3 -m operating.pg.generic_records build [--schema S] [--workers N]
  python3 -m operating.pg.generic_records refresh [--schema S]
  python3 -m operating.pg.generic_records indexes [--schema S]   (any the live tables lack)
  python3 -m operating.pg.generic_records labels [--schema S]    (label the served rows again)
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import re
import sys
import time
from typing import Any, Mapping, Sequence

from operating.pg import access_classes as ac
from operating.chunking import headers
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
# The comms tables own these (operating.pg.business_comms and its successors), and the
# attachments and files are the documents' (search reads their text). Every other type is a
# record here: Teams chats and channels, channel messages, meeting objects and container
# members (no table of their own) are not a business system's, so a full reader's only
# (``FULL_KEY``) and served once the people exclusion decided their version.
LEFT_OUT = ("Email", "Attachment", "SharePointFile", "CalendarEvent", "MeetingTranscript",
            "TeamsChatMessage")
UNMARKED = "[]"
# ``records.fields``'s column class (``col_classes.marks``): the union of the markings of the
# record's cells marked beyond it, as one marking set (``ontology.store_ddl.MARKING_SETS``'
# spelling: sorted, quoted, no spaces), or nothing marked
COL_MARKS_SQL = (
    "CASE WHEN cardinality(COALESCE(f.col_marks, '{}'::text[])) = 0 THEN '{}'::jsonb ELSE "
    "jsonb_build_object('fields', (SELECT '[' || string_agg('\"' || m || '\"', ',' ORDER BY m) "
    "|| ']' FROM (SELECT DISTINCT jsonb_array_elements_text(x::jsonb) AS m FROM "
    "unnest(f.col_marks) x) q)) END")
# every link type of ``record_links`` by its index, one step per type (a loose scan)
LINK_TYPES_SQL = ("WITH RECURSIVE t(x) AS ((SELECT link_type FROM {s}.record_links ORDER BY 1 "
                  "LIMIT 1) UNION ALL SELECT (SELECT l.link_type FROM {s}.record_links l WHERE "
                  "l.link_type > t.x ORDER BY 1 LIMIT 1) FROM t WHERE t.x IS NOT NULL) "
                  "SELECT x FROM t WHERE x IS NOT NULL")
# the column a reader not holding a masked column's marking reads in its place
FALLBACK = {"fields": "fields_open"}

RECORDS_DDL = """CREATE TABLE {s}.{t} (
    rid bigint PRIMARY KEY, record_id text NOT NULL, type text NOT NULL, system text NOT NULL,
    source text NOT NULL, handle text NOT NULL, label text, record_date text,
    first_seen text, changed_at text, access_class integer NOT NULL,
    col_class integer NOT NULL DEFAULT 0, ai_ok boolean NOT NULL, link_ok boolean NOT NULL,
    fields jsonb NOT NULL, ver bigint NOT NULL, biz boolean NOT NULL,
    fields_open jsonb NOT NULL DEFAULT '{{}}')"""
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
        "(SELECT array_agg(k ORDER BY k) FROM unnest("
        f"CASE WHEN split_part(v.l1_handle, '://', 1) = ANY({business}) THEN "
        f"ARRAY[o.audience_key] ELSE ARRAY[o.audience_key, {_lit(ac.FULL_KEY)}] END "
        # a record of the people set: the owner's alone
        f"|| CASE WHEN EXISTS (SELECT 1 FROM {ONT}.people_excluded_objects peo WHERE "
        f"peo.object_id = o.object_id) THEN ARRAY[{_lit(ac.OWNER_KEY)}] ELSE '{{}}'::text[] "
        "END) k) AS keys, "
        f"ARRAY(SELECT DISTINCT w.markings FROM {ONT}.object_versions w WHERE w.object_id = "
        "o.object_id AND w.superseded_at IS NULL ORDER BY 1) AS marks, "
        f"NOT (COALESCE(v.lineage, '') = ANY({_arr(bv.NEVER_AUTHORITY_LINEAGES)})) AS link_ok, "
        f"NOT (concat_ws(' ', v.l1_handle, o.binding_id, o.native_key) ~* {pat}) AS ai_ok "
        f"FROM {ONT}.objects o CROSS JOIN LATERAL (SELECT * FROM {ONT}.object_versions v "
        "WHERE v.object_id = o.object_id AND v.superseded_at IS NULL ORDER BY v.ver DESC "
        f"LIMIT 1) v WHERE ({where}) AND NOT (o.object_type = ANY({_arr(LEFT_OUT)})) "
        f"AND NOT {removed_sql('o', _now())} "
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
        # the fields a reader of the record reads whatever else it holds (``fields_open``)
        "COALESCE(jsonb_object_agg(property, CASE WHEN value_type = ANY(" + nums + ") AND "
        "value_num IS NOT NULL THEN to_jsonb(value_num) ELSE to_jsonb(value) END) FILTER "
        "(WHERE free), '{}') || COALESCE(jsonb_object_agg(property || '_currency', currency) "
        "FILTER (WHERE free AND currency IS NOT NULL), '{}') || COALESCE(jsonb_object_agg("
        "property || '_unit', unit) FILTER (WHERE free AND unit IS NOT NULL), '{}') "
        "AS fields_open, "
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


# A value that is not a name never becomes a label (11-21: "tbd", "NA", a lone quote, a date
# read as a title). By shape, in three strengths by where the text came from:
# * true junk (``junk_sql``): empty, no letter or digit, a generic placeholder; applies to
#   every source, identifiers included (the label column's own clearing uses only this);
# * a display field (``display_valid_sql``): also under two characters; a date is allowed
#   (a record's own number may read as one: SO "17-06-2025", PO "12/11/2018");
# * free text (title, name, a list item's JSON Title; ``free_valid_sql``): also a date.
# A record's own identifier is its name whatever it looks like (lot "09/08/22", CAS
# "9005-65-6") and a one-character code ("X", "9") is kept (11-21 review).
PLACEHOLDERS = ("tbd", "tba", "tbc", "n/a", "na", "n.a.", "none", "null", "nil", "unknown",
                "unspecified", "undefined", "not applicable", "no data", "missing")
_MONTH = ("(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|"
          "sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)")
_DATE_SHAPES = ("^[0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2}([T ][0-9:.+Z-]*)?$",
                "^[0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4}$",
                f"^{_MONTH}[.]? [0-9]{{1,2}},? [0-9]{{2,4}}$")


def junk_sql(e: str) -> str:
    """SQL: whether text ``e`` is true junk: empty, no letter or digit, a placeholder, or
    numerically zero ("0", "00.0": unset; 107k live RelSageApIbd ``transnbr`` = 0 rows, whose
    ``cntbtch`` holds the real batch, so the record's next label field names it)."""
    return (f"(btrim({e}) = '' OR {e} !~ '[[:alnum:]]' OR lower(btrim({e})) = "
            f"ANY({_arr(PLACEHOLDERS)}) OR btrim({e}) ~ '^0+([.]0+)?$')")


def _date_sql(e: str) -> str:
    return "(" + " OR ".join(f"lower(btrim({e})) ~ {_lit(d.lower())}" for d in _DATE_SHAPES) + ")"


def valid_sql(e: str) -> str:
    """SQL: ``e`` trimmed, NULL when it is true junk (an identifier's rule)."""
    return f"(CASE WHEN NOT {junk_sql(e)} THEN btrim({e}) END)"


def display_valid_sql(e: str) -> str:
    return f"(CASE WHEN NOT {junk_sql(e)} AND length(btrim({e})) >= 2 THEN btrim({e}) END)"


def free_valid_sql(e: str) -> str:
    return (f"(CASE WHEN NOT {junk_sql(e)} AND length(btrim({e})) >= 2 AND NOT {_date_sql(e)} "
            f"THEN btrim({e}) END)")


def _first_unmarked(col: str, expr: str, cond: str) -> str:
    """``expr`` over the record's first-ranked unmarked cell of the first of the catalog's
    ``col`` fields (in their declared order) that has one meeting ``cond``."""
    return (f"(SELECT {expr} FROM gr_c c WHERE c.rid = b.rid AND c.free "
            f"AND c.value IS NOT NULL AND c.property = ANY(rt.{col}) AND "
            f"{cond} ORDER BY array_position(rt.{col}, c.property) LIMIT 1)")


def _insert(pg: Any, schema: str, table: str) -> int:
    """Classes for the staged patterns, then the staged rows into ``<schema>.<table>``."""
    # the record's class is its own markings; a field marked beyond them marks the column
    # (``fields``: the union of those cells' markings, one marking set), so a reader lacking
    # it reads the record with ``fields_open`` in its place
    pg.execute("DROP TABLE IF EXISTS pg_temp.gr_k")
    pg.execute("CREATE TEMP TABLE gr_k AS SELECT b.rid, b.keys, ARRAY(SELECT DISTINCT m FROM "
               f"unnest(b.marks || ARRAY[{_lit(UNMARKED)}]) m ORDER BY 1) AS marks, "
               f"{COL_MARKS_SQL} AS cms FROM gr_b b LEFT JOIN gr_f f ON f.rid = b.rid")
    with pg.transaction():   # short: a build's parallel batches only wait on the ids
        ac.assign(pg, schema, "SELECT keys, marks FROM gr_k")
        ac.assign_cols(pg, schema, "SELECT DISTINCT cms FROM gr_k")
    label = _first_unmarked("label_fields", display_valid_sql("c.value"),
                            f"{display_valid_sql('c.value')} IS NOT NULL")
    record_date = _first_unmarked("date_fields", DAY, f"({DAY}) IS NOT NULL")
    cur = pg.execute(
        f"INSERT INTO {schema}.{table} (rid, record_id, type, system, source, handle, label, "
        "record_date, first_seen, changed_at, access_class, col_class, ai_ok, link_ok, "
        "fields, fields_open, ver, biz) SELECT b.rid, b.object_id, b.object_type, "
        f"{gv.system_sql('b.binding_id')}, b.binding_id, b.l1_handle, {label}, "
        f"{record_date}, b.first_recorded_at, b.recorded_at, k.id, cc.id, "
        "b.ai_ok AND NOT COALESCE(f.fc, false), b.link_ok, COALESCE(f.fields, '{}'), "
        "COALESCE(f.fields_open, '{}'), b.ver, b.biz "
        f"FROM gr_b b JOIN gr_k gk ON gk.rid = b.rid JOIN {schema}.access_classes k ON "
        "k.audience_keys = gk.keys AND k.rec_marks = gk.marks AND k.doc_marks = '{}' AND "
        f"NOT k.blocked JOIN {schema}.col_classes cc ON cc.marks = gk.cms AND "
        "cc.needs = '{}'::jsonb LEFT JOIN gr_f f ON f.rid = b.rid "
        f"LEFT JOIN {schema}.record_types rt ON rt.type = b.object_type")
    _labels(pg, schema, table, "r.rid IN (SELECT rid FROM gr_b)")
    return cur.rowcount


# A record no display field names (``record_types.label_fields`` empty, or none of them
# holding a value: 1.64M of 10.3M live on 11-21, mostly lines) is labelled by its own title
# or name field as any reader of the record reads it (``fields_open``: 4,490 live, e.g.
# expense receipts), else by the Title of a field holding its columns as one JSON object
# (a SharePoint list item's ``fields``), else by its first current, unmarked business
# identifier with the kind's word ("PO5267", "Lot 390373", "Tracking 1Z..."), so
# ``label ILIKE`` and plx.search's records leg find it by its name or number.
# Never a technical identifier (``chunking.headers.TECHNICAL``: FileMaker row ids, BC
# system ids, hashes: 11-21 review, 5,669 FileMaker POs would have read "5292" for PO5267),
# and a carrier's reference (a tracking number) only when nothing else names the record.
CARRIER_KINDS = ("tracking_number",)
_ID_WORDS = "CASE i.identifier_kind " + " ".join(
    f"WHEN {_lit(k)} THEN {_lit(w)}" for k, w in sorted(headers._LABELS.items())) + " END"
_ID_LABEL = (
    "(SELECT CASE WHEN w.word IS NULL THEN w.raw WHEN w.word !~ ' ' AND w.raw ~* ('^' || "
    "w.word || '[-#[:space:]]?[0-9]') THEN w.raw ELSE w.word || ' ' || w.raw END FROM ("
    f"SELECT btrim(i.raw) AS raw, {_ID_WORDS} AS word FROM {ONT}.identifiers i WHERE "
    "i.object_id = r.record_id AND i.superseded_at IS NULL AND "
    f"i.markings = {_lit(UNMARKED)} AND {valid_sql('i.raw')} IS NOT NULL AND NOT "
    f"(i.identifier_kind = ANY({_arr(sorted(headers.TECHNICAL))})) ORDER BY "
    f"(i.identifier_kind = ANY({_arr(CARRIER_KINDS)})), (i.role = 'primary') DESC, "
    "i.identifier_kind, i.normalized LIMIT 1) w)")
# a title that is only a number (a list's date column exported as an Excel serial) names
# nothing
_TITLE = free_valid_sql("COALESCE(x.j->>'Title', x.j->>'title')")
_JSON_TITLE = (
    f"(SELECT {_TITLE} FROM "
    "jsonb_each(r.fields_open) e(k, v) CROSS JOIN LATERAL (SELECT CASE WHEN "
    "jsonb_typeof(e.v) = 'string' AND left(ltrim(e.v #>> '{}'), 1) = '{' AND "
    "pg_input_is_valid(e.v #>> '{}', 'jsonb') THEN (e.v #>> '{}')::jsonb WHEN "
    "jsonb_typeof(e.v) = 'object' THEN e.v END AS j) x WHERE jsonb_typeof(x.j) = 'object' "
    f"AND {_TITLE} IS NOT NULL AND {_TITLE} !~ '^[0-9]+([.][0-9]+)?$' ORDER BY e.k LIMIT 1)")
# A Sage shipment or receipt header is named by its own number ("Shipment SHI0001",
# "Receipt RCP0042"; live 11-21: 86,129 Shipment and 6,517 Receipt records, none labelled):
# the type's number property (live names: ``fields_open`` sage_shipment_number /
# sage_receipt_number).
NUMBER_FIELDS = {"Shipment": ("sage_shipment_number", "Shipment"),
                 "Receipt": ("sage_receipt_number", "Receipt")}


def _own_number() -> str:
    arms = []
    for typ, (col, word) in sorted(NUMBER_FIELDS.items()):
        n = valid_sql(f"r.fields_open->>{_lit(col)}")
        arms.append(f"WHEN {_lit(typ)} THEN (CASE WHEN {n} IS NULL THEN NULL WHEN {n} ~* "
                    f"{_lit('^' + word)} THEN {n} ELSE {_lit(word + ' ')} || {n} END)")
    return "(CASE r.type " + " ".join(arms) + " END)"


_OWN_TITLE = free_valid_sql("r.fields_open->>'title'")
_OWN_NAME = free_valid_sql("r.fields_open->>'name'")
# the record's display fields as the catalog declares them (``record_types.label_fields``,
# in order), the first holding a name: what a build writes, read again from ``fields_open``
_DISPLAY = (f"(SELECT {display_valid_sql('r.fields_open->>u.p')} FROM @SCHEMA@.record_types rt "
            "CROSS JOIN LATERAL unnest(rt.label_fields) WITH ORDINALITY u(p, n) WHERE "
            f"rt.type = r.type AND {display_valid_sql('r.fields_open->>u.p')} IS NOT NULL "
            "ORDER BY u.n LIMIT 1)")
LABEL_SQL = (f"COALESCE({_OWN_TITLE}, {_OWN_NAME}, {_DISPLAY}, {_JSON_TITLE}, "
             f"{_own_number()}, {_ID_LABEL})")


def _labels(pg: Any, schema: str, table: str, where: str) -> int:
    """Label the rows of ``where`` (over ``r``) that have none (``LABEL_SQL``, read once
    per row)."""
    return pg.execute(f"UPDATE {schema}.{table} t SET label = x.l FROM (SELECT r.rid, "
                      f"{LABEL_SQL.replace('@SCHEMA@', schema)} AS l FROM {schema}.{table} r WHERE ({where}) AND "
                      "r.label IS NULL) x WHERE t.rid = x.rid AND x.l IS NOT NULL").rowcount


def relabel(dsn: str, schema: str | None = None, *, step: int = BATCH) -> dict[str, int]:
    """The served rows with no label labelled as a build labels them now (``_labels``), in
    place: one short transaction per ``step`` rows of ``rid`` (row locks only; the refresh
    goes on). A padded label is left as it is (11-21 live: 6.85M rows, a rewrite of most of
    the table): a build or refresh writes the row trimmed."""
    import psycopg
    s = schema or bv.SCHEMA
    out = {"cleared": 0, "labelled": 0}
    with psycopg.connect(dsn, autocommit=True) as pg:
        _session(pg)
        lo, hi = pg.execute(f"SELECT coalesce(min(rid), 0), coalesce(max(rid), -1) FROM "
                            f"{s}.records").fetchone()
        from operating.pg import wal_budget
        budget = wal_budget.Budget.for_conn(pg, job="records_relabel")
        longest = wal_budget.max_wait_seconds()
        for start in range(lo, hi + 1, step):
            budget.wait(max_seconds=longest)   # between the steps' transactions
            with pg.transaction():
                out["cleared"] += pg.execute(
                    f"UPDATE {s}.records SET label = NULL WHERE rid >= {int(start)} AND rid < "
                    f"{int(start + step)} AND label IS NOT NULL AND ({junk_sql('label')} OR "
                    f"(type = 'SharePointListItem' AND {_date_sql('label')}))").rowcount
                out["labelled"] += _labels(pg, s, "records", f"r.rid >= {int(start)} AND "
                                           f"r.rid < {int(start + step)}")
    return out


def _link_insert(pg: Any, schema: str, records: str, table: str, where: str) -> int:
    """Live links of ``where`` (over ``l``, ``ontology.link_rows``) between two rows of
    ``records``, each with its class (both ends, its markings, mail evidence), its version
    and its evidence records.

    Evidence (``evidence_handles``) other than a business system's or the from record's
    own handle (mail, Teams, SharePoint, Planner...) makes the link a full reader's only
    (``FULL_KEY``) and not a business link (``biz`` false: the views serve it only once the
    people exclusion decided its newest version, ``ver``); its records are the link's
    ``ev_ids`` (``lineage.read_ids``: the exclusion makes the link the owner's alone,
    ``door.exclude``). A link with evidence from an excluded person's record is the
    owner's alone (``OWNER_KEY``); one with evidence the store cannot name as a record
    (undecidable) is left out (fail closed); a page or
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
        f"ARRAY[{_lit(ac.FULL_KEY)}] ELSE '{{}}'::text[] END "
        # evidence from the people set: the owner's alone
        f"|| CASE WHEN EXISTS (SELECT 1 FROM unnest(e.ids) i JOIN {ONT}.people_excluded_objects "
        f"peo ON peo.object_id = i) THEN ARRAY[{_lit(ac.OWNER_KEY)}] ELSE '{{}}'::text[] END) k "
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
        "AND f.link_ok AND t.link_ok AND e.lost = 0")
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
         sleep: Any = None, free: Any = None, wal: bool = True) -> None:
    """Before a batch: wait until the database's disk has the floor plus ``workers``
    batches' temp tables free; still short after ``wait`` seconds (or its free space
    unreadable): raise (the batch's transaction never starts; a build keeps the served
    tables). ``wal``: also wait for the WAL budget (``wal_budget``, up to
    ``PLX_WAL_MAX_WAIT_S``); the in-place refresh (the delta) passes False."""
    import time as _time
    from operating.pg import business_comms
    sleep = sleep or _time.sleep
    if wal:
        from operating.pg import wal_budget
        wal_budget.Budget.for_conn(pg, job="records_build", check_seconds=0).wait(
            max_seconds=wal_budget.max_wait_seconds(), sleep=sleep)
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


def comms_types() -> list[str]:
    """The types a communications table holds one row per record of (``View.fill``)."""
    return sorted({v.reads[0] for v in bv.VIEWS.values()
                   if v.reads and getattr(v, "fill", None) and getattr(v, "covers", True)})


def type_choice(uncovered: bool, mirrors: bool,
                skip_prefixes: Sequence[str] = (), business: bool = False) -> dict[str, Any]:
    """A build's type choice (kept in its notes: the refresh follows the same types).
    ``skip_prefixes``: type name prefixes left out besides (``RelSage``: Sage's 7.5M mirror
    records, too many for the disk free at the time). A prefix is letters and digits only.
    ``business`` with ``uncovered``: the curated tables' own types too (each system's copy
    its own record, with that system's own words: a curated row folds the copies into one
    status by rule, so "open in BC" by BC's own word was read nowhere), only the
    communications tables' types left out."""
    bad = [p for p in skip_prefixes if not (isinstance(p, str) and p.isalnum())]
    if bad:
        raise ValueError(f"a type prefix is letters and digits only: {bad!r}")
    skip = (comms_types() if business else covered_types()) if uncovered else []
    return {"skip_types": skip,
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


def narrowed(served: Mapping[str, Any], choice: Mapping[str, Any]) -> list[str]:
    """What a build's type choice (``type_choice``) leaves out that the served build's
    (its kept ``notes``) held: types, ``Rel*`` (the mirrors) or ``<prefix>*``. A rebuild
    that narrows the served records takes those types out of plx.search and the door
    without a word (11-19 live: a rebuild with ``--uncovered`` and without ``--business``
    left out every customer, product, order, invoice, supplier, PO and batch). Empty:
    nothing the served records hold is lost."""
    old_skip = set(served.get("skip_types") or ())
    old_mirrors = bool(served.get("skip_mirrors"))
    old_prefixes = [p for p in served.get("skip_prefixes") or () if isinstance(p, str)]
    new_mirrors = bool(choice.get("skip_mirrors"))

    def held(name: str) -> bool:
        return not ((old_mirrors and name.startswith("Rel"))
                    or any(name.startswith(p) for p in old_prefixes))
    out = sorted(t for t in set(choice.get("skip_types") or ()) - old_skip if held(t))
    if new_mirrors and not old_mirrors:
        out.append("Rel*")
    out += sorted(f"{p}*" for p in set(choice.get("skip_prefixes") or ())
                  if held(p) and not (new_mirrors and p.startswith("Rel")))
    return out


def served_notes(pg: Any, s: str) -> dict[str, Any] | None:
    """The served ``records`` build's kept notes (its type choice); ``None`` before a
    first build."""
    if not pg.execute("SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL",
                      (f"{s}.state", f"{s}.records")).fetchone()[0]:
        return None
    row = pg.execute(f"SELECT notes FROM {s}.state WHERE view = 'records'").fetchone()
    if row is None:
        return None
    notes = row[0]
    return dict(json.loads(notes) if isinstance(notes, str) else notes or {})


def door_side(pg: Any, s: str, touched: Mapping[str, str] | None = None,
              tables: Mapping[str, str] | None = None) -> None:
    """The door's side tables for the generic tables: their ``lineage`` (each row's own
    records: the people exclusion drops a row naming one, ``door.exclude``; a handle two
    records share names both) and ``class_counts``. ``touched``: per view, a query of the
    handles whose rows changed (a refresh: only those are decided again); else whole. In
    the caller's transaction. ``tables``: per view, the table the rows are read from when it
    is not the served one (a build's own ``_new`` table, decided before the swap takes its
    locks)."""
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
                   f"FROM {s}.{(tables or {}).get(view, view)} LEFT JOIN LATERAL "
                   f"unnest({read}) y ON true, unnest({ids}) x {only} GROUP BY handle",
                   (view,))
        door.recount(pg, s, view, (tables or {}).get(view))


def build(dsn: str, schema: str | None = None, workers: int = 4, *,
          uncovered: bool = False, mirrors: bool = False,
          skip_prefixes: Sequence[str] = (), business: bool = False,
          narrow: bool = False) -> dict[str, Any]:
    """Build ``records`` and ``record_links`` beside the live ones, index them, then swap
    them in with the guarded views re-made (one transaction: readers see old or new).
    ``uncovered``: the types no curated table holds; ``mirrors`` with it: the raw relational
    mirrors (``Rel...``, 184 types and 7.8M records on 11-18, Sage's history among them)
    too, else they are left out (a first build that must stay small); ``business`` with it:
    the curated tables' own types too (``type_choice``). A choice that leaves out types the
    served build holds (``narrowed``) is refused before anything is dropped or written,
    unless ``narrow``."""
    import psycopg
    s = schema or bv.SCHEMA
    started = time.monotonic()
    choice = type_choice(uncovered, mirrors, skip_prefixes, business)
    which = types_sql(choice)
    with psycopg.connect(dsn, autocommit=True) as pg:
        served = served_notes(pg, s)
        lost = narrowed(served, choice) if served is not None else []
        if lost and not narrow:
            raise ValueError("this build leaves out types the served records hold: "
                             f"{', '.join(lost)} (same types: the served build's choice, e.g. "
                             "--uncovered --mirrors --business; on purpose: --narrow)")
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


# The served tables' indexes (besides the record id's unique one), as (table, method, key):
# a build makes them on its new tables, ``ensure_indexes`` adds any the live tables lack.
# ``record_links`` is read by the text ids the guarded view shows (``from_id``/``to_id``: a
# reader's filter or a join to ``records.record_id``); without them every such read scanned
# its link type or all 25M links (11-21 live: 45-58 s, a records join timed out at 60 s).
INDEXES = (("records", "btree", "type, access_class) INCLUDE (ai_ok, record_id"),
           ("records", "btree", "handle"), ("records", "btree", "type, record_date"),
           ("records", "btree", "source"),
           # a record found by its name or number (``label ILIKE '%...%'``) without a scan
           ("records", "gin", "label gin_trgm_ops"),
           # the search box's number lookup (``page_reads.FIND_SQL``): a label equal to a
           # number or starting with one, by ``texteq`` / ``text_pattern_ge`` (leakproof, so
           # the guarded view lets them reach this index; ``lower()`` could not: a scan of
           # every shipment's row, 410 ms)
           ("records", "btree", "type, label text_pattern_ops"),
           ("record_links", "btree", "from_rid, link_type"),
           ("record_links", "btree", "to_rid, link_type"),
           ("record_links", "btree", "link_type"),
           ("record_links", "btree", "from_id, link_type"),
           ("record_links", "btree", "to_id, link_type"))


def _trgm(pg: Any) -> bool:
    return pg.execute("SELECT 1 FROM pg_opclass WHERE opcname = 'gin_trgm_ops'").fetchone() \
        is not None


def index(pg: Any, s: str, records: str, links: str) -> None:
    names = {"records": records, "record_links": links}
    trgm = _trgm(pg)
    for table, method, key in INDEXES:
        if "gin_trgm_ops" in key and not trgm:
            continue
        pg.execute(f"CREATE INDEX ON {s}.{names[table]} USING {method} ({key})")
    pg.execute(f"ANALYZE {s}.{records}")
    pg.execute(f"ANALYZE {s}.{links}")


def _index_shape(method: str, key: str) -> str:
    """An index's method and key as ``pg_get_indexdef`` spells them, spaces dropped."""
    return re.sub(r"\s+", "", f"USING {method} ({key})").lower()


LOCK_TIMEOUT_S = 600


def ensure_indexes(dsn: str, schema: str | None = None, *, free: Any = None,
                   wait: float = ROOM_WAIT_SECONDS) -> list[str]:
    """Make each ``INDEXES`` entry the live tables lack, ``CONCURRENTLY`` (reads and the
    refresh's writes go on), one at a time, each after the disk check a batch makes
    (``room``: the floor plus one batch free). An invalid index of the same shape (a
    concurrent build that failed) is dropped and made again. Returns what it made; nothing
    before ``records`` exists."""
    import psycopg
    s = schema or bv.SCHEMA
    made: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as pg:
        # a concurrent build waits for every older transaction: bounded, an index it
        # could not finish is left invalid and made again on the next run
        pg.execute(f"SET lock_timeout = '{int(LOCK_TIMEOUT_S)}s'")
        trgm = _trgm(pg)
        for table, method, key in INDEXES:
            if "gin_trgm_ops" in key and not trgm:
                continue
            if pg.execute("SELECT to_regclass(%s)", (f"{s}.{table}",)).fetchone()[0] is None:
                continue
            want = _index_shape(method, key)
            have = [(name, valid) for name, valid, ddl in pg.execute(
                "SELECT c.relname, i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = %s::regclass",
                (f"{s}.{table}",)).fetchall()
                if re.sub(r"\s+", "", ddl).lower().endswith(want)]
            if any(valid for _, valid in have):
                continue
            for name, _ in have:
                pg.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {s}."{name}"')
            room(pg, 1, free=free, wait=wait)
            pg.execute(f"CREATE INDEX CONCURRENTLY ON {s}.{table} USING {method} ({key})")
            made.append(f"{table} ({key})")
    return made


def swap(pg: Any, s: str, got: Mapping[str, int], notes: Mapping[str, Any]) -> None:
    """The new tables in place of the old in one transaction, the guarded views re-made
    over them (a view follows its table, so it is dropped and made again). Everything heavy
    (each table's lineage, its class counts, the state row: 11-19 live, 22 minutes over
    ~36M rows) is done first against the new tables, none of it locking a table a reader
    has open; the lock-taking statements then run as ``swap_lock.swap_in`` does for every
    other view (a short lock wait, tried again), so a reader never queues behind the swap."""
    from operating.pg import swap_lock
    with pg.transaction():
        dst = gv.sql_views.guarded_schema(s)
        door_side(pg, s, tables={"records": "records_new", "record_links": "record_links_new"})
        _state(pg, s, got, notes, "records_new")

        def steps() -> None:
            if pg.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is not None:
                for name in ("record_links", "records"):
                    gv.sql_views.drop(pg, name, s)
            for old, new in (("records", "records_new"), ("record_links", "record_links_new")):
                pg.execute(f"DROP TABLE IF EXISTS {s}.{old}")
                pg.execute(f"ALTER TABLE {s}.{new} RENAME TO {old}")
            if pg.execute("SELECT to_regclass(%s)", (f"{s}.record_types",)).fetchone()[0]:
                gv.mark_tables(pg, s)
            if pg.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is not None:
                gv.make_views(pg, s)
        swap_lock.swap_in(pg, steps)


def _state(pg: Any, s: str, got: Mapping[str, int], notes: Mapping[str, Any],
           table: str = "records") -> None:
    pg.execute(f"CREATE TABLE IF NOT EXISTS {s}.state (view text PRIMARY KEY, "
               "built_at text NOT NULL, digest text, rows bigint NOT NULL, "
               "seconds double precision, notes jsonb NOT NULL DEFAULT '{}')")
    rows = pg.execute(f"SELECT count(*) FROM {s}.{table}").fetchone()[0]
    pg.execute(f"INSERT INTO {s}.state (view, built_at, rows, notes) VALUES ('records', %s, "
               "%s, %s) ON CONFLICT (view) DO UPDATE SET built_at = EXCLUDED.built_at, rows = "
               "EXCLUDED.rows, notes = EXCLUDED.notes",
               (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), rows,
                json.dumps({**notes, "marks": dict(got)})))


def _probe_plan(pg: Any, *temps: str | None) -> None:
    """Keep the store's links read by index probes (11-20 live: from 12:54 PM ET every
    refresh failed past ``temp_file_limit``). The temp tables are analysed first (a table
    made a moment ago has no statistics, so the planner merge-sorted all 45M live link rows
    against it), and hash and merge joins are off for the transaction: the planner still
    judged a hash join over every link partition cheaper than 11k index probes, and its
    union then spilled 10 GB. Called with no table to turn them back on."""
    on = "on" if not [t for t in temps if t] else "off"
    for t in temps:
        if t:
            pg.execute(f"ANALYZE {t}")
    pg.execute(f"SET LOCAL enable_hashjoin = {on}")
    pg.execute(f"SET LOCAL enable_mergejoin = {on}")


DEAD_WORKERS = 8


def _dead_links(dsn: str, s: str, workers: int = DEAD_WORKERS) -> list[tuple[str, str, str]]:
    """The served links (from, type, to) the store no longer holds live at their tier, found
    one link type at a time on ``workers`` read-only connections before the refresh's
    transaction opens (it held 164 s of the transaction on 17-25M links, one connection).
    The store's links are partitioned by type, so each check is an index probe per link in
    that type's partition; over the whole table the planner hashed all 17M links against all
    live link rows and spilled past temp_file_limit (10 GB) on every refresh once Sage's
    links were served (2026-11-19 12:37 AM ET on: the records step failed each run). A link
    that dies after this read is found by the next refresh; one found here but live again by
    the transaction is read again from the store, so it is served either way."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as pg:
        types = [t for (t,) in pg.execute(LINK_TYPES_SQL.format(s=s)).fetchall()]

    def check(chunk: Sequence[str]) -> list[tuple[str, str, str]]:
        found: list[tuple[str, str, str]] = []
        with psycopg.connect(dsn, autocommit=True) as pg:
            pg.execute("SET work_mem = '256MB'")
            pg.execute("SET default_transaction_read_only = on")
            for link_type in chunk:
                found.extend(pg.execute(
                    "SELECT l.from_id, l.link_type, l.to_id FROM "
                    f"{s}.record_links l WHERE l.link_type = %(t)s AND NOT EXISTS (SELECT 1 "
                    f"FROM {ONT}.link_rows r WHERE r.link_type = %(t)s AND r.from_object = "
                    "l.from_id AND r.link_type = l.link_type AND r.to_object = l.to_id AND "
                    "r.superseded_at IS NULL AND r.tier = l.tier)", {"t": link_type}).fetchall())
        return found

    n = max(1, min(workers, len(types)))
    with cf.ThreadPoolExecutor(max_workers=n) as pool:
        return [row for part in pool.map(check, [types[i::n] for i in range(n)])
                for row in part]


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
        room(pg, 1, wal=False)   # before the transaction: never waits holding its locks
        _open_column(pg, s)
        dead = _dead_links(dsn, s)
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
                + "OR EXISTS (SELECT 1 FROM "
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
            # no new row): found before the transaction (``_dead_links``), dropped here and
            # read again from whatever live rows it has left
            with pg.cursor().copy("COPY gr_lt FROM STDIN") as cp:
                for row in dead:
                    cp.write_row(row)
            pg.execute("DROP TABLE IF EXISTS pg_temp.gr_ldel")
            pg.execute(
                "CREATE TEMP TABLE gr_ldel AS WITH d AS ("
                f"DELETE FROM {s}.record_links l WHERE l.from_rid IN (SELECT rid FROM "
                "gr_gone) OR l.to_rid IN (SELECT rid FROM gr_gone) OR EXISTS (SELECT 1 FROM "
                f"gr_lt t JOIN {s}.records f ON f.record_id = t.from_object JOIN {s}.records "
                "x ON x.record_id = t.to_object WHERE f.rid = l.from_rid AND x.rid = "
                "l.to_rid AND t.link_type = l.link_type) RETURNING l.handle) SELECT * FROM d")
            _probe_plan(pg, "gr_lt", "gr_gone")
            pg.execute(
                f"CREATE TEMP TABLE gr_lid AS SELECT l.link_id, l.link_type FROM "
                f"{ONT}.link_rows l JOIN gr_lt t ON t.from_object = l.from_object AND "
                "t.link_type = l.link_type AND t.to_object = l.to_object UNION SELECT "
                f"l.link_id, l.link_type FROM {ONT}.link_rows l JOIN gr_gone g ON "
                "g.record_id = l.from_object UNION SELECT l.link_id, l.link_type FROM "
                f"{ONT}.link_rows l JOIN gr_gone g ON g.record_id = l.to_object")
            _probe_plan(pg, None)
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
            _views_follow_masks(pg, s)
            door.follow(pg, s, "records")
            door.follow(pg, s, "record_links")
            door._bump(pg, s)
    return {"dropped": gone, "read": added, "links_read": linked, "marks": got}


def _open_column(pg: Any, s: str) -> None:
    """``records.fields_open`` on a table built before it (empty: such a row's class holds
    its marked fields' markings, as built then); its DDL only when missing (ALTER TABLE
    locks the table even when the column exists)."""
    if pg.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = %s AND "
                  "table_name = 'records' AND column_name = 'fields_open'", (s,)).fetchone():
        return
    pg.execute(f"ALTER TABLE {s}.records ADD COLUMN IF NOT EXISTS fields_open jsonb NOT NULL "
               "DEFAULT '{}'")


def _views_follow_masks(pg: Any, s: str) -> None:
    """The guarded views made again when the rows' column classes now mark a column their
    view does not mask (a refresh read the first record with a marked field): until then
    ``door.reader_scope`` refuses the view for every reader (``refused``), never shows the
    column unmasked. In the caller's transaction."""
    from operating.pg import door
    for view in ("records", "record_links"):
        recorded = pg.execute(f"SELECT masked FROM {s}.{door.VIEWS_TABLE} WHERE view = %s",
                              (view,)).fetchone()
        if recorded is not None and set(door.masked_columns(pg, s, view)) <= set(
                recorded[0] or ()):
            continue
        dst = gv.sql_views.guarded_schema(s)
        if pg.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is not None:
            gv.make_views(pg, s)
        return


def code_fingerprints() -> dict[str, str]:
    """Digests of the code that writes the catalog and ``records``/``record_links``
    (``ontology.build_keys.code_digest``); a table whose code cannot be read has none."""
    from ontology.build_keys import code_digest
    mod = "operating.pg.generic_records"
    out: dict[str, str] = {}
    for table, roots in (("catalog", [("operating.pg.generic_views", "build_catalog")]),
                         ("records", [(mod, "refresh"), (mod, "build")])):
        digest = code_digest(roots)
        if digest:
            out[table] = digest[:32]
    return out


def _held_codes(dsn: str, s: str) -> dict[str, str | None]:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as pg:
        return {view: (notes or {}).get("code") for view, notes in pg.execute(
            f"SELECT view, notes FROM {s}.state WHERE view IN ('records', 'record_types')")}


def _record_codes(dsn: str, s: str, codes: dict[str, str]) -> None:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as pg:
        for view, table in (("record_types", "catalog"), ("records", "records")):
            if table in codes:
                pg.execute(f"UPDATE {s}.state SET notes = COALESCE(notes, '{{}}'::jsonb) || "
                           "jsonb_build_object('code', %s::text) WHERE view = %s",
                           (codes[table], view))


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
    # the code that writes each table, as of its last build/refresh (state.notes['code'])
    codes = code_fingerprints()
    held = _held_codes(dsn, s)
    if not new_day and codes.get("catalog") and held.get("record_types") not in (
            None, codes["catalog"]):   # none held: adopted below, no rebuild
        new_day = True   # the catalog's own code changed: it is small, built again now
        out["catalog_code_changed"] = True
    if new_day:
        from ontology.schema import load_schema
        from operating.ontology_tools_identity import SchemaView
        out["catalog"] = gv.build_catalog(dsn, SchemaView.from_compiled(load_schema()), s)
    if codes.get("records") and held.get("records") not in (None, codes["records"]):
        # a full rebuild of ``records`` is a deliberate job (hours: ``build``), never a
        # side effect of a refresh: reported once, then the new code is the baseline
        out["records_code_changed"] = ("rows written before this change keep the old "
                                       "rule until a deliberate generic_records build")
    _record_codes(dsn, s, codes)
    out["records"] = refresh(dsn, s, sweep=new_day)
    try:   # speed only: a failure is reported, never the refresh's
        made = ensure_indexes(dsn, s)
    except (psycopg.Error, RuntimeError) as exc:
        out["indexes_failed"] = str(exc).splitlines()[0][:300]
    else:
        if made:
            out["indexes"] = made
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.generic_records")
    ap.add_argument("command", choices=("build", "refresh", "indexes", "labels"))
    ap.add_argument("--dsn")
    ap.add_argument("--schema")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--uncovered", action="store_true",
                    help="build: only the types no curated table reads, no Rel* mirrors")
    ap.add_argument("--mirrors", action="store_true",
                    help="build --uncovered: the Rel* raw relational mirrors too")
    ap.add_argument("--business", action="store_true",
                    help="build --uncovered: the curated tables' types too, each system's "
                         "copy its own record (only the comms tables' types left out)")
    ap.add_argument("--narrow", action="store_true",
                    help="build: allow leaving out types the served records hold")
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
                    mirrors=args.mirrors, skip_prefixes=args.skip_prefix,
                    business=args.business, narrow=args.narrow)
    elif args.command == "indexes":
        out = {"indexes": ensure_indexes(dsn, args.schema)}
    elif args.command == "labels":
        out = relabel(dsn, args.schema)
    else:
        out = refresh(dsn, args.schema)
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
