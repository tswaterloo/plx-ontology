"""Source coverage through the door (ARCH addendum 9/11, owner 11-18: "everything in every
system properly searchable and reachable, always"): per type the served store holds records
of, and per system, how many records the sources hold (``served``: the catalog's count, not
removed, not an excluded person's) and how many a full reader reaches through the AI's door
(``reachable``), with the door's tables that hold the type.

Reachable is counted with the door's own rule: a full reader's held classes are decided by
``door.reader_scope`` (every audience key and marking set the classes use, every document
held), and per table:

* ``records``: its rows of the type read through the guarded view ``<biz>_sql.records`` as
  ``plx_sql_reader`` with those settings (the real door: a missing view, grant or broken
  predicate reads 0);
* a curated table holding one row per record of its first read type: the distinct records
  of that type its rows the reader holds (class held, ``ai_ok``) were made from
  (``lineage.object_ids``), with the guarded view's row count read through the door as a
  cross-check (a difference is a ``door_mismatch``);
* a communications table: its rows' own records that the AI may read (``ai_ok``, not
  flagged excluded).

Findings: ``unreachable`` (served records, none reachable), ``door_mismatch``. ``alarming``
compares a run with the ``baseline`` (the last good share reachable/served per type and the
gaps known at the first run): ``dropped`` (a type's share fell more than ``DROP_SHARE``
below its baseline), ``new_unreachable`` (a type not known as a gap is unreachable).

  python3 -m operating.pg.source_coverage [--dsn D] [--json]
  python3 -m operating.pg.source_coverage run --state-dir STATE   (the timer: writes
      STATE/source-coverage/status.json and baseline.json; ``ops_alarms`` reads them)
Exit 0: no alarm; 1: alarms (the table says which); 2: the check could not run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from operating.pg import business_views as bv
from operating.pg import sql_views

PROBE_MS = 120_000
RETRY_WAIT_S = 90
DROP_SHARE = 0.02
STATUS_DIR = "source-coverage"
STATUS_SCHEMA = "plx-source-coverage/v1"


def _parse(text: Any) -> dt.datetime | None:
    try:
        got = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return got if got.tzinfo else got.replace(tzinfo=dt.timezone.utc)


def full_scope(pg: Any, s: str) -> tuple[dict[str, str], list[int]]:
    """A full reader's door settings over ``s`` (``door.reader_scope`` with every key and
    marking set the classes use, every document held) and the classes it holds."""
    from operating.pg import door
    keys, marks = pg.execute(
        "SELECT COALESCE(array_agg(DISTINCT k) FILTER (WHERE k IS NOT NULL), '{}'), "
        "COALESCE(array_agg(DISTINCT m) FILTER (WHERE m IS NOT NULL), '{}') FROM "
        f"{s}.access_classes LEFT JOIN LATERAL unnest(audience_keys) k ON true "
        "LEFT JOIN LATERAL unnest(rec_marks) m ON true WHERE NOT blocked").fetchone()
    scope = door.reader_scope(pg, s, audience_keys=keys, marking_sets=["[]", *marks],
                              held=(), holds_every_doc=True)
    held = [int(x) for part in ("plx.classes", "plx.dclasses")
            for x in scope["settings"][part].strip("{}").split(",") if x]
    return scope["settings"], held


def _door_counts(pg: Any, s: str, settings: Mapping[str, str], views: Sequence[str]
                 ) -> dict[str, Any]:
    """Through the AI's guarded views as ``plx_sql_reader``: rows per view (None: no view
    or it failed) and the records view's rows per type, in one READ ONLY transaction."""
    import psycopg
    dst = sql_views.guarded_schema(s)
    out: dict[str, Any] = {"views": {}, "records": {}}
    with pg.transaction():
        pg.execute("SET TRANSACTION READ ONLY")
        pg.execute(f"SET LOCAL statement_timeout = {PROBE_MS}")
        names = sorted(settings)
        if names:
            pg.execute("SELECT " + ", ".join("set_config(%s, %s, true)" for _ in names),
                       [x for n in names for x in (n, settings[n])])
        pg.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
        for view in views:
            if pg.execute("SELECT to_regclass(%s)", (f"{dst}.{view}",)).fetchone()[0] is None:
                out["views"][view] = None
                continue
            try:
                with pg.transaction():
                    if view == "records":
                        got = pg.execute(f"SELECT type, count(*) FROM {dst}.records "
                                         "GROUP BY 1").fetchall()
                        out["records"] = {t: int(n) for t, n in got}
                        out["views"][view] = sum(out["records"].values())
                    else:
                        out["views"][view] = int(pg.execute(
                            f"SELECT count(*) FROM {dst}.{view}").fetchone()[0])
            except psycopg.Error:
                out["views"][view] = None
    return out


