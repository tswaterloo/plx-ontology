"""Prototype: a plan's build steps run against Postgres in place, without the step input copy.

``ontology.step_migrate.build_layer`` copies every row the steps may read (the served
ontology's rows of their declared types, the unit layer laid over them) into a SQLite
input: ~100-130 GB at full scale. Here (``python3 -m ontology.pg_step_inplace build``)
the same steps run with the same code, reading the ontology itself:

* one Postgres transaction, opened with the apply's lock: the unit layer applied in it
  (``ontology_migrate._apply_units``, as the combined apply does), then the served links
  of the re-run steps deleted (their prefixes, from their from types, as ``_apply_steps``
  deletes them) and the served links of the later steps not re-run set aside, so each
  step sees what ``build_layer`` shows it: the store after the unit migration, the
  other steps' links in name order, never its own;
* each step in name order (one at a time) with a builder whose reads (``SELECT`` /
  ``WITH``, :func:`operating.pg.sqlite_compat.read_only`) go to that transaction through
  ``PgCompatConnection`` (the read tools' translation of the store SQL) and whose writes
  go to a small SQLite store of its own; after it, its links are inserted into the
  transaction (``ontology_delta._insert``) for the steps after it, and the steps' links
  gathered into one store;
* the step layer written from that store as ``build_layer`` writes it (same record and
  content digest), then the transaction rolled back to the point after the unit layer,
  and, with ``--commit`` only (a scratch database), ``_apply_steps`` and the migration
  records applied and committed: the combined apply's rows.

Fails closed (``Refused``) where the prototype would not show a step what
``build_layer`` shows it: a plan re-deriving only some from types, a step re-marking
text, a step owning values that the store holds or that writes values, a step that
reads link or object rows after it wrote some (its reads would miss its own writes),
a statement Postgres cannot answer (raised as the compat layer raises it), and a step's
worker process opening the builder's file (``temp`` is a file that is no database). The
packed current-value read of ``ontology.mc_task_links.current_values`` is answered in
Postgres' one-cell-per-field shape (:data:`_PG_CELLS`); any other read of the packed
layouts raises.

Stdlib and psycopg. Not wired into the launcher: measured on the 1% sample only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence


class Refused(RuntimeError):
    """The prototype cannot show a step what ``build_layer`` would: nothing is written."""


# The current-value reads of ``mc_task_links._CELLS`` and ``sender_parties._CELLS`` on the
# packed SQLite layouts, in Postgres' shape (one cell per field, its kind on the cell:
# ``operating.pg.ontology_load``): the same rows, one per field of a current cell
_PACKED_FROM = "FROM value_layout_fields lf JOIN value_cells c ON c.layout = lf.layout"
_PG_SHAPE = (("CASE WHEN c.field IS NULL THEN json_extract(c.value, lf.path) ELSE c.value "
              "END AS value", "c.value AS value"),
             (_PACKED_FROM, "FROM value_cells c"),
             ("k.kind = lf.kind", "k.kind = c.kind"),
             ("lf.field", "c.field"))
_LINK_TABLES = re.compile(r"\b(link_rows|links)\b", re.I)
_OBJECT_TABLES = re.compile(r"\b(objects|object_versions|object_values|identifiers|"
                            r"value_cells|value_sources)\b", re.I)


def _read_only(sql: str) -> bool:
    """A statement that only reads (``SELECT`` / ``WITH`` writing nothing)."""
    return bool(re.match(r"(?is)\s*(SELECT|WITH)\b", sql)) and not re.search(
        r"(?i)\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", sql)


def pg_form(sql: str) -> str:
    """``sql`` as the Postgres ontology answers it: the packed current-value read in
    Postgres' shape; any other read of the packed layouts refused."""
    if _PACKED_FROM in sql:
        for old, new in _PG_SHAPE:
            sql = sql.replace(old, new)
    if "value_layout_fields" in sql or re.search(r"\blf\.", sql):
        raise Refused("packed_layout_read")
    return sql


class _SplitCursor:
    def __init__(self, split: "SplitConnection") -> None:
        self._split = split
        self._cur: Any = None
        self.row_factory: Any = None

    def execute(self, sql: str, params: Any = ()) -> "_SplitCursor":
        self._cur = self._split._route(sql, params, self.row_factory)
        return self

    def executemany(self, sql: str, rows: Any) -> "_SplitCursor":
        self._cur = self._split.executemany(sql, rows)
        return self

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()

    def fetchmany(self, size: int = 1) -> list[Any]:
        return self._cur.fetchmany(size)

    def __iter__(self) -> Any:
        return iter(self._cur)

    @property
    def rowcount(self) -> int:
        return getattr(self._cur, "rowcount", -1)

    @property
    def description(self) -> Any:
        return getattr(self._cur, "description", None)

    def close(self) -> None:
        pass


class SplitConnection:
    """The step's ``builder.connection``: reads to Postgres (``pg``, a
    ``PgCompatConnection`` inside the migration's transaction), writes and everything
    else to the step's own SQLite store (``local``). ``reads`` counts the statements."""

    def __init__(self, local: sqlite3.Connection, pg: Any) -> None:
        self.local = local
        self.pg = pg
        self.reads = 0
        self.local_reads = 0

    def _wrote(self, pattern: re.Pattern[str], sql: str) -> bool:
        if not pattern.search(sql):
            return False
        if pattern is _LINK_TABLES:
            return self.local.execute("SELECT EXISTS (SELECT 1 FROM link_rows)").fetchone()[0]
        return self.local.execute("SELECT EXISTS (SELECT 1 FROM objects)").fetchone()[0]

    def _route(self, sql: str, params: Any, row_factory: Any = None) -> Any:
        if _read_only(sql):
            if re.search(r"\btemp\.|\bsqlite_(master|schema|temp_master)\b", sql, re.I):
                raise Refused("local_read:" + sql[:80])
            if self._wrote(_LINK_TABLES, sql) or self._wrote(_OBJECT_TABLES, sql):
                raise Refused("read_after_write:" + sql[:80])
            self.reads += 1
            return self.pg.execute(pg_form(sql), params)
        cur = self.local.cursor()
        if row_factory is not None:
            cur.row_factory = row_factory
        return cur.execute(sql, params)

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._route(sql, params, self.local.row_factory)

    def executemany(self, sql: str, rows: Any) -> Any:
        return self.local.executemany(sql, rows)

    def executescript(self, sql: str) -> Any:
        return self.local.executescript(sql)

    def cursor(self) -> _SplitCursor:
        return _SplitCursor(self)

    def create_function(self, *args: Any, **kwargs: Any) -> None:
        self.local.create_function(*args, **kwargs)
        self.pg.create_function(*args, **kwargs)

    def commit(self) -> None:
        self.local.commit()   # the Postgres transaction ends with the migration

    @property
    def in_transaction(self) -> bool:
        return self.local.in_transaction

    def __getattr__(self, name: str) -> Any:
        return getattr(self.local, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "row_factory":
            self.local.row_factory = value
            return
        object.__setattr__(self, name, value)


class SplitBuilder:
    """A ``StoreBuilder`` whose connection reads Postgres (:class:`SplitConnection`);
    ``temp`` names a file that is no database, so a worker process opening the store
    fails instead of reading an empty one."""

    def __init__(self, inner: Any, pg: Any, poison: Path) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "connection", SplitConnection(inner.connection, pg))
        object.__setattr__(self, "temp", poison)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)


def _poison(folder: Path) -> Path:
    path = folder / "no-step-input.sqlite"
    path.write_bytes(b"pg_step_inplace: the step input is Postgres, not this file\n" * 128)
    return path


def _local_store(path: Path) -> Any:
    from ontology.project import open_generation
    from ontology.store import StoreBuilder
    path.unlink(missing_ok=True)
    builder = StoreBuilder(path, foreign_keys=False)
    open_generation(builder)
    return builder


def _link_rows(conn: sqlite3.Connection) -> Iterable[list[dict[str, Any]]]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM links ORDER BY rowid")
    while True:
        batch = cur.fetchmany(20_000)
        if not batch:
            return
        yield [dict(r) for r in batch]


def _range_where(owned: Sequence[Mapping[str, Any]]) -> tuple[str, list[Any]]:
    parts, params = [], []
    for o in owned:
        parts.append("(link_id COLLATE \"C\" >= %s AND link_id COLLATE \"C\" < %s AND "
                     "(%s::text[] IS NULL OR EXISTS (SELECT 1 FROM unnest(%s::text[]) f "
                     "WHERE starts_with(from_object, f))))")
        params += [o["prefix"], o["end"], o["from"], o["from"]]
    return " OR ".join(parts) or "FALSE", params


