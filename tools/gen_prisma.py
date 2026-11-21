"""Prisma schemas of the Workbench's real tables, by introspection (stdlib + psycopg).

Two outputs, both generated, both checked in, neither edited by hand:

* ``prisma/schema.prisma``: the Postgres database (schemas ``ontology``, ``search``,
  ``plx_meta``), read from the catalog of a database that holds the structure: a throwaway
  cluster this tool starts and removes itself (``--scratch-cluster``), restored from a
  structure-only dump (``--from-dump``) or built by the repo's migrations
  (``operating/pg/migrations``, the default), or a scratch database named by an
  environment variable (``--dsn-env``; the value is never printed).
* ``prisma/app/<store>.prisma``: each SQLite store of ``operating/declared_schema`` (one
  file per store: each store is its own SQLite file, and a Prisma schema has one
  datasource), read from an in-memory database the store's declared steps built.

The output follows ``prisma db pull`` (Prisma 6) for what Prisma can express: the same
types and ``@db`` attributes, ``@id``/``@@id``, ``@unique``/``@@unique``, ``@@index`` with
``map``/``type``/``ops``, ``@default`` and ``Unsupported`` types. Where Prisma cannot
express a database fact (check constraints, partial and expression indexes, operator
classes and collations of btree indexes, ``NULLS NOT DISTINCT``, triggers, a vector's
dimension) it is written as a ``///`` comment beside the model or field, never dropped.

The database declares no foreign keys (bulk loads and in-place deltas write the graph
tables set-based). ``LOGICAL_RELATIONS`` names the references the code relies on; they
are written as Prisma relations under ``relationMode = "prisma"`` (no foreign key is
implied) and each is commented as not enforced by the database.

An unknown column type, index method or default expression fails generation (exit 2)
instead of guessing a Prisma type.

Usage::

    python3 tools/gen_prisma.py app [--check]
    python3 tools/gen_prisma.py pg --scratch-cluster [--from-dump PATH] [--check]
    python3 tools/gen_prisma.py pg --dsn-env NAME [--check]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import pkgutil
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRISMA_DIR = ROOT / "prisma"
PG_OUT = PRISMA_DIR / "schema.prisma"
APP_DIR = PRISMA_DIR / "app"
MIGRATIONS_DIR = ROOT / "operating" / "pg" / "migrations"
SCHEMAS = ("ontology", "plx_meta", "search")
EXTENSIONS = ("vector", "pg_trgm", "unaccent")


class GenerationError(RuntimeError):
    """A database fact this generator has no Prisma rendering for: generation stops."""


# --------------------------------------------------------------------------- relations

# (child schema.table, child column, parent schema.table, parent column, field on the
# child, back-relation field on the parent). The database holds no foreign keys; these are
# the references the loaders write and the readers join on (operating/pg/migrations
# 0006-0010, ontology/store_ddl.py). Not enforced: an orphan row is possible.
LOGICAL_RELATIONS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("ontology.object_versions", "object_id", "ontology.objects", "object_id",
     "object", "versions"),
    ("ontology.value_sources", "ver", "ontology.object_versions", "ver",
     "version", "value_sources"),
    ("ontology.value_cells", "src", "ontology.value_sources", "src",
     "value_source", "cells"),
    ("ontology.value_cells", "field", "ontology.value_fields", "field",
     "value_field", "cells"),
    ("ontology.value_cells", "kind", "ontology.value_kinds", "kind",
     "value_kind", "cells"),
    ("ontology.packed_sources", "src", "ontology.value_sources", "src",
     "value_source", "packed"),
    ("ontology.link_rows", "from_object", "ontology.objects", "object_id",
     "from", "links_out"),
    ("ontology.link_rows", "to_object", "ontology.objects", "object_id",
     "to", "links_in"),
    ("ontology.link_rows", "reason_id", "ontology.link_skeletons", "id",
     "reason_skeleton", "reason_of"),
    ("ontology.link_rows", "evidence_id", "ontology.link_skeletons", "id",
     "evidence_skeleton", "evidence_of"),
    ("ontology.identifiers", "object_id", "ontology.objects", "object_id",
     "object", "identifiers"),
    ("ontology.event_objects", "event_id", "ontology.events", "event_id",
     "event", "objects"),
    ("ontology.event_objects", "object_id", "ontology.objects", "object_id",
     "object", "event_objects"),
    ("ontology.text_fields", "object_id", "ontology.objects", "object_id",
     "object", "text_fields"),
    ("ontology.text_chunks", "field_id", "ontology.text_fields", "field_id",
     "text_field", "chunks"),
    ("ontology.chunk_words", "chunk_id", "ontology.text_chunks", "chunk_id",
     "chunk", "words"),
    ("ontology.object_removals", "object_id", "ontology.objects", "object_id",
     "object", "removals"),
)

# Indexes production holds that no migration creates (found 2026-11-12 by restoring the
# production structure dump beside a database built by the migrations). They are part of
# the real structure, so schema.prisma shows them; the up-to-date test adds them to the
# migrations' catalog and fails when a migration starts creating one (then drop it here).
# (object_versions_by_object_c went with migration 0018, GROWTH-AUDIT F7.)
PRODUCTION_ONLY_INDEXES: tuple[dict[str, Any], ...] = (
    {"table": "ontology.value_cells", "name": "value_cells_by_field",
     "method": "btree", "columns": [{"name": "field", "opclass_default": True,
                                     "collation": None, "desc": False},
                                    {"name": "src", "opclass_default": True,
                                     "collation": None, "desc": False}],
     "include": [], "predicate": None, "expression": False,
     "definition": "CREATE INDEX value_cells_by_field ON ontology.value_cells "
                   "USING btree (field, src)"},
)


# --------------------------------------------------------------------------- Postgres catalog

_COLUMNS_SQL = """
SELECT n.nspname, c.relname, c.relkind, a.attname, a.attnum,
       format_type(a.atttypid, a.atttypmod), t.typname, t.typcategory,
       et.typname, a.attnotnull, pg_get_expr(d.adbin, d.adrelid), a.attgenerated,
       a.attidentity
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
  JOIN pg_type t ON t.oid = a.atttypid
  LEFT JOIN pg_type et ON et.oid = t.typelem AND t.typcategory = 'A'
  LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
 WHERE n.nspname = ANY(%(schemas)s) AND c.relkind IN ('r', 'p', 'v', 'm')
 ORDER BY n.nspname, c.relname, a.attnum
