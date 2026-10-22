"""Generate the ontology schema skeleton. No network.

Inputs:

* the Portal reporting-API relation catalog: a ``GET /v1/relations`` dump taken by
  the credentialed live checks (``--live-catalog``), or, offline, the catalog the
  API builds from the upstream manifest (``services/workbench-api/manifest/
  allowlist.json``) and its committed catalog facts, with the key and ``since``
  choice mirrored from ``services/workbench-api/src/core/catalog.mjs``;
* Portal Prisma (``portal/prisma/schema.prisma``): ``@@map``/``@map`` names,
  scalar types, enums, ``@id``/``@unique`` and ``@relation`` foreign keys;
* the source registry (``ontology/source_registry.json``, ``scripts/source_registry.py``):
  the reporting relations and Prisma models each Portal API route reads, from its own
  route scan;
* the existing Workbench contracts: ``operating/link_contracts.json``,
  ``ontology/relation_contracts.FM_SOURCE_FK`` and
  ``operating/source_unit_contracts.json``;
* the declarative overrides ``ontology/schema/overrides/*.json``: which raw columns are
  money (measure, tax basis, header, unit column; finance-marked, their currency read
  from the record's own ``currency`` column or its header's, else declared unknown),
  which column names carry a marking, when a key is a verified identity, and the default
  lineage per relation prefix.

This script is the Portal source adapter: it reads the catalog and Prisma into source
facts (``portal_facts``); ``ontology/generator`` turns facts into types, identity keys,
links and markings for any source.

``refresh [--portal-repo PATH]`` reads the Portal at its one repo-level pin
(``verify/upstream-pins.json`` ``repo_pins``, the commit the source registry is
generated at; local git reads of the checkout, never its HEAD) and optionally a live
catalog dump, and writes the pinned inputs ``ontology/schema/generated/meta/
portal_catalog.json`` and ``portal_prisma.json``; ``generate`` (the default)
turns the pinned inputs into ``ontology/schema/generated/portal_<domain>.json``
(C0 ``plx-ontology-domain/v1`` files, ``origin: generated``) and
``meta/coverage.json`` (every catalog relation bound or explicitly excluded, the
catalog digest, and which contracts were or were not bound, with reasons);
``generate --check`` exits 1 when the committed files differ from a regeneration.
``drift_live`` (run as ``python3 -m scripts.live_read_probes schema-drift`` by the
credentialed live checks, spec ``verify/live-checks/schema.catalog_drift.json``)
compares the committed schema with the live catalog.

Raw types are named ``Rel<Schema><Relation>`` and stay out of the way of curated
types: a curated binding on the same relation shadows the raw type
(``ontology/schema.py``). Every Prisma model the reporting API does not serve, every
catalog relation it excludes and every relation a Portal API route reads that the manifest
does not approve is a ``not_served`` type with the upstream ask ``UPSTREAM_ASK`` in
``generated/portal_not_served_<a>_<z>.json`` (``NOT_SERVED_FILES``), named ``Rel*`` or by
the curated id ``overrides/names.json`` gives its relation, unless a pending file
(``ontology/schema/pending/``) already binds its relation: that pending type is then its
one declaration. ``coverage.json`` ``not_served`` maps each such relation to its type, and
each foreign key to one (``prisma_relations_not_bound``) names its ``target_type``: the
served column stays a plain reference value, never a link. ``coverage.json``
``upstream_ask`` ranks the unserved relations by the Portal API routes serving them would
complete (``q3_ask``). The generator never invents a link: it emits Prisma
foreign keys between served relations and the declared link contracts; an
``FM_SOURCE_FK`` without a link contract is reported with the served relations
that carry its column, as a curation lead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ontology import generator as gen  # noqa: E402
from ontology import schema as ontology_schema  # noqa: E402
from ontology.relation_contracts import FM_SOURCE_FK  # noqa: E402
from scripts.source_registry import REGISTRY_PATH, data_keys, load_registry  # noqa: E402

GENERATED_DIR = ontology_schema.GENERATED_DIR
META_DIR = ontology_schema.META_DIR
CATALOG_PATH = ontology_schema.CATALOG_SNAPSHOT_PATH
PRISMA_FACTS_PATH = META_DIR / "portal_prisma.json"
COVERAGE_PATH = ontology_schema.COVERAGE_PATH
LINK_CONTRACTS_PATH = ROOT / "operating" / "link_contracts.json"
UNIT_CONTRACTS_PATH = ROOT / "operating" / "source_unit_contracts.json"

PORTAL = "plx-customer-portal"
MANIFEST_PATH = "services/workbench-api/manifest/allowlist.json"
CATALOG_FACTS_PATH = "services/workbench-api/test/core/fixtures/catalog-facts.json"
CATALOG_CODE_PATH = "services/workbench-api/src/core/catalog.mjs"
CATALOG_HELPERS_PATH = "services/workbench-api/test/core/helpers.mjs"
PRISMA_PATH = "portal/prisma/schema.prisma"
PRISMA_FORMAT = "plx-prisma-facts/v1"
# The upstream ask to serve more Portal tables (open
# question Q3: approve a manifest expansion), as the curated not_served placeholders cite it.
UPSTREAM_ASK = "Q3"
# Generated not_served files by the first letter of the table (``app_`` dropped): a C0
# file holds at most 200 object types, and a table always lands in the same file.
NOT_SERVED_FILES = (("a", "e"), ("f", "o"), ("p", "r"), ("s", "z"))


# The ranked ask lists this many relations. A Prisma column whose name ends so stores a
# credential (a password hash, a 2FA secret, a reset, OAuth or session token, a key hash):
# serving its table needs an owner decision on those columns first.
ASK_TOP = 25
CREDENTIAL_COLUMN = re.compile(r"(?:^|_)(?:password|passwd|secret|token|api_key|private_key|"
                               r"key_hash|token_hash)$")


def not_served_domain(relation: str) -> str:
    table = relation.split(".", 1)[1]
    first = (table[4:] if table.startswith("app_") else table)[:1]
    low, high = next(((lo, hi) for lo, hi in NOT_SERVED_FILES if lo <= first <= hi),
                     NOT_SERVED_FILES[0])
    return f"not_served_{low}_{high}"

# Money, marking, identity and lineage semantics: ontology/schema/overrides/*.json. The
# finance name rule is named here too (ontology/tests/test_schema.py reads it).
OVERRIDES = gen.load_overrides()
_FINANCE = OVERRIDES.name_rule("finance")
_MONEY_NAME, _NOT_MONEY_NAME, NOT_MONEY = _FINANCE.matches, _FINANCE.unless, _FINANCE.exceptions

# --------------------------------------------------------------- catalog (offline)
# Mirrors services/workbench-api/src/core/catalog.mjs (SINCE_COLUMNS, keyValueType,
# sinceMode, chooseKey) so the offline catalog has the key and since the API serves.
SINCE_COLUMNS = ("updated_at", "audittimestampmodified", "audtdate")


def key_value_type(format_type: str) -> str | None:
    if format_type == "smallint":
        return "int2"
    if format_type == "integer":
        return "int4"
    if format_type == "bigint":
        return "int8"
    if re.fullmatch(r"numeric(\(\d+(,\d+)?\))?", format_type):
        return "numeric"
    if format_type == "uuid":
        return "uuid"
    if format_type == "text" or re.fullmatch(r"character( varying)?(\(\d+\))?", format_type):
        return "text"
    return None


def since_mode(column: str, format_type: str) -> str | None:
    if re.fullmatch(r"timestamp(\(\d\))? with time zone", format_type):
        return "timestamptz"
    if re.fullmatch(r"timestamp(\(\d\))? without time zone", format_type):
        return "timestamp-utc"
    if column == "audtdate" and key_value_type(format_type) == "text":
        return "yyyymmdd-text"
    return None


def choose_key(live: Mapping[str, Mapping[str, Any]],
               constraints: list[Mapping[str, Any]]) -> tuple[list[str], str] | None:
    def usable(cols: list[str]) -> bool:
        return bool(cols) and all(c in live and key_value_type(live[c]["type"]) for c in cols)

    def not_null(cols: list[str]) -> bool:
        return all(live[c].get("notnull") for c in cols)

    cands = sorted((c for c in constraints if usable(c["cols"])),
                   key=lambda c: (len(c["cols"]), c.get("name", "")))
    for c in cands:
        if c["type"] == "p" and not_null(c["cols"]):
            return list(c["cols"]), "primary-key"
    for c in cands:
        if c["type"] == "u" and not_null(c["cols"]):
            return list(c["cols"]), "unique"
    for c in cands:
        if c["type"] == "u" and len(c["cols"]) == 1:
            return list(c["cols"]), "nullable-unique"
    if usable(["id"]):
        return ["id"], "id"
    return None


def catalog_from_manifest(manifest: Mapping[str, Any], facts: Mapping[str, Any],
                          commit: str) -> dict[str, Any]:
    """The catalog the reporting API builds from its manifest, offline (not live)."""
    by_name = {row["rel"]: row for row in facts["relations"]}
    relations = []
    for entry in manifest["entries"]:
        name = f"{entry['schema']}.{entry['relation']}"
        row = by_name.get(name)
        out: dict[str, Any] = {"name": name, "schema": entry["schema"],
                               "relation": entry["relation"], "domain": entry.get("domain"),
                               "columns": list(entry["columns"]), "key": None, "since": None,
                               "unavailable": None}
        if row is None or not row.get("relkind"):
            out["unavailable"] = "relation not found in the catalog facts"
            relations.append(out)
            continue
        # As upstream factsCatalogRows (test/core/helpers.mjs): the facts list only key
        # and timestamp columns, so any other approved column is text, and NOT NULL
        # only when it is in the primary key.
        primary = {c for k in row["constraints"] if k["type"] == "p" for c in k["cols"]}
        known = {c["name"]: c for c in row["cols"]}
        live = {name: {"type": known[name]["type"] if name in known else "text",
                       "notnull": known[name]["notnull"] if name in known else name in primary,
                       "known": name in known}
                for name in entry["columns"]}
        constraints = [{"name": f"{entry['relation']}_{'_'.join(k['cols'])}_"
                                f"{'pkey' if k['type'] == 'p' else 'key'}",
                        "type": k["type"], "cols": k["cols"]} for k in row["constraints"]]
        key = choose_key(live, constraints)
        if key is None:
            out["unavailable"] = "no usable pagination key"
        else:
            out["key"], out["key_source"] = key
            since = next((c for c in SINCE_COLUMNS if c in live), None)
            mode = since_mode(since, live[since]["type"]) if since else None
            out["since"] = {"column": since, "mode": mode} if mode else None
        out["relkind"] = row["relkind"]
        out["column_types"] = {c: live[c]["type"] for c in sorted(live) if live[c]["known"]}
        relations.append(out)
    return {
        "format": ontology_schema.CATALOG_FORMAT,
        "evidence_mode": "upstream-manifest",
        "note": ("Offline catalog: the relations, approved columns, key and since the "
                 "Portal reporting API derives from its manifest, built from the upstream "
                 "manifest and committed catalog facts. Not a live read; regenerate from a "
                 "live GET /v1/relations dump with refresh --live-catalog."),
        "source": {"repo": PORTAL, "commit": commit,
                   "paths": [MANIFEST_PATH, CATALOG_FACTS_PATH, CATALOG_CODE_PATH,
                             CATALOG_HELPERS_PATH]},
        "relations": relations,
    }


def catalog_from_live_dump(dump: Mapping[str, Any], ref: str, commit: str) -> dict[str, Any]:
    """Normalise a recorded ``GET /v1/relations`` body (live-read evidence)."""
    parsed = ontology_schema.catalog_from_json(dump)  # validates the shape
    relations = []
    for name in parsed.relations:
        entry = (dump["relations"][name] if isinstance(dump["relations"], dict) else
                 next(e for e in dump["relations"] if e.get("name") == name))
        schema, relation = name.split(".", 1)
        relations.append({"name": name, "schema": schema, "relation": relation,
                          "domain": entry.get("domain"), "columns": list(entry["columns"]),
                          "key": entry.get("key"), "since": entry.get("since"),
                          "unavailable": entry.get("unavailable")})
    return {"format": ontology_schema.CATALOG_FORMAT, "evidence_mode": "live-read",
            "note": "Recorded live GET /v1/relations body (read-only reporting API).",
            "source": {"repo": PORTAL, "commit": commit, "live_ref": ref},
            "relations": relations}


# --------------------------------------------------------------- Prisma

_BLOCK = re.compile(r"^(model|enum)\s+(\w+)\s*\{\s*$")
_FIELD = re.compile(r"^(\w+)\s+(\w+(?:\([^)]*\))?)(\[\]|\?)?\s*(.*)$")


def _strip_comment(line: str) -> str:
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif not quoted and line.startswith("//", i):
            return line[:i]
    return line


def _attr_args(attrs: str, name: str) -> str | None:
    """Raw argument text of ``@name(...)`` (balanced parentheses), else None."""
    m = re.search(r"(?<![@\w])" + re.escape(name) + r"\(", attrs)
    if not m:
        return None
    depth, start = 0, m.end() - 1
    for i in range(start, len(attrs)):
        if attrs[i] == "(":
            depth += 1
        elif attrs[i] == ")":
            depth -= 1
            if depth == 0:
                return attrs[start + 1:i]
    return None


def _list_arg(args: str, key: str) -> list[str]:
    m = re.search(key + r"\s*:\s*\[([^\]]*)\]", args)
    return [p.strip() for p in m.group(1).split(",") if p.strip()] if m else []


def parse_prisma(text: str) -> dict[str, Any]:
    """Models and enums of a Prisma schema with 1-based line spans. The ``///`` documentation
    comments right above a model or a field are kept as its ``doc``."""
    models: dict[str, Any] = {}
    enums: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    doc: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if raw.strip().startswith("///"):
            doc.append(raw.strip()[3:].strip())
            continue
        pending, doc = " ".join(d for d in doc if d), []
        line = _strip_comment(raw).strip()
        if current is None:
            m = _BLOCK.match(line)
            if m:
                current = {"kind": m.group(1), "name": m.group(2), "start": number,
                           "fields": [], "attrs": [], "values": [], "doc": pending}
            continue
        if line == "}":
            current["end"] = number
            if current["kind"] == "model":
                models[current["name"]] = current
            else:
                enums[current["name"]] = current
            current = None
            continue
        if not line:
            continue
        if current["kind"] == "enum":
            m = re.match(r"^(\w+)", line)
            if m and not line.startswith("@@"):
                mapped = re.search(r'@map\("([^"]+)"\)', line)  # the stored value
                current["values"].append(mapped.group(1) if mapped else m.group(1))
            continue
        if line.startswith("@@"):
            current["attrs"].append(line)
            continue
        m = _FIELD.match(line)
        if m:
            current["fields"].append({"name": m.group(1), "type": m.group(2),
                                      "modifier": m.group(3) or "", "attrs": m.group(4),
                                      "line": number, "doc": pending})
    return {"models": models, "enums": enums}


def prisma_facts(text: str, tables: Iterable[str], commit: str) -> dict[str, Any]:
    """Facts for the models mapped to ``tables`` (Prisma ``@@map`` names)."""
    parsed = parse_prisma(text)
    models, enums = parsed["models"], parsed["enums"]
    table_of = {}
    for name, model in models.items():
        mapped = next((re.search(r'@@map\("([^"]+)"\)', a) for a in model["attrs"]
                       if a.startswith("@@map(")), None)
        table_of[name] = mapped.group(1) if mapped else name
    wanted = set(tables)
    out_models: dict[str, Any] = {}
    other_models: dict[str, Any] = {}
    used_enums: set[str] = set()
    for name, model in sorted(models.items()):
        if table_of[name] not in wanted:
            other_models[name] = {"table": table_of[name],
                                  "source": {"lines": f"{model['start']}-{model['end']}"}}
            columns = [(m.group(1) if m else f["name"]) for f in model["fields"]
                       if f["type"] not in models
                       for m in [re.search(r'@map\("([^"]+)"\)', f["attrs"])]]
            credentials = sorted(c for c in columns if CREDENTIAL_COLUMN.search(gen.snake(c)))
            if credentials:
                other_models[name]["credential_columns"] = credentials
            # Upstream's own notes (append-only tables, one-party CHECKs) travel with the
            # not_served type, so a reader sees them before the relation is ever served.
            # Quoted upstream text: kept under the provenance key ``notes``.
            notes = {"model": model["doc"]} if model["doc"] else {}
            columns_doc = {(m.group(1) if m else f["name"]): f["doc"] for f in model["fields"]
                           if f["doc"] and f["type"] not in models
                           for m in [re.search(r'@map\("([^"]+)"\)', f["attrs"])]}
            if columns_doc:
                notes["columns"] = columns_doc
            if notes:
                other_models[name]["notes"] = notes
            continue
        columns: dict[str, Any] = {}
        column_of_field: dict[str, str] = {}
        relations, back = [], []
        unique: list[list[str]] = []
        ident: list[str] = []
        for f in model["fields"]:
            ftype = f["type"]
            if ftype in models:
                rel_args = _attr_args(f["attrs"], "@relation")
                rel_name = None
                if rel_args:
                    nm = re.match(r'\s*"([^"]+)"', rel_args) or re.search(
                        r'name\s*:\s*"([^"]+)"', rel_args)
                    rel_name = nm.group(1) if nm else None
                fields = _list_arg(rel_args, "fields") if rel_args else []
                if fields:
                    relations.append({"field": f["name"], "target": ftype,
                                      "target_table": table_of[ftype], "fields": fields,
                                      "references": _list_arg(rel_args, "references"),
                                      "name": rel_name, "optional": f["modifier"] == "?",
                                      "on_delete": (re.search(r"onDelete\s*:\s*(\w+)",
                                                              rel_args) or [None, None])[1],
                                      "source": {"line": f["line"]}})
                else:
                    back.append({"field": f["name"], "target": ftype, "name": rel_name,
                                 "list": f["modifier"] == "[]"})
                continue
            mapped = re.search(r'@map\("([^"]+)"\)', f["attrs"])
            column = mapped.group(1) if mapped else f["name"]
            column_of_field[f["name"]] = column
            db = re.search(r"@db\.(\w+)", f["attrs"])
            base = re.match(r"\w+", ftype).group(0)
            if base in enums:
                used_enums.add(base)
            columns[column] = {"field": f["name"], "type": base,
                               "list": f["modifier"] == "[]", "optional": f["modifier"] == "?",
                               "db": db.group(1) if db else None}
            if re.search(r"(?<!@)@id\b", f["attrs"]):
                ident = [column]
            if re.search(r"@unique\b", f["attrs"]):
                unique.append([column])
        for attr in model["attrs"]:
            for kind in ("@@id", "@@unique"):
                if attr.startswith(kind + "("):
                    cols = [column_of_field.get(c, c) for c in
                            re.findall(r"\w+", (re.match(re.escape(kind) + r"\(\s*\[([^\]]*)\]",
                                                         attr) or [None, ""])[1])]
                    if kind == "@@id":
                        ident = cols
                    elif cols:
                        unique.append(cols)
        for rel in relations:
            rel["columns"] = [column_of_field.get(c, c) for c in rel.pop("fields")]
        out_models[name] = {"table": table_of[name],
                            "source": {"lines": f"{model['start']}-{model['end']}"},
                            "columns": columns, "id": ident, "unique": unique,
                            "relations": relations, "back_relations": back}
    # Referenced columns of relation targets (needed for link pairs).
    for model in out_models.values():
        for rel in model["relations"]:
            target = models[rel["target"]]
            cmap = {}
            for f in target["fields"]:
                if f["type"] in models:
                    continue
                mapped = re.search(r'@map\("([^"]+)"\)', f["attrs"])
                cmap[f["name"]] = (mapped.group(1) if mapped else f["name"],
                                   re.match(r"\w+", f["type"]).group(0))
            rel["reference_columns"] = [cmap.get(r, (r, None))[0] for r in rel["references"]]
            rel["reference_types"] = [cmap.get(r, (r, None))[1] for r in rel["references"]]
    return {"format": PRISMA_FORMAT, "repo": PORTAL, "path": PRISMA_PATH, "commit": commit,
            "models": out_models,
            "enums": {name: enums[name]["values"] for name in sorted(used_enums)},
            "other_models": other_models}


# --------------------------------------------------------------- Portal facts

_PRISMA_TYPES = {"String": "text", "Int": "integer", "BigInt": "integer", "Float": "decimal",
                 "Decimal": "decimal", "Boolean": "boolean", "DateTime": "timestamp",
                 "Json": "json", "Bytes": "text"}


def _pg_value_type(format_type: str) -> str:
    if format_type in ("smallint", "integer", "bigint"):
        return "integer"
    if format_type.startswith("numeric") or format_type in ("real", "double precision"):
        return "decimal"
    if format_type.startswith("timestamp"):
        return "timestamp"
    if format_type == "date":
        return "date"
    if format_type == "boolean":
        return "boolean"
    if format_type in ("json", "jsonb"):
        return "json"
    return "text"


def _value_type(column: str, model: Mapping[str, Any] | None, enums: Mapping[str, list],
                column_types: Mapping[str, str]) -> tuple[str, list[str] | None]:
    info = model["columns"].get(column) if model else None
    if info is not None:
        if info["list"]:
            return "json", None
        if info["type"] in enums:
            values = enums[info["type"]]
            if 1 <= len(values) <= 64 and all(1 <= len(v) <= 64 for v in values):
                return "enum", list(values)
            return "text", None
        if info["type"] == "DateTime" and info["db"] == "Date":
            return "date", None
        return _PRISMA_TYPES.get(info["type"], "text"), None
    if column in column_types:
        return _pg_value_type(column_types[column]), None
    return "text", None


def _evidence_prisma(prisma: Mapping[str, Any], model_name: str, note: str | None,
                     lines: str | None = None) -> dict[str, Any]:
    out = {"kind": "upstream_code", "repo": PORTAL, "path": prisma["path"],
           "commit": prisma["commit"], "symbol": model_name,
           "lines": lines or prisma["models"][model_name]["source"]["lines"]}
    if note is not None:
        out["note"] = note[:2000]
    return out


_CITATION = re.compile(r"^(?P<repo>[^:]+):(?P<path>[^@]+)@(?P<commit>[0-9a-f]{7,40})"
                       r"(?::(?P<line>[1-9][0-9]*))?$")
ROUTE_EVIDENCE = 3   # route citations per route-read relation


def route_reads(registry: Mapping[str, Any] | None) -> dict[str, list[Mapping[str, Any]]]:
    """Relation -> the registry's Portal API routes whose SQL names it (its route scan)."""
    out: dict[str, list[Mapping[str, Any]]] = {}
    for entry in (registry or {}).get("entries", ()):
        if entry["kind"] == "api_route":
            for relation in entry.get("relations", ()):
                out.setdefault(relation, []).append(entry)
    return out