def _owner_counts(pg: Any, s: str, held: Sequence[int]) -> dict[str, Any]:
    """Per business table built in ``s``: rows the full reader holds (the door's rule read
    as the owner) and the distinct records of its first read type they were made from."""
    from operating.pg import door
    out: dict[str, Any] = {}
    for name, v in sorted(bv.VIEWS.items()):
        if not v.reads or pg.execute("SELECT to_regclass(%s)", (f"{s}.{name}",)
                                     ).fetchone()[0] is None:
            continue
        prefix = v.reads[0] + ":"
        if v.fill:
            n = pg.execute(f"SELECT count(DISTINCT t.object_ids[1]) FROM {s}.{name} t WHERE "
                           "t.ai_ok AND t.excluded IS FALSE AND starts_with(t.object_ids[1], "
                           "%s)", (prefix,)).fetchone()[0]
            out[name] = {"type": v.reads[0], "records": int(n), "rows": None}
            continue
        if not door.is_v2(pg, s, name):
            out[name] = {"type": v.reads[0], "records": 0, "rows": 0}
            continue
        rows, n = pg.execute(
            f"SELECT (SELECT count(*) FROM {s}.{name} t WHERE t.ai_ok AND t.access_class = "
            f"ANY(%s)), (SELECT count(DISTINCT o) FROM {s}.{name} t JOIN {s}.lineage l ON "
            "l.view = %s AND l.handle = t.handle CROSS JOIN unnest(l.object_ids) o WHERE "
            "t.ai_ok AND t.access_class = ANY(%s) AND starts_with(o, %s))",
            (list(held), name, list(held), prefix)).fetchone()
        out[name] = {"type": v.reads[0], "records": int(n), "rows": int(rows)}
    return out


def check(dsn: str, biz: str | None = None, *, now: dt.datetime | None = None
          ) -> dict[str, Any]:
    """The coverage table: per type and per system, with the findings."""
    import psycopg
    s = biz or bv.SCHEMA
    now = now or dt.datetime.now(dt.timezone.utc)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(f"SET statement_timeout = {PROBE_MS}")
        catalog = pg.execute(f"SELECT type, systems, rows, newest, declared, tables FROM "
                             f"{s}.record_types WHERE rows > 0 ORDER BY type").fetchall()
        settings, held = full_scope(pg, s)
        owner = _owner_counts(pg, s, held)
        views = ["records", *sorted(n for n, o in owner.items() if o["rows"] is not None)]
        door_got = _door_counts(pg, s, settings, views)
        for _ in range(2):
            if door_got["views"].get("records") is not None:
                break
            # the records read failed (timeout, a repack or build holding locks): try again
            time.sleep(RETRY_WAIT_S)
            door_got = _door_counts(pg, s, settings, views)
        if door_got["views"].get("records") is None:
            # never report every records-only type unreachable (and never keep that as the
            # baseline) because the one read through the door failed: the run did not run
            raise RuntimeError("the records view could not be read through the door")
    findings: list[dict[str, Any]] = []
    for name, o in owner.items():
        seen = door_got["views"].get(name)
        if o["rows"] is not None and seen != o["rows"]:
            findings.append({"kind": "door_mismatch", "view": name, "door": seen,
                             "held": o["rows"]})
    by_type: dict[str, int] = {}
    for o in owner.values():
        by_type[o["type"]] = max(by_type.get(o["type"], 0), o["records"])
    for otype, n in door_got["records"].items():
        by_type[otype] = max(by_type.get(otype, 0), n)
    types = []
    for otype, systems, rows, newest, declared, tables in catalog:
        seen = _parse(newest)
        row = {"type": otype, "systems": list(systems or ()), "tables": list(tables or ()),
               "served": int(rows), "reachable": int(by_type.get(otype, 0)),
               "newest": newest, "declared": bool(declared),
               "age_hours": round((now - seen).total_seconds() / 3600, 1) if seen else None}
        types.append(row)
        if row["reachable"] == 0:
            findings.append({"kind": "unreachable", "type": otype, "systems": row["systems"]})
    systems: dict[str, dict[str, Any]] = {}
    for row in types:
        for name in row["systems"] or ["unknown"]:
            got = systems.setdefault(name, {"system": name, "types": 0, "served": 0,
                                            "reachable": 0, "unreachable_types": []})
            got["types"] += 1
            got["served"] += row["served"]
            got["reachable"] += min(row["reachable"], row["served"])
            if row["reachable"] == 0:
                got["unreachable_types"].append(row["type"])
    return {"schema": STATUS_SCHEMA, "checked_at": now.isoformat(), "types": types,
            "systems": [systems[k] for k in sorted(systems)], "findings": findings,
            "summary": {"systems": len(systems), "types": len(types),
                        "reachable_types": sum(r["reachable"] > 0 for r in types),
                        "served": sum(r["served"] for r in types),
                        "reachable": sum(min(r["reachable"], r["served"]) for r in types)}}


def _share(row: Mapping[str, Any]) -> float:
    return min(row["reachable"], row["served"]) / row["served"] if row["served"] else 1.0