"""

_CONSTRAINTS_SQL = """
SELECT n.nspname, c.relname, k.conname, k.contype,
       ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY u(num, ord)
               JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.num
              ORDER BY u.ord),
       pg_get_constraintdef(k.oid), coalesce(i.indnullsnotdistinct, false)
  FROM pg_constraint k
  JOIN pg_class c ON c.oid = k.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_index i ON i.indexrelid = k.conindid
 WHERE n.nspname = ANY(%(schemas)s) AND k.contype IN ('p', 'u', 'c', 'f', 'x')
 ORDER BY n.nspname, c.relname, k.conname
"""

_INDEXES_SQL = """
SELECT n.nspname, c.relname, ic.relname, am.amname, i.indnkeyatts, i.indnatts,
       ARRAY(SELECT coalesce(a.attname, '') FROM unnest(i.indkey) WITH ORDINALITY u(num, ord)
               LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = u.num
              ORDER BY u.ord),
       ARRAY(SELECT o.opcname || '|' || o.opcdefault::text
               FROM unnest(i.indclass) WITH ORDINALITY u(oid, ord)
               JOIN pg_opclass o ON o.oid = u.oid ORDER BY u.ord),
       ARRAY(SELECT coalesce(nullif(co.collname, 'default'), '')
               FROM unnest(i.indcollation) WITH ORDINALITY u(oid, ord)
               LEFT JOIN pg_collation co ON co.oid = u.oid ORDER BY u.ord),
       ARRAY(SELECT (u.opt & 1)::int FROM unnest(i.indoption) WITH ORDINALITY u(opt, ord)
              ORDER BY u.ord),
       i.indpred IS NOT NULL, i.indexprs IS NOT NULL, pg_get_indexdef(i.indexrelid),
       pg_get_expr(i.indpred, i.indrelid)
  FROM pg_index i
  JOIN pg_class ic ON ic.oid = i.indexrelid
  JOIN pg_class c ON c.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_am am ON am.oid = ic.relam
 WHERE n.nspname = ANY(%(schemas)s)
   AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid
                    AND k.contype IN ('p', 'u', 'x'))
 ORDER BY n.nspname, c.relname, ic.relname
"""

_TRIGGERS_SQL = """
SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid)
  FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = ANY(%(schemas)s) AND NOT t.tgisinternal
 ORDER BY n.nspname, c.relname, t.tgname
"""

_FUNCTIONS_SQL = """
SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
       pg_get_function_result(p.oid)
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = ANY(%(schemas)s)
 ORDER BY 1, 2, 3
