"""Every captured type through the one door (ARCH addendum 9): ``record_types``, ``records``
and ``record_links`` in the guarded schemas (``<biz schema>_sql`` for the AI, ``_page`` for a
person's pages) beside the curated views, so the model and the pages can reach any record of
any source by reading the catalog and writing SQL, with no per-type code.

* ``<biz schema>.record_types`` (a small table, ``build_catalog``): one row per type the
  served store holds records of, generated from the compiled schema and the store itself:
  plain name, description, the systems and source bindings holding it, how many records
  its sources still hold and when the newest was first seen, its fields (name, kind,
  description), the fields that label and date a record, its identity link types, the
  link types out of it and into it, and ``tables``: the door's tables that hold its
  records (a curated table holding one row per record of it, a communications table, or
  ``records``; ``mark_tables``). A new source, type or field appears at the next build.
* ``records`` and ``record_links``: DOOR v2 views (``door.view_ddl``: the reader's held
  access classes as ranges, ``ai_ok`` in the AI's schema) over the narrow tables
  ``operating.pg.generic_records`` builds and refreshes in Postgres: one row per current
  record of every type the comms tables do not own (type, system, source, handle, record
  id, label, record date, first seen / changed times, ``fields`` jsonb: the first-ranked
  value per property with ``<field>_currency`` / ``<field>_unit``), and one row per live
  link between two of them. Everything the scope needs was decided at build: the row's
  ``access_class`` (a field marked beyond its record joins the record's markings: a reader
  lacking it reads no row of the record) and ``ai_ok``; removed, people-excluded and
  PLX-issued records are not in the table. Any scope setting unset: no row.
* The catalog's view never shows how many records a type holds or when the newest came
  (count through ``records``, which the door scopes); a type whose own words name
  Freightcom is not listed.

A read of ``records``/``record_links`` is costed by ``plx.sql`` before it runs (``guard``): a
plan reading more than ``MAX_RECORDS`` rows of the tables (a whole-table scan) is refused
with what to filter on instead.

  python3 -m operating.pg.generic_views catalog [--dsn D]   (build record_types)
  python3 -m operating.pg.generic_views install [--dsn D]   (the guarded views)
  python3 -m operating.pg.generic_records build             (the tables, then the views)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from operating.pg import business_views as bv
from operating.pg import sql_views

ONTOLOGY = "ontology"
VIEWS = ("record_types", "records", "record_links")
BUSINESS_SCHEMES = bv.BUSINESS_SCHEMES
IDENTITY_TIERS = bv.IDENTITY_TIERS
# link tiers shown: every live link that is one (a lead is a guess: its tier says so)
LINK_TIERS = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead")
NUMERIC_KINDS = ("money", "decimal", "integer", "quantity")
DATE_KINDS = ("date", "timestamp")
# a plan reading more records or links than this is refused (``guard``): the largest
# types (Sage GL lines, ~1.5M) stay readable by type, a whole-table scan (~10M) does not
MAX_RECORDS = 2_000_000
# Which system a source binding belongs to: declared once, read by the catalog and the
# views alike. First matching prefix wins; anything else is its binding's first part.
SYSTEMS = (("gen.bc.", "bc"), ("bc.", "bc"),
           ("portal.sage_", "sage"), ("gen.sage.", "sage"),
           ("portal.fm_", "filemaker"), ("gen.silver.", "filemaker"),
           ("portal.", "portal"), ("gen.public.", "portal"),
           ("gen.teams.", "teams"), ("teams.", "teams"),
           ("gen.sharepoint.", "sharepoint"), ("sharepoint.", "sharepoint"),
           ("graph_mail.", "mail"), ("mission_control.", "mission_control"),
           ("meetings.planner_", "planner"), ("meetings.", "meetings"))
_PLAIN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def system_of(binding: str) -> str:
    b = binding or ""
    for prefix, system in SYSTEMS:
        if b.startswith(prefix):
            return system
    return b.split(".", 1)[0] or "unknown"


def _lit(text: str) -> str:
    return sql_views._literal(text)


def _arr(values: Iterable[str]) -> str:
    return "ARRAY[" + ", ".join(_lit(v) for v in values) + "]::text[]"


def system_sql(col: str) -> str:
    """``system_of`` in SQL over the binding column ``col``."""
    whens = " ".join(f"WHEN starts_with({col}, {_lit(p)}) THEN {_lit(s)}"
                     for p, s in SYSTEMS)
    return f"(CASE {whens} ELSE split_part({col}, '.', 1) END)"


def plain_name(type_id: str) -> str:
    words = _PLAIN.sub(" ", type_id).split()
    return " ".join(w if w.isupper() else w.lower() for w in words)


# --------------------------------------------------------------------------- catalog

CATALOG_DDL = """CREATE TABLE IF NOT EXISTS {s}.record_types (
    type text PRIMARY KEY, name text NOT NULL, description text, systems text[] NOT NULL,
    sources text[] NOT NULL, rows bigint NOT NULL, newest text, declared boolean NOT NULL,
    label_fields text[] NOT NULL, date_fields text[] NOT NULL, identity_links text[] NOT NULL,
    fields jsonb NOT NULL, links_out jsonb NOT NULL, links_in jsonb NOT NULL,
    built_at text NOT NULL);
