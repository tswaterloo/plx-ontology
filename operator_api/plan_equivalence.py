"""Live equivalence of a record page's plan sections: the section read by running the plan for the
reader (today's path) against the precomputed row (``operator_api.plan_section_door``), for N real
records and each reader class, with first-read timings. Run on a Spark like the batch runner
(``deploy/spark/plan_batch.sh`` shows the sandbox; the readers are employees of the host config):

  python3 -m operator_api.plan_equivalence --ids FILE --reader NAME=EMPLOYEE_ID ... [--family late_po]

A record is ``same`` when the precomputed section's model equals the live one, ``fallback`` when
the door gave no row for this reader (the page reads the plan live: always safe), ``DIFFERENT``
when both answered and they differ (a defect: exit 1), ``no live`` when the live plan itself
gave nothing. What the model holds is exactly what the section's presentation reads of the run
(``page_parts.late_of``): the status and text and the cited lines the reader may open.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from typing import Any, Mapping, Optional, Sequence

from operating.evidence_trail import FAILED_GAPS


def model(ran: Optional[Mapping[str, Any]], family: str = "late_po", view: Any = None
          ) -> Optional[tuple]:
    """The section's own reading of a run: the late section's inputs (``late_of``'s), or the
    people ``people_of`` names for this reader (their name, roles and cited handle)."""
    if family == "people_on_record":
        from types import SimpleNamespace
        from operator_api import models as m
        from operator_api.page_parts import people_of
        stub = SimpleNamespace(_source=lambda v, h: m.Source(system="Email", handle=h, label=h))
        got = people_of(stub, view, ran or {})
        return None if got is None else tuple((p.name, p.role, p.sources[0].handle) for p in got)
    if family == "po_trail":
        from types import SimpleNamespace
        from operator_api import models as m
        from operator_api.trail import Mapper
        raw = (ran or {}).get("trail")
        if not isinstance(raw, Mapping):
            return None
        stub = SimpleNamespace(_source=lambda v, h, openable=None: m.Source(
            system="Email", handle=h, label=h))
        mapper = Mapper(stub, view, object_url=lambda a, b: "", open_url=lambda h: "",
                        host_schemes=())
        return (mapper.po({**raw, "label": "x"}).model_dump_json(), mapper.hidden)
    if family == "landed_cost":
        from types import SimpleNamespace
        from operator_api import models as m
        from operator_api.page_parts import landed_cost_of
        stub = SimpleNamespace(_source=lambda v, h: m.Source(system="Email", handle=h, label=h))
        got = landed_cost_of(stub, view, ran or {})
        return (got.status, got.total.model_dump_json() if got.total else None,
                tuple((l.label, l.amount.model_dump_json(), tuple(s.handle for s in l.sources))
                      for l in got.lines), tuple(got.missing))
    late = (ran or {}).get("verdict")
    if not isinstance(late, Mapping):
        return None
    if late.get("state") in (None, "not_established"):
        return (None, ())
    cited = tuple((str(f.get("text") or "")[:300], f["citation"]) for f in late.get("facts") or ()  # guard-ok: the equivalence tool's own comparison mirrors the page's reading / console print
                  if isinstance(f, Mapping) and isinstance(f.get("citation"), str)
                  and f.get("openable"))[:20]
    return (str(late.get("state")), str(late.get("text"))[:600], cited)  # guard-ok: the equivalence tool's own comparison mirrors the page's reading / console print


NOTE = None


def without_capture_note(value: Any) -> Any:
    """``value`` with the engine's ``(as captured: the <source> capture is stale)`` note taken
    out of every string in it (it states a source's freshness at the moment the answer was made)."""
    import re
    global NOTE
    NOTE = NOTE or re.compile(r" \(as captured: [^)]*\)")
    if isinstance(value, str):
        return NOTE.sub("", value)
    if isinstance(value, (list, tuple)):
        return type(value)(without_capture_note(v) for v in value)
    return value


def first_difference(live: Any, item: Any, path: str = "", depth: int = 0) -> str:
    """Where two models first differ (JSON strings are read), short enough to print."""
    import json
    if isinstance(live, str) and isinstance(item, str) and live[:1] in "{[":
        try:
            live, item = json.loads(live), json.loads(item)
        except ValueError:
            pass
    if isinstance(live, dict) and isinstance(item, dict) and depth < 6:
        for k in sorted(set(live) | set(item)):
            if live.get(k) != item.get(k):
                return first_difference(live.get(k), item.get(k), f"{path}.{k}", depth + 1)
    if isinstance(live, (list, tuple)) and isinstance(item, (list, tuple)) and depth < 6:
        if len(live) != len(item):
            return (f"{path}: live has {len(live)} items, stored {len(item)}; live {str(live)[:300]} "  # guard-ok: the equivalence tool's own comparison mirrors the page's reading / console print
                    f"stored {str(item)[:300]}")  # guard-ok: the equivalence tool's own comparison mirrors the page's reading / console print
        for i, (a, b) in enumerate(zip(live, item)):
            if a != b:
                return first_difference(a, b, f"{path}[{i}]", depth + 1)
    return f"{path}: live {str(live)[:200]!r} stored {str(item)[:200]!r}"  # guard-ok: the equivalence tool's own comparison mirrors the page's reading / console print


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operator_api.plan_equivalence")
    ap.add_argument("--ids", required=True)
    ap.add_argument("--reader", action="append", required=True, help="NAME=EMPLOYEE_ID")
    ap.add_argument("--family", default="late_po", choices=("late_po", "people_on_record", "landed_cost", "po_trail"))
    ap.add_argument("--n", type=int, default=60)
    args = ap.parse_args(argv)
    from operator_api import plan_section_door as door
    from operator_api.backend import plan_runs
    from operator_api.cli import console_argv
    from operator_api.server import build_parser
    from operator_api.workspace import case_console
    from operating.ontology_tools_policy import snapshot_reads
    from operating.session_identity import warm_session
    state = os.environ.get("PLX_WORKBENCH_STATE_DIR", "/data/plx-state")
    parser = build_parser()
    ns = parser.parse_args(["--dev", "--dev-ticket-login", "--state-dir", state, "--host-config",
                            os.environ["PLX_BATCH_HOST_CONFIG"], "--pg-dsn",
                            os.environ["PLX_PG_DSN"], "--app-data-dir", state + "/app-data"])
    with open(args.ids, encoding="utf-8") as fh:
        ids = [x.rstrip("\n") for x in fh if x.strip()][: args.n]
    bad = 0

    def serve(ctx: dict[str, Any]) -> int:
        nonlocal bad
        os.environ[door.SWITCH] = "1"
        for pair in args.reader:
            name, _, employee = pair.partition("=")
            sess = warm_session(ctx["resolver"], employee, expires_at=int(time.time()) + 7200)
            view = ctx["objects_for"](sess)
            same = fallback = differ = nolive = note_only = stale_ok = old_differ = 0
            why: dict[str, int] = {}
            errors: dict[str, int] = {}
            live_t, pre_t = [], []
            for oid in ids:
                t = time.monotonic()
                rates_before = door.rates()
                if args.family == "po_trail":
                    from operator_api import trail as trails   # the page's own path to the row
                    one = trails.stored_trail(view, oid, None)
                    got = {"trail": one} if one is not None else None
                else:
                    got = door.precomputed(view, "PurchaseOrder", oid, args.family)
                pre_t.append(time.monotonic() - t)
                rates_after = door.rates()
                outcome = next((k for k, v in rates_after.items() if v > rates_before.get(k, 0)),
                               "?")
                t = time.monotonic()
                try:
                    with view._request(), snapshot_reads():
                        if args.family == "po_trail":
                            from operating import evidence_trail as et
                            live = {"trail": et.po_trail(
                                lambda n, a: view._call(n, dict(a)), oid, seconds=60.0)}
                        else:
                            live = plan_runs(view, "PurchaseOrder", oid, (args.family,)).get(
                                args.family)
                except Exception as exc:  # noqa: BLE001 - the live page leaves the plan out
                    live = None
                    key = f"{type(exc).__name__}: {str(exc)[:80]}"  # guard-ok: the equivalence tool's own comparison mirrors the page's reading / console print
                    errors[key] = errors.get(key, 0) + 1
                live_t.append(time.monotonic() - t)
                if args.family == "po_trail" and live is not None and any(
                        isinstance(g, Mapping) and g.get("reason") in FAILED_GAPS
                        for g in (live.get("trail") or {}).get("gaps") or ()):
                    live = None      # the live read itself ran out of time: it is no answer to compare
                if live is None:
                    nolive += 1
                elif got is None:
                    fallback += 1
                    why[outcome] = why.get(outcome, 0) + 1
                elif model(got, args.family, view) == model(live, args.family, view):
                    same += 1
                    stale_ok += outcome == "served_stale"
                elif without_capture_note(model(got, args.family, view)) == without_capture_note(
                        model(live, args.family, view)):
                    # the only difference is the note that a source's capture was stale when the
                    # row was made (or is now): a fact of the moment, not of the record. Counted
                    # apart and printed, never folded into "same"
                    note_only += 1
                elif outcome == "served_stale":
                    # an OLD row (its dependencies moved or it is from an earlier day) read as such,
                    # with its age: it differs from the live plan because the record moved since,
                    # until the resident renews it. Counted and printed apart, never as "same"
                    old_differ += 1
                    print(f"DIFFERENT-OLD {name} {oid[:80]}\n  {first_difference(model(live, args.family, view), model(got, args.family, view))}",
                          flush=True)
                else:
                    differ += 1
                    print(f"DIFFERENT {name} {oid[:80]}\n  {first_difference(model(live, args.family, view), model(got, args.family, view))}",  # guard-ok: the equivalence tool's own comparison mirrors the page's reading / console print
                          flush=True)
            bad += differ
            print(f"{name}: {len(ids)} records: same {same}, fallback {fallback}, DIFFERENT "
                  f"{differ}, no live {nolive} (of the same, {stale_ok} served as old rows; "
                  f"capture-note-only differences {note_only}; DIFFERENT-OLD (an old row that the record moved past) {old_differ}; fallbacks by reason {why}); first read live median {statistics.median(live_t):.2f}s "
                  f"max {max(live_t):.2f}s, precomputed median {statistics.median(pre_t) * 1000:.0f}ms "
                  f"max {max(pre_t) * 1000:.0f}ms; live errors {errors}", flush=True)
        return 1 if bad else 0
    return case_console.main(console_argv(parser, ns, "dev"), serve_with=serve)


if __name__ == "__main__":
    sys.exit(main())
