"""Live check of cross-process dependencies (process.dependencies): counts and enums only.

    python3 -m scripts.process_dependencies_probe --db DIR/mirror.sqlite \\
        --store DIR/ontology.sqlite [--budget-seconds N] [--deps-budget-seconds N] \\
        [--per-type N] [--scan N] [--workers N] [--bc-capture] [--live-audience FILE] \\
        [--previous-report DAILY.json] [--session-projection SESSION_DIR]

GET only. The L7 probe (``scripts.ontology_projection_probe --build-only``: one build, no
rebuild and no claim checks; a failed build prints its ``code``, ``phase``, ``store`` and
``disk``, and a refused build its ``need_bytes`` and ``free_bytes``: ``build_failure``;
the build's ``disk.peak`` is passed on) builds the throwaway mirror and projection store
(a GET-only Portal sync; with ``--bc-capture`` one GET-only BC capture) and lays them out
as the served host state, as ``workspace.object_view`` does; with ``--session-projection``
it opens the verifier session's projection instead when this session committed it (no
sync, capture or projection of its own). Then, offline over that state and without any client, ``plx.process`` runs through
the served host (``Host.served_tools``: the tools ``mcp`` serves, under the policy each
call is served with) as the served host's capture principal on sampled roots of every
process graph stage that declares ``prerequisites`` (``ontology/schema/process``; no
process, stage or type is named here): up to ``--scan`` roots of the graph's root type
the reader's policy grants, lowest SHA-256 of the object id first, until ``--per-type``
of them are open (the stage is not known reached, so the answer carries its
``prerequisites``). Recorded per graph stage:

* ``prerequisites``: open roots by state, ``clear`` / ``blocked`` / ``undecided``;
* ``gap_reasons``: blocker entries by the stage's state and the entry's gap reason
  (``dependency_not_pegged``, ``dependency_unproven``, ``absent_in_source`` reasons ...);
* ``proven_blockers`` and ``undecided_candidates``: entries naming a record, by the
  source of its binding, split by whether the entry is proven (``absent_in_source``) or
  undecided (a ``dependency_unproven`` candidate among them: the BC POs the item path
  reached and did not find received); twins one identity link joins are one entry;
* ``not_pegged``: the ``dependency_not_pegged`` entries per graph stage by the records they
  are about (``operating.ontology_tools_process.not_pegged_waits_on``: ``open``, a live
  record read is not met and is its own entry; ``met``, every live record is read and
  met; ``unread``, records past the cap are not read), ``unclassified`` for one whose
  detail is not read (a detail drift, never a type). A prerequisite reaching no live
  record has no such entry: its reach's own entry says why;
* ``draft_records``: reached records in draft (``prerequisite_draft`` gaps: not issued,
  so neither blocker nor candidate), by source;
* ``risks``: schedule risks of the stage's prerequisites (``operating/process_risks.py``)
  by ``proven`` (a proven blocker due after it is needed) and ``candidate`` (a record
  linked by item or undecided), and ``undecided`` comparisons (a ``schedule_undecided``
  gap: a side without one proven date);
* ``undecided_causes``: those undecided comparisons by the side without a date
  (``needed_by``: one gap for every record its waiting record waits on, or
  ``available_by``) and its cause (``operating/process_risks.py`` CAUSES: ``undated``,
  ``withheld``, ``unproven``, ``disagreeing``, ``staging_only``), ``unclassified`` for
  one whose cause is not read (a detail drift, never a cause). Gaps are counted before
  the answer's gap cap: when the served list is cut (``gaps_truncated``) the same call is read again through
  ``plx.process``'s tools under the same served policy for its uncut gaps;
* ``clear_unproven`` (must be 0): a ``clear`` answer on a stage not proven missing, or
  on a stage with a prerequisite whose paths do not prove the dependency
  (``proves_dependency`` false: linked by item, never met);
* ``contract_failures`` and ``process_errors`` (must be 0).

With the Portal down the build reports ``blocked_upstream`` and the answers run over what
was projected (blocked, never a pass); with nothing projected the probe prints
``sources_blocked`` and measures nothing. Output: counts, booleans, process and stage
names, gap reasons and source names only; never a value, key, identifier or citation.
"""

from __future__ import annotations

import argparse
import contextlib
from hashlib import sha256
import io
import json
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence

PROBE = "process-dependencies/v1"


def _add(total: dict[str, Any], key: str, amount: int = 1) -> None:
    total[key] = total.get(key, 0) + amount


def dependent_stages(graphs: Mapping[str, Mapping[str, Any]]
                     ) -> list[tuple[str, str, Mapping[str, Any]]]:
    """``(process, root type, stage)`` of every stage that declares prerequisites."""
    return [(gid, graph["root_type"], stage) for gid, graph in sorted(graphs.items())
            for stage in graph["stages"] if stage.get("prerequisites")]


