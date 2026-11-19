"""DOOR v2 (ARCH-FINAL section 14): permissions resolved to small integers when a business
table is built, so the reader's door is a cheap, indexable predicate.

The tables follow the CANONICAL CLASSES CONTRACT (migration 0041, ``operating.pg.
business_views_access``): ``business_views.build`` writes them; ``convert`` only turns an
old-shape (v1) table into the same shape. A v2 business table ``<schema>.<view>`` is
NARROW: its business columns, ``handle`` and three permission columns decided at build:

* ``access_class integer``: id (append-only, never re-used) in ``<schema>.access_classes``
  of the row's pattern: its audience keys, its record marking sets (``'[]'`` always), the
  marking tokens of the documents it was read from (``doc_marks``; ``UNREADABLE``: the
  documents are resolved by id at read time, ``object_ids || read_ids`` in lineage) and
  ``blocked`` (a row no reader may read: an unknown or empty key, no object ids or handle,
  a PLX-issued document held as a supplier's, ``operating.pg.own_issued``);
* ``col_class integer``: id in the global ``<schema>.col_classes`` of its column pattern:
  ``marks`` (``{column: marking set}`` of the cell) and ``needs`` (``{column:
  {audience_keys, rec_marks, doc_marks, blocked}}`` a reader must also hold to read that
  column: a value read from another record than the row's, e.g. a mailed promise date);
* ``ai_ok boolean``: false when any text cell names Freightcom (the owner's no-AI rule,
  ``integrations.freightcom_rule``): decided here, never by a regex at query time.

Rows made from any object of ``ontology.people_excluded_objects`` are not in the table
(``exclude`` drops rows whose lineage names a newly excluded object, set-based; the people
exclusion's ``update`` and ``build`` call it on every v2 schema). ``door_state.excluded_at``
records the set's stamp (its list tag and ``covered_ver``) the rows were last decided
against; a reader is refused (``serves``) while it is not the set's current one, so a row
of a newly excluded person is never served between the set growing and the drop. Lineage
(object ids, read ids, column objects, cites, systems, copies) is the side table
``<schema>.lineage`` keyed by (view, handle): read for citations by handle, never by a query;
``exclude`` tests ``object_ids || read_ids`` (the row goes) and ``col_objects`` (the cell goes). ``<schema>.class_counts`` holds rows per (view,
access class, col class), so the cells withheld from a reader are counted without a scan.

The reader's side (``reader_scope``): once per (scope, ``door_state.stamp``) the classes the
reader holds outright (``plx.classes``), the classes it holds once the row's documents are
checked by id (``plx.dclasses``: doc_marks ``UNREADABLE``) with the handles whose lineage
names a document it may not open (``plx.d_hidden``), and, per column any class marks or
needs, the col classes whose value it may read (``plx.u_<column>``: the cell's marking held
AND every ``needs`` held). The guarded view keeps a row when its class is held (the AI's
view, ``plx.sql``'s, also only when ``ai_ok``; a person's page view does not filter on it) (a dclass only for a handle not hidden); a masked column reads NULL unless its
``col_class`` is in the allow-list (an unset setting or a class added later masks: fail
closed); the cells masked for a reader are counted from ``class_counts`` (the answer is
partial). ``door_views`` records the columns each view
masks; a view whose rows carry a mark on a column it does not mask is refused."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from operating.pg import business_views as bv

CLASSES, COL_CLASSES, LINEAGE, COUNTS = "access_classes", "col_classes", "lineage", "class_counts"
STATE, VIEWS_TABLE = "door_state", "door_views"
UNREADABLE = "*search-store-unreadable*"
PERMISSION_COLS = (("access_class", "integer"), ("col_class", "integer"),
                   ("ai_ok", "boolean"))
_COL = re.compile(r"[a-z][a-z0-9_]{0,50}")
# (the database, schema, the exclusion's tables) whose tables ``check`` found: its next call reads the
# door in one statement (a table dropped since fails that statement: forgotten, asked again)
_CHECK_SQL: dict[tuple[Any, ...], bool] = {}


def setting_for(column: str) -> str:
    """The transaction setting holding the col classes whose ``column`` the reader may
    read (an allow-list: a class added after the reader's scope was read stays masked), as
    an ``int4[]`` (views made before the ranges: ``RANGE_SETTINGS``)."""
    if not _COL.fullmatch(column):
        raise ValueError(f"not a maskable column name: {column!r}")
    return f"plx.u_{column}"


def range_setting_for(column: str) -> str:
    """``setting_for``'s allow-list as an ``int4multirange`` (the views ``view_ddl`` makes
    now read this one)."""
    return "plx.r_" + setting_for(column)[len("plx.u_"):]


# The held classes as int4multiranges, the settings the guarded views read now: a row's
# class is tested by a binary search over a few ranges, where ``= ANY(<int4[]>)`` over a
# setting (a parameter, never hashed) walked the whole list for every row (the owner's
# 628 classes: 315-450 ms for a count of sales_order_lines' 219,199 rows, 37-42 ms as
# ranges; measured 2026-11-18). The int4[] settings beside them serve views made before
# (until ``sql_views install`` or the next build re-creates them; plx.sql sends them only
# while such a view is there, ``ontology_tools_sql._ranges_only``). A view made now reads
# the int4[] list only while its range setting is unset (``_in``): code from before the
# ranges (a release rollback, a revert) still reads these views with its own rows.
HELD, DHELD = "plx.held", "plx.dheld"
RANGE_SETTINGS = {"plx.classes": HELD, "plx.dclasses": DHELD}


def ranges(ids: Iterable[int]) -> str:
    """``ids`` as an int4multirange literal: each run of consecutive ids one range (a
    class id is never re-used, so an id inside a run is one of ``ids``; any other id, a
    class made later included, is outside every range)."""
    runs: list[list[int]] = []
    for i in sorted({int(x) for x in ids}):
        if runs and i == runs[-1][1]:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])
    return "{" + ",".join(f"[{a},{b})" for a, b in runs) + "}"


def ensure(write: Any, schema: str) -> None:
    """The door's own tables (``door_state``, ``door_views``) over the canonical class
    tables (migration 0041). A scratch schema missing them gets 0041's DDL with its name;
    the served ``biz`` is only made by the migration; an old door shape (int2 ids,
    ``col_marks``) is refused (fail closed: drop the scratch schema and build again)."""
    if write.execute("SELECT to_regclass(%s)", (f"{schema}.{CLASSES}",)).fetchone()[0] is None:
        if schema == bv.SERVED_SCHEMA:
            raise RuntimeError("biz.access_classes is missing: apply migration 0041")
        with open(bv.MIGRATION, encoding="utf-8") as fh:
            write.execute(re.sub(r"\bbiz\b", schema, fh.read()))
    shape = {r[0]: r[1] for r in write.execute(
        "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name IN (%s, %s, %s)",
        (schema, CLASSES, COL_CLASSES, LINEAGE)).fetchall()}
    if (shape.get(f"{CLASSES}.id") != "integer" or f"{COL_CLASSES}.needs" not in shape
            or f"{LINEAGE}.read_ids" not in shape):
        raise RuntimeError(f"{schema}: class tables of an older shape (not migration 0041's)")
    # DDL only for what is missing: ALTER TABLE takes an ACCESS EXCLUSIVE lock even when
    # the column exists, and ``exclude`` calls this first in a transaction that runs for
    # minutes; every reader's door check (``check``, a SELECT on door_state) waited behind
    # it and was refused (measured: plx.sql "being rebuilt" ~3 min of every 10)
    have = {r[0] for r in write.execute(
        "SELECT table_name || '.' || column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name IN (%s, %s)",
        (schema, VIEWS_TABLE, STATE)).fetchall()}
    if f"{VIEWS_TABLE}.masked" not in have:
        write.execute(f"CREATE TABLE IF NOT EXISTS {schema}.{VIEWS_TABLE} ("
                      "view text PRIMARY KEY, masked text[] NOT NULL)")
    if f"{STATE}.stamp" not in have:
        write.execute(f"CREATE TABLE IF NOT EXISTS {schema}.{STATE} ("
                      "one boolean PRIMARY KEY DEFAULT true CHECK (one), stamp bigint NOT NULL)")
    write.execute(f"INSERT INTO {schema}.{STATE} VALUES (true, 0) ON CONFLICT DO NOTHING")
    if f"{STATE}.excluded_at" not in have:
        write.execute(f"ALTER TABLE {schema}.{STATE} ADD COLUMN IF NOT EXISTS excluded_at text")


def exclusion_stamp(pg: Any) -> str:
    """The people exclusion set's stamp: its list tag and ``covered_ver`` (``[null, null]``
    while it was never built). LookupError when its tables are missing."""
    from operating.pg import people_excluded as pe
    for name in (pe.TABLE, pe.STATE):
        if pg.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is None:
            raise LookupError(f"{name} is missing")
    row = pg.execute(f"SELECT exclusion, covered_ver FROM {pe.STATE}").fetchone()
    return json.dumps([row[0], row[1]] if row else [None, None])


def serves(pg: Any, schema: str) -> bool:
    """Whether the v2 tables of ``schema`` were decided against the people exclusion as
    it is now (fail closed: never recorded, or the set's tables missing, refuses)."""
    try:
        now = exclusion_stamp(pg)
    except LookupError:
        return False
    got = pg.execute(f"SELECT excluded_at FROM {schema}.{STATE}").fetchone()
    return bool(got) and got[0] == now


def schemas(pg: Any) -> list[str]:
    """Every schema holding v2 tables (a ``door_state`` table)."""
    return [r[0] for r in pg.execute(
        "SELECT table_schema FROM information_schema.tables WHERE table_name = %s "
        "ORDER BY 1", (STATE,)).fetchall()]


V1_COLS = ("audience_keys", "rec_marks", "col_marks", "object_ids", "cites")


def is_v1(write: Any, schema: str, name: str) -> bool:
    """A v1 business table: it carries every ``business_views.COMMON`` permission and
    lineage column. Only such a table is ever converted; any other one (the comms tables
    with their own unit rule, a table already v2) is its builder's to keep."""
    return write.execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = %s AND "
        "table_name = %s AND column_name = ANY(%s)",
        (schema, name, list(V1_COLS))).fetchone()[0] == len(V1_COLS)


