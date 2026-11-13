"""Schema changes applied in place: a migration layer instead of a generation build.

``docs/no-builds.md`` has the design. In short:

* :func:`diff` compares the served schema with the new one unit by unit
  (``ontology.project_incremental.plan``): a unit whose schema slice
  (``ontology.build_keys.unit_slice``, everything of the schema its rows can read) differs,
  or that only the new schema has, is *affected*; a link unit only the served schema has is
  *dropped*. Equal slices give equal rows, so nothing else is re-derived. What a unit
  rewrite cannot express is refused with a reason (never guessed): a binding that changes
  type or goes away (object ids move, and step links name them), a property whose
  ``identifier_kind`` changes (mentions and twins resolve identifiers store-wide), and an
  affected type with ``post_project`` properties (a build step writes those).
* :func:`build_layer` projects only the affected units, under the new schema, from the
  capture sources (read-only), with the one projector (a partial ``Projector``:
  ``scope`` = their dependency bindings, ``emit`` = the units), into a store of the
  generation DDL, and records in its ``schema_migration`` table what the Postgres apply
  needs: both digests, the id prefixes of the affected bindings, the affected and dropped
  link units, the event ids' parts under the served schema (to delete the served rows)
  and the Postgres delta mark the snapshot began at (:mod:`operating.pg.ontology_migrate`).

``python3 -m ontology.schema_migrate diff --from-ref <commit>`` prints the plan of moving
from the schema at ``<commit>`` to the working tree's.

Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence

FORMAT = "plx-schema-migration/v1"
TABLE = "schema_migration"


def _get(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _props(t: Mapping[str, Any]) -> dict[str, Any]:
    return {p["name"]: p for p in t.get("properties", ()) if isinstance(p, Mapping)}


def _event_parts(schema: Any, binding: str, tid: str) -> list[list[str]]:
    """(event type, part) of every event a version of ``binding`` can yield
    (``project_incremental._delete_unit``)."""
    out = []
    for eid, ev in sorted((_get(schema, "event_types") or {}).items()):
        if ev.get("object_type") != tid:
            continue
        if ev["time_basis"] == "capture_observed":
            out.append([eid, ev["observed_property"]])
        elif ev["time"]["binding"] == binding:
            out.append([eid, ev["time"]["path"]])
    return out


def _changes(old: Any, new: Any) -> dict[str, Any]:
    """What changed, by name: types, their properties (and markings), link and event types."""
    out: dict[str, Any] = {}
    otypes, ntypes = dict(_get(old, "object_types") or {}), dict(_get(new, "object_types") or {})
    for tid in sorted(set(otypes) | set(ntypes)):
        a, b = otypes.get(tid), ntypes.get(tid)
        if a == b:
            continue
        if a is None or b is None:
            out.setdefault("types", {})[tid] = "added" if a is None else "removed"
            continue
        entry: dict[str, Any] = {}
        pa, pb = _props(a), _props(b)
        for name in sorted(set(pa) | set(pb)):
            if pa.get(name) != pb.get(name):
                entry.setdefault("properties", {})[name] = (
                    "added" if name not in pa else "removed" if name not in pb else
                    "markings" if {**pa[name], "markings": None} == {**pb[name], "markings": None}
                    else "changed")
        if sorted(a.get("markings", ())) != sorted(b.get("markings", ())):
            entry["markings"] = [sorted(a.get("markings", ())), sorted(b.get("markings", ()))]
        rest = [k for k in sorted(set(a) | set(b)) if k not in ("properties", "markings")
                and a.get(k) != b.get(k)]
        if rest:
            entry["changed"] = rest
        out.setdefault("types", {})[tid] = entry or "changed"
    for kind in ("link_types", "event_types"):
        oa, ob = dict(_get(old, kind) or {}), dict(_get(new, kind) or {})
        for key in sorted(set(oa) | set(ob)):
            if oa.get(key) != ob.get(key):
                out.setdefault(kind, {})[key] = ("added" if key not in oa else "removed"
                                                 if key not in ob else "changed")
    return out


def binding_marks(schema: Any, bid: str) -> dict[str, Any]:
    """What a reader of a text field of an object of binding ``bid`` must hold besides its
    version's markings (``Projector._marks_for_path``): the type's markings and, per source
    path of the binding, its properties' (``paths``); ``all``: every marking the binding's
    rows can carry; ``text_paths``: the paths a text value is projected from (a text field
    the projection holds as a value is not indexed again: ``ontology.text_index``)."""
    for tid, t in sorted((_get(schema, "object_types") or {}).items()):
        if not any(b.get("id") == bid for b in t.get("bindings", ())):
            continue
        type_marks = sorted(set(t.get("markings", ())))
        paths: dict[str, set[str]] = {}
        text_paths: set[str] = set()
        for p in t.get("properties", ()):
            for src in p.get("sources", ()):
                if src.get("binding") != bid or not isinstance(src.get("path"), str):
                    continue
                paths.setdefault(src["path"], set()).update(p.get("markings", ()))
                if p.get("value_type") == "text":
                    text_paths.add(src["path"])
        every = set(type_marks).union(*paths.values()) if paths else set(type_marks)
        return {"type": type_marks, "paths": {k: sorted(v) for k, v in sorted(paths.items())},
                "all": sorted(every), "text_paths": sorted(text_paths)}
    return {"type": [], "paths": {}, "all": [], "text_paths": []}


def _added_marks(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """Every marking a reader of some path of the binding must newly hold (the type's, or a
    path's property's): what a copy of its text (a search row) must gain."""
    def at(marks: Mapping[str, Any], path: str | None) -> set[str]:
        return set(marks["type"]) | set(marks["paths"].get(path, ()) if path else ())
    added = at(new, None) - at(old, None)
    for path in new["paths"]:
        added |= at(new, path) - at(old, path)
    return sorted(added)


def diff(served: Any, new: Any, *, force: Any = (), present: Any = None,
         specs: Any = None) -> dict[str, Any]:
    """The migration plan from ``served`` to ``new`` (module docstring). ``force``: units
    affected whatever their slices (their derivation code changed:
    ``ontology.step_migrate.plan``). ``present``: the build steps this tree runs (None:
    ``ontology.step_migrate.present_steps``); a step whose declared inputs the affected
    units touch must run again (``steps_required``; refused ``step_input_changed`` unless a
    step migration covers it, ``ontology.step_migrate.plan_schemas``)."""
    from ontology import build_keys
    from ontology import step_migrate
    from ontology.project import POST_PROJECT
    from ontology.project_incremental import plan

    old_plan, new_plan = plan(served), plan(new)
    out: dict[str, Any] = {
        "format": FORMAT, "from": _get(served, "digest"), "to": _get(new, "digest"),
        "changes": _changes(served, new), "refused": []}
    out["id"] = _sha([out["from"], out["to"], sorted(force)])[:16]
    if not (old_plan.acyclic and new_plan.acyclic):
        out["refused"].append("cyclic_lineage")
        return out
    op, np_ = build_keys.SchemaParts(served), build_keys.SchemaParts(new)
    old_slices = {u: build_keys.unit_slice(op, u, d) for u, d in old_plan.units.items()}
    new_slices = {u: build_keys.unit_slice(np_, u, d) for u, d in new_plan.units.items()}
    force = set(force)
    affected = sorted(u for u in new_slices
                      if old_slices.get(u) != new_slices[u] or u in force)
    dropped = sorted(set(old_slices) - set(new_slices))
    out["units"] = {"total": len(new_slices), "affected": affected, "dropped": dropped}
    # the plan's id: what it rewrites (a code-only migration forcing other units differs);
    # a layer's id adds its content and a sequence (:func:`build_layer`)
    out["id"] = _sha([out["from"], out["to"], affected, dropped, sorted(force)])[:16]

    for bid, tid in sorted(op.binding_type.items()):
        if bid not in np_.binding_type:
            out["refused"].append(f"binding_removed:{bid}")
        elif np_.binding_type[bid] != tid:
            out["refused"].append(f"binding_changes_type:{bid}")
    otypes, ntypes = dict(_get(served, "object_types") or {}), dict(_get(new, "object_types") or {})
    touched = {new_plan.rows[u][1] for u in affected if new_plan.rows[u][0] == "binding"}
    for tid in sorted({new_plan.bindings[b][0] for b in touched}):
        pa, pb = _props(otypes.get(tid) or {}), _props(ntypes[tid])
        for name in sorted(set(pa) | set(pb)):
            if (pa.get(name) or {}).get("identifier_kind") != \
                    (pb.get(name) or {}).get("identifier_kind"):
                out["refused"].append(f"identifier_kind_changes:{tid}.{name}")
            if any(s.get("parser") == POST_PROJECT for p in (pa.get(name), pb.get(name)) if p
                   for s in p.get("sources", ())):
                out["refused"].append(f"post_project_property:{tid}.{name}")

    # a link type whose links a build step writes (a mention, sender or document link:
    # no unit holds them) is the step's to re-derive: a step migration, not this one
    from ontology.project import _PROJECTED_LINK_KINDS
    olinks, nlinks = dict(_get(served, "link_types") or {}), dict(_get(new, "link_types") or {})
    for lid in sorted(set(olinks) | set(nlinks)):
        if olinks.get(lid) == nlinks.get(lid):
            continue
        kinds = {lb.get("kind") for link in (olinks.get(lid), nlinks.get(lid)) if link
                 for lb in link.get("bindings", ())}
        stepped = sorted(k for k in kinds - set(_PROJECTED_LINK_KINDS) if k)
        if stepped:
            out["refused"].append(f"step_link_type:{lid}:{'+'.join(stepped)}")

    def prefix(the_plan: Any, bid: str) -> str:
        return f"{the_plan.bindings[bid][0]}:{bid}:"

    bindings = {}
    for unit in affected:
        if new_plan.rows[unit][0] == "binding":
            bid = new_plan.rows[unit][1]
            if bid in old_plan.bindings:   # its served rows go (old ids, old events)
                old_marks, new_marks = binding_marks(served, bid), binding_marks(new, bid)
                bindings[bid] = {"prefix": prefix(old_plan, bid),
                                 "type": old_plan.bindings[bid][0],
                                 "events": _event_parts(served, bid,
                                                        old_plan.bindings[bid][0]),
                                 # the text index's markings of its objects, recomputed
                                 # at the apply (``ontology_migrate._text_index``)
                                 "marks": {k: new_marks[k] for k in ("type", "paths")},
                                 "added_marks": _added_marks(old_marks, new_marks),
                                 "text_paths_changed":
                                     old_marks["text_paths"] != new_marks["text_paths"]}
    links = []
    for unit in sorted(set(affected) | set(dropped)):
        rows = (old_plan.rows if unit in old_plan.rows else new_plan.rows)[unit]
        if rows[0] != "links":
            continue
        _, lid, frm, to, kind = rows
        the_plan = old_plan if frm in old_plan.bindings and to in old_plan.bindings \
            else new_plan
        links.append({"unit": unit, "link_type": lid, "kind": kind,
                      "from": prefix(the_plan, frm), "to": prefix(the_plan, to)})
    out["delete"] = {"bindings": bindings, "links": links}
    # every object whose rows a delta layer may hold under the served schema and the
    # migration rewrites: the affected bindings' and the ends of the affected link units
    out["scope"] = sorted({b["prefix"] for b in bindings.values()}
                          | {e for link in links for e in (link["from"], link["to"])})
    out["deps"] = sorted(set().union(*(new_plan.units[u] for u in affected))) \
        if affected else []
    # the object types whose rows (objects, identifiers, values, versions, links) change:
    # a build step reading one runs again over them (a mention resolves identifiers, a
    # twin reads values, a step link names objects a filter may drop)
    touched: set[str] = set()
    touched_links: set[tuple[str, str]] = set()
    for unit in sorted(set(affected) | set(dropped)):
        for the_plan in (old_plan, new_plan):
            rows = the_plan.rows.get(unit)
            if rows is None:
                continue
            # by the unit's kind of rows (``project_incremental.plan``): a gaps-only
            # link unit (a link binding no projector unit writes) holds no rows; a kind
            # this diff does not know is refused, never guessed
            if rows[0] in ("binding", "shard"):
                touched.add(the_plan.bindings[rows[1]][0])
            elif rows[0] == "links":
                if rows[2] in the_plan.bindings and rows[3] in the_plan.bindings:
                    touched_links.add((the_plan.bindings[rows[2]][0],
                                       the_plan.bindings[rows[3]][0]))
            elif rows[0] != "gaps":
                out["refused"].append(f"unit_rows_unknown:{unit}:{rows[0]}")
    out["touched_types"] = sorted(touched)
    out["touched_links"] = sorted([list(e) for e in touched_links])
    present = step_migrate.present_steps() if present is None else present
    required = step_migrate.step_inputs(
        (served, new), touched, present, specs or step_migrate.STEPS, links=touched_links)
    out["steps_required"] = required
    out["refused"].extend(f"step_input_changed:{name}:{','.join(v['types'])}"
                          for name, v in sorted(required.items()))
    return out


def content_digest(conn: sqlite3.Connection, tables: Sequence[str]) -> str:
    """A digest of every row of ``tables`` of a layer, in row order (its content)."""
    h = hashlib.sha256()
    for table in tables:
        h.update(f"\x1e{table}".encode())
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def next_sequence(dsn: str) -> int:
    """One past the number of migrations Postgres applied (a layer's id carries it, so a
    second migration of the same content, or one applied again after a revert, is new)."""
    import psycopg
    with psycopg.connect(dsn) as pg:
        if not pg.execute("SELECT to_regclass('ontology.schema_migrations') IS NOT NULL"
                          ).fetchone()[0]:
            return 1
        return pg.execute("SELECT count(*) + 1 FROM ontology.schema_migrations"
                          ).fetchone()[0]


def served_families(dsn: str) -> set[str] | None:
    """The source families the served generation read (``ontology.store_meta``
    ``source_freshness``); None when Postgres records none (then every wanted source is
    required: :func:`require_sources`)."""
    import psycopg
    with psycopg.connect(dsn) as pg:
        if not pg.execute("SELECT to_regclass('ontology.store_meta') IS NOT NULL"
                          ).fetchone()[0]:
            return None
        row = pg.execute("SELECT value FROM ontology.store_meta "
                         "WHERE key = 'source_freshness'").fetchone()
    try:
        fresh = json.loads(row[0]) if row and row[0] else None
    except ValueError:
        return None
    return set(fresh) if isinstance(fresh, dict) and fresh else None


def require_sources(wanted: Iterable[str], sources: Mapping[str, Any],
                    served: Iterable[str] | None) -> list[str]:
    """Refuse (``missing_source:<family>,...``) a build that reads a source family the
    served generation holds data of while the state directory holds no capture of it: it
    would build a valid, empty replacement of everything bound to it (SCHEMA-001).
    ``served``: :func:`served_families` (None: every wanted family counts as served). The
    wanted families the served generation never read (nothing served to replace) are
    returned, for the layer's record."""
    wanted = set(wanted)
    held = wanted if served is None else wanted & set(served)
    missing = sorted(held - set(sources))
    if missing:
        raise ValueError("missing_source:" + ",".join(missing))
    return sorted(wanted - held - set(sources))


