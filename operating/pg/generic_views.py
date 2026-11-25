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
from typing import Any, Callable, Iterable, Mapping, Sequence

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
ALTER TABLE {s}.record_types ADD COLUMN IF NOT EXISTS tables text[] NOT NULL DEFAULT '{{}}';
ALTER TABLE {s}.record_types ADD COLUMN IF NOT EXISTS kinds jsonb NOT NULL DEFAULT '{{}}';
ALTER TABLE {s}.record_types ADD COLUMN IF NOT EXISTS dates jsonb NOT NULL DEFAULT '[]'"""

# The declared day of a record the build wrote before the declarations existed
# (``date_declare apply``): a side table, never an UPDATE of the 17 GB ``records`` heap (every
# such update is non-HOT over 8 indexes: 25-40 GB of WAL). The guarded ``records`` view reads
# ``COALESCE(record_dates.day, records.record_date)``; a row only counts for the version it
# was read from (``ver``), so a record changed since reads its own, newly built date or none.
DATES_TABLE = "record_dates"
DATES_DDL = """CREATE TABLE IF NOT EXISTS {s}.record_dates (
    type text NOT NULL, rid bigint NOT NULL, ver bigint NOT NULL, day text NOT NULL,
    field text NOT NULL, PRIMARY KEY (type, rid))"""
# the label search reads it as the API / eval roles (migration 0077 grants the same)
DATES_GRANT = """DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        EXECUTE 'GRANT SELECT ON {s}.record_dates TO plx_api'; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        EXECUTE 'GRANT SELECT ON {s}.record_dates TO plx_eval'; END IF;
END $$"""
DATES_JOIN = ("LEFT JOIN {s}.record_dates d ON d.type = t.type AND d.rid = t.rid AND "
              "d.ver = t.ver")


# Kinds: the values a type's category-like text fields take (``kinds_of``).
KIND_MAX_VALUES = 30       # a field with more distinct values is not category-like
KIND_MIN_RECORDS = 5       # a value named needs this many records ...
KIND_MIN_CLASSES = 3       # ... in this many different access classes (never one audience's)
KIND_MAX_LEN = 60
KIND_SAMPLE = 100_000
_KIND_FIELD_TYPES = ("text", "enum")


# a field that names who did or owns something holds people, whatever its values look like
_PERSON_FIELD = re.compile(
    r"(^|_)(by|rep|name|names)(_|$)|owner|assign|user|author|creator|created|modified|updated|"
    r"account|person|people|manager|buyer|purchaser|contact|operator|worker|employee|staff|"
    r"member|sender|recipient|approver|reviewer|requester|address|street|city|phone|email|"
    r"driver|technician|attendee|organi[sz]er|participant|agent|postal|zip", re.I)


def kind_fields(spec: Mapping[str, Any]) -> list[str]:
    """The fields of a type whose values may be listed: text or enum, never marked."""
    return [p["name"] for p in (spec.get("properties") or ())
            if isinstance(p, Mapping) and p.get("name") and not p.get("markings")
            and (p.get("value_type") or "text") in _KIND_FIELD_TYPES
            and not _PERSON_FIELD.search(p["name"])]


def _name_words(pg: Any, s: str) -> set[str]:
    out: set[str] = set()
    try:
        rows = pg.execute(f"SELECT name, email FROM {s}.staff").fetchall()
    except Exception:   # no staff table: nothing to compare with
        pg.rollback() if hasattr(pg, "rollback") else None
        return out
    for name, email in rows:
        for v in (name, email):
            if v and str(v).strip():
                out.add(str(v).strip().lower())
        # a first name, a surname and a mail user name stand for the person as much as the
        # whole name (a field like created_by_account holds "Jesse" or "oleg")
        out.update(w for w in re.split(r"[^a-z0-9]+", f"{name or ''} "
                                       f"{str(email or '').split('@')[0]}".lower())
                   if len(w) >= 3)
    return out


def nameable(value: Any, people: set[str]) -> bool:
    """Whether a field value may sit in the catalog beside the row: short plain text that is
    no address and no person's name (a staff name, whole)."""
    v = str(value or "").strip()
    return bool(v) and len(v) <= KIND_MAX_LEN and "@" not in v and "\n" not in v \
        and v.lower() not in people \
        and not re.fullmatch(r"[\d\W_]+", v) \
        and not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v, re.I)