def is_v2(write: Any, schema: str, name: str) -> bool:
    return write.execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = %s AND "
        "table_name = %s AND column_name = ANY(%s)",
        (schema, name, [c for c, _ in PERMISSION_COLS])).fetchone()[0] == len(PERMISSION_COLS)


def _bump(write: Any, schema: str) -> None:
    write.execute(f"UPDATE {schema}.{STATE} SET stamp = stamp + 1")


def _document_marks(write: Any) -> bool:
    """Temp tables ``_door_docs`` (marked document id -> its marking tokens) and
    ``_door_own`` (PLX-issued documents held as a supplier's); False when the marks could
    not be read (every document then carries ``UNREADABLE``)."""
    from operating.search import document_marks as dm
    write.execute("CREATE TEMP TABLE IF NOT EXISTS _door_docs (object_id text PRIMARY KEY, "
                  "marks text[] NOT NULL) ON COMMIT DROP")
    write.execute("CREATE TEMP TABLE IF NOT EXISTS _door_own (object_id text PRIMARY KEY) "
                  "ON COMMIT DROP")
    write.execute("TRUNCATE _door_docs, _door_own")
    own = sorted(dm._own_issued())
    if own:
        write.execute("INSERT INTO _door_own SELECT unnest(%s::text[]) ON CONFLICT DO NOTHING",
                      (own,))
    got = dm._MARKED.get() if dm.enabled() else (({}, {}))
    if got is None:
        return False
    ids = got[0]
    if ids:
        keys = sorted(ids)
        write.execute("INSERT INTO _door_docs SELECT o, ARRAY(SELECT json_array_elements_text("
                      "m::json)) FROM unnest(%s::text[], %s::text[]) AS u(o, m)",
                      (keys, [json.dumps(sorted(ids[k])) for k in keys]))
    return True


