"""Access classes, the build and swap of one view, of the curated business views (split out
of ``business_views`` for size; re-exported there). Constants are read from the parent
module at call time, so a patched ``business_views.SCHEMA`` applies here too.

The canonical classes contract (migration 0041; every builder of a ``biz``-shaped schema
writes it, every reader reads it):

* ``access_classes(id int4, audience_keys text[], rec_marks text[], doc_marks text[],
  blocked bool)``: one row per pattern, unique on all four, ids append-only (a reader's held
  list stays right across rebuilds). A reader holds a class when it is not ``blocked`` and
  the reader holds every audience key, every marking set and (``doc_marks``) every document
  marking token of it. This builder writes ``doc_marks`` ``{UNREADABLE}`` for a row read
  from a document type (``search.document_marks.DOCUMENT_TYPES``) and resolves documents by
  id at read time (``lineage``); ``blocked`` for a row read from a record whose audience key
  is unknown or empty.
* ``col_classes(id int4, marks jsonb, needs jsonb)``: one row per column pattern, unique on
  both, ids append-only, id 0 = nothing marked. ``marks``: ``{column: marking set}`` (the
  cell's own markings); ``needs``: ``{column: {audience_keys, rec_marks, doc_marks,
  blocked}}``: what a reader must also hold to use that column (a value read from a record
  the row is not: the supplier's mailed promise). A reader lacking either does not read the
  column: plx.business withholds a row whose USED column it may not read, a guarded view
  masks the cell.
* ``lineage(view, handle, object_ids, read_ids, col_objects, cites, systems, copies)``:
  ``object_ids`` the row's own records, ``read_ids`` every other record a row value was
  read from, ``col_objects`` ``{column: [ids]}`` the records only that column was read
  from. A reader may not open one of them (a withheld document): the row (column) is out.
* ``class_counts(view, access_class, col_class, rows)``: rows per class pair, written
  with the view's swap (withheld counts without a scan)."""
from __future__ import annotations

import json
import time
import datetime as dt
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from operating.pg import swap_lock

UNREADABLE = "*search-store-unreadable*"   # operating/pg/door.py's token: documents unresolved
CHUNK = 20000   # rows resolved at a time (bounded memory over the largest view)


def _code() -> Any:
    from operating.pg import business_views_code
    return business_views_code


def _bv() -> Any:
    from operating.pg import business_views
    return business_views


Pattern = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]


def access_pattern(row: Any) -> Pattern:
    """(audience keys, marking sets, document marks, blocked) of the row as made (before
    its other records are resolved): an empty or unknown key blocks it."""
    keys = tuple(sorted({a for a in row.audience if isinstance(a, str) and a}))
    blocked = any(not (isinstance(a, str) and a) for a in row.audience)
    return keys, tuple(sorted(row.marks | {"[]"})), (), blocked


def _key(pat: Pattern) -> Pattern:
    return tuple(tuple(sorted(set(p))) for p in pat[:3]) + (bool(pat[3]),)  # type: ignore


def excluded_objects(write: Any, ids: Iterable[str]) -> set[str]:
    """Which of ``ids`` are an excluded person's own data (``_bv().EXCLUDED_TABLE``, migration
    0040); no such table: the build stops (fail closed, never built unfiltered)."""
    if write.execute("SELECT to_regclass(%s)", (_bv().EXCLUDED_TABLE,)).fetchone()[0] is None:
        raise RuntimeError(f"{_bv().EXCLUDED_TABLE} is missing (migration 0040): the people "
                           "exclusion cannot be applied, nothing built")
    ids = sorted(set(ids))
    out: set[str] = set()
    for i in range(0, len(ids), 5000):
        out |= {r[0] for r in write.execute(
            f"SELECT DISTINCT object_id FROM {_bv().EXCLUDED_TABLE} WHERE object_id = ANY(%s)",
            (ids[i:i + 5000],))}
    return out