def _evidence_route(entry: Mapping[str, Any], relation: str) -> dict[str, Any]:
    m = _CITATION.match(entry["evidence"][0])
    if m is None or m.group("repo") != PORTAL:
        raise ValueError(f"registry entry {entry['id']}: not a Portal citation: "
                         f"{entry['evidence'][0]!r}")
    out = {"kind": "upstream_code", "repo": PORTAL, "path": m.group("path"),
           "commit": m.group("commit"),
           "note": (f"Route {entry['name']} reads {relation} (source registry route scan: "
                    "the route file or a first-party module it imports names it in SQL).")}
    if m.group("line"):
        out["lines"] = m.group("line")
    return out


def portal_facts(catalog: Mapping[str, Any], prisma: Mapping[str, Any],
                 declared: Mapping[str, Mapping[str, str]] | None = None,
                 registry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The generator facts (``ontology/generator/build.py``) of the Portal reporting API:
    its catalog relations typed from Prisma (else the catalog's column types), the
    Prisma foreign keys, and the unserved relations (Prisma models outside the catalog,
    catalog relations the API does not serve, relations a ``registry`` API route reads
    that the catalog does not list); ``declared``: relation -> the curated declaration
    (``object_type``, ``file``) that already stands for one."""
    parsed = ontology_schema.catalog_from_json(catalog)
    commit = catalog["source"]["commit"]
    live = catalog.get("evidence_mode") == "live-read"
    model_by_table = {m["table"]: name for name, m in prisma["models"].items()}
    enums = prisma.get("enums", {})
    entries = {e["name"]: e for e in catalog["relations"]}
    relations = []
    for name in sorted(parsed.relations):
        rel = parsed.relations[name]
        domain = entries[name].get("domain")
        if not rel.served:
            relations.append({"name": name, "domain": domain, "columns": [
                {"name": c, "value_type": "text"} for c in rel.columns],
                "excluded": f"not served by the reporting API: {rel.unavailable or 'no key'}"})
            continue
        table = name.split(".", 1)[1]
        model_name = model_by_table.get(table) if name.startswith("public.") else None
        model = prisma["models"].get(model_name) if model_name else None
        column_types = entries[name].get("column_types", {})
        key_source = entries[name].get("key_source")
        evidence = [{"kind": "upstream_code", "repo": PORTAL, "path": MANIFEST_PATH,
                     "commit": commit, "note": f"{name} is an approved reporting-API relation "
                                               f"(manifest domain {domain or 'unassigned'})."}]
        if live:
            evidence.append({"kind": "live_trace", "ref": catalog["source"]["live_ref"][:200],
                             "note": "Relation listed by GET /v1/relations."})
        if model is not None:
            evidence.append(_evidence_prisma(prisma, model_name,
                                             f'model {model_name} @@map("{table}")'))
        columns = []
        for column in rel.columns:
            vtype, values = _value_type(column, model, enums, column_types)
            columns.append({"name": column, "value_type": vtype, "enum_values": values})
        relations.append({
            "name": name, "domain": domain, "excluded": None, "key": list(rel.key),
            "key_source": key_source, "columns": columns,
            "description": (f"Generated raw type: one record of the Portal reporting-API "
                            f"relation {name}" +
                            (f" (Prisma model {model_name})" if model_name else "") +
                            ". Raw source records with every approved column; curated types "
                            "in ontology/schema/domains/ shadow it."),
            "evidence": evidence,
            "key_evidence": {"kind": "upstream_code", "repo": PORTAL, "path": CATALOG_CODE_PATH,
                             "commit": commit, "symbol": "chooseKey",
                             "note": f"Pagination key {list(rel.key)} (key source: "
                                     f"{key_source or 'not recorded'})."},
            "record_evidence": _evidence_prisma(prisma, model_name, None) if model else None,
        })
    links = []
    for model_name in sorted(prisma["models"]):
        model = prisma["models"][model_name]
        unique_cols = [sorted(u) for u in model["unique"]] + [sorted(model["id"])]
        for rel in sorted(model["relations"], key=lambda r: r["field"]):
            cols, refs = rel["columns"], rel["reference_columns"]
            # back relations of a served target are in the facts; others are not recorded.
            back = [b for b in prisma["models"].get(rel["target"], {}).get("back_relations", [])
                    if b["target"] == model_name and b["name"] == rel["name"]]
            src, dst = "public." + model["table"], "public." + rel["target_table"]
            links.append({
                "from": src, "to": dst, "field": rel["field"], "columns": cols,
                "references": refs,
                "compares": ["integer_id" if t in ("Int", "BigInt") else "text_id"
                             for t in rel["reference_types"]],
                "unique": sorted(cols) in unique_cols,
                "reverse": gen.snake(back[0]["field"]) if len(back) == 1 else None,
                "description": (f"Prisma foreign key {model_name}.{rel['field']} "
                                f"({src}.{', '.join(cols)} -> {dst}.{', '.join(refs)})."),
                "evidence": [_evidence_prisma(prisma, model_name,
                                              f"@relation(fields: [{', '.join(cols)}], "
                                              f"references: [{', '.join(refs)}])",
                                              str(rel["source"]["line"]))],
            })
    unserved = []
    by_table = {**{m["table"]: (n, None) for n, m in prisma["models"].items()},
                **{m["table"]: (n, m["source"]["lines"])
                   for n, m in prisma.get("other_models", {}).items()}}
    excluded = {r["name"]: r["excluded"] for r in relations if r["excluded"]}
    for other in prisma.get("other_models", {}).values():
        excluded.setdefault("public." + other["table"], None)
    reads = route_reads(registry)
    for name in [*(declared or {}), *reads]:
        if name not in entries:   # a pending declaration or a route read outside Prisma
            excluded.setdefault(name, None)
    for name, why in sorted(excluded.items()):
        model_name, lines = by_table.get(name.split(".", 1)[1], (None, None)) \
            if name.startswith("public.") else (None, None)
        evidence = [{"kind": "upstream_code", "repo": PORTAL, "path": MANIFEST_PATH,
                     "commit": commit,
                     "note": (f"{name} is approved but {why}." if why else
                              f"{name} is not an approved reporting-API relation.")}]
        if model_name is not None:
            upstream = prisma.get("other_models", {}).get(model_name, {}).get("notes", {})
            notes = ([f"/// {upstream['model']}"] if upstream.get("model") else []) + [
                f"{column}: /// {doc}" for column, doc in upstream.get("columns", {}).items()]
            evidence.append(_evidence_prisma(prisma, model_name, f'model {model_name} @@map('
                                             f'"{name.split(".", 1)[1]}")'
                                             + "".join("; " + n for n in notes), lines))
        routes = reads.get(name, [])
        evidence += [_evidence_route(e, name) for e in routes[:ROUTE_EVIDENCE]]
        read = (f" is read by {len(routes)} Portal API route{'s' * (len(routes) != 1)} "
                "(source registry route scan) but" if routes and not model_name else "")
        unserved.append({
            "relation": name, "domain": not_served_domain(name), "upstream_name": model_name,
            "upstream_ask": UPSTREAM_ASK,
            "reason": (f"{name}" + (f" (Prisma model {model_name})" if model_name else "") +
                       (f" is approved but {why}" if why else
                        f"{read} is not an approved relation of the Portal reporting API "
                        f"({MANIFEST_PATH}@{commit})") +
                       ": no record of it is readable."),
            "description": (f"Generated not_served type: the Portal table {name}" +
                            (f" (Prisma model {model_name})" if model_name else "") +
                            ". No record is readable until the reporting API serves it "
                            f"(upstream ask {UPSTREAM_ASK}); a served column that "
                            "references it is a plain reference value, never a link."),
            "evidence": evidence, "declared": (declared or {}).get(name)})
    return {
        "source": "portal_api", "file_prefix": "portal_",
        "description": ("Generated by scripts/gen_ontology_skeleton.py from the Portal "
                        f"reporting-API catalog ({catalog['evidence_mode']}), Portal Prisma "
                        "and the Workbench link/unit contracts. Do not edit: curate in "
                        "ontology/schema/domains/ (a curated binding shadows the raw type)."),
        "upstream_pins": {PORTAL: prisma["commit"]},
        "relations": relations, "links": links, "unserved": unserved,
    }


# --------------------------------------------------------------- generate

def registry_read_digest(registry: Mapping[str, Any]) -> str:
    """Digest of the registry data this adapter reads (each API route's name, first
    citation, models and relations; each Prisma model's table), so a registry change it
    does not read leaves the generated files as they are."""
    read = {"routes": {e["id"]: [e["name"], e["evidence"][0], sorted(e.get("models", ())),
                                 sorted(e.get("relations", ()))]
                       for e in registry["entries"] if e["kind"] == "api_route"},
            "tables": {e["name"]: e["table"] for e in registry["entries"]
                       if e["kind"] == "prisma_model"}}
    text = json.dumps(read, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _credential_columns(prisma: Mapping[str, Any], relation: str) -> list[str] | None:
    """The credential columns of the Prisma model of ``relation``; None: no Prisma model,
    its columns are unknown."""
    table = relation.split(".", 1)[1] if relation.startswith("public.") else None
    for model in prisma["models"].values():
        if model["table"] == table:
            return sorted(c for c in model["columns"] if CREDENTIAL_COLUMN.search(gen.snake(c)))
    for model in prisma.get("other_models", {}).values():
        if model["table"] == table:
            return list(model.get("credential_columns", []))
    return None


def q3_ask(registry: Mapping[str, Any] | None, served: Iterable[str],
           not_served: Mapping[str, Mapping[str, Any]], prisma: Mapping[str, Any]
           ) -> dict[str, Any]:
    """The upstream ask Q3 ranked by what serving would complete: the registry's Portal API
    routes whose data (``scripts.source_registry.data_keys``) the reporting API does not
    fully serve (and a count of its unserved Prisma models). Greedy: each next relation
    completes the most routes still incomplete (ties: the most of them reading it, then
    the name), so the first k relations complete ``routes_completed_total`` routes (no
    other set of k is claimed to do better). A relation whose Prisma model stores
    credentials is flagged."""
    served = set(served)
    entries = (registry or {}).get("entries", [])
    tables = {e["name"]: e["table"] for e in entries if e["kind"] == "prisma_model"}
    reading: dict[str, int] = {}
    open_routes: list[set[str]] = []
    with_data = 0
    for entry in entries:
        if entry["kind"] != "api_route":
            continue
        keys = {relation for family, relation in data_keys(entry, tables)
                if family == "portal_api"}
        with_data += bool(keys)
        for relation in keys - served:
            reading[relation] = reading.get(relation, 0) + 1
        if keys - served:
            open_routes.append(keys - served)
    unknown = sorted(set(reading) - set(not_served))
    if unknown:   # every unserved relation a route reads is declared (portal_facts)
        raise ValueError("routes read relations no not_served type declares: "
                         + ", ".join(unknown))
    ranked: list[dict[str, Any]] = []
    total, remaining = 0, [set(r) for r in open_routes]
    while remaining and len(ranked) < ASK_TOP:
        last: dict[str, int] = {}
        still: dict[str, int] = {}
        for need in remaining:
            for relation in need:
                still[relation] = still.get(relation, 0) + 1
            if len(need) == 1:
                (only,) = need
                last[only] = last.get(only, 0) + 1
        relation = min(still, key=lambda r: (-last.get(r, 0), -still[r], r))
        for need in remaining:
            need.discard(relation)
        done = sum(1 for need in remaining if not need)
        remaining = [need for need in remaining if need]
        total += done
        ranked.append({"relation": relation,
                       "object_type": not_served[relation]["object_type"],
                       "routes_reading": reading[relation], "routes_completed": done,
                       "routes_completed_total": total,
                       "credential_columns": _credential_columns(prisma, relation)})
    unserved_models = sum(1 for e in entries if e["kind"] == "prisma_model"
                          and "public." + e["table"] not in served)
    return {
        "id": UPSTREAM_ASK,
        "ask": ("Approve a reporting-API manifest expansion: serve "
                "these relations, in this order; each completes the "
                "Portal API routes whose data is otherwise served."),
        "method": ("Greedy over the source registry's api_route data keys against the pinned "
                   "catalog: the next relation completes the most routes still incomplete "
                   "(ties: most routes reading it, then name). credential_columns: the Prisma "
                   "columns that store a credential (CREDENTIAL_COLUMN); an owner decides "
                   "their exclusion before the relation is served; null: no Prisma model, "
                   "columns unknown."),
        "routes": {"with_data": with_data, "fully_served": with_data - len(open_routes),
                   "need_unserved": len(open_routes)},
        "prisma_models_not_served": unserved_models,
        # The gap ledger's coverage gaps this ask records: verify/gaps.py gap_id of its
        # registry keys (not imported: the verify modules stay out of the product's graph).
        "gaps": {"gap-" + hashlib.sha256(f"coverage\0{key}".encode()).hexdigest()[:12]: key
                 for key in (
            "registry:portal.api_route:not_served", "registry:portal.prisma_model:not_served")},
        "relations_needed": len(reading),
        "ranked": ranked,
        "sensitive": [r["relation"] for r in ranked if r["credential_columns"]],
    }


def generate(catalog: Mapping[str, Any], prisma: Mapping[str, Any],
             link_contracts: Mapping[str, Any], unit_contracts: Mapping[str, Any],
             fm_source_fk: Mapping[str, tuple[str, str]],
             overrides: gen.Overrides | None = None,
             declared: Mapping[str, Mapping[str, str]] | None = None,
             registry: Mapping[str, Any] | None = None,
             ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ({file name: domain document}, coverage document). Deterministic.
    ``declared``, ``registry``: as ``portal_facts`` (default: none)."""
    overrides = overrides or OVERRIDES
    parsed = ontology_schema.catalog_from_json(catalog)
    docs, report = gen.generate(portal_facts(catalog, prisma, declared, registry),
                                link_contracts, unit_contracts, fm_source_fk, overrides)
    bound, excluded = report["type_of_relation"], report["excluded"]
    coverage = {
        "format": ontology_schema.COVERAGE_FORMAT,
        "catalog_digest": "sha256:" + parsed.digest,
        "catalog_evidence_mode": catalog["evidence_mode"],
        "inputs": {"catalog": catalog["source"],
                   "prisma": {"repo": PORTAL, "path": prisma["path"], "commit": prisma["commit"]},
                   "registry": ({"path": str(REGISTRY_PATH.relative_to(ROOT)),
                                 "read_digest": registry_read_digest(registry),
                                 "commit": registry["upstream"][PORTAL]["commit"][:12]}
                                if registry else None),
                   "workbench": ["operating/link_contracts.json",
                                 "ontology/relation_contracts.py:FM_SOURCE_FK",
                                 "operating/source_unit_contracts.json", *overrides.paths]},
        "counts": {"catalog_relations": len(parsed.relations),
                   "served_relations": len(parsed.served()),
                   "bound_relations": len(bound), "excluded_relations": len(excluded),
                   **report["counts"]},
        "relations": {name: {"object_type": bound[name], "binding": gen.binding_id(name)}
                      if name in bound else {"excluded": excluded[name]}
                      for name in sorted(parsed.relations)},
        "excluded_relations": dict(sorted(excluded.items())),
        "not_served": report["not_served"],
        "prisma_relations_not_bound": report["links_not_bound"],
        "link_contracts": report["link_contracts"],
        "fm_source_fk": report["fm_source_fk"],
        "unit_contracts": report["unit_contracts"],
        "money": report["money"],
        "upstream_ask": q3_ask(registry, parsed.served(), report["not_served"], prisma),
    }
    return docs, coverage


# --------------------------------------------------------------- IO

def dump(value: Any) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False) + "\n"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def pending_declarations(root: Path = ontology_schema.SCHEMA_DIR) -> dict[str, dict[str, str]]:
    """Portal relation -> the type a pending file binds it with (``object_type``, the
    pending ``file``): its curated not_served placeholder declares the relation."""
    out: dict[str, dict[str, str]] = {}
    for name, doc in ontology_schema.load_documents(root, include_pending=True):
        if not name.startswith("pending/"):
            continue
        for t in doc.get("object_types", []):
            for b in t.get("bindings", []):
                if b.get("source") == "portal_api":
                    out[b["relation"]] = {"object_type": t["id"], "file": name}
    return out


def build(catalog_path: Path = CATALOG_PATH, prisma_path: Path = PRISMA_FACTS_PATH
          ) -> dict[Path, str]:
    docs, coverage = generate(_read(catalog_path), _read(prisma_path), _read(LINK_CONTRACTS_PATH),
                              _read(UNIT_CONTRACTS_PATH), FM_SOURCE_FK,
                              declared=pending_declarations(), registry=load_registry())
    out = {GENERATED_DIR / name: dump(doc) for name, doc in docs.items()}
    out[COVERAGE_PATH] = dump(coverage)
    # The Graph generator's files (ontology/generator/graph.py), so --check covers them and
    # generate never removes them as unexpected.
    from ontology.generator import graph as graph_generator
    out.update({ontology_schema.SCHEMA_DIR / name: graph_generator.render(doc)
                for name, doc in graph_generator.build().items()})
    # The BC generator's files (ontology/generator/bc.py), likewise.
    from ontology.generator import bc as bc_generator
    out.update(bc_generator.build())
    return out


def portal_pin(pins_path: Path = ROOT / "verify" / "upstream-pins.json") -> str:
    """The Portal's one repo-level pin (the commit the source registry is generated at)."""
    from scripts.source_registry import repo_pins
    pins = repo_pins(_read(pins_path))
    if PORTAL not in pins:
        raise SystemExit(f"no resolved repo-level pin for {PORTAL} in {pins_path}")
    return pins[PORTAL]["commit"]


def read_portal(portal_repo: Path, commit: str, paths: Iterable[str]) -> dict[str, str]:
    """``paths`` of the Portal at ``commit`` (local git reads; every path must exist)."""
    from scripts.source_registry import Tree
    if not Tree.holds(portal_repo, commit):
        raise SystemExit(f"{portal_repo} lacks {commit[:12]} "
                         f"(git -C {portal_repo} fetch --depth 1 origin {commit})")
    paths = list(paths)
    files = Tree(PORTAL, portal_repo, commit).read(paths)
    missing = [p for p in paths if p not in files]
    if missing:
        raise SystemExit(f"{PORTAL}@{commit[:12]} lacks " + ", ".join(missing))
    return files


def refresh(portal_repo: Path | None = None, live_catalog: Path | None = None,
            commit: str | None = None) -> None:
    """Re-pin the inputs from the Portal at ``commit`` (default: its repo-level pin)."""
    from scripts.check_upstream_drift import checkout_for
    full = commit or portal_pin()
    portal_repo = portal_repo or checkout_for(PORTAL, {}, _read(ROOT / "verify" /
                                                                "upstream-pins.json"))
    files = read_portal(portal_repo, full, [PRISMA_PATH] + (
        [] if live_catalog is not None else [MANIFEST_PATH, CATALOG_FACTS_PATH]))
    commit = full[:12]
    if live_catalog is not None:
        catalog = catalog_from_live_dump(_read(live_catalog), str(live_catalog), commit)
    else:
        catalog = catalog_from_manifest(json.loads(files[MANIFEST_PATH]),
                                        json.loads(files[CATALOG_FACTS_PATH]), commit)
    tables = [e["relation"] for e in catalog["relations"] if e["schema"] == "public"]
    facts = prisma_facts(files[PRISMA_PATH], tables, commit)
    META_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(dump(catalog), encoding="utf-8")
    PRISMA_FACTS_PATH.write_text(dump(facts), encoding="utf-8")


def drift_live(environ: Mapping[str, str], *, client: Any = None) -> dict[str, Any]:
    """Drift of the committed schema against the live catalog.

    One ``GET /v1/relations`` through the GET-only reporting-API client; the result
    holds relation and column names, digests and counts, never row values."""
    if client is None:
        base_url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
        if not base_url or not key:
            raise LookupError("missing environment variables: PLX_WORKBENCH_API_URL, "
                              "PLX_WORKBENCH_API_KEY")
        from integrations.workbench_read_api import WorkbenchReadApiClient
        client = WorkbenchReadApiClient(base_url, key)
    live = ontology_schema.catalog_from_json({"relations": client.relations(),
                                              "evidence_mode": "live-read"})
    compiled = ontology_schema.load_schema()  # structure only; drift is against `live`
    report = ontology_schema.drift_report(compiled, live)
    # A bound relation the live catalog still lists but marks unavailable with an upstream
    # reason (e.g. a Sage full load recreated it before its column grants were restored:
    # Portal docs/runbooks/WORKBENCH-API.md "Column drift after an FM or Sage full load")
    # is an upstream outage: a blocked part, never a pass and never a rebinding. One the
    # catalog no longer lists, or lists unserved without a reason, is schema drift.
    blocked = [m for m in report["missing_relations"]
               if (rel := live.relations.get(m["relation"])) is not None and rel.unavailable]
    report.update({"probe": "schema-catalog-drift/v1",
                   "catalog_relation_count": len(live.relations),
                   "unbound_relation_count": len(report["unbound_relations"]),
                   "missing_relation_count": len(report["missing_relations"]),
                   "missing_unexplained_count": len(report["missing_relations"]) - len(blocked),
                   "blocked_upstream": [{"part": m["relation"], "source": "portal-workbench-api",
                                         "cause": "relation_unavailable", "reason": m["reason"]}
                                        for m in blocked],
                   "degraded_count": len(report["degraded"]),
                   "unbound_column_count": sum(map(len, report["unbound_columns"].values())),
                   "catalog_digest_prefix": report["catalog_digest"][:23],
                   "pinned_catalog_digest_prefix": (report["pinned_catalog_digest"] or "")[:23],
                   "schema_digest_prefix": report["schema_digest"][:23]})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command")
    r = sub.add_parser("refresh", help="re-pin inputs from the Portal at its repo-level pin, "
                                       "then generate")
    r.add_argument("--portal-repo", type=Path,
                   help="Portal checkout holding the pin (default: the pin file's checkout)")
    r.add_argument("--live-catalog", type=Path,
                   help="recorded GET /v1/relations body (taken by the live checks)")
    g = sub.add_parser("generate", help="generate from the pinned inputs (default)")
    g.add_argument("--check", action="store_true",
                   help="exit 1 if the committed skeleton differs from a regeneration")
    args = parser.parse_args(argv)
    if args.command == "refresh":
        refresh(args.portal_repo, args.live_catalog)
    outputs = build()
    if getattr(args, "check", False):
        stale = sorted(str(p.relative_to(ROOT)) for p, text in outputs.items()
                       if not p.is_file() or p.read_text(encoding="utf-8") != text)
        extra = sorted(str(p.relative_to(ROOT)) for p in GENERATED_DIR.glob("*.json")
                       if p not in outputs)
        # The one format check of the committed schema files (load_schema skips it).
        try:
            ontology_schema.load_schema(ontology_schema.SCHEMA_DIR, check_format=True)
            invalid: list[str] = []
        except ontology_schema.SchemaLoadError as exc:
            invalid = exc.errors
        if stale or extra or invalid:
            print(json.dumps({"ok": False, "stale": stale, "unexpected": extra,
                              "invalid": invalid}, indent=1))
            return 1
        print(json.dumps({"ok": True, "files": len(outputs)}))
        return 0
    for path in GENERATED_DIR.glob("*.json"):
        if path not in outputs:
            path.unlink()
    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    coverage = json.loads(outputs[COVERAGE_PATH])
    print(json.dumps({"ok": True, "files": len(outputs), "counts": coverage["counts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