def convert(write: Any, schema: str, name: str, *, v: bv.View | None = None) -> dict[str, Any]:
    """The v1 business table ``<schema>.<name>`` (``business_views.COMMON`` permission and
    lineage columns) replaced by its v2 table in one transaction of ``write``; its guarded
    view re-created. Classes are added, never renumbered."""
    from integrations.freightcom_rule import sql_match
    from operating.pg import sql_views
    from operating.pg.people_excluded import TABLE as EXCLUDED
    v = v or bv.VIEWS[name]
    if not is_v1(write, schema, name):
        # fail closed: a table without the v1 columns has its own rule (comms units) or is
        # converted already; rewriting it here would corrupt it
        raise ValueError(f"{schema}.{name} is not a v1 business table: not converted")
    from operating.search.document_marks import DOCUMENT_TYPES
    with write.transaction():
        ensure(write, schema)
        readable = _document_marks(write)
        doc_types = sorted(DOCUMENT_TYPES)
        text_cols = [c.name for c in v.cols if c.kind == "text"] + ["handle"]
        named = "concat_ws(' ', " + ", ".join(f't."{c}"::text' for c in text_cols) + ")"
        unknown = ("" if readable else
                   f"|| CASE WHEN EXISTS (SELECT 1 FROM unnest(t.object_ids) o WHERE "
                   f"split_part(o, ':', 1) = ANY(%(doc_types)s)) THEN ARRAY[%(unreadable)s] "
                   "ELSE '{}'::text[] END")
        fc_params: dict[str, str] = {}

        def fc_bind(value: str) -> str:
            key = f"fc{len(fc_params)}"
            fc_params[key] = value
            return f"%({key})s"
        fc_match = sql_match(named, fc_bind)
        write.execute("DROP TABLE IF EXISTS _door_rows")
        write.execute(f"""
            CREATE TEMP TABLE _door_rows ON COMMIT DROP AS SELECT t.ctid AS rid,
              COALESCE(ARRAY(SELECT DISTINCT k FROM unnest(t.audience_keys) k
                             WHERE k IS NOT NULL ORDER BY 1), '{{}}') AS aks,
              ARRAY(SELECT DISTINCT m FROM unnest(t.rec_marks || '{{[]}}'::text[]) m
                    WHERE m IS NOT NULL ORDER BY 1) AS rms,
              ARRAY(SELECT DISTINCT m FROM unnest(t.object_ids) o JOIN _door_docs d
                    ON d.object_id = o, unnest(d.marks) m ORDER BY 1) {unknown} AS dms,
              (t.audience_keys IS NULL OR array_position(t.audience_keys, NULL) IS NOT NULL
               OR t.rec_marks IS NULL OR array_position(t.rec_marks, NULL) IS NOT NULL
               OR t.object_ids IS NULL OR cardinality(t.object_ids) = 0
               OR array_position(t.object_ids, NULL) IS NOT NULL OR t.handle IS NULL
               OR EXISTS (SELECT 1 FROM unnest(t.object_ids) o JOIN _door_own w
                          ON w.object_id = o)) AS blocked,
              COALESCE(t.col_marks, '{{}}'::jsonb) AS cms,
              NOT COALESCE({fc_match}, false) AS ai_ok,
              EXISTS (SELECT 1 FROM unnest(t.object_ids) x JOIN {EXCLUDED} p
                      ON p.object_id = x) AS excluded
            FROM {schema}.{name} t""",
            {"doc_types": doc_types, "unreadable": UNREADABLE, **fc_params})
        # ids append-only, allocated as the builder allocates them (one at a time)
        write.execute(f"LOCK TABLE {schema}.{CLASSES}, {schema}.{COL_CLASSES} IN SHARE ROW "
                      "EXCLUSIVE MODE")
        write.execute(f"""
            INSERT INTO {schema}.{CLASSES} (id, audience_keys, rec_marks, doc_marks, blocked)
            SELECT (SELECT COALESCE(max(id), 0) FROM {schema}.{CLASSES}) + row_number() OVER (),
                   p.* FROM (SELECT DISTINCT aks, rms, dms, blocked FROM _door_rows r
                             WHERE NOT excluded) p
            WHERE NOT EXISTS (SELECT 1 FROM {schema}.{CLASSES} c WHERE c.audience_keys = p.aks
                AND c.rec_marks = p.rms AND c.doc_marks = p.dms AND c.blocked = p.blocked)""")
        write.execute(f"""
            INSERT INTO {schema}.{COL_CLASSES} (id, marks, needs)
            SELECT (SELECT COALESCE(max(id), 0) FROM {schema}.{COL_CLASSES})
                   + row_number() OVER (), p.cms, '{{}}'::jsonb
            FROM (SELECT DISTINCT cms FROM _door_rows WHERE NOT excluded) p
            WHERE NOT EXISTS (SELECT 1 FROM {schema}.{COL_CLASSES} c WHERE c.marks = p.cms
                              AND c.needs = '{{}}'::jsonb)""")
        biz = ", ".join(f't."{c.name}"' for c in v.cols)
        new = f"{name}__door"
        write.execute(f"DROP TABLE IF EXISTS {schema}.{new}")
        write.execute(f"""
            CREATE TABLE {schema}.{new} AS SELECT {biz}, t.handle,
              a.id AS access_class, c.id AS col_class, r.ai_ok
            FROM {schema}.{name} t JOIN _door_rows r ON r.rid = t.ctid
            JOIN {schema}.{CLASSES} a ON a.audience_keys = r.aks AND a.rec_marks = r.rms
              AND a.doc_marks = r.dms AND a.blocked = r.blocked
            JOIN {schema}.{COL_CLASSES} c ON c.marks = r.cms AND c.needs = '{{}}'::jsonb
            WHERE NOT r.excluded""")
        write.execute(f"DELETE FROM {schema}.{LINEAGE} WHERE view = %s", (name,))
        write.execute(f"""
            INSERT INTO {schema}.{LINEAGE} (view, handle, object_ids, systems, cites, copies)
            SELECT DISTINCT ON (t.handle) %s, t.handle, t.object_ids,
                   COALESCE(t.systems, '{{}}'), COALESCE(t.cites, '{{}}'), COALESCE(t.copies, 0)
            FROM {schema}.{name} t JOIN _door_rows r ON r.rid = t.ctid
            WHERE t.handle IS NOT NULL AND t.object_ids IS NOT NULL AND NOT r.excluded""",
            (name,))
        sql_views.drop(write, name, schema)
        write.execute(f"DROP TABLE {schema}.{name}")
        write.execute(f"ALTER TABLE {schema}.{new} RENAME TO {name}")
        index(write, schema, name, v)
        recount(write, schema, name)
        _bump(write, schema)
        # the guarded view; ``follow`` there records the people exclusion's stamp on a
        # schema's first table (read before its drop), else drops what a grown set names
        sql_views.create(write, v, schema)
        (rows,) = write.execute(f"SELECT count(*) FROM {schema}.{name}").fetchone()
    return {"view": name, "rows": rows}