def build_layer(migration: Mapping[str, Any], new: Any, sources: Mapping[str, Any],
                out: str | Path, *, normalizer: Any = None, as_known_at: Any = None,
                delta_mark: int = 0, sequence: int | None = None) -> dict[str, Any]:
    """Project the affected units of ``migration`` (:func:`diff`) under ``new`` from
    ``sources`` into the store ``out`` (replaced) and record the migration in it.
    ``delta_mark``: the Postgres delta mark the capture snapshot began at
    (``operating.pg.ontology_migrate.delta_mark``). ``sequence``: :func:`next_sequence`
    (None: the build time in milliseconds). The layer's id is its plan's, its rows' content
    and the sequence (unique per migration). Returns the report."""
    from ontology.project import Projector, open_generation
    from ontology.store import StoreBuilder

    if migration.get("refused"):
        raise ValueError("migration_refused:" + ",".join(migration["refused"]))
    if migration["to"] != _get(new, "digest"):
        raise ValueError("migration_of_another_schema")
    out = Path(out)
    out.unlink(missing_ok=True)
    started = time.monotonic()
    affected = list(migration["units"]["affected"])
    builder = StoreBuilder(out, foreign_keys=False)   # links name objects Postgres holds
    try:
        open_generation(builder)
        projector = Projector(new, sources, normalizer=normalizer, as_known_at=as_known_at,
                              scratch_dir=out.parent, scope=migration["deps"], emit=affected)
        projector.project_into(builder)
        builder.set_meta("schema_digest", projector.digest)
        builder.set_meta("format", FORMAT)
        conn = builder.connection
        conn.execute(f"CREATE TABLE {TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        sequence = int(time.time() * 1000) if sequence is None else int(sequence)
        content = content_digest(conn, ("objects", "object_versions", "object_removals",
                                        "identifiers", "events", "event_objects",
                                        "value_cells", "link_rows"))
        documents = {name for name, src in sources.items()
                     if callable(getattr(src, "documents", None))
                     or callable(getattr(src, "document_groups", None))}
        held = {b["id"]: b["source"] for t in (_get(new, "object_types") or {}).values()
                for b in t.get("bindings", ())}
        record = {**migration, "plan": migration["id"],
                  "id": f"{_sha([migration['id'], content, sequence])[:16]}-{sequence}",
                  "content": content, "sequence": sequence,
                  # bindings over document sources: the text index holds their text
                  "document_bindings": sorted(b for b in migration["delete"]["bindings"]
                                              if held.get(b) in documents),
                  "delta_mark": int(delta_mark),
                  # the source families the layer was built from (SCHEMA-001)
                  "sources": sorted(sources),
                  "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        conn.executemany(f"INSERT INTO {TABLE} VALUES (?, ?)",
                         [(k, json.dumps(v, sort_keys=True)) for k, v in record.items()])
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("objects", "object_versions", "value_cells", "identifiers",
                            "events", "link_rows")}
        path = builder.keep_as_part()
    except BaseException:
        builder.abort()
        raise
    path.replace(out)
    return {"id": record["id"], "plan": migration["id"], "units": len(affected),
            "counts": counts,
            "seconds": round(time.monotonic() - started, 3), "store": str(out)}