def alarming(report: Mapping[str, Any], baseline: Mapping[str, Any] | None
             ) -> list[dict[str, Any]]:
    """What a run alarms on against ``baseline``: a door mismatch, a type whose share
    reachable fell over ``DROP_SHARE`` below its baseline, an unreachable type that was not
    a known gap. With no baseline yet (the first run) every gap is the known starting
    state, listed in ``findings``, never an alarm (11-18: the first run alarmed all 192
    unreachable types and failed its unit)."""
    base = baseline or {}
    shares = base.get("shares") or {}
    known = (set(base.get("known_unreachable") or ()) if baseline is not None else
             {r["type"] for r in report["types"] if r["reachable"] == 0})
    out = [f for f in report["findings"] if f["kind"] == "door_mismatch"]
    for row in report["types"]:
        was = shares.get(row["type"])
        if isinstance(was, (int, float)) and _share(row) < was - DROP_SHARE and \
                row["reachable"] > 0:
            out.append({"kind": "dropped", "type": row["type"], "share": round(_share(row), 4),
                        "baseline": was})
        if row["reachable"] == 0 and row["type"] not in known:
            out.append({"kind": "new_unreachable", "type": row["type"],
                        "systems": row["systems"]})
    return out


def next_baseline(report: Mapping[str, Any], baseline: Mapping[str, Any] | None
                  ) -> dict[str, Any]:
    """The baseline after ``report``: the first run's gaps are known; a type's share is
    kept at its best while it alarms (a drop keeps alarming until it recovers) and follows
    the run otherwise; a new gap alarms once (this run) and is known from then on, so it
    never fails every later run (``findings`` keeps listing it); a gap that became
    reachable is no longer known."""
    if baseline is None:
        return {"known_unreachable": sorted(r["type"] for r in report["types"]
                                            if r["reachable"] == 0),
                "shares": {r["type"]: round(_share(r), 4) for r in report["types"]
                           if r["reachable"] > 0}}
    dropped = {f["type"] for f in alarming(report, baseline) if f["kind"] == "dropped"}
    shares = dict(baseline.get("shares") or {})
    for r in report["types"]:
        if r["reachable"] > 0 and r["type"] not in dropped:
            shares[r["type"]] = round(_share(r), 4)
    known = sorted(r["type"] for r in report["types"] if r["reachable"] == 0)
    return {"known_unreachable": known, "shares": shares}


def render(report: Mapping[str, Any], alarms: Sequence[Mapping[str, Any]] = ()) -> str:
    """The coverage as plain text tables (systems, then types), findings last."""
    lines = ["system            types   served  reachable  unreachable types", "-" * 78]
    for s in report["systems"]:
        lines.append(f"{s['system'][:17]:<17} {s['types']:>5} {s['served']:>8} "
                     f"{s['reachable']:>10}  {', '.join(s['unreachable_types'])[:60]}")
    lines += ["", "type                              systems          served  reachable  "
              "tables", "-" * 90]
    for t in report["types"]:
        lines.append(f"{t['type'][:33]:<33} {','.join(t['systems'])[:16]:<16} "
                     f"{t['served']:>7} {t['reachable']:>10}  {','.join(t['tables']) or '-'}")
    sm = report["summary"]
    lines += ["", f"{sm['systems']} systems, {sm['types']} types ({sm['reachable_types']} "
              f"reachable), {sm['reachable']:,} of {sm['served']:,} records reachable"]
    lines += [f"FINDING {json.dumps(f, sort_keys=True)}" for f in report["findings"]]
    lines += [f"ALARM {json.dumps(f, sort_keys=True)}" for f in alarms]
    return "\n".join(lines)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, doc: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def run(dsn: str, state_dir: Path) -> dict[str, Any]:
    """One timer run: the check, its alarms against the kept baseline, the status file
    ``ops_alarms`` reads (``ok`` False with the error when the check could not run) and the
    next baseline."""
    home = state_dir / STATUS_DIR
    home.mkdir(parents=True, exist_ok=True)
    baseline = _read(home / "baseline.json")
    try:
        report = check(dsn)
    except Exception as exc:   # noqa: BLE001 - the alarm says the check could not run
        status = {"schema": STATUS_SCHEMA, "ok": False,
                  "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "error": f"{type(exc).__name__}: {exc}"[:300]}
        _write(home / "status.json", status)
        return status
    alarms = alarming(report, baseline)
    status = {**report, "ok": True, "alarms": alarms}
    _write(home / "status.json", status)
    _write(home / "baseline.json", next_baseline(report, baseline))
    return status


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.source_coverage")
    ap.add_argument("command", nargs="?", choices=("check", "run"), default="check")
    ap.add_argument("--dsn")
    ap.add_argument("--state-dir")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    if args.command == "run":
        if not args.state_dir:
            ap.error("run needs --state-dir")
        status = run(dsn, Path(args.state_dir))
        print(json.dumps({k: status.get(k) for k in ("ok", "checked_at", "summary", "alarms",
                                                      "error")}, default=str))
        return 2 if not status.get("ok") else (1 if status.get("alarms") else 0)
    try:
        report = check(dsn)
    except Exception as exc:   # noqa: BLE001 - the check could not run: say so, exit 2
        print(f"coverage check could not run: {type(exc).__name__}: {exc}"[:500],
              file=sys.stderr)
        return 2
    alarms = alarming(report, None)
    print(json.dumps({**report, "alarms": alarms}, indent=1, default=str) if args.json
          else render(report, alarms))
    return 1 if alarms else 0


if __name__ == "__main__":
    sys.exit(main())