def index(write: Any, schema: str, name: str, v: bv.View) -> None:
    """handle, access class, the keys (``*_handle``), dates, status; trigram on names
    when ``pg_trgm`` is installed."""
    cols = ["handle", "access_class"] + [
        c.name for c in v.cols if c.kind == "date" or c.name.endswith("_handle")
        or c.name in ("status", "customer_code", "supplier_code")]
    for col in dict.fromkeys(cols):
        write.execute(f'CREATE INDEX ON {schema}.{name} ("{col}")')
    trgm = write.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'").fetchone()
    if trgm:
        for c in v.cols:
            if c.kind == "text" and c.name in ("name", "customer", "supplier", "description"):
                write.execute(f'CREATE INDEX ON {schema}.{name} USING gin ("{c.name}" '
                              "gin_trgm_ops)")
    write.execute(f"ANALYZE {schema}.{name}")


def recount(write: Any, schema: str, name: str) -> None:
    write.execute(f"DELETE FROM {schema}.{COUNTS} WHERE view = %s", (name,))
    write.execute(f"INSERT INTO {schema}.{COUNTS} SELECT %s, access_class, col_class, "
                  f"count(*) FROM {schema}.{name} GROUP BY 2, 3", (name,))


def exclude(write: Any, schema: str, *, only: str | None = None, record: bool = True,
            among: Sequence[str] | None = None) -> int:
    """Drop every row of every v2 table (``only``: of that view) whose lineage names an
    object of the people exclusion among its own or read records (``object_ids ||
    read_ids``), empty a cell whose column was read from one (``col_objects``), and
    (``record``) record the set's stamp the rows now answer to (``serves``). ``among``:
    only these objects are tested (the ones a grown set added, over a door decided
    against the set before it: ``exclude_within``), found by the lineage's object-id
    indexes in well under a second, where the whole set's test scans the lineage (41 s
    live, three times)."""
    from operating.pg.people_excluded import TABLE as EXCLUDED
    gone = 0
    view_only = "AND l.view = %(only)s" if only else ""
    params: dict[str, Any] = {"only": only, "among": list(among or ())}
    with write.transaction():
        ensure(write, schema)
        set_stamp = exclusion_stamp(write)   # first: a set grown after it stays refused
        if among is None:
            hit = (f"EXISTS (SELECT 1 FROM unnest(l.object_ids || l.read_ids) o "
                   f"JOIN {EXCLUDED} p ON p.object_id = o)")
            named = f"JOIN {EXCLUDED} p ON p.object_id = o"
        else:
            hit = ("(l.object_ids && %(among)s::text[] OR l.read_ids && %(among)s::text[])")
            named = "WHERE o = ANY(%(among)s::text[])"
        views = {r[0] for r in write.execute(
            f"SELECT DISTINCT l.view FROM {schema}.{LINEAGE} l WHERE {hit} {view_only}",
            params).fetchall()}
        cells: dict[tuple[str, str], list[str]] = {}
        for view, col, handle in write.execute(
                f"SELECT l.view, e.k, l.handle FROM {schema}.{LINEAGE} l, "
                "jsonb_each(l.col_objects) e(k, ids) WHERE jsonb_typeof(e.ids) = 'array' "
                f"AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(e.ids) o "
                f"{named}) {view_only}", params).fetchall():
            cells.setdefault((view, col), []).append(handle)
        for view in sorted(views | {v for v, _ in cells}):
            if not is_v2(write, schema, view):
                continue
            # a curated view's columns; another v2 table's (the generic ``records`` and
            # ``record_links``, ``operating.pg.generic_records``) as the table has them
            known = ({c.name for c in bv.VIEWS[view].cols} if view in bv.VIEWS else
                     {r[0] for r in write.execute(
                         "SELECT column_name FROM information_schema.columns WHERE "
                         "table_schema = %s AND table_name = %s", (schema, view))})
            for (cv, col), handles in sorted(cells.items()):
                if cv != view:
                    continue
                if col not in known or not _COL.fullmatch(col):
                    # a column the view does not have: its rows go whole (fail closed)
                    gone += write.execute(f"DELETE FROM {schema}.{view} WHERE handle = "
                                          "ANY(%s)", (handles,)).rowcount
                    write.execute(f"DELETE FROM {schema}.{LINEAGE} WHERE view = %s AND "
                                  "handle = ANY(%s)", (view, handles))
                    continue
                gone += write.execute(f'UPDATE {schema}.{view} SET "{col}" = NULL WHERE '
                                      'handle = ANY(%s)', (handles,)).rowcount
                write.execute(f"UPDATE {schema}.{LINEAGE} SET col_objects = col_objects - "
                              "%s, cites = cites - %s WHERE view = %s AND handle = ANY(%s)",
                              (col, col, view, handles))
            gone += write.execute(
                f"DELETE FROM {schema}.{view} t USING {schema}.{LINEAGE} l WHERE "
                f"l.view = %(view)s AND l.handle = t.handle AND {hit}",
                {**params, "view": view}).rowcount
            write.execute(f"DELETE FROM {schema}.{LINEAGE} l WHERE l.view = %(view)s AND {hit}",
                          {**params, "view": view})
            recount(write, schema, view)
        if gone:
            _bump(write, schema)
        if record:
            write.execute(f"UPDATE {schema}.{STATE} SET excluded_at = %s", (set_stamp,))
    return gone