def build_layer(the_plan: Mapping[str, Any], new: Any, steps: Mapping[str, Any], dsn: str,
                out: str | Path, *, sources: Mapping[str, Any] | None = None,
                unit_layer: str | Path | None = None, normalizer: Any = None,
                delta_mark: int = 0, sequence: int | None = None,
                commit: bool = False) -> dict[str, Any]:
    """``step_migrate.build_layer``'s layer, the steps reading Postgres in place (module
    docstring). ``commit``: apply both layers in the same transaction and commit (a
    scratch database only; the caller decides)."""
    from ontology import step_migrate as sm
    from ontology.schema_migrate import content_digest, read_layer
    from operating.pg import ontology_migrate as om
    from operating.pg.ontology_delta import _insert
    from operating.pg.sqlite_compat import PgCompatConnection

    specs = sm.STEPS
    run = dict(the_plan["steps"]["run"])
    if the_plan["steps"]["refused"]:
        raise ValueError("step_migration_refused:" + ",".join(the_plan["steps"]["refused"]))
    if the_plan["steps"].get("remark"):
        raise Refused("remark_unsupported")
    if any(v is not None for v in run.values()):
        raise Refused("from_types_unsupported")
    missing = sorted(set(run) - set(steps))
    if missing:
        raise ValueError("steps_missing:" + ",".join(missing))
    unit = read_layer(unit_layer) if unit_layer is not None else None
    units = the_plan.get("units") or {}
    if unit is None and ((units.get("units") or {}).get("affected")
                         or (units.get("units") or {}).get("dropped")):
        raise ValueError("unit_layer_required")
    if unit is not None and unit.get("plan") != units.get("id"):
        raise ValueError("unit_layer_of_another_plan")
    owned = [o for name in sorted(run) for o in sm._owned(name, run[name], specs)]
    values_owned = sm._owned_values(run, specs)
    order = sorted(steps)
    first = min(run) if run else None
    names = [n for n in order if first is not None and n >= first]
    # the served links of steps not re-run but after the first: shown at their turn
    held = [o for name in names if name not in run
            for o in sm._owned(name, None, specs)]
    started = time.monotonic()
    out = Path(out)
    out.unlink(missing_ok=True)
    report: dict[str, Any] = {"id": the_plan["id"], "steps": {}, "run": run,
                              "mode": "pg_inplace"}
    folder = Path(tempfile.mkdtemp(prefix=".pg-inplace-", dir=out.parent))
    pg = PgCompatConnection(dsn, statement_timeout_ms=0)
    cur = pg._pg.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute(om.LOCK)
        cur.execute("SET LOCAL jit = off")
        t = time.monotonic()
        if unit is not None:
            served = om.served_schema(cur)
            if served != unit["from"]:
                raise Refused(f"other_schema:{served}")
            unit_report: dict[str, Any] = {}
            om._apply_units(cur, Path(unit_layer), unit, unit_report)
            report["unit_apply"] = {"seconds": round(time.monotonic() - t, 3)}
        cur.execute("SAVEPOINT after_units")
        where, params = _range_where(owned)
        cur.execute(f"DELETE FROM ontology.link_rows WHERE {where}", params)
        report["input"] = {"links_rederived": cur.rowcount}
        cur.execute("CREATE TEMP TABLE held_links ON COMMIT DROP AS SELECT * FROM "
                    "ontology.link_rows WHERE FALSE")
        for o in held:
            w, p = _range_where([o])
            cur.execute(f"WITH moved AS (DELETE FROM ontology.link_rows WHERE {w} "
                        "RETURNING *) INSERT INTO held_links SELECT * FROM moved", p)
        report["input"]["links_held"] = cur.execute(
            "SELECT count(*) FROM held_links").fetchone()[0]
        if values_owned:
            vw, vp = sm.owned_values_where(values_owned)
            found = pg.execute("SELECT count(*) FROM object_values WHERE " + vw,
                               vp).fetchone()[0]
            if found:
                raise Refused(f"values_owned_unsupported:{found}")
        report["input"]["seconds"] = round(time.monotonic() - t, 3)
        poison = _poison(folder)
        every = _local_store(folder / "links.sqlite")
        try:
            for name in names:
                t = time.monotonic()
                if name in run:
                    spec = specs[name]
                    srcs = sm._sources_of(new, sources or {}, run[name]) if spec.documents \
                        else dict(sources or {})
                    projector = sm._projector(new, srcs, normalizer)
                    local = _local_store(folder / f"{name}.sqlite")
                    try:
                        split = SplitBuilder(local, pg, poison)
                        result = steps[name](split, projector)
                        local.connection.commit()
                        lc = local.connection
                        if lc.execute("SELECT EXISTS (SELECT 1 FROM object_values)").fetchone()[0]:
                            raise Refused(f"values_written:{name}")
                        n = _insert(lc, str(local.temp), cur, "[]", every_link=True)
                        for rows in _link_rows(lc):
                            every.add("links", rows)
                        every.connection.commit()
                        report["steps"][name] = {
                            "seconds": round(time.monotonic() - t, 3), "report": result,
                            "mode": "pg", "reads": split.connection.reads,
                            "links": n.get("copied.link_rows")}
                    finally:
                        local.abort()
                for o in (o for o in held if sm.step_of(o["prefix"], specs) == name):
                    w, p = _range_where([o])
                    cur.execute(f"INSERT INTO ontology.link_rows SELECT * FROM held_links "
                                f"WHERE {w}", p)
            t = time.monotonic()
            layer = _local_store(folder / "layer.sqlite")
            try:
                with tempfile.TemporaryDirectory(prefix=".parts-", dir=folder) as parts:
                    links, ends = sm.write_layer_links(layer, every.connection, every.temp,
                                                       [] if staged else owned, 1,
                                                       Path(parts))
                links += sum(int(v["written"]) for v in staged.values())
                ends = set(ends) | {e for v in staged.values() for e in v.get("ends") or ()}
                layer.set_meta("format", sm.FORMAT)
                layer.set_meta("steps_digest", the_plan["steps_to"])
                c = layer.connection
                scope = sorted(ends | {p for o in owned if o["from"] for p in o["from"]}
                               | {o["prefix"] for o in values_owned})
                content = content_digest(c, ("link_rows", "objects", "object_versions",
                                             "value_sources", "value_cells"))
                sequence = int(time.time() * 1000) if sequence is None else int(sequence)
                unit_id = unit["id"] if unit is not None else None
                record = {"id": f"{sm._sha([the_plan['id'], unit_id, content, sequence])[:16]}"
                                f"-{sequence}",
                          "plan": the_plan["id"], "unit": unit_id, "content": content,
                          "sequence": sequence, "from": the_plan["steps_from"],
                          "to": the_plan["steps_to"], "owned": owned, "values": values_owned,
                          "scope": scope, "steps": sorted(run),
                          "inputs": sm.declared_inputs(run, (new,), specs),
                          "delta_mark": int(delta_mark),
                          "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                c.execute(f"CREATE TABLE {sm.TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                c.executemany(f"INSERT INTO {sm.TABLE} VALUES (?, ?)",
                              [(k, json.dumps(v, sort_keys=True)) for k, v in record.items()])
                path = layer.keep_as_part()
            except BaseException:
                layer.abort()
                raise
            report["layer_seconds"] = round(time.monotonic() - t, 3)
        finally:
            every.abort()
        Path(path).replace(out)
        report.update(id=record["id"], links=links, scope=scope, content=content)
        cur.execute("ROLLBACK TO SAVEPOINT after_units")
        if commit:
            t = time.monotonic()
            applied: dict[str, Any] = {"steps": {}}
            om._apply_steps(cur, out, record, applied)
            for rec, rid, rep in ((unit, unit["id"] if unit else None, unit_report
                                   if unit else None),
                                  (record, "steps:" + record["id"], applied["steps"])):
                if rec is not None:
                    cur.execute("INSERT INTO ontology.schema_migrations (id, from_digest, "
                                "to_digest, scope, report) VALUES (%s, %s, %s, %s, %s)",
                                (rid, rec["from"], rec["to"], json.dumps(rec["scope"]),
                                 json.dumps(rep, sort_keys=True, default=str)))
            cur.execute("COMMIT")
            report["committed"] = {"seconds": round(time.monotonic() - t, 3)}
        else:
            cur.execute("ROLLBACK")
    except BaseException:
        with contextlib.suppress(Exception):
            cur.execute("ROLLBACK")
        raise
    finally:
        pg.close()
        for p in sorted(folder.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        folder.rmdir()
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


# ------------------------------------------------------------------ committed: run

# A session's view of the served links while one step runs (``run``): the served link
# rows the plan hides from this step left out, the links the steps before it re-derived
# (``run_rows``) and its own so far (``step_rows``) laid over them, as ``link_rows`` and
# ``links`` (temporary views: the session's search path finds them before ``ontology``).
_LINKS_VIEW = """CREATE OR REPLACE TEMP VIEW links AS SELECT
    l.link_id, l.link_type, l.from_object, l.to_object, l.tier, l.binding_kind,
    l.link_contract,
    coalesce(l.reason, ontology.link_text(l.reason_id, l.slots, l.ek + 1, 64)) AS reason,
    l.why_not,
    coalesce(l.evidence_handles, ontology.link_text(l.evidence_id, l.slots, 1, l.ek))
        AS evidence_handles,
    l.reviewed_by, l.reviewed_at, l.review_action_id, l.markings, l.recorded_at,
    l.superseded_at
  FROM pg_temp.{rows} l"""
# the corroboration's enumeration of the step's own lead links (``mentions.corroborate_links``):
# in a full run the store holds only this step's links of its types, here every served one
_OWN_ENUMERATION = re.compile(r"SELECT DISTINCT from_object FROM links WHERE binding_kind = \? "
                              r"AND tier = \? AND link_type IN \(")
_OWN_DELETE = re.compile(r"\s*DELETE FROM links WHERE substr\(link_id, 1, \?\) = \?\s*$")
_LINK_WRITE = re.compile(r"\s*(UPDATE\s+links\b|DELETE\s+FROM\s+links\b)", re.I)
_PG_TEMP = re.compile(r"\b(?:temp\.)?(plx_mention_threads)\b")
# ``mentions.corroborate_links``' conversation table, made once for a staged run
_THREADS_DROP = re.compile(r"DROP TABLE IF EXISTS temp\.plx_mention_threads")
_THREADS_INDEX = re.compile(r"CREATE INDEX temp\.plx_mention_threads_by_value ON "
                            r"plx_mention_threads\(value\)")
_THREADS_MAKE = re.compile(r"CREATE TABLE temp\.plx_mention_threads AS (?P<select>SELECT "
                           r"value, object_id, l1_handle FROM object_values WHERE "
                           r"property = \? AND value IS NOT NULL)")
_ROW_NO_BASE = 1 << 60


class Overlay:
    """The session objects of :data:`_LINKS_VIEW` on ``pg`` (a ``PgCompatConnection``):
    ``run_rows`` and ``step_rows`` (as ``ontology.link_rows``, texts plain) and the views.
    Created once; a lost session is never silently re-created (``OverlayPg``).
    (A lower ``toast_tuple_target`` makes them larger, not smaller: their texts are
    digests and handles that hardly compress, and each moved value is a TOAST row,
    3.1 KB a link against 2.2 KB, measured 11-11.)"""

    def __init__(self, pg: Any) -> None:
        self.pg = pg
        self.columns = [r[0] for r in self._raw(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = "
            "'ontology' AND table_name = 'link_rows' ORDER BY ordinal_position").fetchall()]
        for table in ("run_rows", "step_rows"):
            self._raw(f"CREATE TEMP TABLE {table} (LIKE ontology.link_rows INCLUDING DEFAULTS)")
            self._raw(f"CREATE UNIQUE INDEX ON {table} (link_id)")
            self._raw(f"CREATE INDEX ON {table} (from_object, link_type)")
            self._raw(f"CREATE INDEX ON {table} (to_object, link_type)")
        self._raw("CREATE TEMP SEQUENCE overlay_row_no START %s" % _ROW_NO_BASE)
        self._raw(_LINKS_VIEW.format(rows="step_rows").replace("TEMP VIEW links",
                                                               "TEMP VIEW step_links"))
        self.show([])

    def _raw(self, sql: str, params: Any = ()) -> Any:
        return self.pg._pg.execute(sql, params)

    def show(self, hidden: Sequence[Mapping[str, Any]], own: bool = True,
             staged_before: str | None = None, part_before: int | None = None) -> None:
        """The views for one step: the served links of ``hidden`` (owned entries; one
        naming ``objects`` hides the links from the session's ``scope_docs``) left out.
        ``own`` False: ``step_rows`` is not in them (a step whose own links are read from
        its local store: :data:`OWN_LINKS_LOCAL`).
        ``scope_docs`` is probed by its key per row (``EXISTS``), never ``IN (SELECT ...)``:
        that is a hashed subplan built again on every read through the view, 41 ms a read
        with a 108k-object part and ~0.5 s with 1.2M (0.11 ms with the probe; served plx,
        11-11), the reason the scoped mention parts did not finish.
        ``staged_before`` (a step name): the steps before it staged their links
        (``operating.pg.step_staging``): the served links they replaced or removed left out
        and their staged links shown, read in place (no ``run_rows`` copy)."""
        parts, params = [], []
        for o in hidden:
            if o.get("objects"):
                parts.append("(l.link_id COLLATE \"C\" >= %s AND l.link_id COLLATE \"C\" < %s "
                             "AND EXISTS (SELECT 1 FROM pg_temp.scope_docs s "
                             "WHERE s.object_id = l.from_object))")
                params += [o["prefix"], o["end"]]
                continue
            parts.append("(l.link_id COLLATE \"C\" >= %s AND l.link_id COLLATE \"C\" < %s "
                         "AND (%s::text[] IS NULL OR EXISTS (SELECT 1 FROM unnest(%s::text[]) "
                         "f WHERE starts_with(l.from_object, f))))")
            params += [o["prefix"], o["end"], o["from"], o["from"]]
        where = " OR ".join(parts) or "FALSE"
        cur = self.pg._pg.cursor()
        hide = cur.mogrify(where, params)
        cols = ", ".join(self.columns)
        keep, staged = f"NOT ({hide})", ""
        if staged_before is not None:
            from operating.pg import step_staging
            gone, rows, sparams = step_staging.overlay_sql(staged_before, part_before)
            if self._raw("SELECT to_regclass(%s)", (f"{step_staging.schema()}.links",)
                         ).fetchone()[0] is not None:
                half = len(sparams) // 2
                keep += f" AND {cur.mogrify(gone, sparams[:half])}"
                staged = f" UNION ALL SELECT {cols} {cur.mogrify(rows, sparams[half:])}"
        self._raw(f"CREATE OR REPLACE TEMP VIEW link_rows AS SELECT {cols} FROM "
                  f"ontology.link_rows l WHERE {keep}{staged} UNION ALL SELECT {cols} "
                  f"FROM pg_temp.run_rows" + (f" UNION ALL SELECT {cols} FROM "
                                              f"pg_temp.step_rows" if own else ""))
        self._raw(_LINKS_VIEW.format(rows="link_rows"))

    def add(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        """Plain link rows (``store_ddl.LINK_COLUMNS``) into ``table``, numbered from a block
        of the overlay's sequence reserved for them (in their order). Never an UPDATE over
        the table: that rescanned and renumbered every earlier row on each call (quadratic;
        16 s a call on served plx for document_fields)."""
        from ontology import store_ddl
        cols = list(store_ddl.LINK_COLUMNS)
        rows = list(rows)
        if not rows:
            return 0
        (last,) = self._raw("SELECT setval('overlay_row_no', nextval('overlay_row_no') + %s)",
                            (len(rows) - 1,)).fetchone()
        first = last - len(rows) + 1
        with self.pg._pg.cursor() as cur:
            with cur.copy(f"COPY pg_temp.{table} ({', '.join(cols)}, row_no) FROM STDIN") as cp:
                for n, row in enumerate(rows):
                    cp.write_row([row.get(c) for c in cols] + [first + n])
        return len(rows)

    def promote(self, store: Path | None = None) -> None:
        """The finished step's rows join ``run_rows`` (``store``: a step whose own links
        were kept in its local store only, :data:`OWN_LINKS_LOCAL`: read from there)."""
        if store is not None:
            for rows in _store_link_batches(store):
                self.add("run_rows", rows)
        self._raw("INSERT INTO pg_temp.run_rows SELECT * FROM pg_temp.step_rows")
        self._raw("TRUNCATE pg_temp.step_rows")
        self._raw("ANALYZE pg_temp.run_rows")

    def scope_handles(self) -> set[str]:
        """The version handles of the objects in ``scope_docs``."""
        return {r[0] for r in self._raw(
            "SELECT DISTINCT v.l1_handle FROM ontology.object_versions v JOIN "
            "pg_temp.scope_docs s ON s.object_id = v.object_id").fetchall()}

    def scope(self, objects: Iterable[str]) -> int:
        self._raw("CREATE TEMP TABLE IF NOT EXISTS scope_docs (object_id text PRIMARY KEY)")
        self._raw("TRUNCATE pg_temp.scope_docs")
        n = 0
        with self.pg._pg.cursor() as cur:
            with cur.copy("COPY pg_temp.scope_docs (object_id) FROM STDIN") as cp:
                for oid in objects:
                    cp.write_row((oid,))
                    n += 1
        self._raw("ANALYZE pg_temp.scope_docs")
        return n


# Per-object link reads a step repeats for every object of a type (``key_links``:
# ``FirmParties._hop`` and ``_confirming``, ``one_record``; ``identity_scoring._PartyTwin``):
# through the overlay view each is a round trip of ~2.5 ms, and the twin judge's link_id
# subquery over the view ~57 s (served plx, 11-10). After ``BATCH_AFTER`` calls of one
# shape with the same link type (and other parameters), the shape is answered from ONE
# set-based read of that link type, partitioned by the object it was keyed on: the same
# rows by construction (the per-object predicate is the batch predicate plus an equality
# on the key column). Only the shapes below; anything else goes to Postgres as before.
BATCH_AFTER = int(os.environ.get("PLX_INPLACE_BATCH_AFTER", "16"))   # 0: every call (proofs)
# The steps whose per-object link reads are batched (measured: the ones that repeat them
# per object of a type; a step adding links between such reads would re-read each batch).
BATCHED_STEPS = frozenset({"key_joins", "twins_parties"})
_SPACES = re.compile(r"\s+")
_KEYED = re.compile(r"^SELECT (?P<cols>\w+(?:, \w+)*) FROM link_rows WHERE "
                    r"(?P<key>from_object|to_object) = \? AND link_type = \? AND "
                    r"(?P<rest>.+)$")
# what the rest may hold: conjuncts of a column against NULL, a literal word or ``?``
_CONJUNCT = r"\w+ (?:IS (?:NOT )?NULL|(?:=|<>) (?:\?|'\w+')|IN \((?:\?|'\w+')(?:, ?(?:\?|'\w+'))*\))"
_REST = re.compile(rf"^{_CONJUNCT}(?: AND {_CONJUNCT})*$")
# ``mentions._store_candidates``' read of one candidate key, and the same read for every key
# of a session table (in the same order within a key: the key first, then the read's own)
_CANDIDATES = (
    "SELECT i.object_id, o.object_type, i.identifier_kind, i.normalized, i.property, "
    "i.role, i.l1_handle, i.field_path, i.lineage, i.recorded_at, i.superseded_at, "
    "v.digest, v.markings FROM identifiers i JOIN objects o ON o.object_id = i.object_id "
    "JOIN object_versions v ON v.object_id = i.object_id AND v.l1_handle = i.l1_handle "
    "WHERE i.identifier_kind = ? AND i.normalized = ? AND i.object_id <> ? "
    "ORDER BY i.object_id, i.recorded_at, i.l1_handle, i.field_path")
_CANDIDATES_BATCH = (
    "SELECT i.object_id, o.object_type, i.identifier_kind, i.normalized, i.property, "
    "i.role, i.l1_handle, i.field_path, i.lineage, i.recorded_at, i.superseded_at, "
    "v.digest, v.markings, k.kind AS plx_key_kind, k.value AS plx_key_value "
    "FROM pg_temp.plx_keys k JOIN identifiers i ON i.identifier_kind = k.kind AND "
    "i.normalized = k.value JOIN objects o ON o.object_id = i.object_id "
    "JOIN object_versions v ON v.object_id = i.object_id AND v.l1_handle = i.l1_handle "
    "ORDER BY k.kind, k.value, i.object_id, i.recorded_at, i.l1_handle, i.field_path")
_VERSIONS_BATCH = (
    "SELECT v.object_id, o.object_type, v.digest, v.lineage, v.markings, v.recorded_at, "
    "v.superseded_at, (SELECT json_group_array(json_array(rm.since, rm.until)) "
    "FROM object_removals rm WHERE rm.object_id = o.object_id) AS removals, "
    "h.handle AS plx_batch_key FROM pg_temp.plx_handles h JOIN object_versions v ON "
    "v.l1_handle = h.handle JOIN objects o ON o.object_id = v.object_id")
_CONTRADICTED = re.compile(r"^SELECT (?P<cols>\w+(?:, \w+)*) FROM link_rows WHERE "
                           r"from_object = \? AND tier = 'contradicted'$")
# every link type of the served rows, by the (link type, tier) index (one descent a type)
_LINK_TYPES = ("WITH RECURSIVE t AS (SELECT min(link_type) AS lt FROM ontology.link_rows "
               "UNION ALL SELECT (SELECT min(link_type) FROM ontology.link_rows WHERE "
               "link_type > t.lt) FROM t WHERE t.lt IS NOT NULL) SELECT lt FROM t WHERE "
               "lt IS NOT NULL")
_TWIN_LEADS = ("SELECT * FROM links WHERE link_id IN (SELECT link_id FROM link_rows WHERE "
               "from_object = :o AND link_type = :t UNION SELECT link_id FROM link_rows WHERE "
               "to_object = :o AND link_type = :t) AND binding_kind = 'same_record' AND "
               "tier = 'lead' AND superseded_at IS NULL ORDER BY link_id")


class BatchedReads:
    """The per-object link reads above answered set-based (``answer``: a compat cursor,
    or None for a statement it does not take). ``clear`` after any link write of the step:
    a batch read before it would miss the step's own rows."""

    def __init__(self, pg: Any, links: bool = True) -> None:
        self.pg = pg
        self.links = links   # False: only the read-ahead rows (a step adding links often)
        self.calls: dict[Any, int] = {}
        self.batches: dict[Any, dict[str, list[tuple]]] = {}
        self.counts = {"answered": 0, "batch_reads": 0, "batch_rows": 0}
        # rows that no link the step writes can change (identifier and version rows,
        # :meth:`prefetch_mentions`): kept across :meth:`clear`
        self.fixed: dict[str, tuple[tuple[str, ...], dict[Any, list[tuple]]]] = {}

    def clear(self) -> None:
        self.batches.clear()

    def answer(self, sql: str, params: Any) -> Any:
        if self.fixed and sql in self.fixed and not isinstance(params, Mapping):
            return self._fixed(sql, tuple(params or ()))
        if not self.links:
            return None
        text = _SPACES.sub(" ", sql).strip()
        if text == _TWIN_LEADS and isinstance(params, Mapping):
            return self._twin_leads(params)
        if isinstance(params, Mapping):
            return None
        params = tuple(params or ())
        found = _CONTRADICTED.match(text)
        if found is not None and len(params) == 1:
            return self._contradicted(found["cols"], params[0])
        parts = text.split(" UNION ")
        keyed = [_KEYED.match(p) for p in parts]
        if not 1 <= len(parts) <= 2 or not all(keyed) \
                or not all(_REST.match(m["rest"]) for m in keyed) \
                or (len(parts) == 2 and any(len(m["cols"].split(", ")) != 1 for m in keyed)):
            return None
        bound, at = [], 0
        for p in parts:
            n = p.count("?")
            bound.append(params[at:at + n])
            at += n
        if at != len(params):
            return None
        names, rows = None, []
        for m, ps in zip(keyed, bound):
            got = self._keyed(m, ps)
            if got is None:
                return None
            names = names or got[0]
            rows += got[1]
        if len(parts) == 2:   # UNION: distinct values
            rows = sorted(set(rows), key=repr)
        from operating.pg.sqlite_compat import Cursor
        self.counts["answered"] += 1
        return Cursor(names, rows, len(rows), self.pg.row_factory is None)

    def _keyed(self, m: Any, params: tuple) -> tuple[tuple[str, ...], list[tuple]] | None:
        cols = tuple(m["cols"].split(", "))
        batch = (m["cols"], m["key"], m["rest"], params[1:])
        self.calls[batch] = self.calls.get(batch, 0) + 1
        if batch not in self.batches:
            if self.calls[batch] <= BATCH_AFTER:
                return None
            key = m["key"]
            sql = (f"SELECT {m['cols']}, {key} AS plx_batch_key FROM link_rows WHERE "
                   f"link_type = ? AND {m['rest']}")
            groups: dict[str, list[tuple]] = {}
            for r in self.pg.execute(pg_form(sql), params[1:]).fetchall():
                values = tuple(r)
                groups.setdefault(values[-1], []).append(values[:-1])
            self.batches[batch] = groups
            self.counts["batch_reads"] += 1
            self.counts["batch_rows"] += sum(len(v) for v in groups.values())
        return cols, list(self.batches[batch].get(params[0], ()))

    def _fixed(self, sql: str, params: tuple) -> Any:
        from operating.pg.sqlite_compat import Cursor
        names, rows = self.fixed[sql]
        if sql == _CANDIDATES:
            kind, value, exclude = params
            got = rows.get((kind, value))
            if got is None:
                return None   # a key not read ahead: Postgres answers it
            out = [r for r in got if r[0] != exclude]
        else:   # VERSION_BY_HANDLE_SQL
            got = rows.get(params[0])
            if got is None or len(got) > 1:
                return None   # not read ahead, or more than one row: as Postgres gives it
            out = list(got)
        self.counts["answered"] += 1
        return Cursor(names, out, len(out), self.pg.row_factory is None)

    def prefetch_mentions(self, keys: Iterable[tuple[str, str]], handles: Iterable[str],
                          ddl: Any = None) -> dict[str, int]:
        """The scoped mentions step's per-mention reads read ahead, set-based: the
        identifier rows answering every candidate key of the scope's documents' mentions
        (``mentions._store_candidates``, in its order within each key) and the version row
        of every handle the documents and their mentions cite (``VERSION_BY_HANDLE_SQL``),
        each through one join with a session table of the keys / handles."""
        from ontology import mentions as M
        raw = self.pg._pg
        ddl = ddl or raw.execute   # the session's own temp tables (``Overlay._raw``)
        keys = sorted(set(keys))
        handles = sorted(set(handles))
        ddl("CREATE TEMP TABLE IF NOT EXISTS plx_keys (kind text, value text)")
        ddl("CREATE TEMP TABLE IF NOT EXISTS plx_handles (handle text)")
        ddl("TRUNCATE pg_temp.plx_keys")
        ddl("TRUNCATE pg_temp.plx_handles")
        with raw.cursor() as cur:
            with cur.copy("COPY pg_temp.plx_keys (kind, value) FROM STDIN") as cp:
                for k in keys:
                    cp.write_row(k)
            with cur.copy("COPY pg_temp.plx_handles (handle) FROM STDIN") as cp:
                for h in handles:
                    cp.write_row((h,))
        ddl("ANALYZE pg_temp.plx_keys")
        ddl("ANALYZE pg_temp.plx_handles")
        cand = self.pg.execute(_CANDIDATES_BATCH, ())
        names = tuple(d[0] for d in cand.description)[:-2]
        by_key: dict[Any, list[tuple]] = {}
        for r in cand.fetchall():
            values = tuple(r)
            by_key.setdefault((values[-2], values[-1]), []).append(values[:-2])
        for k in keys:
            by_key.setdefault(k, [])
        self.fixed[_CANDIDATES] = (names, by_key)
        ver = self.pg.execute(_VERSIONS_BATCH, ())
        vnames = tuple(d[0] for d in ver.description)[:-1]
        by_handle: dict[Any, list[tuple]] = {}
        for r in ver.fetchall():
            values = tuple(r)
            by_handle.setdefault(values[-1], []).append(values[:-1])
        for h in handles:
            by_handle.setdefault(h, [])
        self.fixed[M.VERSION_BY_HANDLE_SQL] = (vnames, by_handle)
        return {"keys": len(keys), "candidate_rows": sum(len(v) for v in by_key.values()),
                "handles": len(handles),
                "version_rows": sum(len(v) for v in by_handle.values())}

    def _contradicted(self, cols: str, oid: str) -> Any:
        """``posting_links._rejected``'s read of one object's rejections (a ``contradicted``
        link of any type): read once for every link type the overlay holds, through the
        (link type, tier) index, each type's contradicted rows."""
        from operating.pg.sqlite_compat import Cursor
        batch = ("contradicted", cols)
        self.calls[batch] = self.calls.get(batch, 0) + 1
        if batch not in self.batches:
            if self.calls[batch] <= BATCH_AFTER:
                return None
            raw = self.pg._pg
            types = [r[0] for r in raw.execute(_LINK_TYPES).fetchall()]
            types += [r[0] for r in raw.execute(
                "SELECT DISTINCT link_type FROM pg_temp.run_rows UNION SELECT DISTINCT "
                "link_type FROM pg_temp.step_rows").fetchall()]
            groups: dict[str, list[tuple]] = {}
            for r in self.pg.execute(
                    f"SELECT {cols}, from_object AS plx_batch_key FROM link_rows WHERE "
                    "link_type IN (SELECT value FROM json_each(?)) AND tier = 'contradicted'",
                    (json.dumps(sorted(set(types))),)).fetchall():
                values = tuple(r)
                groups.setdefault(values[-1], []).append(values[:-1])
            self.batches[batch] = groups
            self.counts["batch_reads"] += 1
            self.counts["batch_rows"] += sum(len(v) for v in groups.values())
        rows = list(self.batches[batch].get(oid, ()))
        self.counts["answered"] += 1
        return Cursor(tuple(cols.split(", ")), rows, len(rows), self.pg.row_factory is None)

    def _twin_leads(self, params: Mapping[str, Any]) -> Any:
        """``_PartyTwin.leads``' read of one object's current same-record leads of one link
        type: from the type's leads read once, the ones naming the object at either end,
        in link id order."""
        from operating.pg.sqlite_compat import Cursor
        batch = ("twin_leads", params["t"])
        if batch not in self.batches:   # from the first call: one per object took ~57 s
            cur = self.pg.execute(
                "SELECT * FROM links WHERE link_type = ? AND binding_kind = 'same_record' "
                "AND tier = 'lead' AND superseded_at IS NULL ORDER BY link_id", (params["t"],))
            names = tuple(d[0] for d in cur.description)
            groups: dict[str, list[tuple]] = {}
            seen: set[str] = set()
            fi, ti, li = (names.index(c) for c in ("from_object", "to_object", "link_id"))
            for r in cur.fetchall():
                values = tuple(r)
                if values[li] in seen:   # one link id is one row of the overlay
                    raise Refused("twin_leads_link_id_twice")
                seen.add(values[li])
                for end in {values[fi], values[ti]}:
                    groups.setdefault(end, []).append(values)
            self.batches[batch] = {"names": names, "groups": groups}   # type: ignore[assignment]
            self.counts["batch_reads"] += 1
            self.counts["batch_rows"] += len(seen)
        got = self.batches[batch]
        rows = list(got["groups"].get(params["o"], ()))   # type: ignore[union-attr]
        self.counts["answered"] += 1
        return Cursor(got["names"], rows, len(rows), self.pg.row_factory is None)  # type: ignore[arg-type]


# The steps whose own links stay in their local store only (never in ``step_rows``): a
# read of links is answered by the overlay without them and the store, merged by link id
# (:meth:`OverlayConnection._own_read`). ``mentions`` re-derives ~30M links for a 1.5M
# document scope: in ``step_rows`` each was a second plain copy with three indexes and
# every corroboration a dead row (8 parts: 76 GB of temp files at half way, 11-11).
# ``with_people`` writes ~8M links for every message and task: ~15 GB of temp table and
# index in its session (/data down to 13 GB free, killed, 11-11); its reads of its own
# are sets (``people_links``: the pairs written so far, contradicted pairs).
# PLX_INPLACE_OWN_LINKS_IN_PG=1: every step as before (the proofs compare both).
OWN_LINKS_LOCAL = frozenset({"mentions", "with_people"})
# the link reads a step of OWN_LINKS_LOCAL may make: plain rows of ``links`` by a filter,
# in link id order (no join, subquery, grouping, limit or set operation)
_OWN_MERGED = re.compile(r"^SELECT (?:\*|\w+(?:, \w+)*) FROM links WHERE (?P<where>.+) "
                         r"ORDER BY link_id$")
# ... or named columns of ``link_rows`` by a filter in no order (the rows as a set: the
# overlay's union gave them in no order either)
_OWN_UNORDERED = re.compile(r"^SELECT (?P<cols>\w+(?:, \w+)*) FROM link_rows WHERE "
                            r"(?P<where>.+)$")
_OWN_NOT = re.compile(r"\b(SELECT|FROM|JOIN|UNION|INTERSECT|EXCEPT|LIMIT|OFFSET|GROUP|"
                      r"HAVING|ORDER|WITH)\b", re.I)


def own_links_local(name: str) -> bool:
    return name in OWN_LINKS_LOCAL and os.environ.get("PLX_INPLACE_OWN_LINKS_IN_PG") != "1"


class OverlayConnection(SplitConnection):
    """The step's connection in a committed run: reads go to the overlay; the links the
    step adds, updates or deletes are written to its local store and to ``step_rows``, so
    its reads see them (``Refused`` when such a write would touch a link that is not the
    step's own: a served link the in-place run cannot change); the corroboration's
    temporary thread table is made in the session. ``prefixes``: the step's own.
    ``own_local``: its links are in its local store only (:data:`OWN_LINKS_LOCAL`)."""

    def __init__(self, local: sqlite3.Connection, pg: Any, overlay: Overlay,
                 prefixes: Sequence[str], batched: "BatchedReads | None" = None,
                 own_local: bool = False, shared_threads: bool = False) -> None:
        super().__init__(local, pg)
        object.__setattr__(self, "overlay", overlay)
        object.__setattr__(self, "prefixes", tuple(prefixes))
        object.__setattr__(self, "batched", batched)
        object.__setattr__(self, "own_local", own_local)
        object.__setattr__(self, "shared_threads", shared_threads)

    def _wrote(self, pattern: re.Pattern[str], sql: str) -> bool:
        if pattern is _LINK_TABLES:
            return False   # its links are in the overlay too
        return super()._wrote(pattern, sql)

    def _route(self, sql: str, params: Any, row_factory: Any = None) -> Any:
        if _PG_TEMP.search(sql):
            return self._session_temp(sql, params)
        if self.own_local and _read_only(sql) and _LINK_TABLES.search(sql):
            return self._own_read(sql, params, row_factory)
        if _read_only(sql) and _OWN_ENUMERATION.search(sql):
            sql = re.sub(r"\bFROM links\b", "FROM pg_temp.step_links", sql, count=1)
        if not _read_only(sql) and _LINK_WRITE.match(sql):
            if self.batched is not None:
                self.batched.clear()
            return self._link_write(sql, params)
        if self.batched is not None and _read_only(sql):
            found = self.batched.answer(sql, params)
            if found is not None:
                self.reads += 1
                return found
        return super()._route(sql, params, row_factory)

    def _own_read(self, sql: str, params: Any, row_factory: Any) -> Any:
        """A read of links by a step whose own links are in its local store only: its own
        lead enumeration from the store alone (``step_links`` before); any other a plain
        filtered read in link id order (:data:`_OWN_MERGED`), answered by the overlay
        (without its links) and by the store, the rows of both in link id order, as the
        overlay with ``step_rows`` gave them. The database orders text by code point
        (C.UTF-8), as Python and SQLite do. Any other shape, columns that differ or one
        link id from both, is refused, never answered from one side."""
        from operating.pg.sqlite_compat import Cursor
        text = _SPACES.sub(" ", sql).strip()
        params = tuple(params or ())
        plain = self.pg.row_factory is None
        if _OWN_ENUMERATION.search(text):
            cur = self.local.cursor()
            cur.row_factory = None
            rows = cur.execute(sql, params).fetchall()
            self.local_reads += 1
            return Cursor(tuple(d[0] for d in cur.description), rows, len(rows), plain)
        found = _OWN_MERGED.match(text)
        unordered = _OWN_UNORDERED.match(text) if found is None else None
        if unordered is not None and not _OWN_NOT.search(unordered["where"]):
            return self._own_set_read(sql, params, row_factory, text)
        if found is None or _OWN_NOT.search(found["where"]) or re.search(r"\blink_rows\b",
                                                                         text):
            raise Refused("own_links_read:" + text[:120])
        got = super()._route(sql, params, row_factory)
        names = tuple(d[0] for d in got.description)
        rows = [tuple(r) for r in got.fetchall()]
        cur = self.local.cursor()
        cur.row_factory = None
        own = cur.execute(sql, params).fetchall()
        self.local_reads += 1
        if own:
            if tuple(d[0] for d in cur.description) != names or "link_id" not in names:
                raise Refused("own_links_columns:" + text[:120])
            at = names.index("link_id")
            rows = sorted(rows + [tuple(r) for r in own], key=lambda r: r[at])
            if any(a[at] == b[at] for a, b in zip(rows, rows[1:])):
                raise Refused("own_link_id_twice:" + text[:120])
        return Cursor(names, rows, len(rows), plain)

    def _own_set_read(self, sql: str, params: Any, row_factory: Any, text: str) -> Any:
        """A read of named ``link_rows`` columns in no order (:data:`_OWN_UNORDERED`): the
        overlay's rows (without its links), then the store's, the rows the overlay with
        ``step_rows`` gave (a union in no order). Columns that differ are refused, and one
        link id from both sides when the link id is read."""
        from operating.pg.sqlite_compat import Cursor
        params = tuple(params or ())
        got = super()._route(sql, params, row_factory)
        names = tuple(d[0] for d in got.description)
        rows = [tuple(r) for r in got.fetchall()]
        cur = self.local.cursor()
        cur.row_factory = None
        own = [tuple(r) for r in cur.execute(sql, params).fetchall()]
        self.local_reads += 1
        if tuple(d[0] for d in cur.description) != names:
            raise Refused("own_links_columns:" + text[:120])
        if own and "link_id" in names:
            at = names.index("link_id")
            if {r[at] for r in rows} & {r[at] for r in own}:
                raise Refused("own_link_id_twice:" + text[:120])
        return Cursor(names, rows + own, len(rows) + len(own), self.pg.row_factory is None)

    def _session_temp(self, sql: str, params: Any) -> Any:
        if self.shared_threads:
            done = self._shared_threads(sql, params)
            if done is not None:
                return done
        sql = _PG_TEMP.sub(r"pg_temp.\1", sql)
        # an index is made in its table's schema: Postgres refuses a schema on its name
        sql = re.sub(r"(?i)(\bINDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?)(?:pg_)?temp\.", r"\1", sql)
        if _read_only(sql):
            self.reads += 1
            return self.pg.execute(sql, params)
        return self.overlay._raw(sql.replace("?", "%s"), tuple(params or ()))

    def _shared_threads(self, sql: str, params: Any) -> Any:
        """The corroboration's conversation table made once for the run, in the stage
        schema (``operating.pg.step_staging``), and shown to this session as the temporary
        view of its name: the same rows (the same statement; nothing writes values while
        the run reads), ~1.6 GB once instead of in each mention part's session. Its index
        is the shared table's; dropping it drops the view. Another statement: None."""
        from operating.pg import step_staging
        text = _SPACES.sub(" ", sql).strip()
        if _THREADS_DROP.fullmatch(text):
            return self.overlay._raw("DROP VIEW IF EXISTS pg_temp.plx_mention_threads")
        if _THREADS_INDEX.fullmatch(text):
            return self.overlay._raw("SELECT 1")   # the shared table's, made with it
        made = _THREADS_MAKE.fullmatch(text)
        if made is None:
            return None
        params = tuple(params or ())
        import hashlib
        name = (f"{step_staging.schema()}.mention_threads_"
                f"{hashlib.sha256(json.dumps([text, params]).encode()).hexdigest()[:12]}")
        lock = int(hashlib.sha256(name.encode()).hexdigest()[:15], 16)
        self.overlay._raw("SELECT pg_advisory_lock(%s)", (lock,))
        try:
            if self.overlay._raw("SELECT to_regclass(%s)", (name,)).fetchone()[0] is None:
                self.overlay._raw(f"CREATE SCHEMA IF NOT EXISTS {step_staging.schema()}")
                self.overlay._raw(f"DROP TABLE IF EXISTS {name}_part")
                self.overlay._raw(f"CREATE UNLOGGED TABLE {name}_part AS "
                                  + made["select"].replace("?", "%s"), params)
                self.overlay._raw(f"CREATE INDEX ON {name}_part (value)")
                self.overlay._raw(f"ANALYZE {name}_part")
                self.overlay._raw(f"ALTER TABLE {name}_part RENAME TO {name.split('.')[1]}")
        finally:
            self.overlay._raw("SELECT pg_advisory_unlock(%s)", (lock,))
        return self.overlay._raw(f"CREATE OR REPLACE TEMP VIEW plx_mention_threads AS "
                                 f"SELECT * FROM {name}")

    def _link_write(self, sql: str, params: Any) -> Any:
        params = tuple(params or ())
        if _OWN_DELETE.match(sql) and params and params[1] in self.prefixes:
            return self.local.execute(sql, params)   # its own served links: hidden already
        where = re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.I)
        if len(where) != 2:
            raise Refused("link_write_without_where:" + sql[:80])
        cond = where[1]
        nset = sql[:sql.upper().index("WHERE")].count("?")
        found = self.pg.execute("SELECT count(*) FROM links WHERE " + cond,
                                params[nset:]).fetchone()[0]
        own = self.local.execute("SELECT count(*) FROM links WHERE " + cond,
                                 params[nset:]).fetchone()[0]
        if self.own_local:   # the overlay does not hold its own: any found is another's
            if found:
                raise Refused("link_write_outside_step:" + sql[:80])
            return self.local.execute(sql, params)
        if own != found:   # it would change a link that is not its own
            raise Refused("link_write_outside_step:" + sql[:80])
        cur = self.local.execute(sql, params)
        target = re.sub(r"\b(UPDATE|FROM)\s+links\b", r"\1 pg_temp.step_rows", sql, count=1,
                        flags=re.I)
        self.overlay._raw(target.replace("?", "%s"), params)
        return cur


class OverlayBuilder(SplitBuilder):
    """:class:`SplitBuilder` whose added links go to ``step_rows`` too (``own_local``:
    to its local store only, :data:`OWN_LINKS_LOCAL`)."""

    def __init__(self, inner: Any, pg: Any, overlay: Overlay, prefixes: Sequence[str],
                 absent: Path, values: bool = False, batched: bool | str = False,
                 own_local: bool = False, shared_threads: bool = False) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_values", values)   # a step owning values may add them
        object.__setattr__(self, "batched", BatchedReads(pg, links=batched != "ahead")
                           if batched else None)
        object.__setattr__(self, "connection",
                           OverlayConnection(inner.connection, pg, overlay, prefixes,
                                             self.batched, own_local, shared_threads))
        object.__setattr__(self, "_own_local", own_local)
        object.__setattr__(self, "temp", absent)   # no file: the steps run serially
        object.__setattr__(self, "_overlay", overlay)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "add":   # a caller wrapping ``add`` wraps this one (the overlay's too)
            object.__setattr__(self, name, value)
            return
        super().__setattr__(name, value)

    def _versions_of(self, rows: Sequence[Mapping[str, Any]]) -> None:
        _copy_versions(self._inner, self.connection.pg, rows)

    def add(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        rows = list(rows)
        if table == "links":
            if self.batched is not None:
                self.batched.clear()
            if not self._own_local:
                self._overlay.add("step_rows", rows)
        elif table == "object_values" and self._values:
            # its own store only (compared with the served ones after it: run_steps), with
            # the served object and version rows the values name (the store's key)
            self._versions_of(rows)
        elif table in ("objects", "object_versions", "object_values"):
            raise Refused(f"step_writes_{table}")
        return self._inner.add(table, rows)


def _copy_versions(inner: Any, pg: Any, rows: Sequence[Mapping[str, Any]]) -> None:
    """The object and version rows of the objects ``rows`` (values) name, read from
    Postgres into the store of ``inner`` when it does not hold them yet."""
    conn = inner.connection
    ids = sorted({r["object_id"] for r in rows})
    held = set()
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        held |= {r[0] for r in conn.execute(
            f"SELECT object_id FROM objects WHERE object_id IN ({', '.join('?' * len(part))})",
            part)}
    ids = [i for i in ids if i not in held]
    for table in ("objects", "object_versions"):
        cols = inner._INSERT[table]
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            got = [dict(zip(cols, r)) for r in pg.execute(
                f"SELECT {', '.join(cols)} FROM {table} WHERE object_id IN "
                f"({', '.join('?' * len(part))}) ORDER BY object_id", part).fetchall()]
            if got:
                inner.add(table, got)


# The steps whose own module judges with worker processes, each opening the build's file
# (``builder.temp``) read-only: in place there is no such file, the pool fails at its first
# worker and the step runs serially through one Postgres session (``unique_mentions``: one
# read per object of 474k holding a mention lead, served plx 11-11: over 40 min, not
# finished). Here, in a staged run only (``run_rows`` empty: every earlier step's links are
# in the stage schema, so a fresh session shows the same overlay), the module's worker
# initializer is replaced by :func:`_overlay_worker_init`: each worker opens its own
# Postgres session, read-only, with the very view of the step's session (the same hidden
# entries, the same staged steps, ``step_rows`` empty as the step's is while it judges).
# The workers' function and the step's order of collecting are the module's own.
OVERLAY_WORKERS = {"unique_mentions": "ontology.mention_firm_parallel"}
_WORKER_SPEC = "plx-overlay-worker/1"


def _overlay_worker_init(spec_path: str) -> None:
    """A step module's worker initializer, in place (:data:`OVERLAY_WORKERS`): the
    module's worker connection is a read-only Postgres session showing the step's
    overlay (``spec_path``: written by :func:`_overlay_workers`)."""
    import tempfile
    from ontology import mention_firm_parallel
    from operating.pg.sqlite_compat import PgCompatConnection
    spec = json.loads(Path(spec_path).read_text())
    if spec.get("format") != _WORKER_SPEC or spec.get("module") != mention_firm_parallel.__name__:
        raise Refused("overlay_worker_spec")
    dsn = os.environ.get(spec["dsn_env"])
    if not dsn:
        raise Refused("overlay_worker_dsn")
    pg = PgCompatConnection(dsn, statement_timeout_ms=0)
    overlay = Overlay(pg)
    overlay.show(spec["hidden"], own=spec["own"], staged_before=spec["staged_before"])
    if overlay._raw("SELECT EXISTS (SELECT 1 FROM pg_temp.run_rows) OR EXISTS "
                    "(SELECT 1 FROM pg_temp.step_rows)").fetchone()[0]:
        raise Refused("overlay_worker_rows")
    pg._pg.commit()
    # it only reads: the server refuses any write of this session from here on
    pg._pg.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    pg._pg.commit()
    folder = Path(tempfile.mkdtemp(prefix=".overlay-worker-", dir=spec["folder"]))
    local = _local_store(folder / "w.sqlite")
    conn = OverlayConnection(local.connection, pg, overlay, spec["prefixes"])
    mention_firm_parallel._WORKER["conn"] = conn


@contextlib.contextmanager
def _overlay_workers(name: str, builder: Any, hidden: Sequence[Mapping[str, Any]],
                     own: bool, work: Path, dsn: str, prefixes: Sequence[str],
                     staged_before: str | None = None) -> Iterator[None]:
    """While step ``name`` runs (one of :data:`OVERLAY_WORKERS`), its module's workers
    read through the step's overlay (:func:`_overlay_worker_init`). Only for hidden
    entries a fresh session can show (no ``scope_docs``). ``staged_before``: as the
    step's own overlay shows them (None when the steps before are applied to the served
    links: their staged links shown again over them read other rows than the step's own
    overlay, and ~0.2 s a read, 11-11)."""
    if name not in OVERLAY_WORKERS or any(o.get("objects") for o in hidden):
        yield
        return
    from ontology import mention_firm_parallel as module   # the one module of OVERLAY_WORKERS
    assert OVERLAY_WORKERS[name] == module.__name__
    spec_path = work / f".{name}.overlay-workers.json"
    env = "PLX_INPLACE_WORKER_DSN"
    spec_path.write_text(json.dumps({
        "format": _WORKER_SPEC, "module": module.__name__, "dsn_env": env,
        "hidden": [dict(o) for o in hidden], "own": own, "staged_before": staged_before,
        "prefixes": list(prefixes), "folder": str(work)}, sort_keys=True))
    held = (module._init, builder.temp, os.environ.get(env))
    module._init = _overlay_worker_init
    object.__setattr__(builder, "temp", spec_path)
    os.environ[env] = dsn   # the spawned workers' (never written to a file)
    try:
        yield
    finally:
        module._init, temp, old = held
        object.__setattr__(builder, "temp", temp)
        if old is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = old
        spec_path.unlink(missing_ok=True)


def hidden_for(name: str, names: Sequence[str], run: Mapping[str, Any],
               scoped: Mapping[str, Sequence[Mapping[str, Any]]],
               specs: Mapping[str, Any], staged: bool = False) -> list[dict[str, Any]]:
    """The served links ``build_layer`` does not show step ``name`` (``names``: the steps
    in order from the first re-run): every re-run step's owned links (its whole owned
    range while it is after ``name``; once at or before, only what it re-derived:
    ``scoped``, else its owned range) and the steps not re-run at or after ``name``.
    ``staged``: the re-run steps before ``name`` staged their difference to the served
    links (``operating.pg.step_staging``), which the overlay applies: none of theirs here."""
    from ontology import step_migrate as sm
    out: list[dict[str, Any]] = []
    for other in names:
        if staged and other in run and other < name:
            continue
        if other in run:
            whole = sm._owned(other, run[other], specs)
            out += whole if other > name else list(scoped.get(other) or whole)
        elif other >= name:
            out += sm._owned(other, None, specs)
    return out


def run_steps(the_plan: Mapping[str, Any], run: Mapping[str, Any], new: Any,
              steps: Mapping[str, Any], dsn: str, work: Path, *,
              sources: Mapping[str, Any] | None = None,
              mention_docs: Sequence[str] | None = None,
              mention_groups: set[str] | None = None, log: Any = None,
              only: set[str] | None = None, specs: Mapping[str, Any] | None = None,
              normalizer: Any = None, mention_cache: str | Path | None = None,
              stage_key: str | None = None, stage_part: int = 0,
              apply_now: bool = False, view: str = "staged",
              part_before: int | None = None, concurrent: bool = False) -> dict[str, Any]:
    """Each step of ``run`` (name -> from types | None) against the committed Postgres
    ontology at ``dsn`` (reads only; module docstring of :func:`write_run_layer`), its
    links kept in ``work/<step>.sqlite`` with ``work/<step>.json`` (a rerun skips a step
    whose JSON is there and loads its links). ``mention_docs`` / ``mention_groups``: the
    ``mentions`` step re-derives only those documents (``operating.pg.mention_scope``).
    ``stage_key`` (the run's): each finished step's difference to the served links is staged
    (``operating.pg.step_staging``, as part ``stage_part``) and the later steps read it in
    place; no step's links are loaded into ``run_rows``. A step done before is staged from
    its store when its markers are not there; one whose store is gone and whose markers do
    not match its JSON is refused (run it again).
    ``apply_now`` (11-11, the owner: applied as they go): each finished step's staged
    difference is written to the served links at once (``step_staging.apply_now``, with
    its undo and per-type counts), and every step reads the served links (``view``
    "applied": nothing staged is shown). ``view`` "staged" with ``part_before``: a mention
    part also sees its step's staged parts numbered below (the apply-as-you-go proof)."""
    from ontology import step_migrate as sm
    from operating.pg.sqlite_compat import PgCompatConnection
    if view not in ("staged", "applied") or (apply_now and view != "applied"):
        raise Refused(f"view:{view}")
    if concurrent and not (apply_now and only):
        raise Refused("concurrent_needs_apply_now_and_only")

    specs = sm.STEPS if specs is None else specs
    remark = set(the_plan["steps"].get("remark") or ())
    names = [n for n in sorted(steps) if run and n >= min(run)]
    scoped: dict[str, list[dict[str, Any]]] = {}
    if mention_docs is not None and "mentions" in run:
        scoped["mentions"] = [{"step": "mentions", "prefix": o["prefix"], "end": o["end"],
                               "from": None, "objects": "scope_docs"}
                              for o in sm._owned("mentions", None, specs)]
    work.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"steps": {}}

    class _Pg(PgCompatConnection):
        ready = False

        def _session(self, conn: Any) -> None:
            if self.ready:   # a new session lost the overlay: never read without it
                raise Refused("pg_session_lost")
            super()._session(conn)

    pg = _Pg(dsn, statement_timeout_ms=0)
    try:
        overlay = Overlay(pg)
        _Pg.ready = True
        if "mentions" in scoped and stage_key is None:
            report["mention_docs"] = overlay.scope(mention_docs or ())
        for name in names:
            if name not in run or name in remark:
                continue
            if concurrent and only is not None and name not in only:
                # (11-11) the steps run at once where none reads
                # another's links: one not named here reads as Postgres serves it now
                if name > max(only):
                    break
                continue
            done = work / f"{name}.json"
            store = work / f"{name}.sqlite"
            if stage_key is not None and done.is_file():
                entry = _staged_entry(dsn, stage_key, name, store, done, run, specs,
                                      mention_docs if name in scoped else None, stage_part,
                                      apply=apply_now)
                report["steps"][name] = {**entry, "resumed": True}
                continue
            if done.is_file() and store.is_file():
                for rows in _store_link_batches(store):
                    overlay.add("run_rows", rows)
                report["steps"][name] = {**json.loads(done.read_text()), "resumed": True}
                continue
            if only is not None and name not in only:
                if name < max(only):
                    raise ValueError(f"step_not_run_before:{name}")
                break
            t = time.monotonic()
            if name in scoped and stage_key is not None:
                # staged: only the scoped step itself hides its served links by the scope
                report["mention_docs"] = overlay.scope(mention_docs or ())
            own_local = own_links_local(name)
            hidden = hidden_for(name, names, run, scoped, specs, staged=stage_key is not None)
            overlay.show(hidden, own=not own_local,
                         staged_before=name if stage_key is not None and view == "staged"
                         else None, part_before=part_before)
            spec = specs[name]
            srcs = sm._sources_of(new, sources or {}, run[name]) if spec.documents \
                else dict(sources or {})
            if name == "mentions" and mention_groups is not None:
                if run[name] is not None:   # a cached group names no source to narrow by
                    raise Refused("mention_scope_with_from_types")
                srcs = scoped_mention_sources(srcs, mention_groups, overlay.scope_handles())
            projector = sm._projector(new, srcs, normalizer)
            part = work / f"{name}.sqlite.part"
            local = _local_store(part)
            try:
                ahead = (name == "mentions" and mention_groups is not None and mention_cache
                         and os.environ.get("PLX_INPLACE_READ_AHEAD", "1") != "0")
                builder = OverlayBuilder(local, pg, overlay, spec.prefixes,
                                         work / "no-step-input", values=bool(spec.values),
                                         batched="ahead" if ahead else name in BATCHED_STEPS,
                                         own_local=own_local,
                                         shared_threads=stage_key is not None)
                if ahead:
                    keys, handles = mention_reads(mention_cache, mention_groups)
                    report["mention_reads_ahead"] = builder.batched.prefetch_mentions(
                        keys, handles, overlay._raw)
                with (_overlay_workers(name, builder, hidden, not own_local, work, dsn,
                                       spec.prefixes,
                                       name if view == "staged" else None)
                      if stage_key is not None
                      else contextlib.nullcontext()):
                    result = steps[name](builder, projector)
                lc = local.connection
                (n,) = lc.execute("SELECT count(*) FROM link_rows").fetchone()
                if spec.values:
                    _values_unseen(name, lc, pg, names, run, new, specs)
                if not lc.in_transaction:   # the step committed its own store
                    lc.execute("BEGIN IMMEDIATE")
                kept = local.keep_as_part()
            except BaseException:
                local.abort()
                raise
            failed = _documents_failed(result)
            if failed and os.environ.get(DOC_FAILURES_OK) != "1":
                # a document the run could not derive is a link the layer would drop: a
                # read Postgres answers otherwise than SQLite fails here, never silently
                raise Refused(f"documents_failed:{name}:{json.dumps(failed, sort_keys=True)}")
            Path(kept).replace(store)
            staged_report: dict[str, Any] = {}
            if stage_key is not None:
                from operating.pg import step_staging
                got = step_staging.stage(dsn, stage_key, name, stage_part, store,
                                         sm._owned(name, run[name], specs),
                                         mention_docs if name in scoped else None)
                staged_report = {"staged": step_staging.totals({stage_part: got}),
                                 "stage_part": stage_part}
                if apply_now:
                    staged_report["applied"] = step_staging.apply_now(
                        dsn, stage_key, name, [stage_part])["bits"]
                overlay._raw("TRUNCATE pg_temp.step_rows")
            else:
                # a step kept local is read into run_rows only for a later step of this
                # process
                overlay.promote(store if own_local and (only is None or max(only) > name)
                                else None)
            entry = {"seconds": round(time.monotonic() - t, 3), "links": n,
                     "reads": builder.connection.reads,
                     **({"batched": builder.batched.counts} if builder.batched else {}),
                     **staged_report,
                     "report": json.loads(json.dumps(result, default=str))}
            done.write_text(json.dumps(entry, sort_keys=True))
            report["steps"][name] = entry
            if log is not None:
                print(f"step {name}: {entry['seconds']} s, {n} links, "
                      f"{entry['reads']} reads", file=log, flush=True)
    finally:
        pg.close()
    return report


def _staged_entry(dsn: str, stage_key: str, name: str, store: Path, done: Path,
                  run: Mapping[str, Any], specs: Mapping[str, Any],
                  objects: Sequence[str] | None, part: int, apply: bool = False
                  ) -> dict[str, Any]:
    """A step done before, staged: its JSON when its markers match it; else staged now
    from its store (as part ``part``) and its JSON rewritten; refused when the store is
    gone too (the step must run again: its JSON removed). ``apply``: applied too (now,
    unless its applied markers match its JSON already)."""
    import psycopg
    from ontology import step_migrate as sm
    from operating.pg import step_staging
    entry = json.loads(done.read_text())
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        parts = step_staging.staged(cur, name)
        were = step_staging.applied(cur, name)
    want = entry.get("staged")
    if want and parts and step_staging.totals(parts) == want:
        if apply and not (were and step_staging.totals(were) == want):
            entry["applied"] = step_staging.apply_now(dsn, stage_key, name)["bits"]
        return entry
    if were:   # applied, but not the stage its JSON names: never staged again over it
        raise Refused(f"applied_other_stage:{name}")
    if not store.is_file():
        raise Refused(f"staged_lost:{name}")
    got = step_staging.stage(dsn, stage_key, name, part, store,
                             sm._owned(name, run[name], specs), objects)
    entry.update(staged=step_staging.totals({part: got}), stage_part=part)
    done.write_text(json.dumps(entry, sort_keys=True))
    if apply:
        entry["applied"] = step_staging.apply_now(dsn, stage_key, name, [part])["bits"]
        done.write_text(json.dumps(entry, sort_keys=True))
    return entry


def _value_rows(conn: Any, owned: Sequence[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    from ontology import step_migrate as sm
    from ontology import store_ddl
    where, params = sm.owned_values_where(owned)
    return sorted((tuple(r) for r in conn.execute(
        f"SELECT {', '.join(store_ddl.VALUE_COLUMNS)} FROM object_values WHERE {where}",
        params).fetchall()), key=repr)


def _values_unseen(name: str, local: sqlite3.Connection, pg: Any, names: Sequence[str],
                   run: Mapping[str, Any], new: Any, specs: Mapping[str, Any]) -> None:
    """A step owning values (``Spec.values``) wrote them to its store only; the steps after
    it read the served ones. Refused when they differ and a later re-run step may read
    their type (``build_layer`` would show it the re-derived ones)."""
    from ontology import step_migrate as sm
    owned = sm._owned_values([name], specs)
    if _value_rows(local, owned) == _value_rows(pg, owned):
        return
    types = {o["type"] for o in owned}
    for later in (n for n in names if n > name and n in run):
        got = specs[later].declared(new)
        if got is None or types & set(got):
            raise Refused(f"values_changed_before_reader:{name}:{later}")


DOC_FAILURES_OK = "PLX_INPLACE_DOCUMENT_FAILURES_OK"   # "1": a step may skip documents


def _documents_failed(result: Any) -> dict[str, int]:
    """The documents a step's report says it skipped (``document_failures``), by error
    class, wherever in the report they are."""
    out: dict[str, int] = {}
    if isinstance(result, Mapping):
        if result.get("documents_failed"):
            for k, v in (result.get("failed_errors") or {"unknown": result["documents_failed"]}
                         ).items():
                out[k] = out.get(k, 0) + int(v)
        for v in result.values():
            for k, n in _documents_failed(v).items():
                out[k] = out.get(k, 0) + n
    elif isinstance(result, (list, tuple)):
        for v in result:
            for k, n in _documents_failed(v).items():
                out[k] = out.get(k, 0) + n
    return out


class _CachedGroups:
    """The scope's document groups of the sources that group their documents (mail,
    SharePoint), named by their keys in the extraction cache: no capture source is listed
    (one listing of the 3.5M groups took ~95 min on served plx, and it was made only to
    keep the scope's few). The step takes each group's documents and mentions from the
    cache (``ontology.mention_cache``: its memo); a group the cache does not hold whole
    would have to be read, and is a failed group instead (the run then refuses:
    ``documents_failed``), never a group left out unseen."""

    def __init__(self, keys: Iterable[str]) -> None:
        self._keys = sorted(keys)

    def document_groups(self) -> Iterable[Any]:
        for key in self._keys:
            yield key, _UncachedGroup(key)


class _UncachedGroup:
    def __init__(self, key: str) -> None:
        self.key = key

    def __call__(self) -> Any:
        raise Refused("mention_group_not_cached")


class _ScopedDocuments:
    """A source that lists documents one by one (no groups: Teams, meetings, MC): only
    its documents whose version an object of the scope holds (``handles``)."""

    def __init__(self, source: Any, handles: set[str]) -> None:
        self._source, self._handles = source, handles

    def __getattr__(self, name: str) -> Any:
        if name == "document_groups":
            raise AttributeError(name)
        return getattr(self._source, name)

    def documents(self) -> Iterable[Any]:
        return (d for d in self._source.documents() if d.citation in self._handles)


def mention_reads(cache: str | Path, groups: Iterable[str]
                  ) -> tuple[set[tuple[str, str]], set[str]]:
    """(every candidate key of the mentions of the documents of ``groups``, every handle
    those documents and their mentions cite), from the extraction cache."""
    import zlib
    from ontology.mentions import candidate_keys
    from ontology.mentions import cited_page
    keys: set[tuple[str, str]] = set()
    handles: set[str] = set()
    db = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
    try:
        for g in sorted(groups):
            row = db.execute("SELECT value FROM groups WHERE key = ?", (g,)).fetchone()
            for k in (json.loads(zlib.decompress(row[0])) or ()) if row is not None else ():
                found = db.execute("SELECT value FROM extracted WHERE key = ?", (k,)).fetchone()
                if found is None:
                    continue
                entry = json.loads(zlib.decompress(found[0]))
                handles.add(entry["document"]["citation"])
                for m in entry.get("mentions") or ():
                    keys.update(candidate_keys(m))
                    if isinstance(m.get("citation"), str):
                        handles.add(m["citation"])
                        base, page = cited_page(m["citation"])
                        if page is not None:
                            handles.add(base)
    finally:
        db.close()
    return keys, handles


def scoped_mention_sources(sources: Mapping[str, Any], groups: Iterable[str],
                           handles: set[str]) -> dict[str, Any]:
    """The ``mentions`` step's sources for a scoped run (``operating.pg.mention_scope``):
    the grouping sources replaced by the scope's cached groups (:class:`_CachedGroups`),
    every other document source narrowed to the scope's versions."""
    out: dict[str, Any] = {}
    grouped = False
    for name, source in sources.items():
        if callable(getattr(source, "document_groups", None)):
            grouped = True
        elif callable(getattr(source, "documents", None)):
            out[name] = _ScopedDocuments(source, handles)
        else:
            out[name] = source
    if grouped:
        out["~scope-groups"] = _CachedGroups(groups)   # after the named sources
    return out


def mention_parts(dsn: str, scope: Mapping[str, Any], cache: str | Path, parts: int
                  ) -> list[dict[str, Any]]:
    """The scope (``operating.pg.mention_scope``: groups, objects) split into ``parts``
    scopes closed under what one document's mention links read of another: a group's
    documents, their objects and the objects sharing a conversation with them (the
    corroboration reads the conversation's other messages' links) are in one part. Each
    part: its groups and the objects of their documents (the first part also the scope's
    objects no group's document is). Balanced by documents, largest component first."""
    import psycopg
    import zlib
    from ontology.mentions import MENTION_FEATURES
    groups = sorted(set(scope["groups"]))
    db = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
    duplicates = 0
    try:
        docs: dict[str, list[str]] = {}
        held: dict[str, str] = {}   # citation -> the document key a kept group lists it by
        for g in groups:
            row = db.execute("SELECT value FROM groups WHERE key = ?", (g,)).fetchone()
            keys = json.loads(zlib.decompress(row[0])) if row is not None else []
            cites, of = [], {}
            for k in keys or ():
                found = db.execute("SELECT value FROM extracted WHERE key = ?", (k,)).fetchone()
                if found is not None:
                    c = json.loads(zlib.decompress(found[0]))["document"]["citation"]
                    cites.append(c)
                    of[c] = k
            # one captured record kept under two group keys (a key holds the repr of a
            # frozenset, so its order follows the hash seed of the run that kept it): the
            # step would read its documents twice, and their links collide (11-10 served,
            # UniqueViolation in 4 of 6 parts). The same documents again: left out; a
            # group sharing a document with another but not the same document: refused.
            shared = [c for c in cites if c in held]
            if shared:
                if len(shared) == len(cites) and all(held[c] == of[c] for c in cites):
                    duplicates += 1
                    continue
                raise Refused(f"mention_groups_overlap:{g}")
            held.update(of)
            docs[g] = cites
    finally:
        db.close()
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    handles = sorted({c for cs in docs.values() for c in cs})
    of_handle: dict[str, set[str]] = {}
    prop = MENTION_FEATURES["petra_thread"]["document_property"]
    with psycopg.connect(dsn) as pg:
        pg.execute("SET TRANSACTION READ ONLY")
        for k in range(0, len(handles), 50_000):
            for h, o in pg.execute("SELECT l1_handle, object_id FROM ontology.object_versions "
                                   "WHERE l1_handle = ANY(%s::text[])",
                                   (handles[k:k + 50_000],)).fetchall():
                of_handle.setdefault(h, set()).add(o)
        objects = sorted({o for os_ in of_handle.values() for o in os_})
        threads: list[tuple[str, str]] = []
        for k in range(0, len(objects), 50_000):
            threads += pg.execute("SELECT object_id, value FROM ontology.object_values WHERE "
                                  "property = %s AND value IS NOT NULL AND object_id = "
                                  "ANY(%s::text[])", (prop, objects[k:k + 50_000])).fetchall()
    for g, cites in docs.items():
        find("g:" + g)
        for c in cites:
            for o in of_handle.get(c, ()):
                union("g:" + g, "o:" + o)
    for o, value in threads:
        union("o:" + o, "c:" + str(value))
    components: dict[str, dict[str, Any]] = {}
    for g, cites in docs.items():
        comp = components.setdefault(find("g:" + g), {"groups": [], "objects": set(), "docs": 0})
        comp["groups"].append(g)
        comp["docs"] += len(cites)
        comp["objects"] |= {o for c in cites for o in of_handle.get(c, ())}
    out = [{"groups": [], "objects": set(), "docs": 0} for _ in range(max(1, parts))]
    for comp in sorted(components.values(), key=lambda c: (-c["docs"], min(c["groups"]))):
        least = min(out, key=lambda p: p["docs"])
        least["groups"] += comp["groups"]
        least["objects"] |= comp["objects"]
        least["docs"] += comp["docs"]
    reached = set().union(*(p["objects"] for p in out))
    # the scope's objects no group's document is: all in the first part (a document of an
    # ungrouped source can be the version of several of them; dealt over the parts, a part
    # derived links from an object another part held: own_link_id_twice, 1% proof 11-11)
    out[0]["objects"] |= set(scope["objects"]) - reached
    return [{"groups": sorted(p["groups"]), "objects": sorted(p["objects"]),
             "counts": {"documents": p["docs"], "groups": len(p["groups"]),
                        **({"duplicate_groups": duplicates} if k == 0 else {})}}
            for k, p in enumerate(out)]


def mentions_in_parts(args: Any, dsn: str, scope: Mapping[str, Any], workers: int
                      ) -> dict[str, Any]:
    """The scoped ``mentions`` step on ``workers`` processes, one :func:`mention_parts`
    part each (the steps before it shared from ``args.work``), their links merged into
    ``args.work/mentions.sqlite`` and its JSON, as one process writes them. A part that
    fails fails the step; a rerun reruns only the parts not done. Each part's store is
    deleted once merged (its JSON kept): the merge never holds the links twice over (a
    rerun after a merge that stopped runs the parts whose store is gone again)."""
    import os
    import subprocess
    work = Path(args.work)
    stage_key = getattr(args, "stage_key", None)
    if (work / "mentions.json").is_file() and ((work / "mentions.sqlite").is_file()
                                               or stage_key is not None):
        if stage_key is not None:
            import psycopg
            from operating.pg import step_staging
            entry = json.loads((work / "mentions.json").read_text())
            with psycopg.connect(dsn) as pg, pg.cursor() as cur:
                parts_held = step_staging.staged(cur, "mentions")
            if not parts_held or step_staging.totals(parts_held) != entry.get("staged"):
                raise Refused("staged_lost:mentions")
        return {**json.loads((work / "mentions.json").read_text()), "resumed": True}
    started = time.monotonic()
    root = work / "mention-parts"
    root.mkdir(parents=True, exist_ok=True)
    plan_file = root / "parts.json"
    batches = max(1, int(getattr(args, "mention_batches", 1) or 1))
    apply = bool(getattr(args, "apply_now", False))
    emulate = bool(getattr(args, "emulate_batches", False))
    if (apply or emulate) and stage_key is None:
        raise Refused("mention_batches_unstaged")
    if not plan_file.is_file():
        parts = mention_parts(dsn, scope, args.mention_cache, workers * batches)
        plan_file.write_text(json.dumps(parts))
    parts = json.loads(plan_file.read_text())
    if len(parts) != workers * batches:
        raise Refused(f"mention_parts_planned_otherwise:{len(parts)}")
    narrowed_run = json.loads(Path(args.narrowed).read_text())["run"]
    before = sorted(n for n in narrowed_run if n < "mentions")
    if stage_key is not None:
        # the steps before staged once here, never by the parts at once
        from ontology import step_migrate as sm
        for n in before:
            _staged_entry(dsn, stage_key, n, work / f"{n}.sqlite", work / f"{n}.json",
                          narrowed_run, sm.STEPS, None, 0, apply=apply)
    import threading
    pending: list[Any] = []   # the batch being applied while the next one runs
    errors: list[BaseException] = []

    def applying(b: int, ks: list[int], t: float) -> None:
        from operating.pg import step_staging
        if (root / f"batch-{b}.json").is_file():
            return   # applied by an earlier run (its record kept)
        try:
            got = step_staging.apply_now(dsn, stage_key, "mentions", ks)
            if os.environ.get("PLX_STAGE_KEEP_PARTS") != "1":
                for k in ks:
                    (root / str(k) / "mentions.sqlite").unlink(missing_ok=True)
            mark = {"batch": b, "parts": ks, "seconds": round(time.monotonic() - t, 1),
                    "applied": got["bits"],
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            (root / f"batch-{b}.json").write_text(json.dumps(mark, sort_keys=True))
            print(f"mention batch {b + 1}/{batches} applied in {mark['seconds']} s: "
                  f"{sum(v.get('inserted', 0) for v in got['bits'].values())} inserted, "
                  f"{sum(v.get('deleted', 0) for v in got['bits'].values())} deleted",
                  file=sys.stderr, flush=True)
        except BaseException as exc:   # noqa: BLE001 - raised by the run below
            errors.append(exc)

    for b in range(batches):
        ks = list(range(b * workers, (b + 1) * workers))
        t = time.monotonic()
        # parts are closed under what their documents read of each other (the 1% proof:
        # all at once equals batch by batch, row for row), so a batch runs while the one
        # before it is applied
        _mention_batch(args, dsn, work, root, parts, ks, before, stage_key,
                       apply, b * workers if emulate else None)
        for th in pending:
            th.join()
        pending.clear()
        if errors:
            raise errors[0]
        if apply:
            th = threading.Thread(target=applying, args=(b, ks, t))
            th.start()
            pending.append(th)
        else:
            print(f"mention batch {b + 1}/{batches} staged in "
                  f"{round(time.monotonic() - t, 1)} s", file=sys.stderr, flush=True)
    for th in pending:
        th.join()
    if errors:
        raise errors[0]
    if stage_key is not None:
        return _staged_parts(dsn, work, root, len(parts), started)
    return _merged_parts(work, root, parts, started)


def _mention_batch(args: Any, dsn: str, work: Path, root: Path, parts: Sequence[Any],
                   ks: Sequence[int], before: Sequence[str], stage_key: str | None,
                   apply: bool, part_before: int | None) -> None:
    """The parts ``ks`` of a parted ``mentions`` run, one process each, at once (a part
    staged before, or applied, is not run again)."""
    import subprocess
    applied: dict[int, Any] = {}
    if apply:
        import psycopg
        from operating.pg import step_staging
        with psycopg.connect(dsn) as pg, pg.cursor() as cur:
            applied = step_staging.applied(cur, "mentions")
    procs = []
    for k in ks:
        part = parts[k]
        d = root / str(k)
        d.mkdir(exist_ok=True)
        for n in before:
            for ext in ("json", "sqlite"):
                link = d / f"{n}.{ext}"
                if not link.exists():
                    link.symlink_to((work / f"{n}.{ext}").resolve())
        (d / "scope.json").write_text(json.dumps({k2: part[k2] for k2 in ("groups", "objects")}))
        if (d / "mentions.json").is_file() and (d / "mentions.sqlite").is_file():
            continue
        if stage_key is not None and (d / "mentions.json").is_file() and \
                json.loads((d / "mentions.json").read_text()).get("staged"):
            continue   # staged, its store deleted (checked against the markers below)
        if k in applied:
            raise Refused(f"mention_part_applied_without_json:{k}")
        (d / "mentions.json").unlink(missing_ok=True)
        # PLX_INPLACE_STEP_CMD (a JSON list): how a part runs this module (a read-only
        # timing harness runs the parts through itself)
        cmd = json.loads(os.environ.get("PLX_INPLACE_STEP_CMD") or "null") or [
            sys.executable, "-m", "ontology.pg_step_inplace"]
        cmd = [*cmd, "steps", "--plan", args.plan,
               "--narrowed", args.narrowed, "--state-dir", args.state_dir, "--work", str(d),
               "--scope", str(d / "scope.json"), "--mention-cache", args.mention_cache,
               "--only", "mentions", "--dsn", dsn,
               *(["--stage-key", stage_key, "--stage-part", str(k)] if stage_key else []),
               *(["--view", "applied"] if apply else []),
               *(["--part-before", str(part_before)] if part_before is not None else [])]
        log = open(d / "run.log", "w")
        procs.append((k, subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT), log))
    failed = []
    for k, proc, log in procs:
        if proc.wait() != 0:
            failed.append(k)
        log.close()
    if failed:
        raise Refused(f"mention_parts_failed:{failed}")


def _merged_parts(work: Path, root: Path, parts: Sequence[Any], started: float
                  ) -> dict[str, Any]:
    """The parts' stores merged into ``work/mentions.sqlite`` (an unstaged run)."""
    from ontology import store_ddl
    part_file = work / "mentions.sqlite.part"
    local = _local_store(part_file)
    try:
        total, reports = 0, []
        for k in range(len(parts)):
            d = root / str(k)
            for rows in _store_link_batches(d / "mentions.sqlite"):
                local.add("links", rows)
                total += len(rows)
            reports.append(json.loads((d / "mentions.json").read_text()))
            (d / "mentions.sqlite").unlink()   # a stopped merge: the part runs again
        conn = local.connection
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        kept = local.keep_as_part()
    except BaseException:
        local.abort()
        raise
    Path(kept).replace(work / "mentions.sqlite")
    del store_ddl
    entry = {"seconds": round(time.monotonic() - started, 3), "links": total,
             "reads": sum(r.get("reads", 0) for r in reports), "parts": len(parts),
             "part_seconds": [r.get("seconds") for r in reports],
             "report": _summed([r.get("report") for r in reports])}
    (work / "mentions.json").write_text(json.dumps(entry, sort_keys=True))
    return entry


def _staged_parts(dsn: str, work: Path, root: Path, count: int, started: float
                  ) -> dict[str, Any]:
    """The staged parts of a parted ``mentions`` run checked against their markers (each
    part's JSON names its own), their stores deleted, ``work/mentions.json`` written with
    the totals: no merged store (its links are staged; the later steps read them there)."""
    import psycopg
    from operating.pg import step_staging
    reports = [json.loads((root / str(k) / "mentions.json").read_text()) for k in range(count)]
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        held = step_staging.staged(cur, "mentions")
    for k, r in enumerate(reports):
        if k not in held or step_staging.totals({k: held[k]}) != r.get("staged"):
            raise Refused(f"mention_part_not_staged:{k}")
    if set(held) != set(range(count)):
        raise Refused(f"mention_parts_staged_other:{sorted(set(held) - set(range(count)))}")
    if os.environ.get("PLX_STAGE_KEEP_PARTS") != "1":   # (a rehearsal compares them)
        for k in range(count):
            (root / str(k) / "mentions.sqlite").unlink(missing_ok=True)
    entry = {"seconds": round(time.monotonic() - started, 3),
             "links": sum(r.get("links", 0) for r in reports),
             "reads": sum(r.get("reads", 0) for r in reports), "parts": count,
             "part_seconds": [r.get("seconds") for r in reports],
             "staged": step_staging.totals(held),
             "report": _summed([r.get("report") for r in reports])}
    (work / "mentions.json").write_text(json.dumps(entry, sort_keys=True))
    return entry


def _summed(reports: Sequence[Any]) -> Any:
    """The parts' step reports as one: numbers added, equal values kept, else a list."""
    if all(isinstance(r, Mapping) for r in reports) and reports:
        keys = sorted({k for r in reports for k in r})
        return {k: _summed([r[k] for r in reports if k in r]) for k in keys}
    if all(isinstance(r, (int, float)) and not isinstance(r, bool) for r in reports):
        return sum(reports)
    return reports[0] if all(r == reports[0] for r in reports) else list(reports)


def _store_link_batches(path: Path, size: int = 20_000) -> Iterator[list[dict[str, Any]]]:
    """A step store's links in their order, ``size`` rows at a time. Never the whole store
    as one list: document_fields' 332k rows as dicts held ~1.2 GB in every process that
    loaded it (each of 12 mention parts: 14.8 GB, 11-10)."""
    from ontology import store_ddl
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = list(store_ddl.LINK_COLUMNS)
        cur = conn.execute(f"SELECT {', '.join(cols)} FROM links ORDER BY rowid")
        while True:
            got = cur.fetchmany(size)
            if not got:
                return
            yield [dict(zip(cols, r)) for r in got]
    finally:
        conn.close()


SCOPE_CHUNK = 50_000   # documents per scoped owned entry: one chunk (transaction) each


def write_run_layer(the_plan: Mapping[str, Any], run: Mapping[str, Any], new: Any,
                    work: Path, out: Path, *, unit_id: str | None, delta_mark: int,
                    sequence: int, mention_docs: Sequence[str] | None = None,
                    marked: Sequence[str] | None = None, marked_scope: Sequence[str] = (),
                    specs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The step layer of a committed run (:func:`run_steps`' stores in ``work``), as
    ``step_migrate.build_layer`` writes it, applied by ``operating.pg.migrate_chunks``:
    the re-run steps' links, ``text_marks`` (``marked``: the version handles a marks-only
    text index change marks; ``marked_scope``: the type prefixes of their objects), and,
    with ``mention_docs``, the ``mentions`` owned range split into entries naming
    ``scope_objects_<n>`` tables (its links from those documents only) and ``from``
    marked :data:`operating.pg.migrate_chunks.SCOPED`, which the plain apply refuses."""
    from ontology import step_migrate as sm
    from ontology.schema_migrate import content_digest
    from operating.pg.migrate_chunks import SCOPED, STAGED
    specs = sm.STEPS if specs is None else specs
    remark = set(the_plan["steps"].get("remark") or ())
    if remark - {"text_index"}:
        raise Refused("remark_unsupported:" + ",".join(sorted(remark - {"text_index"})))
    if remark and marked is None:
        raise ValueError("text_marks_required")
    derived = {k: v for k, v in run.items() if k not in remark}
    values_owned = sm._owned_values(derived, specs)
    missing = [n for n in derived if not (work / f"{n}.json").is_file()]
    if missing:
        raise ValueError("steps_not_run:" + ",".join(sorted(missing)))
    # the steps whose difference to the served links is staged (operating.pg.step_staging):
    # applied from there (``steps:staged:<step>``), none of their links in the layer
    staged = {n: json.loads((work / f"{n}.json").read_text()).get("staged")
              for n in sorted(derived)}
    staged = {n: v for n, v in staged.items() if v}
    if staged and set(staged) != set(derived):
        raise Refused("staged_some:" + ",".join(sorted(set(derived) - set(staged))))
    owned = [o for name in sorted(derived) for o in sm._owned(name, derived[name], specs)]
    folder = Path(tempfile.mkdtemp(prefix=".run-layer-", dir=out.parent))
    try:
        every = _local_store(folder / "links.sqlite")
        try:
            for name in sorted(set(derived) - set(staged)):
                for rows in _store_link_batches(work / f"{name}.sqlite"):
                    every.add("links", rows)
            every.connection.commit()
            layer = _local_store(folder / "layer.sqlite")
            try:
                with tempfile.TemporaryDirectory(prefix=".parts-", dir=folder) as parts:
                    links, ends = sm.write_layer_links(layer, every.connection, every.temp,
                                                       [] if staged else owned, 1,
                                                       Path(parts))
                links += sum(int(v["written"]) for v in staged.values())
                ends = set(ends) | {e for v in staged.values() for e in v.get("ends") or ()}
                values = _layer_values(work, values_owned) if values_owned else {}
                for table in ("objects", "object_versions", "object_values"):
                    rows = values.get(table) or []
                    for i in range(0, len(rows), 20_000):
                        layer.add(table, rows[i:i + 20_000])
                layer.set_meta("format", sm.FORMAT)
                layer.set_meta("steps_digest", the_plan["steps_to"])
                c = layer.connection
                record_owned = list(owned)
                tables: tuple[str, ...] = ()
                if staged:
                    record_owned = []   # every step's links: its staged chunk
                elif mention_docs is not None and "mentions" in derived:
                    if derived["mentions"] is not None:
                        raise Refused("mention_scope_with_from_types")
                    docs = sorted(set(mention_docs))
                    record_owned = [o for o in owned if o["step"] != "mentions"]
                    for o in sm._owned("mentions", None, specs):
                        for k in range(0, max(len(docs), 1), SCOPE_CHUNK):
                            table = f"scope_objects_{len(tables)}"
                            c.execute(f"CREATE TABLE {table} (object_id TEXT PRIMARY KEY)")
                            c.executemany(f"INSERT INTO {table} VALUES (?)",
                                          [(d,) for d in docs[k:k + SCOPE_CHUNK]])
                            record_owned.append({**o, "objects": table})
                            tables += (table,)
                    # every re-derived mention link is from a scoped document
                    names = " UNION ALL ".join(f"SELECT object_id FROM {t}" for t in tables)
                    (astray,) = c.execute(
                        f"SELECT count(*) FROM links WHERE link_id >= ? AND link_id < ? AND "
                        f"from_object NOT IN ({names})", (sm._owned("mentions", None, specs)
                                                          [0]["prefix"],
                                                          sm._owned("mentions", None, specs)
                                                          [0]["end"])).fetchone()
                    if astray:
                        raise Refused(f"mention_links_outside_scope:{astray}")
                if remark:
                    c.execute(f"CREATE TABLE {sm.TEXT_MARKS} (version_handle TEXT PRIMARY KEY)")
                    c.executemany(f"INSERT INTO {sm.TEXT_MARKS} VALUES (?)",
                                  [(h,) for h in sorted(set(marked or ()))])
                    tables += (sm.TEXT_MARKS,)
                scope = sorted(ends | {p for o in owned if o["from"] for p in o["from"]}
                               | {o["prefix"] for o in values_owned} | set(marked_scope))
                content = content_digest(c, ("link_rows", "objects", "object_versions",
                                             "value_sources", "value_cells") + tables)
                ident = [the_plan["id"], unit_id, content, sequence]
                if staged:
                    ident.append({n: v["digest"] for n, v in staged.items()})
                record = {"id": f"{sm._sha(ident)[:16]}-{sequence}",
                          "plan": the_plan["id"], "unit": unit_id, "content": content,
                          "sequence": sequence,
                          "from": (STAGED if staged else SCOPED if mention_docs is not None
                                   else "") + the_plan["steps_from"],
                          "to": the_plan["steps_to"], "owned": record_owned,
                          "values": values_owned,
                          "scope": scope, "steps": sorted(run),
                          "inputs": sm.declared_inputs(run, (new,), specs),
                          "delta_mark": int(delta_mark),
                          "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                if staged:
                    record["staged"] = staged
                if remark:
                    record["text_marks"] = {"steps": sorted(remark),
                                            "marks": list(sm.SENSITIVE_MARKS),
                                            "documents": len(set(marked or ()))}
                c.execute(f"CREATE TABLE {sm.TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                c.executemany(f"INSERT INTO {sm.TABLE} VALUES (?, ?)",
                              [(k, json.dumps(v, sort_keys=True)) for k, v in record.items()])
                path = layer.keep_as_part()
            except BaseException:
                layer.abort()
                raise
        finally:
            every.abort()
        Path(path).replace(out)
    finally:
        for p in sorted(folder.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        folder.rmdir()
    return {"id": record["id"], "links": links, "owned": len(record_owned),
            "scope": len(scope), "text_marks": len(set(marked or ())),
            "values": len(values.get("object_values") or [])}


def _layer_values(work: Path, owned: Sequence[Mapping[str, Any]]
                  ) -> dict[str, list[dict[str, Any]]]:
    """The re-derived values of the value-owning steps (their stores in ``work``) with
    their objects' own and version rows (read from Postgres into the store as the step
    wrote them), as ``build_layer`` writes them (the apply copies only the values, onto
    the served versions they name)."""
    from ontology import store_ddl
    from ontology.store import StoreBuilder
    rows: list[dict[str, Any]] = []
    for name in sorted({o["step"] for o in owned}):
        conn = sqlite3.connect(f"file:{work / f'{name}.sqlite'}?mode=ro", uri=True)
        try:
            rows += [dict(zip(store_ddl.VALUE_COLUMNS, r)) for r in _value_rows(
                conn, [o for o in owned if o["step"] == name])]
        finally:
            conn.close()
    rows.sort(key=lambda r: (r["object_id"], r["l1_handle"] or "", r["binding_id"] or "",
                             r["property"]))
    ids = sorted({r["object_id"] for r in rows})
    out: dict[str, list[dict[str, Any]]] = {"object_values": rows, "objects": [],
                                            "object_versions": []}
    for name in sorted({o["step"] for o in owned}):
        conn = sqlite3.connect(f"file:{work / f'{name}.sqlite'}?mode=ro", uri=True)
        try:
            for table in ("objects", "object_versions"):
                cols = StoreBuilder._INSERT[table]
                out[table] += [dict(zip(cols, r)) for r in conn.execute(
                    f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid")]
        finally:
            conn.close()
    found = {r["object_id"] for r in out["objects"]}
    if found != set(ids):   # a value whose object Postgres does not hold
        raise Refused(f"values_without_object:{len(set(ids) - found)}")
    return out


def narrow(the_plan: Mapping[str, Any], served: Any, new: Any, touched: Iterable[str],
           links: Iterable[tuple[str, str]], present: Iterable[str]) -> dict[str, Any]:
    """The plan's step part again with the unit migration's inputs narrowed to what its
    apply wrote: ``touched`` (object types with an object written, changed or dropped)
    and ``links`` (type pairs of link units with a link written or deleted)
    (``step_migrate.plan_steps``: the code and link-type reasons and the readers stay; a
    step run again only for inputs nothing wrote is not)."""
    from ontology import step_migrate as sm
    present = sorted(present)
    old_code, new_code = _codes(the_plan)
    inputs = sm.step_inputs([served, new], touched, present, links=links)
    out = sm.plan_steps(served, new, old_code, new_code, present, inputs=inputs)
    if out["why"] and {k: v for k, v in out["why"].items() if v == "code"} != \
            {k: v for k, v in the_plan["steps"]["why"].items() if v == "code"}:
        raise ValueError("narrowed_plan_of_other_code")
    if set(out["run"]) - set(the_plan["steps"]["run"]):
        raise ValueError("narrowed_plan_runs_more")
    # the plan's marks-only re-marks stay re-marks: the served code is known here only by
    # a stand-in, so the pin (step_migrate.MARKS_ONLY) cannot match it again
    remark = sorted(set(the_plan["steps"].get("remark") or ()) & set(out["run"]))
    refused = [r for r in out.get("refused") or ()
               if r not in {f"step_unsupported:{n}" for n in remark}]
    if refused or the_plan["steps"].get("refused"):
        raise ValueError("narrowed_plan_refused:" + ",".join(refused))
    out["remark"], out["refused"] = remark, []
    return out


def _codes(the_plan: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """(served, new) step code digests of ``the_plan``: the served ones are known only by
    their digest, so each step whose code the plan says changed gets a stand-in."""
    from ontology import step_migrate as sm
    new_code = sm.code_digests(None)
    if "steps:" + sm._sha(new_code)[:32] != the_plan["steps_to"]:
        raise ValueError("tree_step_code_is_not_the_plans")
    changed = {k for k, v in the_plan["steps"]["why"].items() if v == "code"}
    old = {k: (("served:" + v) if k in changed else v) for k, v in new_code.items()}
    return old, new_code


# The capture sources whose ``document_groups`` lists the latest rows of a whole table
# (mail ``messages``, SharePoint ``items``: ``ontology.project`` ``_LATEST``) with one sort
# of every row and its record: listing them took ~95 min on served plx, and every shard
# listed them all to keep 1/shards (64 shards: ~13 h of listing on 4 workers). Each shard's
# listing here keeps only its own handles inside the latest-version subquery, so a shard
# reads and sorts only its rows (the payroll re-mark judge's listing). The rows, their order and the loads are the source's own; a source whose
# query is not this shape is refused.
_LATEST_TAIL = re.compile(r" FROM (messages|items) GROUP BY l1_handle\)$")
_SHARDED_LOADS = ("_message_documents", "_file_documents")   # the loads of those rows


def _handle_shard(handle: Any, shards: int) -> int:
    import hashlib
    return int(hashlib.sha256(repr(handle).encode("utf-8")).hexdigest()[:8], 16) % shards


def _shard_listings(projector: Any, shard: int, shards: int
                    ) -> tuple[dict[int, tuple[int, int]], str]:
    """Each whole-table listing of the projector's sources restricted to ``shard``;
    {id(source): (its handles, those in this shard)} and the digest of every handle."""
    import hashlib
    sharded: dict[int, tuple[int, int]] = {}
    digest = hashlib.sha256()
    for name in sorted(getattr(projector, "sources", {}) or {}):
        source = projector.sources[name]
        source = getattr(source, "_inner", getattr(source, "_source", source))
        latest = getattr(type(source), "_LATEST", None)
        conn = getattr(source, "_conn", None)
        if not callable(getattr(source, "document_groups", None)) or latest is None \
                or conn is None:
            continue
        found = _LATEST_TAIL.search(latest)
        if found is None or getattr(source, "sample", None) is not None:
            raise Refused(f"text_marks_listing_not_shardable:{name}")
        conn.create_function("plx_marks_shard", 1,
                             lambda handle: int(_handle_shard(handle, shards) == shard),
                             deterministic=True)
        source._LATEST = latest[:-1] + " HAVING plx_marks_shard(l1_handle))"
        handles = mine = 0
        digest.update(f"{name}\x00".encode("utf-8"))
        for (handle,) in conn.execute(f"SELECT l1_handle FROM {found.group(1)} GROUP BY "
                                      "l1_handle ORDER BY l1_handle"):
            handles += 1
            mine += _handle_shard(handle, shards) == shard
            digest.update(repr(handle).encode("utf-8") + b"\x00")
        sharded[id(source)] = (handles, mine)
    return sharded, digest.hexdigest()


def marks_code() -> str:
    """What a judged shard is a function of besides the captures: the text index's step
    code and the classifier it calls (``operating/chunking/sensitive.py``, not in that
    step's code files), so a shard judged by another classifier is never taken."""
    import hashlib
    from ontology import step_migrate as sm
    digest = hashlib.sha256(sm.code_digests()["text_index"].encode())
    digest.update((Path(sm.ROOT) / "operating" / "chunking" / "sensitive.py").read_bytes())
    return digest.hexdigest()


# The judge's exact pre-check of a document (:func:`_may_be_sensitive`). Each signal of
# ``operating.chunking.sensitive.personal_financial`` needs at least one of these words in
# the text it reads (the case-insensitive patterns in the text lowered, the case-sensitive
# ones as written): the statement phrases, a card word (a card number counts only beside
# one), a time sheet's name or hours, a core pay phrase (payroll counts only with one), a
# SIN word, an offer or a termination phrase. A title naming a time sheet and a captured
# label are left to the classifier. Derived from the classifier of ``_PRECHECK_CODE``
# (``marks_code``): under any other the pre-check is off and every document is classified.
_PRECHECK_CODE = "eeeb7feea3178fad317e8592ab02912f183ae37f4aeb4475ae9e774efc4d7634"
_PRECHECK_LOWER = (
    # _STATEMENT
    "minimum ", "credit limit", "available credit", "previous ", "new balance", "statement ",
    "cash advance", "interest rate", "interest charged", "purchases and ", "opening balance",
    "closing balance", "points ",
    # _CARD_WORD
    "card", "visa", "amex", "american express", "discover", "credit", "debit",
    # _TIME_NAME, _TIME_HOURS
    "timesheet", "time sheet", "timecard", "time card", "hours", "hrs",
    # _PAY_CORE (case-insensitive parts), _SIN_WORD
    "gross pay", "gross earnings", "gross wages", "net pay", "net earnings",
    "canada pension plan", "employment insurance", "social insurance",
    "pay stub", "paystub", "pay slip", "payslip", "pay statement", "earnings statement",
    "statement of earnings", "pay advice", "direct deposit advice",
    # _OFFER, _TERMINATION
    "offer", "terminat", "dismissal", "layoff", "lay-off",
)
_PRECHECK_CASED = ("CPP", "C.P.P", "QPP", "EI", "SIN", "T4")
_FOLD: dict[int, str] | None = None


def _ascii_fold() -> dict[int, str]:
    """Every non-ASCII character a case-insensitive pattern matches an ASCII letter with
    (the Kelvin sign, the long s, the dotted capital I, ...), as that letter: the text is
    lowered after it, so a lowered word is found wherever such a pattern could match."""
    global _FOLD
    if _FOLD is None:
        every = "".join(chr(c) for c in range(128, 0x110000) if not 0xD800 <= c < 0xE000)
        fold: dict[int, str] = {}
        for letter in "abcdefghijklmnopqrstuvwxyz":
            for found in re.finditer(letter, every, re.I):
                fold[ord(found.group())] = letter
        _FOLD = fold
    return _FOLD


def _precheck_on() -> bool:
    return marks_code() == _PRECHECK_CODE


def _may_be_sensitive(document: Any) -> bool:
    """False only when ``text_index._sensitive(document)`` is False: the same text, title
    and labels, and no word a signal needs (:data:`_PRECHECK_LOWER`)."""
    from ontology.text_index import FIELD_CAP, _fields
    from operating.chunking.sensitive import _TIME_NAME, folders, labelled
    texts, title, labels = [], None, []
    for _citation, field_path, text in _fields(document):
        if not isinstance(text, str):
            continue
        prop = field_path[len("row."):] if field_path.startswith("row.") else field_path
        if prop in ("name", "subject") and title is None:
            title = text
        if prop == "path":
            labels.extend(folders(text))
        elif prop in ("sensitivity_label", "retention_label", "compliance_tag"):
            labels.append(text)
        texts.append(text[:FIELD_CAP])
    if labels and labelled(labels):
        return True
    if title and _TIME_NAME.search(title):
        return True
    joined = "\n".join(texts)
    if not joined:
        return False
    if any(word in joined for word in _PRECHECK_CASED):
        return True
    lowered = joined.translate(_ascii_fold()).lower()
    return any(word in lowered for word in _PRECHECK_LOWER)


def _needs(items: Any, ci: bool) -> frozenset[tuple[str, bool]] | None:
    """Words one of which every match of the parsed regex ``items`` holds: ``(word,
    lowered)``, ``lowered`` when it is to be found in the text lowered (a case-blind
    part), else as written; None when no word is needed. Each literal run (a zero-width
    assertion inside it keeps it whole; a class of one letter's two cases is that letter
    lowered), a group, a branch of which every arm needs words and a repeat of at least
    one are candidates; the most selective (its shortest word longest) is taken."""
    try:
        from re import _constants as C, _parser as P   # Python 3.11+
    except ImportError:   # pragma: no cover - Python 3.10
        import sre_constants as C   # type: ignore[no-redef]
        import sre_parse as P   # type: ignore[no-redef]
    del P
    best: frozenset[tuple[str, bool]] | None = None

    def offer(found: frozenset[tuple[str, bool]] | None) -> None:
        nonlocal best
        if found and (best is None or min(len(w) for w, _ in found)
                      > min(len(w) for w, _ in best)):
            best = found
    run: list[str] = []
    lowered = ci

    def close() -> None:
        nonlocal run, lowered
        if run:
            word = "".join(run)
            offer(frozenset({(word.lower() if lowered else word, lowered)}))
        run, lowered = [], ci
    for op, av in items:
        if op is C.LITERAL and av < 128:
            run.append(chr(av))
        elif op is C.IN and all(o is C.LITERAL and a < 128 for o, a in av) and \
                len({chr(a).lower() for _o, a in av}) == 1:
            run.append(chr(av[0][1]).lower())
            lowered = True   # a case pair: the run is looked for in the lowered text
        elif op is C.AT:
            continue
        else:
            close()
            if op is C.SUBPATTERN:
                _group, add, remove, body = av
                inner = (ci or bool(add & re.I)) and not remove & re.I
                offer(_needs(body, inner))
            elif op is C.BRANCH:
                arms = [_needs(arm, ci) for arm in av[1]]
                if all(arms):
                    offer(frozenset().union(*arms))   # type: ignore[arg-type]
            elif op in (C.MAX_REPEAT, C.MIN_REPEAT, getattr(C, "POSSESSIVE_REPEAT", None)) \
                    and av[0] >= 1:
                offer(_needs(av[2], ci))
    close()
    return best


class _Lowered:
    """The last few texts lowered for the guards (folded first: :func:`_ascii_fold`),
    held by identity (a document's text, then a window of it, then the text again)."""
    held: list[tuple[str, str]] = []

    @classmethod
    def of(cls, text: str) -> str:
        for seen, lowered in cls.held:
            if seen is text:
                return lowered
        lowered = text.translate(_ascii_fold()).lower()
        cls.held = [(text, lowered), *cls.held[:2]]
        return lowered


class _Guarded:
    """A compiled pattern that is searched only when the text holds a word it needs
    (:func:`_needs`): the same answer, without the scan where it cannot match."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        from re import _parser as P
        self.pattern_ = pattern
        self.needs = _needs(P.parse(pattern.pattern, pattern.flags),
                            bool(pattern.flags & re.I))

    def _may(self, text: str) -> bool:
        if self.needs is None:
            return True
        return any(word in (_Lowered.of(text) if lowered else text)
                   for word, lowered in self.needs)

    def search(self, text: str, *args: Any) -> Any:
        return self.pattern_.search(text, *args) if self._may(text) else None

    def finditer(self, text: str, *args: Any) -> Any:
        return self.pattern_.finditer(text, *args) if self._may(text) else iter(())

    def __getattr__(self, name: str) -> Any:
        return getattr(self.pattern_, name)


@contextlib.contextmanager
def _guarded_classifier() -> Iterator[None]:
    """While open, ``operating.chunking.sensitive``'s patterns are :class:`_Guarded` and
    its card and SIN number checks run only where their word is (a number counts only
    within ``NEAR`` of a card or SIN word): its answers unchanged, its scans skipped where
    they cannot match. Restored on exit."""
    from operating.chunking import sensitive as S
    saved = dict(vars(S))
    try:
        for name, value in saved.items():
            if isinstance(value, re.Pattern):
                setattr(S, name, _Guarded(value))
            elif isinstance(value, tuple) and value and \
                    all(isinstance(v, re.Pattern) for v in value):
                setattr(S, name, tuple(_Guarded(v) for v in value))
        card_word, sin_word = S._CARD_WORD, S._SIN_WORD
        card, sin = saved["_card_number"], saved["_sin_number"]
        S._card_number = lambda text: card_word._may(text) and card(text)
        S._sin_number = lambda text: sin_word._may(text) and sin(text)
        yield
    finally:
        for name, value in saved.items():
            if getattr(S, name, None) is not value:
                setattr(S, name, value)


def judge_shard(state: str, shard: int, shards: int, out: str,
                readings: Sequence[str] = ()) -> dict[str, Any]:
    """The text index's marks-only re-mark judged on the groups of ``shard`` (of
    ``shards``): a group of a whole-table listing by its handle (only this shard's rows
    are listed: :func:`_shard_listings`), any other by its key's digest;
    ``out/shard-<n>.json`` (atomically) with the marked version handles. A group that
    cannot be read fails the shard. A source that lists its documents one by one (no
    group key: Teams, meetings) is read whole by every shard (its documents are built as
    it lists them); each document is judged in the shard of its citation. ``readings``:
    primed reading stores (``ontology.readings_primer``) served before the build's kept
    readings. A document whose text holds no word any of the classifier's signals needs
    is not classified (:func:`_may_be_sensitive`: exact, off unless the classifier is the
    one it was derived from)."""
    import hashlib
    import os
    from ontology import step_migrate as sm
    from ontology.delta import _internal_domains
    from ontology.mentions import source_document_groups
    from ontology.schema import load_schema
    from ontology.text_index import _sensitive
    from operating.host import state_captures
    started = time.monotonic()
    counts = {"groups_listed": 0, "groups": 0, "documents": 0, "failed": 0, "classified": 0}
    precheck = _precheck_on()
    marked: set[str] = set()
    listed = hashlib.sha256()
    projection = Path(state) / "projection"
    with contextlib.ExitStack() as stack:
        sources = state_captures(Path(state), stack,
                                 internal_domains=_internal_domains(os.environ))
        sm.open_snapshots(sources)
        blobs = [s.blobs for s in sources.values()
                 if isinstance(getattr(s, "blobs", None), (str, Path))]
        _serve_kept(projection, blobs, readings)
        projector = sm._projector(load_schema(), sources, None)
        sharded, handles = _shard_listings(projector, shard, shards)
        listed.update(handles.encode())
        counts["groups_listed"] = sum(total for total, _mine in sharded.values())
        seen = {k: 0 for k in sharded}
        if precheck:
            stack.enter_context(_guarded_classifier())
        for key, load in source_document_groups(projector):
            owner = getattr(load, "source", None)
            owner = id(getattr(owner, "_inner", owner)) if owner is not None else None
            # a whole-table listing's group (its message or file version); the same source's
            # other groups (SharePoint list items) are keyed groups as any other source's
            if owner in sharded and getattr(load, "method", None) in _SHARDED_LOADS:
                handle = load.args[0]["l1_handle"]
                if _handle_shard(handle, shards) != shard:
                    raise Refused(f"text_marks_group_outside_its_shard:{shard}")
                seen[owner] += 1
            else:
                if key is None:   # one document a source built as it listed it
                    built, _ = load()
                    if len(built) != 1:
                        raise Refused("text_marks_keyless_group_not_one_document")
                    key = "document\x00" + built[0].citation
                counts["groups_listed"] += 1
                listed.update(key.encode() + b"\x00")
                if int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % shards != shard:
                    continue
            counts["groups"] += 1
            try:
                documents, _ = load()
            except (MemoryError, KeyboardInterrupt, sqlite3.Error):
                raise
            except Exception:  # noqa: BLE001 - counted, the shard fails
                counts["failed"] += 1
                continue
            for document in documents:
                counts["documents"] += 1
                if precheck and not _may_be_sensitive(document):
                    continue
                counts["classified"] += 1
                if _sensitive(document):
                    marked.add(document.citation)
    if any(seen[k] != mine for k, (_total, mine) in sharded.items()):
        raise Refused(f"text_marks_shard_listing_incomplete:{shard}")
    if counts["failed"]:
        raise Refused(f"text_marks_groups_unread:{shard}:{counts['failed']}")
    result = {"shard": shard, "shards": shards, "listed": listed.hexdigest(),
              "code": marks_code(), "counts": counts, "marked": sorted(marked),
              "sharded_sources": len(sharded), "precheck": precheck,
              "seconds": round(time.monotonic() - started, 1)}
    path = Path(out) / f"shard-{shard}.json"
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(result), encoding="utf-8")
    tmp.replace(path)
    return {k: v for k, v in result.items() if k != "marked"}


def text_marks(state: str, out: Path, shards: int, workers: int,
               readings: Sequence[str] = (), served: Sequence[str] | None = None
               ) -> tuple[list[str], dict]:
    """Every shard judged (the ones not in ``out`` yet, by ``workers`` processes); the
    marked handles. Refused unless every shard listed the same groups and the string hash
    seed is fixed (a mail group's key holds a frozenset's repr). ``served``: the source
    families the served generation read (``schema_migrate.served_families``; None: every
    bound family): the re-mark judges every document, so one of them the state directory
    does not hold refuses it (``missing_source``; its documents would never be marked)."""
    import concurrent.futures as futures
    import multiprocessing
    import os
    from ontology import step_migrate as sm
    from ontology.schema import load_schema
    from operating.host import held_captures
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise Refused("text_marks_needs_PYTHONHASHSEED=0")
    sm.require_state_sources({}, load_schema(), held_captures(state), served, every=True)
    out.mkdir(parents=True, exist_ok=True)
    todo = [n for n in range(shards) if not (out / f"shard-{n}.json").is_file()]
    if todo:
        with futures.ProcessPoolExecutor(max_workers=max(1, min(workers, len(todo))),
                                         mp_context=multiprocessing.get_context("spawn")) as pool:
            for f in [pool.submit(judge_shard, state, n, shards, str(out), tuple(readings))
                      for n in todo]:
                f.result()
    results = [json.loads((out / f"shard-{n}.json").read_text()) for n in range(shards)]
    if len({r["listed"] for r in results}) != 1 or {r["shards"] for r in results} != {shards}:
        raise Refused("text_marks_shards_disagree")
    if {r.get("code") for r in results} != {marks_code()}:
        raise Refused("text_marks_of_other_code")
    counts = {k: sum(r["counts"][k] for r in results) for k in results[0]["counts"]}
    counts["groups_listed"] = results[0]["counts"]["groups_listed"]
    if counts["groups"] != counts["groups_listed"]:
        raise Refused("text_marks_groups_missed")
    return sorted({h for r in results for h in r["marked"]}), counts


def build_from_state(the_plan: Mapping[str, Any], new: Any, state: Path, out: Path, *,
                     dsn: str, unit_layer: Path | None = None, tenant: str = "plx",
                     commit: bool = False) -> dict[str, Any]:
    """:func:`build_layer` with the host's capture sources and kept readings, as
    ``step_migrate.build_from_state`` opens them."""
    import os
    from integrations.captured_text import kept_readings
    from integrations.kept_members import kept_members
    from ontology import step_migrate as sm
    from ontology.delta import _internal_domains
    from ontology.project_incremental import MEMBERS, READINGS
    from ontology.schema_migrate import next_sequence, served_families
    from operating import build_steps
    from operating.host import state_captures
    from operating.pg.ontology_migrate import delta_mark

    mark = delta_mark(dsn)
    projection = state / "projection"
    with contextlib.ExitStack() as stack:
        sources = state_captures(state, stack, internal_domains=_internal_domains(os.environ))
        never = sm.require_state_sources(the_plan["steps"]["run"], new, sources,
                                         served_families(dsn))
        unopened = sm.open_snapshots(sources)
        blobs = [s.blobs for s in sources.values()
                 if isinstance(getattr(s, "blobs", None), (str, Path))]
        stack.enter_context(kept_readings(projection / READINGS))
        stack.enter_context(kept_members(projection / MEMBERS, blobs))
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix=".step-migrate-", dir=out.parent)))
        steps = build_steps.post_project(state, tenant, scratch=tmp)
        report = build_layer(the_plan, new, steps, dsn, out, sources=sources,
                             unit_layer=unit_layer, delta_mark=mark,
                             sequence=next_sequence(dsn), commit=commit)
        report["snapshots_unopened"] = unopened
        report["sources_never_served"] = never
        return report


def touched_by_apply(reports: Sequence[Path], layers: Sequence[Path],
                     marks: Path | None) -> tuple[list[str], list[tuple[str, str]]]:
    """(object types, link type pairs) the chunked unit applies wrote: a unit group with an
    object changed, added or dropped; a link unit with a link written or deleted (its
    ends' types); ``units:new`` and ``links:rest`` (every type of their layer's new
    bindings and every link unit: fail-safe wide); a marks-only binding with a row
    re-marked (``marks``: ``{binding: counts}``, the binding's type)."""
    from ontology.schema_migrate import read_layer
    from operating.pg.migrate_chunks import unit_groups
    types: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for report_path, layer in zip(reports, layers):
        chunks = json.loads(Path(report_path).read_text())["chunks"]
        record = read_layer(layer)
        groups = unit_groups(record)
        links = record["delete"]["links"]
        for name, out in chunks.items():
            if "skipped" in out:
                raise ValueError(f"chunk_report_without_counts:{name}")
            kind, _, which = name.partition(":")
            if kind == "units" and which == "new":
                if out.get("objects.added") or out.get("rows.written") or any(
                        k.startswith("copied.") and v for k, v in out.items()):
                    types |= {b["type"] for b in record["delete"]["bindings"].values()}
                    types |= {o.split(":")[0] for o in record.get("scope") or ()}
            elif kind == "units":
                if out.get("objects.changed") or out.get("objects.added") or \
                        out.get("objects.dropped"):
                    types.add(groups[int(which)]["type"])
            elif kind == "links" and which == "rest":
                if out.get("written"):
                    pairs |= {(x["from"].split(":")[0], x["to"].split(":")[0]) for x in links}
            elif kind == "links":
                if out.get("written") or out.get("deleted"):
                    x = links[int(which)]
                    pairs.add((x["from"].split(":")[0], x["to"].split(":")[0]))
    if marks is not None:
        from ontology.schema import load_schema
        of = {b["id"]: tid for tid, t in load_schema().object_types.items()
              for b in t.get("bindings", ())}
        for binding, counts in json.loads(Path(marks).read_text()).items():
            if any(v for k, v in counts.items() if isinstance(v, int)):
                if binding not in of:
                    raise ValueError(f"marks_binding_unknown:{binding}")
                types.add(of[binding])
    return sorted(types), sorted(pairs)


def _open_sources(state: Path, stack: contextlib.ExitStack,
                  run: Mapping[str, Any], dsn: str | None) -> dict[str, Any]:
    """The state directory's capture sources (refused when one the document steps of
    ``run`` read is not held: ``step_migrate.require_state_sources``)."""
    import os
    from ontology import step_migrate as sm
    from ontology.delta import _internal_domains
    from ontology.schema import load_schema
    from ontology.schema_migrate import served_families
    from operating.host import state_captures
    projection = state / "projection"
    sources = state_captures(state, stack, internal_domains=_internal_domains(os.environ))
    sm.require_state_sources(run, load_schema(), sources,
                             served_families(dsn) if dsn else None)
    sm.open_snapshots(sources)
    blobs = [s.blobs for s in sources.values()
             if isinstance(getattr(s, "blobs", None), (str, Path))]
    _serve_kept(projection, blobs)
    return sources


def _serve_kept(projection: Path, blobs: Sequence[Any], primed: Sequence[str] = ()) -> None:
    """The readings the builds kept, served read-only (``kept_members.serve_kept``): an
    in-place run never opens them as a build does, which takes their write lock (a running
    build holds it) and prunes, on a clean close, every reading the last three opens did
    not use. ``primed``: stores primed for this run (``ontology.readings_primer``), first."""
    from integrations.kept_members import serve_kept
    serve_kept([*(Path(p) for p in primed), projection], blobs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.pg_step_inplace")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="the step layer in one transaction (prototype)")
    b.add_argument("--plan", required=True, help="the plan JSON (step_migrate plan)")
    b.add_argument("--state-dir", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--unit-layer", default=None)
    b.add_argument("--dsn", required=True)
    b.add_argument("--commit", action="store_true",
                   help="apply both layers and commit (scratch databases only)")
    n = sub.add_parser("narrow", help="the plan's steps, inputs narrowed to the applies' writes")
    n.add_argument("--plan", required=True)
    n.add_argument("--served-ref", required=True)
    n.add_argument("--report", action="append", default=[], help="a migrate_chunks report")
    n.add_argument("--layer", action="append", default=[], help="its unit layer (same order)")
    n.add_argument("--marks", default=None, help="the marks-only apply's JSON report")
    n.add_argument("--out", required=True)
    r = sub.add_parser("steps", help="run the narrowed steps over committed Postgres")
    r.add_argument("--plan", required=True)
    r.add_argument("--narrowed", required=True)
    r.add_argument("--state-dir", required=True)
    r.add_argument("--work", required=True)
    r.add_argument("--scope", default=None, help="mention scope JSON (groups, objects)")
    r.add_argument("--mention-cache", default=None)
    r.add_argument("--only", default=None, help="run only these steps (comma list)")
    r.add_argument("--dsn", default=None)
    r.add_argument("--mention-workers", type=int, default=1,
                   help="a scoped mentions run on this many processes (thread-closed parts)")
    r.add_argument("--stage-key", default=None,
                   help="stage each step's difference to the served links "
                        "(operating.pg.step_staging) under this run key")
    r.add_argument("--stage-part", type=int, default=0)
    r.add_argument("--apply-now", action="store_true",
                   help="apply each finished step's (and mention batch's) staged difference "
                        "to the served links at once (operating.pg.step_staging.apply_now)")
    r.add_argument("--view", choices=("staged", "applied"), default=None,
                   help="what the step reads of the steps before it (default: applied "
                        "with --apply-now, else staged)")
    r.add_argument("--part-before", type=int, default=None,
                   help="a mention part sees its step's staged parts below this number")
    r.add_argument("--mention-batches", type=int, default=1,
                   help="the scoped mentions run in this many batches of "
                        "--mention-workers parts, one after the other")
    r.add_argument("--concurrent", action="store_true",
                   help="with --apply-now and --only: the steps not named are neither "
                        "staged nor required: read as Postgres serves them now")
    r.add_argument("--emulate-batches", action="store_true",
                   help="(the proof) each batch sees the batches before it staged, as if "
                        "applied; nothing is applied")
    m = sub.add_parser("marks", help="judge the text index re-mark (sharded, resumable)")
    m.add_argument("--state-dir", required=True)
    m.add_argument("--work", required=True)
    m.add_argument("--shards", type=int, default=32)
    m.add_argument("--workers", type=int, default=8)
    m.add_argument("--readings", action="append", default=[],
                   help="a primed readings directory (ontology.readings_primer), served first")
    w = sub.add_parser("layer", help="write the step layer of a finished run")
    w.add_argument("--plan", required=True)
    w.add_argument("--narrowed", required=True)
    w.add_argument("--work", required=True)
    w.add_argument("--scope", default=None)
    w.add_argument("--marks", default=None, help="the marks command's work directory")
    w.add_argument("--out", required=True)
    w.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    from ontology.schema import load_schema
    if args.command == "build":
        if args.commit and "plx_test_a_" not in args.dsn:
            print("refused: --commit only on a scratch database (plx_test_a_*)",
                  file=sys.stderr)
            return 2
        the_plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        report = build_from_state(the_plan, load_schema(), Path(args.state_dir),
                                  Path(args.out), dsn=args.dsn,
                                  unit_layer=Path(args.unit_layer) if args.unit_layer else None,
                                  commit=args.commit)
        print(json.dumps(report, sort_keys=True, default=str))
        return 0
    if args.command == "marks":
        from ontology.schema_migrate import served_families
        from operating.pg import config
        served = served_families(config.dsn())
        marked, counts = text_marks(args.state_dir, Path(args.work), args.shards, args.workers,
                                    args.readings, None if served is None else sorted(served))
        print(json.dumps({**counts, "marked": len(marked)}, sort_keys=True))
        return 0
    from operating.pg import config
    dsn = getattr(args, "dsn", None) or config.dsn()
    the_plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if args.command == "narrow":
        from ontology import schema_migrate
        from ontology import step_migrate as sm
        types, pairs = touched_by_apply([Path(p) for p in args.report],
                                        [Path(p) for p in args.layer],
                                        Path(args.marks) if args.marks else None)
        out = narrow(the_plan, schema_migrate.schema_at(args.served_ref, sm.ROOT),
                     load_schema(), types, pairs, sm.present_steps())
        out["touched"] = {"types": types, "links": [list(p) for p in pairs]}
        Path(args.out).write_text(json.dumps(out, sort_keys=True, indent=1))
        print(json.dumps({"run": out["run"], "skipped": sorted(
            set(the_plan["steps"]["run"]) - set(out["run"]))}, sort_keys=True))
        return 0
    narrowed = json.loads(Path(args.narrowed).read_text(encoding="utf-8"))
    run = dict(narrowed["run"])
    scope = json.loads(Path(args.scope).read_text()) if getattr(args, "scope", None) else None
    if scope is not None and scope.get("refused"):
        scope = None   # the mention scope could not be bounded: every document
    if args.command == "steps" and args.mention_workers > 1 and scope is not None \
            and args.only == "mentions" and args.mention_cache:
        report = mentions_in_parts(args, dsn, scope, args.mention_workers)
        print(json.dumps({"mentions": {k: v for k, v in report.items() if k != "report"}},
                         sort_keys=True))
        return 0
    if args.command == "steps":
        from operating import build_steps
        with contextlib.ExitStack() as stack:
            sources = _open_sources(Path(args.state_dir), stack, run, dsn)
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(
                prefix=".steps-", dir=args.work)))
            steps = build_steps.post_project(Path(args.state_dir), "plx", scratch=tmp)
            if args.mention_cache:
                from ontology.mention_cache import cached_step
                steps["mentions"] = cached_step(steps["mentions"], args.mention_cache)
            report = run_steps({**the_plan, "steps": {**the_plan["steps"], **narrowed}},
                               run, load_schema(), steps, dsn, Path(args.work),
                               sources=sources,
                               mention_docs=scope["objects"] if scope else None,
                               mention_groups=set(scope["groups"]) if scope else None,
                               log=sys.stderr, only=set(args.only.split(","))
                               if args.only else None, mention_cache=args.mention_cache,
                               stage_key=args.stage_key, stage_part=args.stage_part,
                               apply_now=args.apply_now,
                               view=args.view or ("applied" if args.apply_now else "staged"),
                               part_before=args.part_before, concurrent=args.concurrent)
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "report"}
                          for k, v in report["steps"].items()}, sort_keys=True))
        return 0
    if args.command == "layer":
        from operating.pg.ontology_migrate import delta_mark
        from ontology.schema_migrate import next_sequence
        marked = None
        marked_scope: list[str] = []
        if narrowed.get("remark"):
            if not args.marks:
                raise ValueError("text_marks_required")
            from ontology import step_migrate as sm
            got = [json.loads(p.read_text()) for p in sorted(Path(args.marks).glob(
                "shard-*.json"))]
            if not got or len(got) != got[0]["shards"] or \
                    len({g["listed"] for g in got}) != 1:
                raise Refused("text_marks_shards_incomplete")
            marked = sorted({h for g in got for h in g["marked"]})
            marked_scope = sm._text_marks_scope(dsn, marked)
        report = write_run_layer({**the_plan, "steps": {**the_plan["steps"], **narrowed}},
                                 run, load_schema(), Path(args.work), Path(args.out),
                                 unit_id=None, delta_mark=delta_mark(dsn),
                                 sequence=next_sequence(dsn),
                                 mention_docs=scope["objects"] if scope else None,
                                 marked=marked, marked_scope=marked_scope)
        print(json.dumps(report, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