ALTER TABLE {s}.record_types ADD COLUMN IF NOT EXISTS tables text[] NOT NULL DEFAULT '{{}}'"""


def catalog_rows(schema: Any, counts: Mapping[str, tuple[int, str | None, Sequence[str]]],
                 built_at: str) -> list[dict[str, Any]]:
    """One catalog row per type with records (``counts``: type -> (records held, newest
    first-seen time, source bindings)), described by the compiled ``schema``."""
    types: Mapping[str, Mapping[str, Any]] = schema.object_types
    links: Mapping[str, Mapping[str, Any]] = schema.link_types
    identity = schema.identity_link_types() if hasattr(schema, "identity_link_types") else \
        frozenset()
    out = []
    for tid in sorted(counts):
        held, newest, bindings = counts[tid]
        spec = types.get(tid) or {}
        props = list(spec.get("properties") or ())
        fields = [{"name": p["name"], "kind": p.get("value_type") or "text",
                   **({"description": p["description"]} if p.get("description") else {})}
                  for p in props if isinstance(p, Mapping) and p.get("name")]
        out.append({
            "type": tid, "name": plain_name(tid),
            "description": (spec.get("description") or None),
            "systems": sorted({system_of(b) for b in bindings}),
            "sources": sorted(set(bindings)), "rows": int(held), "newest": newest,
            "declared": bool(spec), "label_fields": list(spec.get("display") or ()),
            "date_fields": [f["name"] for f in fields if f["kind"] in DATE_KINDS],
            "identity_links": sorted(lid for lid in identity
                                     if links[lid].get("from") == tid),
            "fields": fields,
            "links_out": [{"link_type": lid, "to": lk.get("to"),
                           **({"description": lk["description"]} if lk.get("description")
                              else {})}
                          for lid, lk in sorted(links.items()) if lk.get("from") == tid],
            "links_in": [{"link_type": lid, "from": lk.get("from")}
                         for lid, lk in sorted(links.items()) if lk.get("to") == tid],
            "built_at": built_at})
    return out


def counts_sql(ont: str = ONTOLOGY) -> str:
    """Per type: records their source still holds, the newest first-seen time, bindings
    (one pass over the records, bounded memory in Postgres)."""
    return (f"SELECT o.object_type, count(*) FILTER (WHERE o.missing_from_source_since IS NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {ont}.object_removals rm WHERE rm.object_id = "
            f"o.object_id) AND NOT EXISTS (SELECT 1 FROM {ont}.people_excluded_objects p "
            "WHERE p.object_id = o.object_id)), max(o.first_recorded_at), array_agg(DISTINCT o.binding_id) "
            f"FROM {ont}.objects o GROUP BY 1")


def served_tables(pg: Any, biz: str | None = None) -> dict[str, list[str]]:
    """type -> the door's tables in ``biz`` that hold its records: a business table built
    there holding one row per record of its first read type (``View.reads[0]``: a curated
    table or a communications table), and ``records`` for each type it holds now."""
    s = biz or bv.SCHEMA
    out: dict[str, list[str]] = {}
    for name, v in sorted(bv.VIEWS.items()):
        if v.reads and pg.execute("SELECT to_regclass(%s)", (f"{s}.{name}",)
                                  ).fetchone()[0] is not None:
            out.setdefault(v.reads[0], []).append(name)
    if pg.execute("SELECT to_regclass(%s)", (f"{s}.records",)).fetchone()[0] is not None:
        # one index probe per type (a loose scan of the (type, ...) index), never a scan
        for (t,) in pg.execute(
                f"WITH RECURSIVE t(type) AS (SELECT min(type) FROM {s}.records UNION ALL "
                f"SELECT (SELECT min(r.type) FROM {s}.records r WHERE r.type > t.type) FROM t "
                "WHERE t.type IS NOT NULL) SELECT type FROM t WHERE type IS NOT NULL"
                ).fetchall():
            out.setdefault(t, []).append("records")
    return out


def mark_tables(pg: Any, biz: str | None = None) -> None:
    """``record_types.tables`` as ``served_tables`` finds them now (the caller's
    transaction): after the catalog or the generic tables were built."""
    s = biz or bv.SCHEMA
    got = served_tables(pg, s)
    pg.execute(f"UPDATE {s}.record_types SET tables = ARRAY(SELECT "
               "jsonb_array_elements_text(%s::jsonb -> type))", (json.dumps(got),))


def build_catalog(dsn: str, schema: Any, biz: str | None = None,
                  ont: str = ONTOLOGY) -> int:
    """(Re)write ``<biz>.record_types`` in one transaction (readers see the old catalog or
    the new one) and its state row; the guarded views stay bound to the table."""
    import psycopg
    from psycopg.types.json import Jsonb
    s = biz or bv.SCHEMA
    built_at = dt.datetime.now(dt.timezone.utc).isoformat()
    with psycopg.connect(dsn) as read:
        read.execute("SET TRANSACTION READ ONLY")
        counts = {t: (n, newest, [b for b in (bindings or []) if b])
                  for t, n, newest, bindings in read.execute(counts_sql(ont)).fetchall()}
    rows = catalog_rows(schema, counts, built_at)
    cols = list(rows[0]) if rows else []
    with psycopg.connect(dsn, autocommit=True) as write:
        with write.transaction():
            write.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
            write.execute(CATALOG_DDL.format(s=s))
            write.execute(f"DELETE FROM {s}.record_types")
            for row in rows:
                write.execute(
                    f"INSERT INTO {s}.record_types ({', '.join(cols)}) VALUES "
                    f"({', '.join(['%s'] * len(cols))})",
                    [Jsonb(row[c]) if c in ("fields", "links_out", "links_in") else row[c]
                     for c in cols])
            write.execute(f"CREATE TABLE IF NOT EXISTS {s}.state (view text PRIMARY KEY, "
                          "built_at text NOT NULL, digest text, rows bigint NOT NULL, "
                          "seconds double precision, notes jsonb NOT NULL DEFAULT '{}')")
            write.execute(f"INSERT INTO {s}.state (view, built_at, rows) VALUES "
                          "('record_types', %s, %s) ON CONFLICT (view) DO UPDATE SET "
                          "built_at = EXCLUDED.built_at, rows = EXCLUDED.rows",
                          (built_at, len(rows)))
            mark_tables(write, s)
    return len(rows)


# --------------------------------------------------------------------------- views

def listed_sql(t: str = "t") -> str:
    """Whether a catalog row (``record_types`` as ``t``) may be named to anyone: no word of
    its type, description or sources names Freightcom. The guarded view and the prompt's
    catalog lines (``ontology_tools_sql.catalog_lines``) both filter by this."""
    from integrations.image_ocr_gpu import FREIGHTCOM
    return (f"NOT (concat_ws(' ', {t}.type, {t}.description, {t}.sources::text) ~* "
            f"{_lit(FREIGHTCOM.pattern)})")


# The people exclusion decides versions only up to its ``covered_ver`` (``people_excluded``):
# a row of a system other than a business system (``biz`` false: records read out of mail,
# Planner, SharePoint lists, links learned from them) is served only once its newest
# version (``ver``) is at or below it, as the communications tables' rule
# (``business_comms.view_ddl``). No set, or one never decided: NULL, no such row (fail
# closed). Business systems' records are not under the set (business rule 2026-11-17).
UNDECIDED = ("t.biz OR t.ver <= (SELECT min(pes.covered_ver) FROM "
             "ontology.people_excluded_state pes WHERE pes.exclusion IS NOT NULL "
             "AND pes.exclusion <> '')")
BOUND_COLS = ("ver", "biz")


def record_types_ddl(biz: str | None = None, dst: str | None = None) -> str:
    """The catalog as any reader reads it: its types, fields and links, never how many
    records a type holds or when the newest came (counted through ``records``, which the
    door scopes); a type whose own words name Freightcom is not listed (``listed_sql``)."""
    src, dst = biz or bv.SCHEMA, dst or sql_views.guarded_schema(biz)
    return (f"CREATE VIEW {dst}.record_types WITH (security_barrier) AS SELECT t.type, "
            "t.name, t.description, t.systems, t.sources, t.declared, t.label_fields, "
            "t.date_fields, t.identity_links, t.fields, t.links_out, t.links_in, t.tables, "
            "t.built_at "
            f"FROM {src}.record_types t WHERE {listed_sql('t')}")


class _Generic:
    """What ``door.view_ddl`` reads of a view (its name and columns)."""

    def __init__(self, name: str, cols: Sequence[str]) -> None:
        self.name = name
        self.cols = [bv.Col(c, "text", "") for c in cols]


def generic_views() -> list[_Generic]:
    from operating.pg import generic_records as gr
    return [_Generic("records", gr.RECORD_COLS), _Generic("record_links", gr.LINK_COLS)]


def make_views(write: Any, biz: str | None = None) -> list[str]:
    """(Re)make the generic guarded views: ``record_types`` (the catalog) and, once
    ``generic_records`` built their tables, ``records`` and ``record_links`` as DOOR v2
    views (``door.view_ddl``: the reader's held classes, ``ai_ok`` for the AI's schema),
    in the AI's schema and the person pages' schema, granted to each schema's role."""
    from operating.pg import access_classes, door
    s = biz or bv.SCHEMA
    access_classes.ensure(write, s)
    write.execute(CATALOG_DDL.format(s=s))
    made = []
    targets = [(sql_views.guarded_schema(biz), sql_views.ROLE, True),
               (sql_views.page_schema(biz), sql_views.PAGE_ROLE, False)]
    for dst, role, ai in targets:
        if write.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is None:
            continue
        write.execute(f"DROP VIEW IF EXISTS {dst}.record_types")
        write.execute(record_types_ddl(biz, dst))
        names = ["record_types"]
        for v in generic_views():
            write.execute(f"DROP VIEW IF EXISTS {dst}.{v.name}")
            if write.execute("SELECT to_regclass(%s)", (f"{s}.{v.name}",)).fetchone()[0] \
                    is None or not door.is_v2(write, s, v.name) or not _bounded(write, s,
                                                                             v.name):
                continue   # not built (or an old shape): no view, fail closed
            write.execute(door.view_ddl(write, v, s, dst, ai_only=ai, also=[UNDECIDED]))
            names.append(v.name)
        for name in names:
            write.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                          f"'{role}') THEN EXECUTE 'GRANT SELECT ON {dst}.{name} "
                          f"TO {role}'; END IF; END $$")
            made.append(f"{dst}.{name}")
    for v in generic_views():
        if write.execute("SELECT to_regclass(%s)", (f"{s}.{v.name}",)).fetchone()[0]:
            door.follow(write, s, v.name)
    door._bump(write, s)
    return made