def follow(write: Any, schema: str, name: str) -> int:
    """After ``name`` was (re)built or converted: a schema whose stamp was never recorded
    is decided whole against the set (``exclude``, stamp read first); else the view's rows
    are decided against the set as it is now and the recorded stamp is left (one behind
    the set stays refused until ``exclude_everywhere``). No set: nothing (``serves``
    refuses)."""
    ensure(write, schema)
    (recorded,) = write.execute(f"SELECT excluded_at FROM {schema}.{STATE}").fetchone()
    try:
        if recorded is None:
            return exclude(write, schema)
        return exclude(write, schema, only=name, record=False)
    except LookupError:
        return 0


def exclude_everywhere(write: Any, *, stale_only: bool = False) -> dict[str, Any]:
    """``exclude`` on every v2 schema (the people exclusion's update and build call this
    after the set changed; ``stale_only``: only where the stamp is behind, as an update
    that found nothing new does, so a schema that failed before catches up). A schema
    that fails keeps its older stamp: refused."""
    out: dict[str, Any] = {}
    for schema in schemas(write):
        if stale_only and serves(write, schema):
            continue
        try:
            out[schema] = exclude(write, schema)
        except Exception as exc:  # noqa: BLE001 - that schema stays refused (serves)
            write.rollback()
            out[schema] = f"failed: {type(exc).__name__}"
    return out


def exclude_within(write: Any, before: str, among: Sequence[str]) -> dict[str, Any]:
    """Inside the caller's open transaction (the people exclusion's update, which grew the
    set from the stamp ``before`` by the objects ``among``): every v2 schema whose door was
    decided against ``before`` drops the rows of those objects alone (``exclude``'s
    ``among``: the lineage's object-id indexes, under a second) and records the grown
    set's stamp. The set and the rows that follow it commit together, seconds after the
    set was decided: a reader reads the old set with the old rows or the grown set with
    the rows that follow it, never a grown set's excluded row, never a door behind the set
    for the minutes a whole-set test takes (41 s a lineage scan, three of them live). A
    schema already behind ``before``, or whose follow fails (its own work rolled back,
    ``exclude``'s savepoint), keeps its older stamp: refused until the whole-set
    catch-up after the commit (``stale_only``) decides it."""
    out: dict[str, Any] = {}
    for schema in schemas(write):
        try:
            with write.transaction():
                ensure(write, schema)
                got = write.execute(f"SELECT excluded_at FROM {schema}.{STATE}").fetchone()
                if not got or got[0] != before:
                    out[schema] = "stale"
                    continue
                out[schema] = exclude(write, schema, among=among)
        except Exception as exc:  # noqa: BLE001 - that schema stays refused (serves)
            out[schema] = f"failed: {type(exc).__name__}"
    return out


def masked_columns(write: Any, schema: str, name: str) -> list[str]:
    """The columns of ``name`` any of its rows' col classes marks or needs."""
    got = write.execute(
        f"SELECT DISTINCT k FROM {schema}.{COUNTS} n JOIN {schema}.{COL_CLASSES} c "
        "ON c.id = n.col_class, LATERAL (SELECT jsonb_object_keys(c.marks) UNION "
        "SELECT jsonb_object_keys(c.needs)) AS j(k) WHERE n.view = %s ORDER BY 1",
        (name,)).fetchall()
    return [k for (k,) in got]


def _held(setting: str) -> str:
    """The setting as one int4multirange value, read once per statement (an initplan);
    unset or '' (a rolled-back transaction set it): NULL, so ``<@`` holds nothing."""
    return f"(SELECT NULLIF(current_setting('{setting}', true), '')::int4multirange)"


def _ids(setting: str) -> str:
    """The older int4[] setting as one value (an initplan; cast outside the sub-select:
    ANY over an array, never over the sub-select's rows)."""
    return f"(SELECT NULLIF(current_setting('{setting}', true), '')::int4[])::int4[]"


def _in(expr: str, held: str, listed: str) -> str:
    """``expr`` held: inside the ranges setting ``held``; only while ``held`` is unset or
    '' (code from before the ranges is running: a rollback or a revert, which sets only the
    int4[] lists), inside the int4[] setting ``listed`` instead. Each code sees exactly the
    rows its own settings hold, never zero rows from a setting it does not know; neither
    set: nothing (fail closed). Code that sends ``held`` always sends it (``'{}'`` for no
    class), and plx.sql checks after the query that every setting it sent is unchanged."""
    return f"({expr} <@ {_held(held)} OR ({_held(held)} IS NULL AND {expr} = ANY({_ids(listed)})))"