"""

_SEQUENCES_SQL = """
SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = ANY(%(schemas)s) AND c.relkind = 'S' ORDER BY 1, 2
"""


def _collation(name: str) -> str | None:
    return name or None


def introspect_pg(conn: Any, schemas: Sequence[str] = SCHEMAS) -> dict[str, Any]:
    """The catalog facts schema.prisma renders, as plain JSON-able data."""
    args = {"schemas": list(schemas)}
    relations: dict[str, dict[str, Any]] = {}
    for (nsp, rel, kind, col, num, fmt, typname, cat, elem, notnull, default, generated,
         identity) in conn.execute(_COLUMNS_SQL, args):
        key = f"{nsp}.{rel}"
        entry = relations.setdefault(key, {
            "schema": nsp, "name": rel, "kind": "view" if kind in ("v", "m") else "table",
            "columns": [], "primary_key": None, "uniques": [], "checks": [],
            "foreign_keys": [], "indexes": [], "triggers": []})
        entry["columns"].append({
            "name": col, "format": fmt, "type": typname, "array": cat == "A",
            "element": elem, "not_null": bool(notnull), "default": default,
            "generated": generated == "s", "identity": identity or None})
    for nsp, rel, name, kind, cols, definition, nulls_not_distinct in conn.execute(
            _CONSTRAINTS_SQL, args):
        entry = relations[f"{nsp}.{rel}"]
        item = {"name": name, "columns": list(cols), "definition": definition}
        if kind == "p":
            entry["primary_key"] = item
        elif kind == "u":
            item["nulls_not_distinct"] = bool(nulls_not_distinct)
            entry["uniques"].append(item)
        elif kind == "c":
            entry["checks"].append(item)
        elif kind == "f":
            entry["foreign_keys"].append(item)
        else:
            raise GenerationError(f"exclusion_constraint_unsupported:{nsp}.{rel}.{name}")
    for (nsp, rel, name, method, nkey, natts, cols, opclasses, collations, desc, partial,
         expression, definition, predicate) in conn.execute(_INDEXES_SQL, args):
        columns = []
        for position in range(nkey):
            opclass, default = opclasses[position].split("|")
            columns.append({"name": cols[position] or None,
                            "opclass": None if default == "true" else opclass,
                            "opclass_default": default == "true",
                            "collation": _collation(collations[position]),
                            "desc": bool(desc[position])})
        for column in columns:
            if column["opclass_default"]:
                column.pop("opclass")
        relations[f"{nsp}.{rel}"]["indexes"].append({
            "table": f"{nsp}.{rel}", "name": name, "method": method, "columns": columns,
            "include": [c for c in cols[nkey:natts]], "predicate": predicate,
            "expression": bool(expression), "definition": definition})
    for nsp, rel, name, definition in conn.execute(_TRIGGERS_SQL, args):
        relations[f"{nsp}.{rel}"]["triggers"].append({"name": name, "definition": definition})
    functions = [f"{n}.{p}({a}) -> {r}" for n, p, a, r in conn.execute(_FUNCTIONS_SQL, args)]
    sequences = [f"{n}.{s}" for n, s in conn.execute(_SEQUENCES_SQL, args)]
    return {"relations": relations, "functions": functions, "sequences": sequences}


def with_production_only(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """``catalog`` (built by the migrations) plus the indexes only production holds.
    One already present means a migration now creates it: that is drift of this list."""
    out = json.loads(json.dumps(catalog))
    for index in PRODUCTION_ONLY_INDEXES:
        table = out["relations"][index["table"]]
        if any(i["name"] == index["name"] for i in table["indexes"]):
            raise GenerationError(f"production_only_index_now_migrated:{index['name']}")
        table["indexes"].append(json.loads(json.dumps(index)))
        table["indexes"].sort(key=lambda i: i["name"])
    return out


# --------------------------------------------------------------------------- rendering

_SCALAR = {  # pg type name -> (prisma type, native attribute)
    "text": ("String", None), "varchar": ("String", None), "bpchar": ("String", None),
    "int8": ("BigInt", None), "int4": ("Int", None), "int2": ("Int", "@db.SmallInt"),
    "bool": ("Boolean", None), "float8": ("Float", None), "float4": ("Float", "@db.Real"),
    "jsonb": ("Json", None), "json": ("Json", "@db.Json"), "bytea": ("Bytes", None),
    "uuid": ("String", "@db.Uuid"), "date": ("DateTime", "@db.Date"),
}
_UNSUPPORTED = frozenset({"tsvector", "halfvec", "vector", "tsquery"})
_INDEX_TYPES = {"btree": None, "gin": "Gin", "gist": "Gist", "hash": "Hash",
                "spgist": "SpGist", "brin": "Brin", "hnsw": None, "ivfflat": None}
_SQL_TEXT_DEFAULT = re.compile(r"^'((?:[^']|'')*)'::(?:text|character varying|jsonb|json)$")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_EMPTY_ARRAY = re.compile(r"^'\{\}'::[a-z0-9_ ]+\[\]$")


def _type_of(column: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """(prisma type, native attribute, note) of one column; unknown types fail."""
    fmt, name = column["format"], column["type"]
    if column["array"]:
        element = {"format": fmt.removesuffix("[]"), "type": column["element"],
                   "array": False}
        inner, native, note = _type_of(element)
        if inner.startswith("Unsupported"):
            return f'Unsupported("{fmt}")', None, None
        return inner + "[]", native, note
    if name in _SCALAR:
        return (*_SCALAR[name], None)
    if name == "numeric":
        found = re.match(r"^numeric\((\d+),(\d+)\)$", fmt)
        return "Decimal", f"@db.Decimal({found[1]}, {found[2]})" if found else "@db.Decimal", None
    if name in ("timestamptz", "timestamp"):
        found = re.search(r"\((\d)\)", fmt)
        native = "Timestamptz" if name == "timestamptz" else "Timestamp"
        return "DateTime", f"@db.{native}({found[1] if found else 6})", None
    if name in _UNSUPPORTED:
        note = fmt if "(" in fmt else None
        return f'Unsupported("{name}")', None, note
    raise GenerationError(f"column_type_unsupported:{name}")


def _default(column: Mapping[str, Any], prisma_type: str) -> str | None:
    text = column["default"]
    if column["identity"]:
        return "@default(autoincrement())"
    if text is None:
        return None
    if column["generated"]:
        return '@default(dbgenerated("' + text.replace("\\", "\\\\").replace('"', '\\"') + '"))'
    if text.startswith("nextval("):
        return "@default(autoincrement())"
    if text == "now()" or text == "CURRENT_TIMESTAMP":
        return "@default(now())"
    if text in ("true", "false"):
        return f"@default({text})"
    if _NUMBER.match(text):
        return f"@default({text})"
    if _EMPTY_ARRAY.match(text) and prisma_type.endswith("[]"):
        return "@default([])"
    found = _SQL_TEXT_DEFAULT.match(text)
    if found:
        value = found[1].replace("''", "'")
        return '@default("' + value.replace("\\", "\\\\").replace('"', '\\"') + '")'
    raise GenerationError(f"default_unsupported:{column['name']}:{text}")


def _default_name(table: str, columns: Sequence[str], suffix: str) -> str:
    """Prisma's default constraint name (``<table>_<cols>_<suffix>``, cut to 63)."""
    name = "_".join([table, *columns]) if columns else table
    full = f"{name}_{suffix}"
    return full if len(full) <= 63 else name[:63 - len(suffix) - 1] + "_" + suffix


def _aligned(rows: Sequence[Sequence[str]]) -> list[str]:
    """Columns aligned the way ``prisma format`` aligns a model's fields."""
    widths = [max((len(r[i]) for r in rows if len(r) > i), default=0)
              for i in range(max((len(r) for r in rows), default=0))]
    out = []
    for row in rows:
        parts = [cell.ljust(widths[i]) if i < len(row) - 1 else cell
                 for i, cell in enumerate(row)]
        out.append("  " + " ".join(parts).rstrip())
    return out