def _bounded(pg: Any, s: str, name: str) -> bool:
    """Whether the generic table has the columns ``UNDECIDED`` reads (a table built before
    them gets no view until built again)."""
    got = {r[0] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND "
        "table_name = %s", (s, name)).fetchall()}
    return set(BOUND_COLS) <= got


def install(dsn: str, biz: str | None = None) -> list[str]:
    """The guarded schema and role (migration 0042's DDL) and the three generic views; the
    tables they read are made empty when not built yet."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as write:
        with write.transaction():
            sql_views.ensure(write, biz)
            return make_views(write, biz)


# --------------------------------------------------------------------------- guard

TABLES = ("records", "record_links")


def plan_reads(plan: Any, sizes: Mapping[str, float] | None = None) -> int:
    """The most rows one scan of the plan reads from ``records`` or ``record_links``: a
    sequential scan reads the whole table (``sizes``: table -> rows; its estimate when
    unknown), any other scan its estimated rows."""
    most = 0
    stack = [plan[0]["Plan"] if isinstance(plan, list) else plan]
    while stack:
        node = stack.pop()
        rel = str(node.get("Relation Name") or "")
        if rel in TABLES:
            rows = float(node.get("Plan Rows") or 0)
            if "Seq Scan" in str(node.get("Node Type") or ""):
                rows = max(rows, float((sizes or {}).get(rel) or 0))
            most = max(most, int(rows))
        stack.extend(node.get("Plans") or ())
    return most


def table_sizes(pg: Any, biz: str | None = None) -> dict[str, float]:
    """The planner's row counts of the generic tables (catalog, readable by any role)."""
    s = biz or bv.SCHEMA
    return {r: float(n or 0) for r, n in pg.execute(
        "SELECT c.relname, c.reltuples FROM pg_class c JOIN pg_namespace n ON n.oid = "
        "c.relnamespace WHERE n.nspname = %s AND c.relname = ANY(%s)", (s, list(TABLES)))}


def guard(plan: Any, sql: str, sizes: Mapping[str, float] | None = None) -> None:
    """Refuse a statement over ``records``/``record_links`` that would read past
    ``MAX_RECORDS``."""
    from operating.ontology_tools_identity import ToolError
    if not re.search(r"\b(records|record_links)\b", sql, re.I):
        return
    n = plan_reads(plan, sizes)
    if n > MAX_RECORDS:
        raise ToolError(
            "too_broad", f"this would read about {n:,} records or links (more than "
            f"{MAX_RECORDS:,} per statement)",
            "name one type (WHERE type = '...'; record_types lists the types and their "
            "fields) and narrow it by record_date, handle, record_id, label or source; a "
            "link by from_id/to_id or link_type; for mail, Teams, meetings and documents "
            "use their own tables or plx.search")


# --------------------------------------------------------------------------- CLI

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.generic_views")
    ap.add_argument("command", choices=("catalog", "install"))
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    if args.command == "install":
        for name in install(dsn):
            print(name)
        return 0
    from ontology.schema import load_schema
    from operating.ontology_tools_identity import SchemaView
    print(build_catalog(dsn, SchemaView.from_compiled(load_schema())), "types")
    return 0


if __name__ == "__main__":
    sys.exit(main())