def read_layer(path: str | Path) -> dict[str, Any]:
    """The migration record of a layer :func:`build_layer` wrote."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {k: json.loads(v) for k, v in conn.execute(f"SELECT key, value FROM {TABLE}")}
    finally:
        conn.close()


_DUMP = """
import dataclasses, json, sys
from ontology.schema import load_schema
schema = load_schema(sys.argv[1])
out = {}
for f in dataclasses.fields(schema):
    value = getattr(schema, f.name)
    if f.name == "company_scope":
        value = {k: {str(i): v for i, v in d.items()} for k, d in value.items()}
    out[f.name] = value
json.dump(out, sys.stdout, default=list)
"""


def schema_at(ref: str, repo: str | Path = ".") -> Any:
    """The compiled schema of ``ontology/schema`` at the git commit ``ref``, compiled by
    that commit's own schema code (``ontology/schema.py`` generates link types too, so
    this tree's compiler could give another digest than the one served)."""
    import os
    from ontology.schema import CompiledSchema

    with tempfile.TemporaryDirectory(prefix="plx-schema-") as tmp:
        archive = subprocess.run(["git", "-C", str(repo), "archive", ref, "ontology",
                                  "integrations", "operating"], check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = tmp
        done = subprocess.run([sys.executable, "-c", _DUMP,
                               str(Path(tmp) / "ontology" / "schema")],
                              cwd=tmp, env=env, check=True, capture_output=True)
    fields = json.loads(done.stdout)
    fields["bindings"] = {k: tuple(v) for k, v in fields["bindings"].items()}
    fields["company_scope"] = {k: {int(i): v for i, v in d.items()}
                               for k, d in fields["company_scope"].items()}
    known = {f for f in CompiledSchema.__dataclass_fields__}
    return CompiledSchema(**{k: v for k, v in fields.items() if k in known})


def main(argv: Sequence[str] | None = None) -> int:
    from ontology.schema import load_schema

    parser = argparse.ArgumentParser(prog="python3 -m ontology.schema_migrate")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("diff", help="the plan from the schema at a commit to this tree's")
    d.add_argument("--from-ref", required=True)
    b = sub.add_parser("build", help="the migration layer from the schema Postgres serves "
                                     "to this tree's, over a state directory's captures")
    b.add_argument("--from-ref", required=True,
                   help="a commit whose schema is the one Postgres serves (checked)")
    b.add_argument("--state-dir", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    new = load_schema()
    if args.command == "diff":
        plan = diff(schema_at(args.from_ref), new)
        print(json.dumps(plan, indent=1, sort_keys=True))
        return 1 if plan["refused"] else 0
    # the unit part of the whole plan: what a step migration re-derives is its to cover
    # (``ontology.step_migrate build --unit-layer``, applied in the same transaction)
    from ontology import step_migrate
    whole = step_migrate.plan(args.from_ref, new)
    if whole["steps"]["refused"]:
        raise SystemExit("step_migration_refused:" + ",".join(whole["steps"]["refused"]))
    plan = whole["units"]
    report = build_from_state(plan, new, Path(args.state_dir), Path(args.out), dsn=args.dsn)
    print(json.dumps(report, sort_keys=True))
    return 0


def build_from_state(plan: Mapping[str, Any], new: Any, state: Path, out: Path, *,
                     dsn: str | None = None) -> dict[str, Any]:
    """:func:`build_layer` over a host state directory's captures, read-only (the Portal
    mirror and every capture store the affected units read), with the build's kept
    readings of captured files (``projection/readings.sqlite``, member readings and
    document fields): a file is read again only when no reading of it is kept."""
    import contextlib
    import os
    from integrations.captured_text import kept_readings
    from integrations.kept_members import kept_members
    from ontology import project
    from ontology.delta import _internal_domains
    from ontology.document_fields import kept_fields
    from ontology.project_incremental import FIELDS, MEMBERS, READINGS
    from operating.host import state_captures
    from operating.pg import config
    from operating.pg.ontology_migrate import delta_mark, served_schema
    import psycopg

    dsn = dsn or config.dsn()
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        served = served_schema(cur)
    if served != plan["from"]:
        raise ValueError(f"served_schema_differs:{served}")
    bindings = {b["id"]: b for t in (_get(new, "object_types") or {}).values()
                for b in t.get("bindings", [])}
    wanted = {bindings[b]["source"] for b in plan["deps"] if b in bindings}
    mark = delta_mark(dsn)   # before the snapshot: anything later is queued at the apply
    projection = state / "projection"
    with contextlib.ExitStack() as stack:
        sources: dict[str, Any] = {}
        mirror = state / "case-runtime" / "source-mirror.sqlite"
        if "portal_api" in wanted and mirror.is_file():
            sources["portal_api"] = stack.enter_context(project.MirrorCaptures(mirror))
        held = state_captures(state, stack, internal_domains=_internal_domains(os.environ))
        sources.update({k: v for k, v in held.items() if k in wanted})
        never = require_sources(wanted, sources, served_families(dsn))
        blobs = [s.blobs for s in sources.values()
                 if isinstance(getattr(s, "blobs", None), (str, Path))]
        stack.enter_context(kept_readings(projection / READINGS))
        stack.enter_context(kept_members(projection / MEMBERS, blobs))
        stack.enter_context(kept_fields(projection / FIELDS, project.derivation_code()))
        report = build_layer(plan, new, sources, out, delta_mark=mark,
                             sequence=next_sequence(dsn))
    report["sources"] = sorted(sources)
    report["sources_never_served"] = never
    return report


if __name__ == "__main__":
    sys.exit(main())