def check_dependencies(host: Any, *, per_type: int, scan: int,
                       budget_seconds: float | None) -> dict[str, Any]:
    """Run ``plx.process`` on sampled roots of every stage with prerequisites through the
    served ``host`` as its capture principal; counts only (module doc)."""
    from ontology.json_contract import validate
    from operating.host import projection_path
    from operating.ontology_tools_process import (CLEAR, ProcessTools, _dedupe, load_graphs,
                                                  not_pegged_waits_on)
    from operating.process_risks import undecided_cause
    from scripts.ontology_projection_probe import reader_actor

    started = time.monotonic()
    report: dict[str, Any] = {
        "roots_scanned": 0, "roots_open": 0, "roots_withheld": 0, "contract_failures": 0,
        "process_errors": 0, "error_codes": {}, "clear_unproven": 0,
        "blockers_truncated": 0, "budget_exhausted": False, "prerequisites": {},
        "gap_reasons": {}, "proven_blockers": {}, "undecided_candidates": {},
        "draft_records": {}, "roots_with_bc_blocker": 0, "risks": {},
        "undecided_causes": {}, "not_pegged": {}}
    store = projection_path(host.state_dir)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, check_same_thread=False)
    reader = reader_actor(host)
    try:
        with host.served_tools(reader) as served:
            schema = served.tools.schema
            binding_source = {b["id"]: b["source"] for spec in schema.object_types.values()
                              for b in spec.get("bindings", ())}
            policy = served.policy_now(reader)
            contract = served.tools.contracts["tools"]["plx.process"]["output"]
            uncut: list[Any] = []   # plx.process's tools, built on the first cut gap list

            def all_gaps(out: Mapping[str, Any], arguments: dict[str, Any]) -> list[Any]:
                """The answer's gaps before its gap cap (module doc)."""
                if not any(g["reason"] == "gaps_truncated" for g in out.get("gaps", ())):
                    return list(out.get("gaps", ()))
                if not uncut:
                    uncut.append(ProcessTools(served.tools))
                served.tools.policy = served.policy_now(reader)   # as the served call
                roots = uncut[0].process(arguments, reader).roots
                return _dedupe(g for d in roots for g in d["gaps"] + d["item_gaps"])

            def source_of(oid: str) -> str:
                row = conn.execute("SELECT binding_id FROM objects WHERE object_id = ?",
                                   (oid,)).fetchone()
                return binding_source.get(row and row[0], "unknown")

            for gid, root_type, stage in dependent_stages(load_graphs()):
                key = f"{gid}.{stage['id']}"
                unpegged = any(not p.get("proves_dependency", True)
                               for p in stage["prerequisites"])
                states = report["prerequisites"].setdefault(key, {})
                risks = report["risks"].setdefault(key, {})
                causes = report["undecided_causes"].setdefault(key, {})
                not_pegged = report["not_pegged"].setdefault(key, {})
                rows = conn.execute("SELECT object_id, audience_key FROM objects "
                                    "WHERE object_type = ?", (root_type,)).fetchall()
                ids = sorted((oid for oid, audience in rows
                              if policy.may_read(reader, audience)),
                             key=lambda i: sha256(i.encode("utf-8")).hexdigest())
                report["roots_withheld"] += len(rows) - len(ids)
                opened = 0
                for oid in ids[:scan]:
                    if budget_seconds is not None and \
                            time.monotonic() - started > budget_seconds:
                        report["budget_exhausted"] = True
                        break
                    if opened >= per_type:
                        break
                    report["roots_scanned"] += 1
                    arguments = {"from": oid, "process": gid}
                    out = served.call("plx.process", arguments, reader)
                    if validate(out, contract, root=served.tools.contracts, limit=1):
                        report["contract_failures"] += 1
                    if "error" in out:
                        report["process_errors"] += 1
                        _add(report["error_codes"], out["error"]["code"])
                        continue
                    item = next((s for s in out["stages"] if s["stage"] == stage["id"]), {})
                    if "prerequisites" not in item:
                        continue   # reached or skipped: waits on nothing
                    opened += 1
                    state = item["prerequisites"]
                    _add(states, state)
                    proven_missing = (item.get("state") == "missing" and item.get(
                        "missing", {}).get("state") == "absent_in_source")
                    report["clear_unproven"] += state == CLEAR and (unpegged
                                                                    or not proven_missing)
                    reasons = report["gap_reasons"].setdefault(state, {})
                    bc_named = False
                    for entry in item.get("blockers", ()):
                        _add(reasons, entry["missing"]["reason"])
                        if entry["missing"]["reason"] == "dependency_not_pegged":
                            _add(not_pegged,
                                 not_pegged_waits_on(entry["missing"]) or "unclassified")
                        if "object" in entry:
                            source = source_of(entry["object"])
                            _add(report["proven_blockers" if entry["missing"]["state"]
                                        == "absent_in_source" else "undecided_candidates"],
                                 source)
                            bc_named |= source == "bc"
                    for risk in item.get("risks", ()):
                        _add(risks, "proven" if risk["proven"] else "candidate")
                    for gap in all_gaps(out, arguments):
                        if gap["reason"] == "prerequisite_draft":   # subject: its handles
                            _add(report["draft_records"], source_of(gap["subject"].split()[0]))
                        elif gap["reason"] == "schedule_undecided":
                            _add(risks, "undecided")
                            cause = undecided_cause(gap)
                            if cause is None:
                                _add(causes, "unclassified")
                            else:
                                _add(causes.setdefault(cause[0], {}), cause[1])
                    report["roots_with_bc_blocker"] += bc_named
                    report["blockers_truncated"] += any(
                        g.get("reason") == "blockers_truncated" for g in out.get("gaps", ()))
                report["roots_open"] += opened
                if report["budget_exhausted"]:
                    break
    finally:
        conn.close()
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         bc_sender: Any = None, bc_books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.process_dependencies_probe")
    parser.add_argument("--db", required=True, help="throwaway mirror file")
    parser.add_argument("--store", required=True, help="throwaway ontology store file")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="the L7 build's budget")
    parser.add_argument("--deps-budget-seconds", type=float, default=None)
    parser.add_argument("--per-type", type=int, default=20,
                        help="open roots answered per graph stage with prerequisites")
    parser.add_argument("--scan", type=int, default=200,
                        help="roots read per graph stage to find the open ones")
    parser.add_argument("--previous-report", default=None,
                        help="last daily verifier report, passed to the L7 build")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--bc-capture", action="store_true")
    parser.add_argument("--live-audience", default=None)
    parser.add_argument("--session-projection", default=None,
                        help="the verifier session's projection (the L7 probe's)")
    args = parser.parse_args(argv)
    phase = None   # the consumer's own phase, once the build succeeded
    try:
        from scripts import ontology_projection_probe as projection

        build_args = ["--db", args.db, "--store", args.store, "--build-only"]
        for flag, value in (("--budget-seconds", args.budget_seconds),
                            ("--previous-report", args.previous_report),
                            ("--workers", args.workers),
                            ("--live-audience", args.live_audience),
                            ("--session-projection", args.session_projection)):
            if value is not None:
                build_args += [flag, str(value)]
        if args.bc_capture:
            build_args.append("--bc-capture")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            built = projection.main(build_args, environ=environ, bc_sender=bc_sender,
                                    bc_books=bc_books)
        if built == projection.EXIT_BUDGET:
            partial = json.loads(out.getvalue())
            print(json.dumps({"probe": PROBE, "status": "budget_exhausted",
                              "phase": partial.get("phase"), "stage": "projection_build"}))
            return projection.EXIT_BUDGET
        if built != 0:
            # the build's own error line: its code, phase, store and disk (build_failure)
            print(json.dumps({**projection.build_failure(out.getvalue()), "probe": PROBE,
                              "error": "projection_build_failed",
                              "stage": "projection_build"}, sort_keys=True))
            return 1
        build = json.loads(out.getvalue())
        parts = {key: build[key] for key in ("blocked_upstream", "bc_capture", "sources",
                                            "session_projection", "disk") if key in build}
        if build.get("sources_blocked"):   # nothing was projected: measured nothing
            print(json.dumps({"probe": PROBE, "sources_blocked": build["sources_blocked"],
                              **parts}, sort_keys=True))
            return 0
        host = projection.served_host(args.store, environ)
        phase = "process_dependencies"   # an error opening the served host is not its own
        report = check_dependencies(host, per_type=args.per_type, scan=args.scan,
                                    budget_seconds=args.deps_budget_seconds)
    except Exception as exc:  # noqa: BLE001 - a class name, or a SQLite error's error line
        from scripts.ontology_projection_probe import consumer_error
        print(json.dumps(consumer_error(exc, PROBE, phase, args.store), sort_keys=True))
        return 1
    report.update(parts, probe=PROBE, mirror_complete=build.get("mirror_complete") is True)
    print(json.dumps(report, sort_keys=True))
    ok = (report["contract_failures"] == 0 and report["process_errors"] == 0
          and report["clear_unproven"] == 0 and not report["budget_exhausted"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