def _index_line(index: Mapping[str, Any], table: str) -> str:
    cols = []
    for column in index["columns"]:
        text = column["name"]
        args = []
        if index["method"] == "gin" and not column["opclass_default"]:
            args.append(f'ops: raw("{column["opclass"]}")')
        if column["desc"]:
            args.append("sort: Desc")
        cols.append(f"{text}({', '.join(args)})" if args else text)
    parts = ["[" + ", ".join(cols) + "]"]
    if index["name"] != _default_name(table, [c["name"] for c in index["columns"]], "idx"):
        parts.append(f'map: "{index["name"]}"')
    kind = _INDEX_TYPES[index["method"]]
    if kind:
        parts.append(f"type: {kind}")
    return "@@index(" + ", ".join(parts) + ")"


def _index_notes(index: Mapping[str, Any]) -> list[str]:
    """What Prisma's @@index cannot say about ``index`` (empty: nothing is lost)."""
    notes = []
    if index["method"] not in ("btree", "gin", "gist", "hash", "spgist", "brin"):
        notes.append(f"method {index['method']}")
    for column in index["columns"]:
        if index["method"] != "gin" and not column["opclass_default"]:
            notes.append(f"{column['name']} {column['opclass']}")
        if column["collation"]:
            notes.append(f"{column['name']} COLLATE \"{column['collation']}\"")
    if index["include"]:
        notes.append("INCLUDE (" + ", ".join(index["include"]) + ")")
    return notes


def _relations_for(key: str) -> tuple[list[tuple], list[tuple]]:
    owned = [r for r in LOGICAL_RELATIONS if r[0] == key]
    back = [r for r in LOGICAL_RELATIONS if r[2] == key]
    return owned, back


def _model_name(key: str, catalog: Mapping[str, Any]) -> str:
    """The table's name, or ``<schema>_<table>`` when two schemas share a table name."""
    schema, name = key.split(".", 1)
    clash = sum(1 for k in catalog["relations"] if k.split(".", 1)[1] == name) > 1
    return f"{schema}_{name}" if clash else name


def render_relation(key: str, rel: Mapping[str, Any], catalog: Mapping[str, Any],
                    schema_attr: bool = True) -> str:
    name = rel["name"]
    model = _model_name(key, catalog)
    pk = rel["primary_key"]
    pk_cols = pk["columns"] if pk else []
    single_unique = {u["columns"][0]: u for u in rel["uniques"] if len(u["columns"]) == 1}
    comments: list[str] = []
    if rel["checks"]:
        comments.append("/// This table contains check constraints and requires additional "
                        "setup for migrations. Visit https://pris.ly/d/check-constraints for "
                        "more info.")
    if any(i["expression"] for i in rel["indexes"]):
        comments.append("/// This model contains an expression index which requires "
                        "additional setup for migrations. Visit "
                        "https://pris.ly/d/expression-indexes for more info.")
    for check in rel["checks"]:
        comments.append(f"/// check {check['name']}: {check['definition']}")
    for unique in rel["uniques"]:
        if unique["nulls_not_distinct"]:
            comments.append(f"/// unique {unique['name']} is NULLS NOT DISTINCT")
    for index in rel["indexes"]:
        if index["predicate"] is not None or index["expression"]:
            comments.append(f"/// index not expressible in Prisma: {index['definition']}")
        else:
            notes = _index_notes(index)
            if notes:
                comments.append(f"/// index {index['name']}: " + "; ".join(notes)
                                + " (not expressible in Prisma)")
    for trigger in rel["triggers"]:
        comments.append(f"/// trigger: {trigger['definition']}")
    owned, back = _relations_for(key)
    if owned:
        comments.append("/// Relations below are logical (relationMode \"prisma\"): the "
                        "database declares no foreign key and does not enforce them.")
    rows: list[list[str]] = []
    field_notes: dict[int, str] = {}
    for column in rel["columns"]:
        prisma_type, native, note = _type_of(column)
        optional = not column["not_null"] and not prisma_type.endswith("[]")
        attrs = []
        if len(pk_cols) == 1 and pk_cols[0] == column["name"]:
            attrs.append("@id")
        if column["name"] in single_unique and not single_unique[column["name"]][
                "nulls_not_distinct"]:
            unique = single_unique[column["name"]]
            attrs.append("@unique" if unique["name"] == _default_name(
                name, [column["name"]], "key") else f'@unique(map: "{unique["name"]}")')
        default = _default(column, prisma_type)
        if default:
            attrs.append(default)
        if native:
            attrs.append(native)
        if note:
            field_notes[len(rows)] = f"/// {note}"
        rows.append([column["name"], prisma_type + ("?" if optional else ""), *(
            [" ".join(attrs)] if attrs else [])])
    columns_by_name = {c["name"]: c for c in rel["columns"]}
    for child, col, parent, pcol, field, _back in owned:
        optional = not columns_by_name[col]["not_null"]
        target = _model_name(parent, catalog)
        relation_name = f'"{_model_name(key, catalog)}_{field}"'
        rows.append([field, target + ("?" if optional else ""),
                     f"@relation({relation_name}, fields: [{col}], references: [{pcol}])"])
    for child, col, parent, pcol, field, back_field in back:
        child_rel = catalog["relations"][child]
        one = (child_rel["primary_key"] or {}).get("columns") == [col] or any(
            u["columns"] == [col] for u in child_rel["uniques"])
        target = _model_name(child, catalog)
        relation_name = f'"{_model_name(child, catalog)}_{field}"'
        rows.append([back_field, target + ("?" if one else "[]"), f"@relation({relation_name})"])
    lines = [*comments, f"{'view' if rel['kind'] == 'view' else 'model'} {model} {{"]
    aligned = _aligned(rows)
    for position, line in enumerate(aligned):
        if position in field_notes:
            lines.append("  " + field_notes[position])
        lines.append(line)
    tail = []
    if len(pk_cols) > 1:
        tail.append("@@id([" + ", ".join(pk_cols) + "]"
                    + ("" if pk["name"] == _default_name(name, [], "pkey") else
                       f', map: "{pk["name"]}"') + ")")
    for unique in rel["uniques"]:
        if len(unique["columns"]) > 1 or unique["nulls_not_distinct"]:
            tail.append("@@unique([" + ", ".join(unique["columns"]) + "]"
                        + ("" if unique["name"] == _default_name(name, unique["columns"], "key")
                           else f', map: "{unique["name"]}"') + ")")
    for index in rel["indexes"]:
        if index["predicate"] is None and not index["expression"]:
            if index["method"] not in _INDEX_TYPES:
                raise GenerationError(f"index_method_unsupported:{index['name']}")
            tail.append(_index_line(index, name))
    if model != name:
        tail.append(f'@@map("{name}")')
    if schema_attr:
        tail.append(f'@@schema("{rel["schema"]}")')
    if tail:
        lines.append("")
        lines.extend("  " + t for t in tail)
    lines.append("}")
    return "\n".join(lines)