def access_ids(write: Any, patterns: Iterable[Pattern]) -> dict[Pattern, int]:
    """The ``access_classes`` id of each pattern, a new pattern appended under the next id
    (ids never change once given). Inside the caller's transaction, the table locked
    against a concurrent build's allocation."""
    table = f"{_bv().SCHEMA}.access_classes"
    write.execute(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE")
    known = {_key((a, m, d, b)): i for i, a, m, d, b in write.execute(
        f"SELECT id, audience_keys, rec_marks, doc_marks, blocked FROM {table}")}
    nxt = max(known.values(), default=0) + 1
    out = {}
    for pat in sorted({_key(p) for p in patterns}):
        if pat not in known:
            if nxt > _bv().CLASS_MAX:
                raise RuntimeError(f"{table}: more than {_bv().CLASS_MAX} access patterns")
            write.execute(f"INSERT INTO {table} (id, audience_keys, rec_marks, doc_marks, "
                          "blocked) VALUES (%s, %s, %s, %s, %s)",
                          (nxt, list(pat[0]), list(pat[1]), list(pat[2]), pat[3]))
            known[pat] = nxt
            nxt += 1
        out[pat] = known[pat]
    return out


def _canon(text: Any) -> str:
    return json.dumps(json.loads(text) if isinstance(text, str) else text, sort_keys=True)


def col_class_ids(write: Any, patterns: Iterable[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """The ``col_classes`` id of each (marks, needs) pattern (canonical JSON), appended as
    ``access_ids`` does; id 0 is nothing marked."""
    table = f"{_bv().SCHEMA}.col_classes"
    write.execute(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE")
    known = {(_canon(m), _canon(n)): i for i, m, n in write.execute(
        f"SELECT id, marks::text, needs::text FROM {table}")}
    if ("{}", "{}") not in known:   # id 0, nothing marked (the migration seeds it)
        write.execute(f"INSERT INTO {table} (id, marks, needs) VALUES (0, '{{}}', '{{}}')")
        known[("{}", "{}")] = 0
    nxt = max(known.values(), default=0) + 1
    out = {}
    for pat in sorted(set(patterns)):
        if pat not in known:
            if nxt > _bv().CLASS_MAX:
                raise RuntimeError(f"{table}: more than {_bv().CLASS_MAX} column patterns")
            write.execute(f"INSERT INTO {table} (id, marks, needs) VALUES (%s, %s, %s)",
                          (nxt, pat[0], pat[1]))
            known[pat] = nxt
            nxt += 1
        out[pat] = known[pat]
    return out


def _versions(reader: Any, ids: Sequence[str]) -> dict[str, tuple[Any, dict[str, str]]]:
    """object id -> (audience key, {current version handle: markings}) of ``ids``."""
    out: dict[str, tuple[Any, dict[str, str]]] = {}
    ids = sorted(set(ids))
    for i in range(0, len(ids), 5000):
        for r in reader.conn.execute(
                "SELECT o.object_id, o.audience_key, v.l1_handle, v.markings FROM objects o "
                "LEFT JOIN object_versions v ON v.object_id = o.object_id AND "
                "v.superseded_at IS NULL WHERE o.object_id IN (SELECT value FROM "
                "json_each(:ids))", {"ids": json.dumps(ids[i:i + 5000])}):
            got = out.setdefault(r["object_id"], (r["audience_key"], {}))
            if r["l1_handle"] is not None:
                got[1][r["l1_handle"]] = r["markings"] or "[]"
    return out


def _resolve(reads: Mapping[str, set[str]], held: Mapping[str, tuple[Any, dict[str, str]]],
             doc_types: frozenset[str]) -> tuple[set[Any], set[str], bool, bool]:
    """(audience keys, marking sets, read a document, blocked) of the records ``reads``:
    a handle its version's markings, ``*`` (or a handle no longer current) every current
    version's; an unknown record blocks."""
    keys: set[Any] = set()
    marks: set[str] = set()
    doc = blocked = False
    for oid, handles in reads.items():
        got = held.get(oid)
        if got is None:
            blocked = True
            continue
        keys.add(got[0])
        doc = doc or oid.split(":", 1)[0] in doc_types
        for h in handles:
            if h and h != "*" and h in got[1]:
                marks.add(got[1][h])
            elif h:
                marks |= set(got[1].values()) or {"[]"}
    return keys, marks, doc, blocked


def _resolved_rows(rows: Sequence[Any], v: Any, name: str, reader: Any, write: Any,
                   doc_types: frozenset[str], notes: Counter) -> list[tuple]:
    """Each row's classes with everything it was read from resolved: its other records'
    keys, markings and ids join the row's pattern, a column's own records its column
    needs; a row read from an excluded person's record is dropped, a column only read from
    one is emptied."""
    ids = {o for r in rows for o in (*r.objects, *r.reads,
                                     *(i for c in r.col_reads.values() for i in c),
                                     *(i for c in r.col_names.values() for i in c))}
    held = _versions(reader, sorted(ids))
    gone = excluded_objects(write, ids)
    out = []
    for row in rows:
        if gone.intersection(row.objects) or gone.intersection(row.reads):
            notes["people_excluded_rows"] += 1
            continue
        keys, marks, doc, blocked = _resolve(row.reads, held, doc_types)
        aud, rec_marks, _d, blocked0 = access_pattern(row)
        doc = doc or any(o.split(":", 1)[0] in doc_types for o in row.objects)
        all_keys = set(aud) | keys
        pattern = (tuple(k for k in all_keys if isinstance(k, str) and k),
                   tuple(set(rec_marks) | marks), (UNREADABLE,) if doc else (),
                   blocked0 or blocked or any(not (isinstance(k, str) and k) for k in keys))
        needs, col_objects = {}, {}
        for col in sorted(set(row.col_reads) | set(row.col_link_marks)):
            reads = row.col_reads.get(col, {})
            if not reads and not row.col_link_marks.get(col):
                continue
            if gone.intersection(reads):
                notes["people_excluded_cells"] += 1
                row.values[col] = None
                row.cites.pop(col, None)
                continue
            ck, cm, cdoc, cblocked = _resolve(reads, held, doc_types)
            needs[col] = {"audience_keys": sorted(k for k in ck if isinstance(k, str) and k),
                          "rec_marks": sorted(cm | row.col_link_marks.get(col, set()) | {"[]"}),
                          "doc_marks": [UNREADABLE] if cdoc else [],
                          "blocked": cblocked or any(not (isinstance(k, str) and k)
                                                     for k in ck)}
            col_objects[col] = sorted(reads)
        for col, named in sorted(row.col_names.items()):
            # a cell naming a record no value was read from: only its exclusion matters
            if gone.intersection(named):
                notes["people_excluded_cells"] += 1
                row.values[col] = None
                row.cites.pop(col, None)
                col_objects.pop(col, None)
                continue
            col_objects[col] = sorted(set(col_objects.get(col, ())) | set(named))
        cols = (json.dumps(row.col_marks, sort_keys=True), json.dumps(needs, sort_keys=True))
        if v.fill is None:   # a curated view's systems: its copies' (never a comms table's)
            row.values.setdefault(_bv().SYSTEMS_COL, ",".join(row.systems) or None)
        out.append(((*[row.values.get(c.name) for c in v.cols], row.handle), _key(pattern),
                    cols, not _bv().freightcom(row, v),
                    (name, row.handle, row.objects, sorted(set(row.reads) - set(row.objects)),
                     json.dumps(col_objects, sort_keys=True),
                     json.dumps(row.cites, sort_keys=True), row.systems, row.copies)))
    return out


def build_view(name: str, reader: Any, write: Any, *, as_of: dt.date,
               digest: str | None = None) -> dict[str, Any]:
    """Build ``<schema>.<name>`` and swap it in, its classes, counts and lineage with it, in
    one transaction (``write``: a psycopg connection that may write the schema). A failed
    build or swap leaves the live table as it was and no ``_next`` table behind."""
    from operating.search.document_marks import DOCUMENT_TYPES
    bv = _bv()
    S = bv.SCHEMA
    v = bv.VIEWS[name]
    if v.fill is not None:   # a communications table: built and swapped by its own module
        from operating.pg import business_comms
        return business_comms.build_view(v, write, as_of=as_of, digest=digest)
    guarded = bv._guarded_views()
    leaning = bv.dependent_views(write, name)
    if leaning and guarded is None:
        # views over the live table (plx.sql's guarded views) would block the swap's DROP:
        # fail before the build's work, the served table and its views untouched
        raise RuntimeError(
            f"{S}.{name} has views over it ({', '.join(leaning)}): the swap re-creates "
            "them only through operating.pg.sql_views; build into a schema without them")
    started = time.monotonic()
    notes: Counter = Counter()
    nxt, old, lin = f"{name}_next", f"{name}_old", f"lineage_{name}_next"
    cols = ", ".join(f'"{n}"' for n, _ in bv.columns(v))
    lcols = ", ".join(bv.LINEAGE_COLS)
    rows = 0

    def put(chunk: Sequence[Any]) -> None:
        """Resolve a chunk of made rows and append it to the ``_next`` tables (its own
        transaction: what is held in memory is one chunk, never the whole view)."""
        nonlocal rows
        made = _resolved_rows(chunk, v, name, reader, write, DOCUMENT_TYPES, notes)
        if not made:
            return
        with write.transaction():
            # ids are append-only: one given for a build that then fails stays unused
            ids = access_ids(write, (m[1] for m in made))
            col_ids = col_class_ids(write, (m[2] for m in made))
            with write.cursor().copy(f"COPY {S}.{nxt} ({cols}) FROM STDIN") as cp:
                for vals, pat, cm, ai_ok, _l in made:
                    cp.write_row((*vals, ids[pat], col_ids[cm], ai_ok))
            with write.cursor().copy(f"COPY {S}.{lin} ({lcols}) FROM STDIN") as cp:
                for m in made:
                    cp.write_row(m[4])
        rows += len(made)

    try:
        with write.transaction():
            write.execute(f"DROP TABLE IF EXISTS {S}.{nxt}")
            write.execute(bv._ddl(v, nxt))
            write.execute(f"DROP TABLE IF EXISTS {S}.{lin}")
            write.execute(f"CREATE TABLE {S}.{lin} (LIKE {S}.lineage)")
        chunk: list[Any] = []
        for row in v.build(reader, as_of):
            chunk.append(row)
            if len(chunk) >= CHUNK:
                put(chunk)
                chunk = []
        put(chunk)
        chunk = []
        with write.transaction():
            _indexes(write, v, nxt)
        seconds = round(time.monotonic() - started, 2)
        with write.transaction():
            # everything heavy first: none of it locks a table a reader has open
            decided = _decide_staged(write, S, name, nxt, lin, guarded)
            write.execute(f"DELETE FROM {S}.class_counts WHERE view = %s", (name,))
            write.execute(f"INSERT INTO {S}.class_counts (view, access_class, col_class, "
                          f"rows) SELECT %s, access_class, col_class, count(*) FROM {S}.{nxt} "
                          "GROUP BY 2, 3", (name,))
            rows = write.execute(f"SELECT count(*) FROM {S}.{nxt}").fetchone()[0]
            write.execute(f"DELETE FROM {S}.lineage WHERE view = %s", (name,))
            write.execute(f"INSERT INTO {S}.lineage ({lcols}) SELECT {lcols} FROM {S}.{lin}")
            write.execute(f"DROP TABLE {S}.{lin}")
            write.execute(
                f"INSERT INTO {S}.state (view, built_at, digest, rows, seconds, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (view) DO UPDATE SET "
                "built_at = excluded.built_at, digest = excluded.digest, rows = excluded.rows, "
                "seconds = excluded.seconds, notes = excluded.notes",
                (name, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), digest,
                 rows, seconds, json.dumps({"as_of": as_of.isoformat(),
                                                 "code": _code().fingerprint(v),
                                                 "people_excluded_rows":
                                                     notes["people_excluded_rows"],
                                                 "people_excluded_cells":
                                                     notes["people_excluded_cells"]})))

            def swap() -> None:
                follow = not _still_decided(write, decided)
                if guarded is not None:   # plx.sql's guarded view reads the live table
                    guarded.drop(write, name, S)
                write.execute(f"DROP TABLE IF EXISTS {S}.{old}")
                write.execute(f"ALTER TABLE IF EXISTS {S}.{name} RENAME TO {old}")
                write.execute(f"ALTER TABLE {S}.{nxt} RENAME TO {name}")
                write.execute(f"DROP TABLE IF EXISTS {S}.{old}")
                if guarded is not None:
                    guarded.create(write, v, S, **({} if follow else {"follow": False}))
                write.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                              "'plx_api') THEN EXECUTE 'GRANT SELECT ON " + S + "." + name +
                              " TO plx_api'; END IF; END $$")
            swap_lock.swap_in(write, swap)
    except BaseException:
        _drop_leftovers(write, S, (nxt, lin))
        raise
    return {"view": name, "rows": rows, "seconds": seconds}


def _decide_staged(write: Any, schema: str, name: str, nxt: str, lin: str,
                   guarded: Any) -> str | None:
    """Decide the staged table's rows against the people exclusion now, before the swap
    takes its locks (``door.exclude(staged=)``: the lineage scan it costs no longer runs
    while readers are locked out). Returns the exclusion stamp it decided against, or
    None where the swap's own ``door.follow`` must decide it (no door, an old-shape table,
    a schema whose stamp was never recorded: that one decides the whole schema)."""
    if guarded is None:
        return None
    from operating.pg import door
    try:
        stamp = door.exclusion_stamp(write)
    except LookupError:
        return None
    if write.execute("SELECT to_regclass(%s)", (f"{schema}.{door.STATE}",)).fetchone()[0] is None:
        return None   # a schema with no door yet: the swap's follow makes it and decides it
    if (write.execute(f"SELECT excluded_at FROM {schema}.{door.STATE}").fetchone() or
            (None,))[0] is None or not door.is_v2(write, schema, nxt):
        return None
    door.exclude(write, schema, only=name, record=False,
                 staged=(f"{schema}.{nxt}", f"{schema}.{lin}"))
    return stamp


def _still_decided(write: Any, stamp: str | None) -> bool:
    """Whether the set is still the one the staged table was decided against. Asked with
    the swap's locks held: a set that grows after this waits for the swap's commit, then
    decides the table's rows itself; one that grew before this is a slow path (the swap's
    own ``door.follow``)."""
    if stamp is None:
        return False
    from operating.pg import door
    try:
        return door.exclusion_stamp(write) == stamp
    except LookupError:
        return False


def sweep_leftovers(write: Any) -> list[str]:
    """Drop every ``_next`` table a stopped build left in the schema (a build killed
    between its chunk commits and its swap: the time limit's SIGKILL, a lost connection),
    and a communications build's stage (``<view>_next_stage``, ``_next_stage_text``: its
    readers commit them). Only under the one-build lock (``REFRESH_LOCK``): no other build
    owns them."""
    S = _bv().SCHEMA
    got = [r[0] for r in write.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind = 'r' AND c.relname ~ "
        "'_next(_stage(_text)?)?$' ORDER BY 1", (S,))]
    for t in got:
        write.execute(f'DROP TABLE IF EXISTS {S}."{t}"')
    return got


def _drop_leftovers(write: Any, schema: str, tables: Sequence[str]) -> None:
    """A failed build's ``_next`` tables (committed by its first transaction) go."""
    try:
        with write.transaction():
            for t in tables:
                write.execute(f"DROP TABLE IF EXISTS {schema}.{t}")
    except Exception:  # noqa: BLE001 - the original failure is what is reported
        pass


def _indexes(write: Any, v: Any, table: str) -> None:
    write.execute(f"CREATE UNIQUE INDEX ON {_bv().SCHEMA}.{table} (handle)")
    write.execute(f"CREATE INDEX ON {_bv().SCHEMA}.{table} (access_class)")
    trgm = write.execute("SELECT 1 FROM pg_opclass WHERE opcname = 'gin_trgm_ops'"
                         ).fetchone() is not None
    for c in v.cols:
        if c.name.endswith("_handle") or c.name in _bv().STATUS_COLS or (
                c.kind == "date" and c.name != "as_of"):
            write.execute(f'CREATE INDEX ON {_bv().SCHEMA}.{table} ("{c.name}")')
        elif c.name in _bv().NAME_COLS and c.kind == "text" and trgm:
            write.execute(f'CREATE INDEX ON {_bv().SCHEMA}.{table} USING gin ("{c.name}" '
                          "gin_trgm_ops)")


FEED = "ontology.change_feed"


def feed_head(conn: Any) -> int | None:
    """The change feed's newest seq (migration 0026; None without it): what a view built
    now has seen."""
    if conn.execute("SELECT to_regclass(%s)", (FEED,)).fetchone()[0] is None:
        return None
    pruned, head = conn.execute(
        "SELECT coalesce((SELECT position FROM ontology.change_cursors WHERE consumer = "
        f"'_pruned'), 0), coalesce((SELECT max(seq) FROM {FEED}), 0)").fetchone()
    return max(int(pruned), int(head))


def moved_since(conn: Any, seq: Any, types: Sequence[str]) -> bool:
    """Whether a record of ``types`` (or of no known type, or a migration's scope) changed
    after feed seq ``seq``; True when that cannot be told (no feed, no seq, rows past it
    pruned): the view is rebuilt."""
    if not isinstance(seq, int) or isinstance(seq, bool) or feed_head(conn) is None:
        return True
    pruned = conn.execute("SELECT coalesce((SELECT position FROM ontology.change_cursors "
                          "WHERE consumer = '_pruned'), 0)").fetchone()[0]
    if seq < int(pruned):
        return True
    return conn.execute(
        f"SELECT EXISTS (SELECT 1 FROM {FEED} WHERE seq > %s AND (object_type = ANY(%s) OR "
        "object_type IS NULL OR kind = 'm'))", (seq, sorted(set(types)))).fetchone()[0]


def stale_views(meta: Any, views: Mapping[str, Any], now: str, when: dt.date,
                force: bool = False) -> list[str]:
    """The views to rebuild: missing, built on another day (aging and lateness are by
    day), or (with the change feed) a record of a type the view reads changed since its
    build; without the feed, any view whose store stamp moved.
    Order: missing views first, then those of another day, then those whose inputs moved
    (a communications table after the business views of its rank, whose rows it names), so
    a run stopped part way (its time limit) never spends the next run rebuilding the views
    it finished while a missing one waits. A view whose own code changed since its build
    (``business_views_code``) is rebuilt with the moved ones."""
    built = {r[0]: (r[1], r[2]) for r in meta.execute(
        f"SELECT view, digest, notes FROM {_bv().SCHEMA}.state")}
    feed = feed_head(meta) is not None
    code = set(_code().code_changed(meta, _bv().SCHEMA, views))
    out = []
    for at, (name, v) in enumerate(views.items()):
        comms = getattr(v, "fill", None) is not None
        if force or name not in built:
            out.append((0, comms, at, name))
            continue
        digest, notes = built[name]
        notes = notes if isinstance(notes, dict) else json.loads(notes or "{}")
        if notes.get("as_of") != when.isoformat():
            out.append((1, comms, at, name))
        elif name in code:   # the view's own code changed since its build
            out.append((2, comms, at, name))
        elif digest != now and (not feed or moved_since(meta, notes.get("feed"), v.reads)):
            out.append((2, comms, at, name))
    return [name for *_k, name in sorted(out)]