def kinds_of(pg: Any, s: str, types: Mapping[str, Mapping[str, Any]],
             held: Iterable[str]) -> dict[str, dict[str, list[str]]]:
    """type -> {field: values, most records first} for each text field of the type whose
    distinct values over its records are at most ``KIND_MAX_VALUES`` (family, kind, status,
    category like): so the model knows what a type's records can say before it searches.
    Read only from ``records`` of the record rows anyone holding the record may read
    (unmarked, no marked column, the AI's), a value kept only when ``KIND_MIN_RECORDS``
    business-system records carry it (or it is in ``KIND_MIN_CLASSES`` different mailbox or private
    classes) and ``nameable``; no counts are
    kept (the catalog never shows how many records a type holds)."""
    if pg.execute("SELECT to_regclass(%s)", (f"{s}.records",)).fetchone()[0] is None:
        return {}
    # unmarked classes; the "broad" ones are the business systems' (every audience key an
    # ``api:`` source, held by staff as a whole), the rest one mailbox's or the full reader's
    open_ids, broad_ids = [], []
    for cid, keys in pg.execute(
            f"SELECT id, audience_keys FROM {s}.access_classes WHERE NOT blocked AND rec_marks "
            "= ARRAY['[]']::text[]").fetchall():
        open_ids.append(cid)
        if keys and all(str(k).startswith("api:") for k in keys):
            broad_ids.append(cid)
    if not open_ids:
        return {}
    people = _name_words(pg, s)
    where = "r.type = %s AND r.col_class = 0 AND r.ai_ok AND r.access_class = ANY(%s)"
    out: dict[str, dict[str, list[str]]] = {}
    for t in sorted(held):
        want = kind_fields(types.get(t) or {})
        if not want:
            continue
        cands = [r[0] for r in pg.execute(
            "SELECT k FROM (SELECT e.key k, btrim(e.value) v FROM (SELECT r.fields FROM "
            f"{s}.records r WHERE {where} LIMIT {KIND_SAMPLE}) r, "
            "jsonb_each_text(r.fields) e WHERE e.key = ANY(%s) AND btrim(e.value) <> '') q "
            f"GROUP BY k HAVING count(DISTINCT v) <= {KIND_MAX_VALUES}",
            (t, open_ids, want)).fetchall()]
        if not cands:
            continue
        got: dict[str, list[tuple[int, str]]] = {}
        for k, v, total, n, classes in pg.execute(
                "SELECT e.key, btrim(e.value), count(*), count(*) FILTER (WHERE r.access_class = ANY(%s)), "
                "count(DISTINCT r.access_class) FILTER (WHERE NOT r.access_class = ANY(%s)) "
                "FROM "
                f"{s}.records r, jsonb_each_text(r.fields) e WHERE {where} "
                "AND e.key = ANY(%s) AND btrim(e.value) <> '' GROUP BY 1, 2",
                (broad_ids, broad_ids, t, open_ids, cands)).fetchall():
            got.setdefault(k, []).append((total, v, classes, n))
        fields = {}
        for k, vals in got.items():
            if len(vals) > KIND_MAX_VALUES:      # the whole type says more than the sample
                continue
            keep = [v for total, v, c, n in sorted(vals, key=lambda x: (-x[0], x[1]))
                    if (n >= KIND_MIN_RECORDS or c >= KIND_MIN_CLASSES) and nameable(v, people)]
            if keep:
                fields[k] = keep
        if fields:
            out[t] = dict(sorted(fields.items()))
    return out