def view_ddl(write: Any, v: bv.View, src: str, dst: str, *, ai_only: bool = True,
             also: Sequence[str] = ()) -> str:
    """The guarded v2 view; records the columns it masks in ``door_views``. ``ai_only``
    (the AI's views, ``plx.sql``'s): only ``ai_ok`` rows (no Freightcom). A person's pages
    (``ai_only=False``, ``sql_views.page_schema``) keep them; every other part of the door is
    the same. ``also``: conditions over ``t`` every row must meet as well (a table's own
    rule, ``generic_views.UNDECIDED``)."""
    ensure(write, src)
    known = {c.name for c in v.cols}
    masked = masked_columns(write, src, v.name)
    if any(c not in known for c in masked):
        raise RuntimeError(f"{src}.{v.name}: marks on unknown columns "
                           f"{sorted(set(masked) - known)}")
    write.execute(f"INSERT INTO {src}.{VIEWS_TABLE} VALUES (%s, %s) ON CONFLICT (view) "
                  "DO UPDATE SET masked = EXCLUDED.masked", (v.name, masked))

    def hides(c: str) -> str:
        return (f"NOT COALESCE({_in('t.col_class', range_setting_for(c), setting_for(c))}, "
                "false)")
    shown = [f'CASE WHEN {hides(c.name)} THEN NULL ELSE t."{c.name}" END AS "{c.name}"'
             if c.name in masked else f't."{c.name}"' for c in v.cols]
    withheld = ("array_remove(ARRAY[" + ", ".join(
        f"CASE WHEN {hides(c)} THEN '{c}' END" for c in masked) + "]::text[], NULL)"
                if masked else "'{}'::text[]")
    # a dclass row is held once its documents are checked by id: the reader's hidden
    # handles (unset, or '' once a rolled-back transaction set it: no dclass row, fail
    # closed) as one hashed anti-join; an unset or '' class list holds nothing
    return (f"CREATE VIEW {dst}.{v.name} WITH (security_barrier) AS SELECT "
            + ", ".join(shown) + f", t.handle, {withheld} AS withheld "
            f"FROM {src}.{v.name} t WHERE {'t.ai_ok AND ' if ai_only else ''}("
            f"{_in('t.access_class', HELD, 'plx.classes')} OR ("
            f"{_in('t.access_class', DHELD, 'plx.dclasses')} AND "
            "NULLIF(current_setting('plx.d_hidden', true), '') IS NOT NULL AND t.handle IS NOT "
            "NULL AND NOT (t.handle IN (SELECT unnest(NULLIF(current_setting('plx.d_hidden', "
            "true), '')::text[])))))" + "".join(f" AND ({c})" for c in also))


# ---------------------------------------------------------------------- the reader's side

def present(pg: Any, schema: str) -> bool:
    return pg.execute("SELECT to_regclass(%s)", (f"{schema}.{STATE}",)).fetchone()[0] is not None


def check(pg: Any, schema: str) -> tuple[int, bool] | None:
    """(the door's stamp, whether it ``serves``: decided against the people exclusion as
    it is now), in one statement once the tables were found (a reader's call reads it
    first in its own transaction, so the door is decided at the snapshot its query reads).
    None: ``schema`` has no door, or its ``door_state`` has no row yet. The exclusion's
    tables missing: not served (fail closed). A table dropped since: that statement fails
    (the caller refuses), asked again next."""
    from operating.pg import people_excluded as pe
    info = getattr(pg, "info", None)
    key = (getattr(info, "host", None), getattr(info, "port", None),
           getattr(info, "dbname", None), schema, pe.TABLE, pe.STATE)
    if key not in _CHECK_SQL:
        found = pg.execute("SELECT to_regclass(%s) IS NOT NULL, to_regclass(%s) IS NOT NULL "
                           "AND to_regclass(%s) IS NOT NULL",
                           (f"{schema}.{STATE}", pe.TABLE, pe.STATE)).fetchone()
        if not found[0]:
            return None
        if not found[1]:
            row = pg.execute(f"SELECT stamp FROM {schema}.{STATE}").fetchone()
            return None if row is None else (int(row[0]), False)
    try:
        got = pg.execute(
            f"SELECT d.stamp, d.excluded_at, s.exclusion, s.covered_ver, s.one IS NOT NULL "
            f"FROM {schema}.{STATE} d LEFT JOIN (SELECT exclusion, covered_ver, true AS one "
            f"FROM {pe.STATE} LIMIT 1) s ON true").fetchone()
    except Exception:
        _CHECK_SQL.pop(key, None)
        raise
    _CHECK_SQL[key] = True
    if got is None:
        return None   # an empty door_state: no door yet (the caller: not ready)
    # ``exclusion_stamp``'s text of the set's stamp (``[null, null]``: never built)
    now = json.dumps([got[2], got[3]] if got[4] else [None, None])
    return int(got[0]), got[1] == now


def stamp(pg: Any, schema: str) -> int:
    return int(pg.execute(f"SELECT stamp FROM {schema}.{STATE}").fetchone()[0])


def _holds(need: Any, keys: set[str], sets: set[str], tokens: set[str],
           every_doc: bool) -> bool:
    """Whether the reader holds a column's ``needs`` entry (an unknown shape: no)."""
    if not isinstance(need, dict) or need.get("blocked", True) is not False:
        return False
    parts = [need.get(k) for k in ("audience_keys", "rec_marks", "doc_marks")]
    if any(not isinstance(p, list) or not all(isinstance(x, str) for x in p) for p in parts):
        return False
    aks, rms, dms = (set(p) for p in parts)
    # documents are resolved by id only for a whole row (``plx.d_hidden``): a cell read
    # from a document is masked for a reader not holding every document
    return aks <= keys and rms <= sets and (every_doc or (UNREADABLE not in dms
                                                          and dms <= tokens))