def migrations_digest(directory: Path = MIGRATIONS_DIR) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.sql")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def render_pg(catalog: Mapping[str, Any]) -> str:
    relations = catalog["relations"]
    order = sorted((k for k, r in relations.items() if r["kind"] == "table"),
                   key=lambda k: (relations[k]["schema"], relations[k]["name"])) + sorted(
        (k for k, r in relations.items() if r["kind"] == "view"),
        key=lambda k: (relations[k]["schema"], relations[k]["name"]))
    head = [
        "// Generated by tools/gen_prisma.py: do not edit by hand.",
        "// Source: the production structure (a structure-only dump restored into a",
        "// throwaway cluster), which equals operating/pg/migrations plus",
        "// PRODUCTION_ONLY_INDEXES in tools/gen_prisma.py.",
        f"// Migrations: sha256 {migrations_digest()} (operating/pg/migrations).",
        "// The Workbench does not use Prisma Client: this file describes the database for",
        "// readers who know Prisma. Reads go through operating/ (docs/data-model.md).",
        "//",
        "// Functions:",
        *[f"//   {f}" for f in catalog["functions"]],
        "// Sequences:",
        *[f"//   {s}" for s in catalog["sequences"]],
        "",
        "generator client {",
        '  provider        = "prisma-client-js"',
        '  previewFeatures = ["multiSchema", "postgresqlExtensions", "views"]',
        "}",
        "",
        "datasource db {",
        '  provider     = "postgresql"',
        '  url          = env("DATABASE_URL")',
        '  extensions   = [' + ", ".join(EXTENSIONS) + "]",
        '  schemas      = [' + ", ".join(f'"{s}"' for s in SCHEMAS) + "]",
        '  relationMode = "prisma"',
        "}",
    ]
    blocks = [render_relation(k, relations[k], catalog) for k in order]
    return "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------- SQLite stores

def declared_stores() -> list[Any]:
    from operating import declared_schema
    out = []
    for info in sorted(pkgutil.iter_modules(declared_schema.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"operating.declared_schema.{info.name}")
        if hasattr(module, "STORE") and hasattr(module, "STEPS"):
            out.append(module)
    return out


def _sqlite_type(declared: str) -> tuple[str, str | None]:
    """Prisma's reading of a SQLite declared type (its affinity rules)."""
    text = declared.upper()
    if text == "":
        raise GenerationError("sqlite_column_without_type")
    if "INT" in text:
        return ("BigInt" if text == "BIGINT" else "Int"), None
    if any(t in text for t in ("CHAR", "CLOB", "TEXT")):
        return "String", None
    if "BLOB" in text:
        return "Bytes", None
    if any(t in text for t in ("REAL", "FLOA", "DOUB")):
        return "Float", None
    if text in ("BOOLEAN", "BOOL"):
        return "Boolean", None
    if text in ("DATETIME", "TIMESTAMP", "DATE"):
        return "DateTime", None
    if text.startswith(("NUMERIC", "DECIMAL")):
        return "Decimal", None
    raise GenerationError(f"sqlite_type_unsupported:{declared}")


def introspect_sqlite(conn: sqlite3.Connection) -> dict[str, Any]:
    relations: dict[str, dict[str, Any]] = {}
    tables = conn.execute("SELECT name, type, sql FROM sqlite_master WHERE type IN "
                          "('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
                          ).fetchall()
    for name, kind, sql_text in tables:
        if kind == "table" and re.search(r"CREATE\s+VIRTUAL\s+TABLE", sql_text or "", re.I):
            relations[name] = {"name": name, "kind": "virtual", "sql": sql_text}
            continue
        if any(name.startswith(v + "_") and relations.get(v, {}).get("kind") == "virtual"
               for v in relations):
            continue  # an FTS5 shadow table
        columns = conn.execute(f'PRAGMA table_xinfo("{name}")').fetchall()
        pk = [c[1] for c in sorted((c for c in columns if c[5]), key=lambda c: c[5])]
        uniques, indexes = [], []
        for _seq, iname, unique, origin, partial in conn.execute(
                f'PRAGMA index_list("{name}")'):
            cols = [r[2] for r in conn.execute(f'PRAGMA index_xinfo("{iname}")') if r[5]]
            desc = [bool(r[3]) for r in conn.execute(f'PRAGMA index_xinfo("{iname}")') if r[5]]
            sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?",
                                   (iname,)).fetchone()
            item = {"name": iname, "columns": cols, "desc": desc, "partial": bool(partial),
                    "expression": None in cols, "origin": origin,
                    "definition": sql_row[0] if sql_row else None}
            if origin == "pk":
                continue
            (uniques if unique else indexes).append(item)
        grouped: dict[int, dict[str, Any]] = {}
        for fid, _seq, parent, col, pcol, on_update, on_delete, _match in conn.execute(
                f'PRAGMA foreign_key_list("{name}")'):
            entry = grouped.setdefault(fid, {"id": fid, "table": parent, "from": [], "to": [],
                                             "on_update": on_update, "on_delete": on_delete})
            entry["from"].append(col)
            entry["to"].append(pcol)
        foreign = [grouped[k] for k in sorted(grouped)]
        triggers = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ? "
            "ORDER BY name", (name,))]
        checks = re.findall(r"CHECK\s*\(", sql_text or "", re.I)
        relations[name] = {
            "name": name, "kind": kind, "sql": sql_text,
            "columns": [{"name": c[1], "type": c[2], "not_null": bool(c[3]),
                         "default": c[4], "pk": c[5], "hidden": c[6]} for c in columns],
            "primary_key": pk, "uniques": uniques,   # PRAGMA index_list order, as Prisma
            "indexes": indexes, "foreign_keys": foreign,
            "triggers": triggers, "has_checks": bool(checks),
            "autoincrement": bool(re.search(r"AUTOINCREMENT", sql_text or "", re.I))}
    return relations