def catalog_rows(schema: Any, counts: Mapping[str, tuple[int, str | None, Sequence[str]]],
                 built_at: str,
                 kinds: Mapping[str, Mapping[str, Sequence[str]]] | None = None
                 ) -> list[dict[str, Any]]:
    """One catalog row per type with records (``counts``: type -> (records held, newest
    first-seen time, source bindings)), described by the compiled ``schema``."""
    types: Mapping[str, Mapping[str, Any]] = schema.object_types
    links: Mapping[str, Mapping[str, Any]] = schema.link_types
    identity = schema.identity_link_types() if hasattr(schema, "identity_link_types") else \
        frozenset()
    from operating.pg import date_declare
    declared = date_declare.load()["types"]
    out = []
    for tid in sorted(counts):
        held, newest, bindings = counts[tid]
        spec = types.get(tid) or {}
        props = list(spec.get("properties") or ())
        fields = [{"name": p["name"], "kind": p.get("value_type") or "text",
                   **({"description": p["description"]} if p.get("description") else {})}
                  for p in props if isinstance(p, Mapping) and p.get("name")]
        # the declared dates (operating/pg/date_declarations.json, from a profile of the
        # type's own values) lead the schema's: the first is the date of record
        dec = declared.get(tid) or {}
        dates = date_declare.means(dec)
        date_fields = date_declare.fields_of(dec) + [
            f["name"] for f in fields if f["kind"] in DATE_KINDS
            and f["name"] not in date_declare.fields_of(dec)]
        out.append({
            "type": tid, "name": plain_name(tid),
            "description": (spec.get("description") or None),
            "systems": sorted({system_of(b) for b in bindings}),
            "sources": sorted(set(bindings)), "rows": int(held), "newest": newest,
            "declared": bool(spec), "label_fields": list(spec.get("display") or ()),
            "date_fields": date_fields, "dates": dates,
            "identity_links": sorted(lid for lid in identity
                                     if links[lid].get("from") == tid),
            "fields": fields,
            "links_out": [{"link_type": lid, "to": lk.get("to"),
                           **({"description": lk["description"]} if lk.get("description")
                              else {})}
                          for lid, lk in sorted(links.items()) if lk.get("from") == tid],
            "links_in": [{"link_type": lid, "from": lk.get("from")}
                         for lid, lk in sorted(links.items()) if lk.get("to") == tid],
            "kinds": {k: list(v) for k, v in (kinds or {}).get(tid, {}).items()},
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
    for name, v in sorted(bv.listed().items()):
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
        kinds = kinds_of(read, s, getattr(schema, "object_types", {}), counts)
    rows = catalog_rows(schema, counts, built_at, kinds)
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
                    [Jsonb(row[c]) if c in ("fields", "links_out", "links_in", "kinds", "dates") else row[c]
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
    """Whether a catalog row (``record_types`` as ``t``) may be named to anyone: every row
    (Freightcom is read like any other data, 11-23; kept as the one filter point)."""
    return "true"


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
    door scopes)(``listed_sql``)."""
    src, dst = biz or bv.SCHEMA, dst or sql_views.guarded_schema(biz)
    return (f"CREATE VIEW {dst}.record_types WITH (security_barrier) AS SELECT t.type, "
            "t.name, t.description, t.systems, t.sources, t.declared, t.label_fields, "
            "t.date_fields, t.identity_links, t.fields, t.links_out, t.links_in, t.tables, "
            "t.kinds, t.dates, t.built_at "
            f"FROM {src}.record_types t WHERE {listed_sql('t')}")


class _Generic:
    """What ``door.view_ddl`` reads of a view (its name and columns)."""

    def __init__(self, name: str, cols: Sequence[str],
                 fallback: Mapping[str, str] | None = None, pages_only: bool = False,
                 also: Sequence[str] = (), extra: Sequence[tuple[str, str]] = (),
                 hidden_group: Sequence[str] = (), join: str = "",
                 exprs: Mapping[str, str] | None = None) -> None:
        self.name = name
        self.hidden_group = tuple(hidden_group)   # a count-only ``<name>_hidden`` view, grouped by these
        self.pages_only = pages_only   # the person pages' schema only, never the AI's
        self.also = list(also)         # this table's own conditions over ``t``, beside UNDECIDED
        self.extra = list(extra)       # (name, SQL over ``t``) columns computed at read time
        self.join = join               # a LEFT JOIN (alias d) the view reads besides the table
        self.exprs = dict(exprs or {})  # column -> SQL over t and d shown in place of t."col"
        self.cols = [bv.Col(c, "text", "") for c in cols]
        self.fallback = dict(fallback or {})


def generic_views() -> list[_Generic]:
    from operating.pg import generic_records as gr
    from operating.pg import page_items as pi
    from operating.pg import plan_sections as ps
    return [_Generic("records", gr.RECORD_COLS, gr.FALLBACK, join=DATES_JOIN,
                     exprs={"record_date": 'COALESCE(d."day", t."record_date")'}),
            _Generic("record_links", gr.LINK_COLS),
            _Generic(pi.TABLE, pi.VIEW_COLS, pages_only=True,
                     hidden_group=("page_kind", "page_key", "section")),
            _Generic(ps.TABLE, ps.COLS, pages_only=True, also=[ps.needs_ddl(), ps.marked_ddl()],
                     extra=[("current", ps.deps_ddl())])]


def make_views(write: Any, biz: str | None = None, *,
               step: Callable[[Callable[[Any], Any], str], Any] | None = None,
               follow: bool = True) -> list[str]:
    """(Re)make the generic guarded views: ``record_types`` (the catalog) and, once
    ``generic_records`` built their tables, ``records`` and ``record_links`` as DOOR v2
    views (``door.view_ddl``: the reader's held classes, ``ai_ok`` for the AI's schema),
    in the AI's schema and the person pages' schema, granted to each schema's role.
    ``step(f, what)`` runs each part (``f(write)``): ``sql_views.install`` gives each its
    own short transaction (``sql_views.short``); none: all in the caller's transaction. A
    view whose DDL and tables are unchanged is not touched (``sql_views.put``).
    ``follow=False``: the exclusion follow-up is left to the caller (a swap that already decided
    its new table's rows against the set before taking its locks: the follow-up scans every
    table's lineage, minutes under load, and must not run while a swap holds a table's lock,
    where every reader of the table waits)."""
    from operating.pg import access_classes, door
    run = step or (lambda f, what: f(write))
    s = biz or bv.SCHEMA

    def catalog(w: Any) -> None:
        access_classes.ensure(w, s)
        # DDL only when missing: ALTER TABLE takes an ACCESS EXCLUSIVE lock even when the
        # column exists, and every reader of the catalog would queue behind it
        have = {r[0] for r in w.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = 'record_types'", (s,))}
        if "tables" not in have or "dates" not in have:
            w.execute(CATALOG_DDL.format(s=s))
        # the records view reads it: it exists before any view that joins it is made
        if w.execute("SELECT to_regclass(%s)", (f"{s}.{DATES_TABLE}",)).fetchone()[0] is None:
            w.execute(DATES_DDL.format(s=s))
            w.execute(DATES_GRANT.format(s=s))   # only when made: GRANT locks the table
    run(catalog, f"{s}.record_types")
    # the exclusion follow-up first (rows only, no view lock): a view made below reads
    # rows already decided against the set
    for v in generic_views() if follow else ():
        if write.execute("SELECT to_regclass(%s)", (f"{s}.{v.name}",)).fetchone()[0]:
            run(lambda w, v=v: door.follow(w, s, v.name), f"{s}.{v.name} follow")
    made = []
    targets = [(sql_views.guarded_schema(biz), sql_views.ROLE, True),
               (sql_views.page_schema(biz), sql_views.PAGE_ROLE, False)]
    for dst, role, ai in targets:
        if write.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is None:
            continue

        def types(w: Any, dst: str = dst, role: str = role) -> bool:
            sql_views.put(w, f"{dst}.record_types", record_types_ddl(biz, dst))
            _grant(w, role, f"{dst}.record_types")
            return True
        run(types, f"{dst}.record_types")
        made.append(f"{dst}.record_types")
        for v in generic_views():
            def one(w: Any, v: _Generic = v, dst: str = dst, role: str = role,
                    ai: bool = ai) -> bool:
                if v.pages_only and ai:
                    w.execute(f"DROP VIEW IF EXISTS {dst}.{v.name}_hidden, {dst}.{v.name}")
                    return False   # the precomputed pages are a person's: the AI reads none
                if w.execute("SELECT to_regclass(%s)", (f"{s}.{v.name}",)).fetchone()[0] \
                        is None or not door.is_v2(w, s, v.name) or not _bounded(w, s, v.name):
                    w.execute(f"DROP VIEW IF EXISTS {dst}.{v.name}_hidden, {dst}.{v.name}")
                    return False   # not built (or an old shape): no view, fail closed
                sql_views.put(w, f"{dst}.{v.name}",
                              door.view_ddl(w, v, s, dst, ai_only=ai, also=[UNDECIDED, *(a.replace("{s}", s) for a in v.also)],
                              join=v.join.replace("{s}", s),
                              extra=[(n, e.replace("{s}", s)) for n, e in v.extra]))
                _grant(w, role, f"{dst}.{v.name}")
                if v.hidden_group:   # the count-only view beside it (a person's pages only)
                    sql_views.put(w, f"{dst}.{v.name}_hidden", door.hidden_count_ddl(
                        v.name, s, dst, group=v.hidden_group, ai_only=ai,
                        also=[UNDECIDED, *(a.replace("{s}", s) for a in v.also)]))
                    _grant(w, role, f"{dst}.{v.name}_hidden")
                return True
            if run(one, f"{dst}.{v.name}"):
                made.append(f"{dst}.{v.name}")

        def measure_values_view(w: Any, dst: str = dst, role: str = role,
                                ai: bool = ai) -> bool:
            if ai:
                w.execute(f"DROP VIEW IF EXISTS {dst}.measure_values")
                return False
            if w.execute("SELECT to_regclass(%s)", (f"{s}.measure_values",)).fetchone()[0] \
                    is None or not door.is_class_guarded(w, s, "measure_values"):
                w.execute(f"DROP VIEW IF EXISTS {dst}.measure_values")
                return False
            sql_views.put(w, f"{dst}.measure_values", door.measure_values_ddl(w, s, dst))
            _grant(w, role, f"{dst}.measure_values")
            return True
        if run(measure_values_view, f"{dst}.measure_values"):
            made.append(f"{dst}.measure_values")
    run(lambda w: door._bump(w, s), f"{s} door stamp")
    return made


def _grant(write: Any, role: str, name: str) -> None:
    write.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                  f"'{role}') THEN EXECUTE 'GRANT SELECT ON {name} TO {role}'; END IF; END $$")


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