def _quoted(text: str) -> str:
    """One element of a Postgres array literal, quoted (any character kept as it is)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def reader_scope(pg: Any, schema: str, *, audience_keys: Iterable[str],
                 marking_sets: Iterable[str], held: Iterable[str], holds_every_doc: bool,
                 hidden_docs: Iterable[str] | Callable[[], tuple[Iterable[str], bool]] = (),
                 docs_known: bool = True) -> dict[str, Any]:
    """The reader's door, computed once per (scope, stamp): ``settings`` to set per
    transaction, ``withheld`` per view per column (rows the reader reads with that cell
    NULL, from ``class_counts``) and ``refused`` (views whose marks outrun their masks).
    ``hidden_docs``: the documents the reader may not open (withheld ones, PLX-issued ones
    held as a supplier's): a dclass row naming one among its own or read records is out.
    ``docs_known`` False (the marked documents could not be read): a reader not holding
    every document holds no dclass (which documents it may open is unknown: fail closed).
    ``hidden_docs`` may be a function giving (the hidden documents, docs_known), called
    only when some class is document-resolved (``docs``: True when it was called)."""
    keys = sorted({k for k in audience_keys if isinstance(k, str)})
    sets = sorted({m for m in marking_sets if isinstance(m, str)})
    tokens = sorted({h for h in held if isinstance(h, str)})
    classes, dclasses = [], []
    asked = callable(hidden_docs)
    for cid, dms in pg.execute(
            f"SELECT id, doc_marks FROM {schema}.{CLASSES} WHERE NOT blocked AND "
            "audience_keys <@ %s::text[] AND rec_marks <@ %s::text[] ORDER BY 1",
            (keys, sets)).fetchall():
        dms = set(dms or ())
        if UNREADABLE in dms and callable(hidden_docs):
            hidden_docs, docs_known = hidden_docs()
        if UNREADABLE in dms:
            if holds_every_doc or (docs_known and (dms - {UNREADABLE}) <= set(tokens)):
                dclasses.append(cid)
        elif holds_every_doc or dms <= set(tokens):
            classes.append(cid)
    hidden = sorted({d for d in hidden_docs if isinstance(d, str)}) if not callable(
        hidden_docs) else []
    hidden_handles = sorted({h for (h,) in pg.execute(
        f"SELECT DISTINCT handle FROM {schema}.{LINEAGE} WHERE object_ids && %s::text[] "
        "OR read_ids && %s::text[]", (hidden, hidden)).fetchall()}) if (
            hidden and dclasses) else []
    held_sets, key_set, token_set = set(sets), set(keys), set(tokens)
    masks: dict[str, list[int]] = {}
    marked: dict[int, frozenset[str]] = {}
    for cid, marks, needs in pg.execute(
            f"SELECT id, marks, needs FROM {schema}.{COL_CLASSES}").fetchall():
        marks = marks if isinstance(marks, dict) else json.loads(marks)
        needs = needs if isinstance(needs, dict) else json.loads(needs or "{}")
        marked[cid] = frozenset(marks) | frozenset(needs)
        for col in marked[cid]:
            masks.setdefault(col, [])
            if (col in marks and marks[col] not in held_sets) or (
                    col in needs and not _holds(needs[col], key_set, held_sets, token_set,
                                                holds_every_doc)):
                masks[col].append(cid)
    allowed = {col: sorted(set(marked) - set(ids)) for col, ids in masks.items()}
    views = {v: set(m) for v, m in pg.execute(
        f"SELECT view, masked FROM {schema}.{VIEWS_TABLE}").fetchall()}
    held_set = set(classes) | set(dclasses)
    withheld: dict[str, dict[str, int]] = {}
    per_view: dict[str, set[str]] = {}
    for view, ac, cc, n in pg.execute(
            f"SELECT view, access_class, col_class, rows FROM {schema}.{COUNTS}").fetchall():
        per_view.setdefault(view, set()).update(marked.get(cc, ()))
        if ac not in held_set:
            continue
        for col in marked.get(cc, ()):
            if cc in masks.get(col, ()):
                withheld.setdefault(view, {}).setdefault(col, 0)
                withheld[view][col] += int(n)
    refused = sorted(v for v, cols in per_view.items() if not cols <= views.get(v, set()))

    def arr(xs: Iterable[Any]) -> str:
        return "{" + ",".join(map(str, xs)) + "}"
    settings = {"plx.classes": arr(classes), "plx.dclasses": arr(dclasses),
                HELD: ranges(classes), DHELD: ranges(dclasses),
                "plx.d_hidden": arr(_quoted(h) for h in hidden_handles)}
    for col, ids in sorted(allowed.items()):
        settings[setting_for(col)] = arr(ids)
        settings[range_setting_for(col)] = ranges(ids)
    return {"settings": settings, "withheld": withheld, "refused": refused,
            "docs": not (asked and callable(hidden_docs))}


def lineage(pg: Any, schema: str, pairs: Sequence[tuple[str, str]]) -> dict[tuple[str, str],
                                                                             dict[str, Any]]:
    """(view, handle) -> its own and read object ids, systems and cites (citations after a
    query)."""
    if not pairs:
        return {}
    views, handles = [p[0] for p in pairs], [p[1] for p in pairs]
    got = pg.execute(
        f"SELECT l.view, l.handle, l.object_ids, l.read_ids, l.systems, l.cites "
        f"FROM {schema}.{LINEAGE} l JOIN unnest(%s::text[], %s::text[]) AS q(view, handle) "
        "ON q.view = l.view AND q.handle = l.handle", (views, handles)).fetchall()
    return {(v, h): {"object_ids": list(o or ()), "read_ids": list(r or ()),
                     "systems": list(s or ()), "cites": c}
            for v, h, o, r, s, c in got}


# ---------------------------------------------------------------------- scratch copy, budgets

JOINS = (
    ("order lines x orders x customers",
     "SELECT c.name, count(*), sum(l.line_amount) FROM sales_order_lines l JOIN sales_orders o "
     "ON o.handle = l.order_handle JOIN customers c ON c.handle = o.customer_handle "
     "WHERE o.order_date >= DATE '2025-01-01' GROUP BY 1 ORDER BY 3 DESC NULLS LAST LIMIT 10"),
    ("invoices x customers",
     "SELECT c.name, i.total_currency, sum(i.total) FROM sales_invoices i JOIN customers c "
     "ON c.handle = i.customer_handle GROUP BY 1, 2 ORDER BY 3 DESC NULLS LAST LIMIT 10"))


def copy(dsn: str, src: str, dst: str) -> list[dict[str, Any]]:
    """Every table of ``src`` copied into scratch schema ``dst`` (``src`` untouched): each
    v1 business table (``is_v1``) converted there; every other table (the comms tables and
    ``comms_units`` with their own unit rule, tables already v2, their class and lineage
    tables) copied as it is, never converted. Then the guarded schema ``<dst>_sql``."""
    import psycopg
    from operating.pg import sql_views
    if dst in ("biz", src) or not re.fullmatch(r"biz_[a-z0-9_]{1,40}", dst):
        raise SystemExit(f"refused: {dst!r} is not a scratch schema")
    out = []
    with psycopg.connect(dsn, autocommit=True) as w:
        w.execute(f"CREATE SCHEMA IF NOT EXISTS {dst}")
        w.execute(f"CREATE TABLE IF NOT EXISTS {dst}.state (LIKE {src}.state)")
        w.execute(f"TRUNCATE {dst}.state")
        w.execute(f"INSERT INTO {dst}.state SELECT * FROM {src}.state")
        tables = [r[0] for r in w.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND "
            "table_type = 'BASE TABLE' AND table_name <> 'state' ORDER BY table_name",
            (src,)).fetchall()]
        # support tables first (units, classes, lineage), so a table copied as it is
        # keeps the ids it points at and a conversion appends to them
        for name in sorted(tables, key=lambda n: n in bv.VIEWS):
            started = time.monotonic()
            if name in bv.VIEWS:
                sql_views.drop(w, name, dst)
            w.execute(f'DROP TABLE IF EXISTS {dst}."{name}"')
            # its indexes come along; its defaults do not (a serial's default would draw
            # on src's own sequence)
            w.execute(f'CREATE TABLE {dst}."{name}" (LIKE {src}."{name}" INCLUDING ALL '
                      "EXCLUDING DEFAULTS)")
            w.execute(f'INSERT INTO {dst}."{name}" SELECT * FROM {src}."{name}"')
            if name in bv.VIEWS and is_v1(w, dst, name):
                got = convert(w, dst, name, v=bv.VIEWS[name])
            else:
                got = {"view": name, "kept": True}
            out.append({**got, "seconds": round(time.monotonic() - started, 1)})
        if present(w, dst):
            out.append({"view": "people exclusion", "dropped": exclude(w, dst)})
    sql_views.install(dsn, dst)
    return out


def budget(dsn: str, schema: str, *, runs: int = 3) -> list[dict[str, Any]]:
    """Wall times as ``plx_sql_reader`` through the guarded views for three reader shapes
    over the classes present: owner (every class, every marking set), staff (every class
    whose marks name no finance marking; finance columns masked), restricted (unmarked
    classes holding half the audience keys). First call = scope + first query on a new
    connection."""
    import psycopg
    from operating.pg import sql_views
    with psycopg.connect(dsn, autocommit=True) as c:
        classes = c.execute(f"SELECT audience_keys, rec_marks FROM {schema}.{CLASSES} "
                            "WHERE NOT blocked").fetchall()
        sets = {m for _, ms in classes for m in ms} | {
            m for (cm,) in c.execute(f"SELECT marks FROM {schema}.{COL_CLASSES}")
            for m in (cm if isinstance(cm, dict) else json.loads(cm)).values()}
        keys = sorted({k for ks, _ in classes for k in ks})
        tables = [n for n in bv.VIEWS if c.execute(
            "SELECT to_regclass(%s)", (f"{sql_views.guarded_schema(schema)}.{n}",)
        ).fetchone()[0]]
    readers = {"owner": (keys, sorted(sets)),
               "staff": (keys, sorted(m for m in sets if "finance" not in m)),
               "restricted": (keys[: max(1, len(keys) // 2)], [])}
    out = []
    for who, (ks, ms) in readers.items():
        t0 = time.monotonic()
        with psycopg.connect(dsn, autocommit=True) as c:
            scope = reader_scope(c, schema, audience_keys=ks, marking_sets=ms, held=(),
                                 holds_every_doc=(who == "owner"))

            def timed(sql: str) -> tuple[float, Any]:
                best, got = 1e9, None
                for _ in range(runs):
                    with c.transaction():
                        c.execute("SET TRANSACTION READ ONLY")
                        for k, val in scope["settings"].items():
                            c.execute("SELECT set_config(%s, %s, true)", (k, val))
                        c.execute(f"SET LOCAL search_path = "
                                  f"{sql_views.guarded_schema(schema)}, pg_catalog")
                        c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
                        s0 = time.monotonic()
                        got = c.execute(sql).fetchall()
                        best = min(best, time.monotonic() - s0)
                return best * 1000, got

            first, _ = timed(f"SELECT count(*) FROM {tables[0]}")
            out.append({"reader": who, "what": "first call (scope + query, new conn)",
                        "ms": round((time.monotonic() - t0) * 1000, 1)})
            for n in tables:
                ms, got = timed(f"SELECT count(*) FROM {n}")
                out.append({"reader": who, "what": f"count {n}", "ms": round(ms, 1),
                            "rows": got[0][0]})
                v = bv.VIEWS[n]
                g = next((col.name for col in v.cols if col.name in (
                    "status", "currency") or col.name.endswith("_currency")), v.cols[0].name)
                ms, _ = timed(f'SELECT "{g}", count(*) FROM {n} GROUP BY 1')
                out.append({"reader": who, "what": f"group {n} by {g}", "ms": round(ms, 1)})
            for label, sql in JOINS:
                if all(t in tables for t in re.findall(r"(?:FROM|JOIN) (\w+)", sql)):
                    ms, _ = timed(sql)
                    out.append({"reader": who, "what": label, "ms": round(ms, 1)})
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.door")
    ap.add_argument("command", choices=("copy", "budget"))
    ap.add_argument("--dsn")
    ap.add_argument("--src", default="biz_scratch")
    ap.add_argument("--schema", required=True, help="the scratch v2 schema")
    args = ap.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    got = copy(dsn, args.src, args.schema) if args.command == "copy" else \
        budget(dsn, args.schema)
    for row in got:
        sys.stdout.write(json.dumps(row, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["convert", "ensure", "exclude", "is_v1", "is_v2", "lineage", "present", "reader_scope",
           "setting_for", "stamp", "view_ddl", "serves", "schemas", "exclusion_stamp",
           "exclude_everywhere", "exclude_within", "follow"]
