"""Generate ``operating/typed_graph/models.py`` from the ontology schema (and
``operating/app_tables/rows.py`` from the declared app stores, ``APP_STORES``).

One frozen dataclass per object type the domain documents declare
(``ontology/schema/domains/*.json``) plus every type a declared link of theirs reaches,
with the properties the compiled schema (``ontology.schema.load_schema``) serves for it,
typed by value type, and one accessor per declared link type on each end:

* the link's ``name`` on its ``from`` type (direction forward), its ``reverse_name`` on
  its ``to`` type (direction reverse);
* to-one (returns the object or None) where the cardinality allows one object on that
  side: forward of ``N:1`` and ``1:1``, reverse of ``1:N`` and ``1:1``; every other end
  returns a ``Related`` list.

Names: a property named like a base attribute (``object_id``, ``display``, ...) or a
Python keyword gets a ``_value`` suffix; an accessor named like a property, a base
attribute or another accessor of its type gets ``_<direction>`` (and then
``_<link type>``) added. Generation fails on any name it still cannot make unique, on
an unknown value type and on an unknown cardinality.

``--check`` exits 1 when the checked-in file differs (the test runs the same check).
"""
from __future__ import annotations

import argparse
import json
import keyword
from pathlib import Path
import re
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "operating" / "typed_graph" / "models.py"
APP_OUT = ROOT / "operating" / "app_tables" / "rows.py"
# The app stores with typed rows: approvals and write intents, sessions, work items.
APP_STORES = ("action_path", "confirmation_forms", "session_journal", "session_replay",
              "work_log", "autopilot_queue")
DOMAINS = ROOT / "ontology" / "schema" / "domains"

PY_TYPES = {
    "text": "str", "identifier": "str", "enum": "str", "money": "Money",
    "quantity": "Quantity", "decimal": "Decimal", "integer": "int", "boolean": "bool",
    "date": "dt.date", "timestamp": "dt.datetime", "json": "Any",
}
TO_ONE = {("N:1", "forward"), ("1:1", "forward"), ("1:N", "reverse"), ("1:1", "reverse")}
CARDINALITIES = {"N:1", "1:N", "1:1", "N:M"}
BASE_ATTRIBUTES = {"object_id", "display", "lineage", "values", "conflicts", "withheld",
                   "gaps", "complete", "values_of", "OBJECT_TYPE", "PROPERTIES", "LINKS"}


class GenerationError(RuntimeError):
    pass


def declared() -> tuple[list[str], list[str]]:
    """Object type ids and link type ids the domain documents declare, in file order."""
    types, links = [], []
    for path in sorted(DOMAINS.glob("*.json")):
        doc = json.loads(path.read_text("utf-8"))
        types.extend(t["id"] for t in doc.get("object_types", ()))
        links.extend(link["id"] for link in doc.get("link_types", ()))
    return types, links


