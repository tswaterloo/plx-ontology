"""Prove a step migration (with its unit migration) on real captures in a scratch Postgres
database, the way ``scripts/schema_migration_proof.py`` proves a unit migration.

``python3 -m scripts.step_migration_proof sample --lane L --from-ref A --work DIR``
  (in the new code's tree): a capture sample under ``DIR/state``, copied read-only out of
  the live state directory: the Portal mirror rows of the relations the affected units
  and the lane's steps read (``--cap`` rows each; the parties whole for ``links``), mail
  of mailboxes the people exclusion does not name, the BC and Mission Control records the
  lane reads, and a copy of the kept readings (no document is read again).
``python3 -m scripts.step_migration_proof cold --work DIR --name from|to``
  (in the old, then the new code's tree): a full build of the sample with every build step.
``python3 -m scripts.step_migration_proof prove --work DIR --from-ref A`` (new code's tree):
  a scratch database (created here, dropped at the end) loaded with the ``from`` build; the
  unit migration layer and the step migration layer built against it (the steps run over
  the served rows) and applied in one transaction; every logical row compared with the
  ``to`` build. Prints one JSON report.

``Links`` (Email.sent_by_* from the sender step, the party twins), ``links3``
(Mission Control task links), ``docs`` (document-field references). Nothing outside
``--work`` and the scratch database is written.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

LANE_TYPES = {
    "links": ("Supplier", "Customer", "Contact", "Product", "PurchaseOrder"),
    "links3": ("Supplier", "Customer", "Product", "PurchaseOrder", "SalesOrder"),
    "docs": ("Supplier", "PurchaseOrder", "SalesOrder", "SalesInvoice"),
}
WHOLE = {"links": ("Supplier", "Customer", "Contact")}
BC_SETS = {"links": ("customers", "vendors", "items", "purchaseOrders")}
MC_SETS = {"links3": ("tasks",)}


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _excluded() -> Any:
    from integrations.people_exclusion import PeopleExclusion
    exclusion = PeopleExclusion.from_env(os.environ)
    exclusion.require()   # no guard: no mail
    return exclusion


def sample(lane: str, from_ref: str, work: Path, state: Path, cap: int,
           messages: int) -> dict[str, Any]:
    from ontology import delta
    from ontology.schema import load_schema
    from ontology.step_migrate import plan
    from operating.host import state_capture_paths

    new = load_schema()
    the_plan = plan(from_ref, new)
    out_state = work / "state"
    out_state.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"lane": lane, "plan": {
        "units": len(the_plan["units"]["units"]["affected"]),
        "refused": the_plan["units"]["refused"] + the_plan["steps"]["refused"],
        "steps": the_plan["steps"]["run"], "why": the_plan["steps"]["why"]}}
    bindings = {b["id"]: (tid, b) for tid, t in new.object_types.items()
                for b in t["bindings"]}
    deps = set(the_plan["units"].get("deps") or ())
    relations: dict[str, int | None] = {}
    for bid, (tid, b) in bindings.items():
        if b["source"] != "portal_api":
            continue
        if tid in WHOLE.get(lane, ()):
            relations[b["relation"]] = None
        elif bid in deps or tid in LANE_TYPES[lane]:
            relations.setdefault(b["relation"], cap)
    mirror = state / "case-runtime" / "source-mirror.sqlite"
    keys: list[tuple[str, str]] = []
    with contextlib.closing(_ro(mirror)) as src:
        for relation, limit in sorted(relations.items()):
            keys += src.execute("SELECT DISTINCT relation, key_json FROM row_current WHERE "
                                "relation = ? ORDER BY key_json" +
                                ("" if limit is None else f" LIMIT {int(limit)}"),
                                (relation,)).fetchall()
    target = out_state / "case-runtime" / "source-mirror.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    delta.mirror_subset(mirror, target, keys)
    report["portal"] = {"relations": len(relations), "records": len(keys)}
    paths, out_paths = state_capture_paths(state), state_capture_paths(out_state)
    for family, sets in (("bc", BC_SETS.get(lane)), ("mission_control", MC_SETS.get(lane))):
        if not sets:
            continue
        with contextlib.closing(_ro(paths[family])) as src:
            chosen = []
            for name in sets:
                index = 2 if family == "bc" else 1
                chosen += [r for r in src.execute(
                    "SELECT DISTINCT kind, path FROM versions ORDER BY path").fetchall()
                    if len(r[1].split("/")) > index and r[1].split("/")[index] == name
                    ][:cap * 5]
        delta.store_subset(paths[family], out_paths[family], chosen)
        report[family] = len(chosen)
    if lane in ("links", "docs"):
        exclusion = _excluded()
        live = state / "mail-capture" / "capture.db"
        with contextlib.closing(_ro(live)) as src:
            boxes = [r[0] for r in src.execute(
                "SELECT mailbox_id FROM messages GROUP BY 1 ORDER BY count(*) DESC")]
            boxes = [b for b in boxes if not exclusion.is_excluded(b)][:2]
            with_files = ("EXISTS" if lane == "docs" else "NOT EXISTS") + (
                " (SELECT 1 FROM attachments a WHERE a.message_handle = m.l1_handle" +
                (" AND a.text_sha256 IS NOT NULL)" if lane == "docs" else ")"))
            chosen = []
            for box in boxes:
                chosen += src.execute(
                    "SELECT DISTINCT m.mailbox_id, m.message_id FROM messages m WHERE "
                    f"m.mailbox_id = ? AND m.superseded_at IS NULL AND {with_files} "
                    "ORDER BY m.recorded_at DESC LIMIT ?", (box, messages // 2)).fetchall()
        delta.mail_subset(state, out_state, chosen)
        report["mail"] = {"mailboxes": len(boxes), "messages": len(chosen)}
    projection = out_state / "projection"
    projection.mkdir(exist_ok=True)
    shared = work.parent / "readings"
    shared.mkdir(exist_ok=True)
    for name in ("readings.sqlite", "member-readings.sqlite"):
        live = state / "projection" / name
        if live.is_file() and not (shared / name).is_file():
            with contextlib.closing(_ro(live)) as src, \
                    contextlib.closing(sqlite3.connect(shared / name)) as dst:
                src.backup(dst)
        if (shared / name).is_file() and not (projection / name).exists():
            (projection / name).symlink_to(shared / name)
    return report


@contextlib.contextmanager
def sources_of(state: Path):
    from integrations.captured_text import kept_readings
    from integrations.kept_members import kept_members
    from ontology import project
    from ontology.delta import _internal_domains
    from ontology.project_incremental import MEMBERS, READINGS
    from operating.host import state_captures
    with contextlib.ExitStack() as stack:
        sources = {"portal_api": stack.enter_context(project.MirrorCaptures(
            state / "case-runtime" / "source-mirror.sqlite"))}
        sources.update(state_captures(state, stack,
                                      internal_domains=_internal_domains(os.environ)))
        blobs = [s.blobs for s in sources.values()
                 if isinstance(getattr(s, "blobs", None), (str, Path))]
        stack.enter_context(kept_readings(state / "projection" / READINGS))
        stack.enter_context(kept_members(state / "projection" / MEMBERS, blobs))
        yield sources


def cold(work: Path, name: str) -> dict[str, Any]:
    from ontology import project
    from ontology.schema import load_schema
    from operating import build_steps
    state = work / "state"
    store = work / f"cold-{name}.sqlite"
    store.unlink(missing_ok=True)
    t = time.monotonic()
    with sources_of(state) as sources:
        built = project.build_store(store, load_schema(), sources, post_project=(
            build_steps.post_project(state, "plx", scratch=work / f"steps-{name}")))
    return {"name": name, "seconds": round(time.monotonic() - t, 1),
            "counts": {k: v for k, v in built["counts"].items() if "." not in k},
            "steps": {k: {"seconds": v.get("seconds")} for k, v in
                      (built.get("post_project") or {}).items()}}


def prove(work: Path, from_ref: str) -> dict[str, Any]:
    import psycopg
    from ontology import schema_migrate, step_migrate
    from ontology.schema import load_schema
    from operating import build_steps
    from operating.pg import ontology_load
    from operating.pg.ontology_migrate import apply_migration, delta_mark
    from operating.pg import scratch_db as pg_testdb
    from scripts.schema_migration_proof import _scratch_db, compare, logical_rows

    new = load_schema()
    state = work / "state"
    report: dict[str, Any] = {"from_ref": from_ref}
    t = time.monotonic()
    the_plan = step_migrate.plan(from_ref, new)
    units = the_plan["units"]
    report["plan"] = {"seconds": round(time.monotonic() - t, 2),
                      "units": len(units["units"]["affected"]), "of": units["units"]["total"],
                      "refused": units["refused"] + the_plan["steps"]["refused"],
                      "steps": the_plan["steps"]["run"], "why": the_plan["steps"]["why"]}
    name = None
    try:
        name, dsn = _scratch_db()
        t = time.monotonic()
        ontology_load.load(work / "cold-from.sqlite", dsn, workers=4, replace=True,
                           log=io.StringIO())
        report["load_seconds"] = round(time.monotonic() - t, 1)
        prefixes = sorted({p for s in step_migrate.STEPS.values() for p in s.prefixes})
        with psycopg.connect(dsn) as pg:
            report["served_step_links"] = dict(pg.execute(
                "SELECT p, count(l.link_id) FROM unnest(%s::text[]) p LEFT JOIN "
                "ontology.link_rows l ON starts_with(l.link_id, p) GROUP BY p",
                (prefixes,)).fetchall())
        mark = delta_mark(dsn)
        unit_layer = None
        with sources_of(state) as sources:
            if units["units"]["affected"]:
                t = time.monotonic()
                unit_layer = work / "units.sqlite"
                report["unit_layer"] = schema_migrate.build_layer(
                    units, new, sources, unit_layer, delta_mark=mark)
                report["unit_layer"]["seconds"] = round(time.monotonic() - t, 1)
            t = time.monotonic()
            steps = build_steps.post_project(state, "plx", scratch=work / "steps-migrate")
            built = step_migrate.build_layer(the_plan, new, steps, dsn, work / "steps.sqlite",
                                             sources=sources, unit_layer=unit_layer,
                                             delta_mark=mark)
            built["seconds"] = round(time.monotonic() - t, 1)
            for step in built["steps"].values():
                step.pop("report", None)
            report["step_layer"] = built
        t = time.monotonic()
        report["apply"] = apply_migration(unit_layer, dsn, steps=work / "steps.sqlite")
        report["apply"]["wall_seconds"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        after, cold_to = logical_rows(dsn=dsn), logical_rows(str(work / "cold-to.sqlite"))
        report["equivalence"] = compare(after, cold_to)
        report["equivalent"] = all(v["equal"] for v in report["equivalence"].values())
        before = logical_rows(str(work / "cold-from.sqlite"))
        report["changed_by_lane"] = {k: v.get("differing", 0)
                                     for k, v in compare(before, cold_to).items()}
        report["compare_seconds"] = round(time.monotonic() - t, 1)
    finally:
        if name is not None:
            pg_testdb.drop_db(name)
    return report


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--lane", required=True, choices=sorted(LANE_TYPES))
    s.add_argument("--from-ref", required=True)
    s.add_argument("--work", required=True)
    s.add_argument("--state-dir", default="/data/plx-state")
    s.add_argument("--cap", type=int, default=400)
    s.add_argument("--messages", type=int, default=3000)
    c = sub.add_parser("cold")
    c.add_argument("--work", required=True)
    c.add_argument("--name", required=True, choices=("from", "to"))
    p = sub.add_parser("prove")
    p.add_argument("--work", required=True)
    p.add_argument("--from-ref", required=True)
    args = parser.parse_args(argv)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    if args.command == "sample":
        out = sample(args.lane, args.from_ref, work, Path(args.state_dir), args.cap,
                     args.messages)
    elif args.command == "cold":
        out = cold(work, args.name)
    else:
        out = prove(work, args.from_ref)
    print(json.dumps(out, indent=1, sort_keys=True, default=str))
    return 0 if out.get("equivalent", True) else 1


if __name__ == "__main__":
    sys.exit(main())
