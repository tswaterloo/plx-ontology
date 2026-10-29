"""Live check of plx.around / plx.timeline (L9): counts, booleans and timings only.

    python3 -m scripts.ontology_traverse_probe --db DIR/mirror.sqlite \
        --store DIR/ontology.sqlite [--budget-seconds N] [--traverse-budget-seconds N] \
        [--sample N] [--max-rss-mb N] [--previous-report DAILY.json] \
        [--mirror-audience FILE] [--live-audience FILE]

GET only. Builds the throwaway mirror and projection store once with the L7 probe
(``scripts.ontology_projection_probe --build-only``: no rebuild and no claim checks,
determinism is ``ontology.projection``'s; its ``mirror_complete`` and parts are kept, and
a budget stop inside the build prints the build's partial counts, a failed build its
``code``, ``phase``, ``store`` and ``disk``: ``build_failure``), then
runs the L9 tools on real data under the curated schema, every call through the served
host (``operating.host.Host.served_tools`` over the build's throwaway state, the L7 probe's
``served_host``: the tools ``mcp`` serves, each call under the policy it is served with)
as the L7 probe's reader (its capture principal under the host's audiences,
``--mirror-audience`` / ``--live-audience`` passed to the build; default: granted what the
build captured):

* every declared link type, both directions, one hop from up to ``--sample``
  roots that have such a current link; every declared event type's objects through
  ``plx.timeline``; roots the store does not hold with a current version are counted per
  reason (``unsampled``), never called. Each half samples breadth first under its own
  share of the traversal budget (``check_traversal``), so the timeline half always runs.
  Each answer is validated against the frozen contract and measured
  (``max_result_bytes``, ``call_seconds``, ``max_call_seconds``, and per tool phase
  ``phases``: calls, seconds and the slowest call, with the store facts read once per
  store state under ``phases.store``; ``slowest_around``: the 5 slowest around calls'
  root type, hops, seconds, SQLite VM steps and heaviest statement's table and plan);
* ``order_violations``: events out of time order inside one comparable class;
* ``denied_leaks`` (must be 0): answers an actor with no grant gets other than
  ``object_not_found`` for the same roots (``denied_probed``), answers the reader gets
  other than ``object_not_found`` for a root its served policy withholds by audience unit
  or marking (``reader_denied_probed``, per reason ``reader_denied_by``: 0 when it is
  granted everything, so the live check runs narrowed), and every object the reader's
  answers name (reached objects, edge ends, event objects) that its policy withholds;
* ``unexpected_not_found`` (must be 0): roots the reader may read answered
  ``object_not_found``.

``--budget-seconds`` bounds the mirror sync (as in the L7 probe) and, unless
``--traverse-budget-seconds`` is given, the traversal too. The report carries the
build's ``mirror_complete`` (a traversal over a partial mirror is still checked, and
the partial mirror is visible) and the whole process's peak resident memory
(``maxrss_mb``); with ``--max-rss-mb`` the probe fails when the peak exceeds it
(decision projection-memory: the build streams per binding).

With ``--bc-capture`` the build also captures and projects BC (the L7 probe's
``--bc-capture``): every source is then one part, a Portal that is down or not configured
is reported in ``blocked_upstream`` and the traversal runs over what was projected (a
Portal outage blocks parts, not the check); when no record source was projected the
probe prints ``sources_blocked`` and the blocked parts and measures nothing. Every
handle the answers cite is resolved offline as the served host resolves it for the L7
probe's reader (``citations``: checked, resolved, unresolved, resolved by scheme;
``scripts.ontology_projection_probe.served_citations`` over ``Host.served_tools``).

The served policy grants the reader no marking and only the audience units its
audiences grant; mail, SharePoint and original items it holds no capture or grant of read
as not found. The output never contains a row value, key, identifier or citation.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import sys
import time
from typing import Any, Callable, Mapping, Sequence

PROBE = "ontology-traverse/v1"
DENIED_PRINCIPAL = "ontology-traverse-denied"
MAX_CITATIONS = 5000  # distinct cited handles resolved per run
SLOWEST_AROUND = 5  # around calls whose work the report names


def check_traversal(served: Any, actor: Any, conn: sqlite3.Connection, schema: Any, *,
                    sample: int = 50, budget_seconds: float | None = None,
                    resolve: Callable[[list[str]], dict[str, Any]] | None = None,
                    withheld: str = DENIED_PRINCIPAL,
                    monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Run plx.around / plx.timeline through ``served`` (``Host.served_tools``) as
    ``actor`` and as ``withheld`` (a principal without any grant) from roots read off the
    store ``conn``; counts only. ``resolve`` counts the cited handles that resolve
    (``served_citations``), reported as ``citations``.

    Roots are the ends of current links (per declared link type and direction) and the
    objects of current events (per declared event type) that the store holds with a
    current version; the others are never called and are counted per reason
    (``unsampled``). Each half samples breadth first (every type once before any type
    twice) under its own share of ``budget_seconds``: the around half stops at half of
    it, the timeline half at all of it (it inherits what the around half left), so both
    halves always run. ``budget_exhausted`` means a half stopped before it probed every
    type that has roots once; roots a half did not reach are ``budget_unsampled``.
    ``call_seconds`` / ``max_call_seconds`` measure each half's calls (the reader's and
    the withheld principal's) and the root reads; ``phases`` splits the tools' own time
    per phase (``operating.ontology_tools_traverse.phase_meter``) and ``slowest_around``
    names the work of the slowest around calls (``call_meter``: types, link types, index
    names and counts, never an id or a value)."""
    from functools import lru_cache

    from ontology.json_contract import validate
    from ontology.types import Actor
    from operating import ontology_tools as ot
    from operating import ontology_tools_traverse as tt
    from scripts.ontology_projection_probe import cited_handles

    started = monotonic()
    deadline = ({} if budget_seconds is None else
                {"around": started + budget_seconds / 2, "timeline": started + budget_seconds})
    cited: set[str] = set()
    contracts = served.tools.contracts
    policy = served.policy_now(actor)
    held = policy.held_markings(actor)
    denied_actor = Actor(actor.tenant, frozenset({withheld}), "restricted")
    report: dict[str, Any] = {"probe": PROBE, "around_calls": 0, "around_reached": 0,
                              "around_edges": 0, "around_cited_edges": 0, "timeline_calls": 0,
                              "timeline_events": 0, "order_violations": 0,
                              "contract_failures": 0, "error_codes": {}, "max_result_bytes": 0,
                              "denied_probed": 0, "reader_denied_probed": 0,
                              "reader_denied_by": {}, "unexpected_not_found": 0,
                              "denied_leaks": 0, "unsampled": {}, "budget_exhausted": False,
                              "budget_unsampled": {"around": 0, "timeline": 0},
                              "around_types": 0, "around_types_probed": 0,
                              "timeline_types": 0, "timeline_types_probed": 0,
                              "call_seconds": {"roots": 0.0, "around": 0.0, "timeline": 0.0},
                              "max_call_seconds": {"around": 0.0, "timeline": 0.0}}

    @lru_cache(maxsize=1 << 16)
    def withheld_by(object_id: str) -> str | None:
        """Why the reader's served policy withholds an object (``audience``: its audience
        unit is not granted, ``marking``: a current version carries a marking the reader
        does not hold; ``absent_from_store``), or None when it may read it."""
        row = conn.execute("SELECT audience_key FROM objects WHERE object_id = ?",
                           (object_id,)).fetchone()
        if row is None:
            return "absent_from_store"
        if not policy.may_read(actor, row[0]):
            return "audience"
        marks = conn.execute("SELECT markings FROM object_versions WHERE object_id = ? "
                             "AND superseded_at IS NULL", (object_id,))
        return "marking" if any(not _markings(m) <= held for (m,) in marks) else None

    def not_found(out: Mapping[str, Any]) -> bool:
        return out.get("error", {}).get("code") == "object_not_found"

    def run(name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        out = served.call(name, dict(args), actor)
        if len(cited) < MAX_CITATIONS:
            cited.update(cited_handles(out))
        if validate(out, contracts["tools"][name]["output"], root=contracts, limit=1):
            report["contract_failures"] += 1
        report["max_result_bytes"] = max(report["max_result_bytes"], ot._envelope_bytes(out))
        if "error" in out:
            code = out["error"]["code"]
            report["error_codes"][code] = report["error_codes"].get(code, 0) + 1
        reason = withheld_by(args["from"])
        if reason is not None:
            report["reader_denied_probed"] += 1
            report["reader_denied_by"][reason] = report["reader_denied_by"].get(reason, 0) + 1
            report["denied_leaks"] += not not_found(out)
        else:
            report["unexpected_not_found"] += not_found(out)
            report["denied_leaks"] += sum(1 for object_id in _named(out)
                                          if withheld_by(object_id) is not None)
        report["denied_probed"] += 1
        report["denied_leaks"] += not not_found(served.call(name, dict(args), denied_actor))
        return out

    def roots(ends_sql: str, kinds: Sequence[str]) -> list[tuple[str, list[str]]]:
        """(kind, sampled roots) per kind with roots; the rest counted in ``unsampled``."""
        tick = monotonic()
        sampled, unsampled = _sample_roots(conn, ends_sql, sample)
        for reason, count in unsampled.items():
            report["unsampled"][reason] = report["unsampled"].get(reason, 0) + count
        report["call_seconds"]["roots"] += monotonic() - tick
        return [(kind, sampled[kind]) for kind in kinds if kind in sampled]

    def half(name: str, plans: list[tuple[Any, list[str]]],
             call: Callable[[Any, str], None]) -> None:
        """Probe ``plans`` breadth first until the half's deadline."""
        queue = [(kind, rs[depth]) for depth in range(sample) for kind, rs in plans
                 if depth < len(rs)]
        probed: set[Any] = set()
        for index, (kind, root) in enumerate(queue):
            if name in deadline and monotonic() > deadline[name]:
                report["budget_unsampled"][name] = len(queue) - index
                break
            tick = monotonic()
            call(kind, root)
            spent = monotonic() - tick
            report["call_seconds"][name] += spent
            report["max_call_seconds"][name] = max(report["max_call_seconds"][name], spent)
            probed.add(kind)
        report[f"{name}_types"] = len(plans)
        report[f"{name}_types_probed"] = len(probed)
        if len(probed) < len(plans):
            report["budget_exhausted"] = True

    def around(kind: tuple[str, str], root: str) -> None:
        link_id, direction = kind
        out = run("plx.around", {"from": root, "limit": 50, "path": [
            {"link_type": link_id, "direction": direction}]})
        report["around_calls"] += 1
        if "error" not in out:
            report["around_reached"] += out["hops"][0]["count"] or 0
            report["around_edges"] += len(out["edges"])
            report["around_cited_edges"] += sum(
                1 for e in out["edges"] if any(f.get("citations") for f in e["reason"]))

    def timeline(kind: str, root: str) -> None:
        out = run("plx.timeline", {"from": root, "limit": 50})
        report["timeline_calls"] += 1
        if "error" in out:
            return
        report["timeline_events"] += len(out["events"])
        last: dict[int, str] = {}
        for event in out["events"]:
            klass = tt.order_class(event["time_basis"], event["citation"]["lineage"])
            moment = event["time"] or event["window"]["start"]
            start = tt._store_time(moment) or ""
            if klass in last and start < last[klass]:
                report["order_violations"] += 1
            last[klass] = start

    plans: list[tuple[Any, list[str]]] = []
    for direction, column in (("forward", "from_object"), ("reverse", "to_object")):
        plans += [((link_id, direction), rs) for link_id, rs in roots(
            f"SELECT DISTINCT link_type AS kind, {column} AS oid FROM links "
            "WHERE superseded_at IS NULL", sorted(schema.link_types))]
    with tt.phase_meter() as phases, tt.call_meter() as around_work:
        # The host opens the store generation before it serves: the store facts every
        # reader shares are read here (reported under phases.store and as
        # open_generation_seconds), not inside the first call.
        opened = monotonic()
        open_generation = getattr(served, "open_generation", None)
        if open_generation is not None:
            open_generation()
        spent_opening = monotonic() - opened
        report["open_generation_seconds"] = round(spent_opening, 3)
        for half_name in deadline:   # opening is not the calls' budget
            deadline[half_name] += spent_opening
        half("around", plans, around)
        half("timeline", roots(
            "SELECT DISTINCT e.event_type AS kind, eo.object_id AS oid FROM event_objects eo "
            "JOIN events e ON e.event_id = eo.event_id WHERE e.superseded_at IS NULL",
            sorted(schema.event_types)), timeline)
    report["phases"] = {tool: {phase: {"calls": int(calls), "seconds": round(spent, 3),
                                       "max_seconds": round(slowest, 3)}
                               for phase, (calls, spent, slowest) in sorted(laps.items())}
                        for tool, laps in sorted(phases.items())}
    report["slowest_around"] = sorted(around_work,
                                      key=lambda e: -e["seconds"])[:SLOWEST_AROUND]
    if resolve is not None:
        report["citations"] = resolve(sorted(cited)[:MAX_CITATIONS])
    for key in ("call_seconds", "max_call_seconds"):
        report[key] = {k: round(v, 3) for k, v in report[key].items()}
    report["seconds"] = round(monotonic() - started, 3)
    return report


def _sample_roots(conn: sqlite3.Connection, ends_sql: str, sample: int
                  ) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Up to ``sample`` roots per kind (by object id) among the ``(kind, oid)`` rows of
    ``ends_sql`` that the store holds with a current version, in one read; the other
    distinct ends counted per reason (``absent_from_store``, ``no_current_version``)."""
    rows = conn.execute(
        "SELECT kind, oid, state, n FROM (SELECT kind, oid, state, ROW_NUMBER() OVER "
        "(PARTITION BY kind, state ORDER BY oid) AS rn, COUNT(*) OVER "
        "(PARTITION BY kind, state) AS n FROM (SELECT r.kind, r.oid, CASE "
        "WHEN NOT EXISTS (SELECT 1 FROM objects o WHERE o.object_id = r.oid) "
        "THEN 'absent_from_store' WHEN NOT EXISTS (SELECT 1 FROM object_versions v "
        "WHERE v.object_id = r.oid AND v.superseded_at IS NULL) THEN 'no_current_version' "
        f"END AS state FROM ({ends_sql}) r)) "
        "WHERE (state IS NULL AND rn <= ?) OR (state IS NOT NULL AND rn = 1)", (sample,))
    sampled: dict[str, list[str]] = {}
    unsampled: dict[str, int] = {}
    for kind, oid, state, count in rows:
        if state is None:
            sampled.setdefault(kind, []).append(oid)
        else:
            unsampled[state] = unsampled.get(state, 0) + count
    return {kind: sorted(oids) for kind, oids in sampled.items()}, unsampled


def _markings(text: Any) -> frozenset[str]:
    """A stored markings list; unreadable markings read as a marking no reader holds."""
    try:
        values = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        values = None
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return frozenset({"<unreadable>"})
    return frozenset(values)


def _named(out: Mapping[str, Any]) -> set[str]:
    """Every object an answer names: reached objects, edge ends and event objects."""
    named = {ref["object_id"] for ref in out.get("objects") or ()}
    for edge in out.get("edges") or ():
        named.update((edge["from"], edge["to"]))
    for event in out.get("events") or ():
        named.update(ref["object_id"] for ref in event["objects"])
    return named


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         bc_sender: Any = None, bc_books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.ontology_traverse_probe")
    parser.add_argument("--db", required=True, help="throwaway mirror file")
    parser.add_argument("--store", required=True, help="throwaway ontology store file")
    parser.add_argument("--budget-seconds", type=float, default=None)
    parser.add_argument("--traverse-budget-seconds", type=float, default=None,
                        help="traversal budget (default: --budget-seconds)")
    parser.add_argument("--sample", type=int, default=50)
    parser.add_argument("--max-rss-mb", type=float, default=None,
                        help="peak resident memory budget for the whole probe")
    parser.add_argument("--previous-report", default=None,
                        help="last daily verifier report, passed to the L7 build")
    parser.add_argument("--bc-capture", action="store_true",
                        help="capture and project BC too (the L7 probe's --bc-capture)")
    parser.add_argument("--mirror-audience", default=None,
                        help="the served host's plx-source-mirror-audience/v1 file (the L7 "
                             "probe's --mirror-audience)")
    parser.add_argument("--live-audience", default=None,
                        help="the served host's plx-live-source-audience/v1 file (the L7 "
                             "probe's --live-audience)")
    parser.add_argument("--session-projection", default=None,
                        help="the verifier session's projection (the L7 probe's "
                             "--session-projection)")
    args = parser.parse_args(argv)
    phase = None   # the consumer's own phase, once the build succeeded
    try:
        from ontology import schema as schema_loader  # L4
        from operating import ontology_tools as ot
        from scripts import ontology_projection_probe as projection  # L7

        build_args = ["--db", args.db, "--store", args.store, "--build-only"]
        if args.budget_seconds is not None:
            build_args += ["--budget-seconds", str(args.budget_seconds)]
        if args.previous_report is not None:
            build_args += ["--previous-report", args.previous_report]
        if args.bc_capture:
            build_args.append("--bc-capture")
        for flag, value in (("--mirror-audience", args.mirror_audience),
                            ("--live-audience", args.live_audience),
                            ("--session-projection", args.session_projection)):
            if value is not None:
                build_args += [flag, value]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            built = projection.main(build_args, environ=environ, bc_sender=bc_sender,
                                    bc_books=bc_books)
        if built == projection.EXIT_BUDGET:   # the build's allowlisted partial report
            partial = projection.budget_partial(json.loads(out.getvalue()))   # its counts
            print(json.dumps({**partial, "probe": PROBE, "status": "budget_exhausted",
                              "phase": partial.get("phase"), "stage": "projection_build"},
                             sort_keys=True))
            return projection.EXIT_BUDGET
        if built != 0:
            # the build's own error line: its code, phase, store and disk (build_failure)
            print(json.dumps({**projection.build_failure(out.getvalue()), "probe": PROBE,
                              "error": "projection_build_failed",
                              "stage": "projection_build"}, sort_keys=True))
            return 1
        build = json.loads(out.getvalue())
        parts = {key: build[key] for key in ("blocked_upstream", "bc_capture", "sources",
                                            "session_projection") if key in build}
        if build.get("sources_blocked"):   # nothing was projected: measured nothing
            print(json.dumps({"probe": PROBE, "sources_blocked": build["sources_blocked"],
                              **parts}, sort_keys=True))
            return 0
        phase = "traversal"
        schema = ot.SchemaView.from_compiled(schema_loader.load_schema())
        budget = (args.budget_seconds if args.traverse_budget_seconds is None
                  else args.traverse_budget_seconds)
        host = projection.served_host(args.store, environ)
        reader = projection.reader_actor(host)
        with contextlib.closing(sqlite3.connect(f"file:{args.store}?mode=ro", uri=True,
                                                check_same_thread=False)) as conn, \
                host.served_tools(reader) as served:
            report = check_traversal(served, reader, conn, schema, sample=args.sample,
                                     budget_seconds=budget,
                                     resolve=projection.served_citations(served, reader))
        peak = projection._maxrss_mb()
    except Exception as exc:  # noqa: BLE001 - a class name, or a SQLite error's error line
        from scripts.ontology_projection_probe import consumer_error
        print(json.dumps(consumer_error(exc, PROBE, phase, args.store), sort_keys=True))
        return 1
    report.update(parts)
    report.update(mirror_complete=build.get("mirror_complete") is True, maxrss_mb=peak,
                  max_rss_budget_mb=args.max_rss_mb,
                  maxrss_within_budget=(None if args.max_rss_mb is None or peak is None
                                        else peak <= args.max_rss_mb))
    print(json.dumps(report, sort_keys=True))
    ok = (report["contract_failures"] == 0 and report["denied_leaks"] == 0
          and report["citations"]["unresolved"] == 0
          and report["order_violations"] == 0 and not report["budget_exhausted"]
          and report["unexpected_not_found"] == 0
          and report["maxrss_within_budget"] is not False)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