def _sqlite_default(value: Any, prisma_type: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    if text.upper() in ("CURRENT_TIMESTAMP",):
        return "@default(now())"
    if _NUMBER.match(text):
        if prisma_type == "Boolean":
            return f"@default({'true' if text != '0' else 'false'})"
        return f"@default({text})"
    found = re.match(r"^'((?:[^']|'')*)'$", text)
    if found:
        inner = found[1].replace("''", "'")
        return '@default("' + inner.replace("\\", "\\\\").replace('"', '\\"') + '")'
    return '@default(dbgenerated("' + text.replace("\\", "\\\\").replace('"', '\\"') + '"))'


_ACTIONS = {"NO ACTION": "NoAction", "CASCADE": "Cascade", "SET NULL": "SetNull",
            "SET DEFAULT": "SetDefault", "RESTRICT": "Restrict"}


def _sqlite_identified(rel: Mapping[str, Any]) -> bool:
    """Whether Prisma Client can address the table's rows: a primary key or a plain unique
    index whose columns are all NOT NULL (an INTEGER PRIMARY KEY is the rowid)."""
    columns = {c["name"]: c for c in rel["columns"]}

    def required(name: str) -> bool:
        return columns[name]["not_null"] or (
            rel["primary_key"] == [name] and columns[name]["type"].upper() == "INTEGER")
    if rel["primary_key"] and all(required(c) for c in rel["primary_key"]):
        return True
    return any(not u["partial"] and not u["expression"] and all(required(c)
               for c in u["columns"]) for u in rel["uniques"])


def _sqlite_relations(relations: Mapping[str, Any]) -> dict[str, list[list[str]]]:
    """Relation field rows per model, the way ``prisma db pull`` names them: the other
    model's name, or ``<other>_<relation>`` with relation ``<child>_<cols>To<parent>``
    when one pair of models has several foreign keys."""
    rows: dict[str, list[list[str]]] = {name: [] for name in relations}
    back: dict[str, list[list[str]]] = {name: [] for name in relations}
    tables = {k: v for k, v in relations.items() if v["kind"] == "table"}
    ignored = {k for k, v in tables.items() if not _sqlite_identified(v)}
    for child in sorted(tables):
        keys = tables[child]["foreign_keys"]
        for key in keys:
            parent = key["table"]
            if parent not in tables:
                raise GenerationError(f"foreign_key_target_missing:{child}->{parent}")
            several = sum(1 for k in keys if k["table"] == parent) > 1
            relation = f"{child}_{'_'.join(key['from'])}To{parent}" if several else None
            columns = {c["name"]: c for c in tables[child]["columns"]}
            optional = any(not columns[c]["not_null"] for c in key["from"])
            args = ([f'"{relation}"'] if relation else []) + [
                f"fields: [{', '.join(key['from'])}]", f"references: [{', '.join(key['to'])}]",
                f"onDelete: {_ACTIONS[key['on_delete']]}",
                f"onUpdate: {_ACTIONS[key['on_update']]}"]
            attr = f"@relation({', '.join(args)})"
            if parent in ignored and child not in ignored:
                attr += " @ignore"
            rows[child].append([f"{parent}_{relation}" if relation else parent,
                                parent + ("?" if optional else ""), attr])
            unique = tables[child]["primary_key"] == key["from"] or any(
                u["columns"] == key["from"] for u in tables[child]["uniques"])
            battr = f'@relation("{relation}")' if relation else ""
            if child in ignored and parent not in ignored:
                battr = (battr + " @ignore").strip()
            back[parent].append([f"{child}_{relation}" if relation else child,
                                 child + ("?" if unique else "[]"), *([battr] if battr else [])])
    return {name: rows[name] + back[name] for name in relations}


def render_sqlite_store(store: str, module_name: str, relations: Mapping[str, Any],
                        version: int) -> str:
    head = [
        "// Generated by tools/gen_prisma.py: do not edit by hand.",
        f"// SQLite store \"{store}\": operating/declared_schema/{module_name}.py, "
        f"declared version {version}.",
        "// Each store is its own SQLite file; the Workbench opens it with sqlite3, not",
        "// Prisma Client (docs/data-model.md).",
        "",
        "generator client {",
        '  provider = "prisma-client-js"',
        "}",
        "",
        "datasource db {",
        '  provider = "sqlite"',
        '  url      = env("DATABASE_URL")',
        "}",
    ]
    relation_rows = _sqlite_relations(relations)
    blocks = []
    for name in sorted(relations):
        rel = relations[name]
        if rel["kind"] == "virtual":
            blocks.append(f"// Virtual table {name} (not expressible in Prisma): "
                          + " ".join((rel["sql"] or "").split()))
            continue
        identified = rel["kind"] == "view" or _sqlite_identified(rel)
        comments = []
        if not identified:
            comments.append("/// The underlying table does not contain a valid unique "
                            "identifier and can therefore currently not be handled by "
                            "Prisma Client.")
        if rel["has_checks"]:
            comments.append("/// This table contains check constraints and requires "
                            "additional setup for migrations. Visit "
                            "https://pris.ly/d/check-constraints for more info.")
        for unique in rel["uniques"]:
            if unique["partial"] or unique["expression"]:
                comments.append("/// unique index not expressible in Prisma: "
                                + " ".join((unique["definition"] or "").split()))
        for index in rel["indexes"]:
            if index["partial"] or index["expression"]:
                comments.append("/// index not expressible in Prisma: "
                                + " ".join((index["definition"] or "").split()))
        for trigger in rel["triggers"]:
            comments.append("/// trigger: " + " ".join(trigger.split()))
        for column in rel["columns"]:
            if column["hidden"] in (2, 3):
                comments.append(f"/// generated column {column['name']} {column['type']} "
                                "(not expressible in Prisma)")
        pk = rel["primary_key"]
        single_unique = {u["columns"][0]: u for u in rel["uniques"]
                         if len(u["columns"]) == 1 and not u["partial"]
                         and not u["expression"]}
        rows = []
        for column in (c for c in rel["columns"] if c["hidden"] == 0):
            prisma_type, _native = _sqlite_type(column["type"])
            rowid = pk == [column["name"]] and column["type"].upper() == "INTEGER"
            optional = not column["not_null"] and not rowid
            attrs = []
            if pk == [column["name"]]:
                attrs.append("@id")
                if rowid:
                    attrs.append("@default(autoincrement())")
            if column["name"] in single_unique:
                unique = single_unique[column["name"]]
                attrs.append("@unique" if unique["name"] == _default_name(
                    name, [column["name"]], "key") else f'@unique(map: "{unique["name"]}")')
            default = _sqlite_default(column["default"], prisma_type)
            if default and not rowid:
                attrs.append(default)
            rows.append([column["name"], prisma_type + ("?" if optional else ""),
                         *([" ".join(attrs)] if attrs else [])])
        rows.extend(relation_rows.get(name, []))
        tail = []
        if pk and len(pk) > 1:
            tail.append("@@id([" + ", ".join(pk) + "])")
        for unique in rel["uniques"]:
            if len(unique["columns"]) > 1 and not unique["partial"] and not unique["expression"]:
                tail.append("@@unique([" + ", ".join(unique["columns"]) + "]"
                            + ("" if unique["name"] == _default_name(name, unique["columns"],
                                                                     "key")
                               else f', map: "{unique["name"]}"') + ")")
        for index in rel["indexes"]:
            if not index["partial"] and not index["expression"]:
                cols = [f"{c}(sort: Desc)" if d else c
                        for c, d in zip(index["columns"], index["desc"])]
                tail.append("@@index([" + ", ".join(cols) + "]"
                            + ("" if index["name"] == _default_name(name, index["columns"],
                                                                    "idx")
                               else f', map: "{index["name"]}"') + ")")
        if not identified:
            tail.append("@@ignore")
        lines = [*comments, f"{'view' if rel['kind'] == 'view' else 'model'} {name} {{",
                 *_aligned(rows)]
        if tail:
            lines.append("")
            lines.extend("  " + t for t in tail)
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n"


def _flat(ddl: Any) -> Iterable[str]:
    """A step's statements: a script, or (nested) tuples of them. Two stores
    (``capture_store``, ``host_ledger``) declare a tuple inside the step's tuple and run
    the statements themselves; their tables are the same."""
    if isinstance(ddl, str):
        yield ddl
        return
    for part in ddl:
        yield from _flat(part)


def render_app() -> dict[str, str]:
    """File name -> text of every declared SQLite store's schema."""
    out = {}
    for module in declared_stores():
        conn = sqlite3.connect(":memory:")
        try:
            for _version, _name, ddl in module.STEPS:
                for statement in _flat(ddl):
                    conn.executescript(statement)
            relations = introspect_sqlite(conn)
        finally:
            conn.close()
        module_name = module.__name__.rsplit(".", 1)[1]
        out[f"{module_name}.prisma"] = render_sqlite_store(
            module.STORE, module_name, relations, module.STEPS[-1][0])
    return out


# --------------------------------------------------------------------------- throwaway cluster

def _pg_bindir() -> Path:
    """``PLX_PG_BINDIR`` (a server built with pgvector, e.g. a Spark's ~/plx-tests/pg/bin),
    else the newest installed server whose share directory holds pgvector."""
    named = os.environ.get("PLX_PG_BINDIR")
    if named and (Path(named) / "initdb").exists():
        return Path(named)
    for directory in sorted(Path("/usr/lib/postgresql").glob("*/bin"),
                            key=lambda p: int(p.parent.name), reverse=True):
        share = Path("/usr/share/postgresql") / directory.parent.name / "extension"
        if (directory / "initdb").exists() and (share / "vector.control").exists():
            return directory
    raise GenerationError("no_postgres_with_pgvector_installed")


class ScratchCluster:
    """A Postgres cluster in a temporary directory, reachable only through its own unix
    socket (``listen_addresses=''``, trust for this OS user), removed on exit."""

    def __init__(self) -> None:
        self.bindir = _pg_bindir()
        self.root = Path(tempfile.mkdtemp(prefix="plxprisma-"))  # short: socket path limit  # tmp-lint: socket path
        self.data = self.root / "data"
        self.user = "plxgen"
        self.port = "55432"   # names the socket file only; no TCP listener

    def __enter__(self) -> "ScratchCluster":
        os.chmod(self.root, 0o700)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        subprocess.run([str(self.bindir / "initdb"), "-D", str(self.data), "-U", self.user,
                        "-A", "trust", "--no-instructions", "-E", "UTF8",
                        "--locale=C.UTF-8"], check=True, env=env, stdout=subprocess.DEVNULL)
        options = (f"-c listen_addresses='' -c unix_socket_directories={self.root} "
                   f"-c port={self.port} -c shared_buffers=32MB -c fsync=off")
        subprocess.run([str(self.bindir / "pg_ctl"), "-D", str(self.data), "-w", "-l",
                        str(self.root / "log"), "-o", options, "start"], check=True, env=env,
                       stdout=subprocess.DEVNULL)
        return self

    def createdb(self, database: str) -> None:
        subprocess.run([str(self.bindir / "createdb"), "-h", str(self.root), "-p", self.port,
                        "-U", self.user, database], check=True)

    def dsn(self, database: str = "postgres") -> str:
        return f"host={self.root} port={self.port} user={self.user} dbname={database}"

    def __exit__(self, *_exc: Any) -> None:
        subprocess.run([str(self.bindir / "pg_ctl"), "-D", str(self.data), "-m", "fast",
                        "-w", "stop"], check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        shutil.rmtree(self.root, ignore_errors=True)


def build_from_migrations(dsn: str) -> None:
    """Build the migrations into an EMPTY scratch database. A database that already holds
    any of the schemas (a real one, perhaps production) is refused: this tool never
    migrates a database it did not just get empty."""
    import psycopg
    from operating.pg import migrate
    with psycopg.connect(dsn, autocommit=True) as conn:
        found = conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname = ANY(%s)",
                             (list(SCHEMAS),)).fetchone()[0]
        if found:
            raise GenerationError("database_not_empty: give an empty scratch database")
        migrate.migrate(conn, values=migrate.params("BAAI/bge-m3"))


def build_from_dump(cluster: ScratchCluster, database: str, dump: Path) -> None:
    """Restore a structure-only dump. It carries no extensions (they live in ``public``)
    and orders a SQL function before the table it reads, so the restore creates the
    extensions first and does not check function bodies."""
    preamble = ("SET check_function_bodies = false;\n" + "".join(
        f"CREATE EXTENSION IF NOT EXISTS {e};\n" for e in EXTENSIONS))
    subprocess.run([str(cluster.bindir / "psql"), cluster.dsn(database), "-q",
                    "-v", "ON_ERROR_STOP=1", "-X"],
                   input=preamble + dump.read_text("utf-8"), text=True, check=True,
                   stdout=subprocess.DEVNULL)


def catalog_from(dsn: str) -> dict[str, Any]:
    import psycopg
    with psycopg.connect(dsn) as conn:
        return introspect_pg(conn)


# --------------------------------------------------------------------------- CLI

def _compare_or_write(files: Mapping[Path, str], check: bool, stale_dir: Path | None = None
                      ) -> int:
    problems = []
    for path, text in sorted(files.items()):
        current = path.read_text("utf-8") if path.exists() else None
        if current != text:
            problems.append(str(path.relative_to(ROOT)))
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, "utf-8")
    if stale_dir is not None and stale_dir.exists():
        for path in sorted(stale_dir.glob("*.prisma")):
            if path not in files:
                problems.append(f"{path.relative_to(ROOT)} (no such store)")
                if not check:
                    path.unlink()
    if check and problems:
        print("out of date: " + ", ".join(problems)
              + " (run python3 tools/gen_prisma.py without --check)", file=sys.stderr)
        return 1
    for problem in problems if not check else ():
        print(f"wrote {problem}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="target", required=True)
    app = sub.add_parser("app", help="prisma/app/<store>.prisma from the declared SQLite stores")
    app.add_argument("--check", action="store_true")
    pg = sub.add_parser("pg", help="prisma/schema.prisma from a Postgres catalog")
    pg.add_argument("--check", action="store_true")
    where = pg.add_mutually_exclusive_group(required=True)
    where.add_argument("--scratch-cluster", action="store_true",
                       help="start a throwaway cluster on a private unix socket")
    where.add_argument("--dsn-env", help="environment variable naming a scratch database")
    pg.add_argument("--from-dump", type=Path,
                    help="restore this structure-only dump (default: run the migrations)")
    args = parser.parse_args(argv)
    try:
        if args.target == "app":
            files = {APP_DIR / name: text for name, text in render_app().items()}
            return _compare_or_write(files, args.check, APP_DIR)
        if args.dsn_env:
            dsn = os.environ.get(args.dsn_env)
            if not dsn:
                print(f"{args.dsn_env} is not set", file=sys.stderr)
                return 2
            if args.from_dump:
                print("--from-dump needs --scratch-cluster", file=sys.stderr)
                return 2
            build_from_migrations(dsn)
            catalog = with_production_only(catalog_from(dsn))
        else:
            with ScratchCluster() as cluster:
                cluster.createdb("plx_migrated")
                build_from_migrations(cluster.dsn("plx_migrated"))
                catalog = with_production_only(catalog_from(cluster.dsn("plx_migrated")))
                if args.from_dump:
                    cluster.createdb("plx_dump")
                    build_from_dump(cluster, "plx_dump", args.from_dump)
                    dumped = catalog_from(cluster.dsn("plx_dump"))
                    if render_pg(dumped) != render_pg(catalog):
                        print("the dump differs from the migrations plus "
                              "PRODUCTION_ONLY_INDEXES: not written", file=sys.stderr)
                        return 1
                    catalog = dumped
        return _compare_or_write({PG_OUT: render_pg(catalog)}, args.check)
    except GenerationError as exc:
        print(f"generation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