def _snake(text: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", text).lower().replace(".", "_")


def build(object_types: Mapping[str, Mapping[str, Any]],
          link_types: Mapping[str, Mapping[str, Any]],
          type_ids: list[str], link_ids: list[str]) -> list[dict[str, Any]]:
    links = [link_types[i] for i in link_ids]
    for link in links:
        if link.get("cardinality") not in CARDINALITIES:
            raise GenerationError(f"cardinality_unknown:{link['id']}:{link.get('cardinality')}")
    wanted = list(dict.fromkeys(type_ids + [end for link in links
                                            for end in (link["from"], link["to"])]))
    models = []
    for tid in sorted(wanted):
        spec = object_types.get(tid)
        if spec is None:
            raise GenerationError(f"object_type_not_served:{tid}")
        props = []
        for prop in spec.get("properties", ()):
            kind = prop["value_type"]
            if kind not in PY_TYPES:
                raise GenerationError(f"value_type_unknown:{tid}.{prop['name']}:{kind}")
            name = prop["name"]
            field_name = name + "_value" if (name in BASE_ATTRIBUTES or keyword.iskeyword(name)
                                             or name.startswith("_")) else name
            props.append({"name": name, "field": field_name, "value_type": kind,
                          "enum_values": tuple(prop.get("enum_values") or ())})
        models.append({"id": tid, "props": props, "accessors": []})
    by_id = {m["id"]: m for m in models}
    for link in links:
        for direction, owner, target, name in (
                ("forward", link["from"], link["to"], link["name"]),
                ("reverse", link["to"], link["from"], link["reverse_name"])):
            by_id[owner]["accessors"].append({
                "base": name, "link_type": link["id"], "direction": direction,
                "target": target, "to_one": (link["cardinality"], direction) in TO_ONE})
    for model in models:
        taken = {p["field"] for p in model["props"]} | BASE_ATTRIBUTES
        counts: dict[str, int] = {}
        for acc in model["accessors"]:
            counts[acc["base"]] = counts.get(acc["base"], 0) + 1
        for acc in sorted(model["accessors"], key=lambda a: (a["link_type"], a["direction"])):
            candidates = [acc["base"]] if counts[acc["base"]] == 1 else []
            candidates += [f"{acc['base']}_{acc['direction']}",
                           f"{acc['base']}_{_snake(acc['link_type'])}_{acc['direction']}"]
            for name in candidates:
                if name not in taken and not keyword.iskeyword(name):
                    acc["method"] = name
                    taken.add(name)
                    break
            else:
                raise GenerationError(f"accessor_name_clash:{model['id']}.{acc['base']}")
        model["accessors"].sort(key=lambda a: a["method"])
    return models


def render(models: list[dict[str, Any]], source_note: str) -> str:
    out = [
        '"""Typed models of the ontology\'s object types: GENERATED by tools/gen_models.py',
        f"from {source_note}. Do not edit by hand; see operating/typed_graph/runtime.py.",
        '"""',
        "# flake8: noqa",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "import datetime as dt",
        "from decimal import Decimal",
        "from typing import Any, ClassVar, Mapping, Optional",
        "",
        "from operating.typed_graph.runtime import (",
        "    Cited, GraphModel, LinkSpec, Money, PropertySpec, Quantity, Related)",
        "",
    ]
    for model in models:
        tid = model["id"]
        out += ["", "@dataclass(frozen=True, kw_only=True)", f"class {tid}(GraphModel):",
                f'    OBJECT_TYPE: ClassVar[str] = "{tid}"',
                "    PROPERTIES: ClassVar[Mapping[str, PropertySpec]] = {"]
        for prop in model["props"]:
            enum = f", {prop['enum_values']!r}" if prop["enum_values"] else ", ()"
            out.append(f'        "{prop["name"]}": PropertySpec("{tid}", "{prop["name"]}", '
                       f'"{prop["value_type"]}"{enum}, "{prop["field"]}"),')
        out.append("    }")
        for prop in model["props"]:
            out.append(f"    {prop['field']}: Optional[Cited[{PY_TYPES[prop['value_type']]}]]"
                       " = None")
        for acc in model["accessors"]:
            spec = (f'LinkSpec("{acc["link_type"]}", "{acc["direction"]}", '
                    f'"{acc["target"]}", {acc["to_one"]})')
            if acc["to_one"]:
                out += ["", f"    def {acc['method']}(self) -> Optional[{acc['target']}]:",
                        f'        """{acc["link_type"]} ({acc["direction"]}): at most one."""',
                        f"        return self._one({spec})  # type: ignore[return-value]"]
            else:
                out += ["", f"    def {acc['method']}(self) -> Related[{acc['target']}]:",
                        f'        """{acc["link_type"]} ({acc["direction"]})."""',
                        f"        return self._many({spec})"]
    out += ["", "", "MODELS: dict[str, type[GraphModel]] = {"]
    out += [f'    "{m["id"]}": {m["id"]},' for m in models]
    out += ["}", ""]
    return "\n".join(out)


def _camel(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


def _py_type(declared: str) -> str:
    text = declared.upper()
    if "INT" in text:
        return "int"
    if any(t in text for t in ("CHAR", "CLOB", "TEXT")):
        return "str"
    if "BLOB" in text:
        return "bytes"
    if any(t in text for t in ("REAL", "FLOA", "DOUB")):
        return "float"
    raise GenerationError(f"sqlite_type_unsupported:{declared}")


def generate_app_rows() -> str:
    import importlib
    import sqlite3
    sys.path.insert(0, str(ROOT / "tools"))
    import gen_prisma
    out = [
        '"""Typed rows of the app tables: GENERATED by tools/gen_models.py from',
        "operating/declared_schema (" + ", ".join(APP_STORES) + "). Do not edit by hand;",
        'see operating/app_tables/__init__.py."""',
        "# flake8: noqa",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "from typing import ClassVar, Optional",
        "",
        "from operating.app_tables import AppRow, Table",
        "",
    ]
    tables = []
    for store in APP_STORES:
        module = importlib.import_module(f"operating.declared_schema.{store}")
        conn = sqlite3.connect(":memory:")
        for _version, _name, ddl in module.STEPS:
            for statement in gen_prisma._flat(ddl):
                conn.executescript(statement)
        relations = gen_prisma.introspect_sqlite(conn)
        conn.close()
        for name in sorted(relations):
            rel = relations[name]
            if rel["kind"] != "table":
                continue
            cls = _camel(store) + _camel(name)
            out += ["", "@dataclass(frozen=True)", f"class {cls}(AppRow):",
                    f'    TABLE: ClassVar[str] = "{name}"',
                    f'    STORE: ClassVar[str] = "{module.STORE}"']
            for column in rel["columns"]:
                if column["hidden"]:
                    continue
                kind = _py_type(column["type"])
                rowid = rel["primary_key"] == [column["name"]] and \
                    column["type"].upper() == "INTEGER"
                typed = kind if column["not_null"] or rowid else f"Optional[{kind}]"
                out.append(f"    {column['name']}: {typed}")
            tables.append((store, name, cls))
    out += ["", ""]
    for store, name, cls in tables:
        out.append(f"{store.upper()}_{name.upper()}: Table[{cls}] = Table({cls})")
    out.append("")
    return "\n".join(out)


def generate() -> str:
    from ontology import schema as loader
    compiled = loader.load_schema()
    type_ids, link_ids = declared()
    models = build(compiled.object_types, compiled.link_types, type_ids, link_ids)
    accessors = sum(len(m["accessors"]) for m in models)
    note = (f"ontology/schema/domains ({len(type_ids)} declared object types, "
            f"{len(link_ids)} link types; {len(models)} models with the types those links "
            f"reach, {accessors} accessors)")
    return render(models, note)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        outputs = {OUT: generate(), APP_OUT: generate_app_rows()}
    except GenerationError as exc:
        print(f"generation failed: {exc}", file=sys.stderr)
        return 2
    status = 0
    for path, text in outputs.items():
        current = path.read_text("utf-8") if path.exists() else None
        if current == text:
            continue
        if args.check:
            print(f"{path.relative_to(ROOT)} is out of date: run python3 tools/gen_models.py",
                  file=sys.stderr)
            status = 1
            continue
        path.write_text(text, "utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return status


if __name__ == "__main__":
    sys.exit(main())
