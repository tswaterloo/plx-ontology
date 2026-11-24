"""The batch plan runner: a record page's question plans (``late_po`` ...) run once per record,
off the box, for ``operating.pg.plan_sections`` to store.

It runs on a Spark (``deploy/spark/plan_batch.sh`` through ``spark_run.sh --pg standby``: the
release's own code, the Spark's copy of the state, a read-only login on the s7d3d standby) as the
app's own console builder (``case_console.main``, as the read-ahead process does), as the owner's
full reader: a plan's answer is the same for every reader who may open what it cites, so one
run as a reader who may open everything is tagged afterwards by what it cited. It writes
nothing to any business system or to Postgres: one JSON line per answer, to ``--out``, which
``spark_run.sh`` copies back for ``python3 -m operating.pg.plan_sections load``.

  python3 -m operator_api.plan_batch run --out F [--family late_po] [--ids FILE | --open]
                                          [--shard I/N] [--host-config PATH]

The first line is ``{"stamps": ...}``: the order views' build stamps the standby held at the
start (what the answers were computed from, for the staleness check). A plan that raises or
answers nothing is left out (the page then reads it live).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Sequence

LOG = logging.getLogger(__name__)

FAMILIES = ("late_po", "people_on_record", "landed_cost", "po_trail")
HOST_CONFIG_ENV = "PLX_BATCH_HOST_CONFIG"
READER_ENV = "PLX_BATCH_READER"
# an order is covered when it is still in play: open, draft or unknown status, or a verdict
# that says late/unknown (the pages people open: a closed order's plan is read live)
OPEN_SQL = (
    "SELECT DISTINCT i.page_key AS record_id FROM {s}.page_items i JOIN {s}.purchase_orders p "
    "ON p.handle = i.src_handle WHERE i.page_kind = 'record' AND i.section = 'header' AND "
    "(p.verdict IN ('late', "
    "'unknown') OR p.status IN ('open', 'draft', 'unknown')) ORDER BY 1")


def targets(dsn: str, schema: str = "biz") -> tuple[list[str], dict[str, str]]:
    """The records to run and the stamps of what they are computed from."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as pg:
        ids = [r[0] for r in pg.execute(OPEN_SQL.format(s=schema))]
        return ids, stamps_of(pg, schema)


def verdict_of(ran: Any) -> dict[str, Any] | None:
    """The plan run's verdict as ``plan_sections.item`` reads it; None: nothing to store."""
    v = (ran or {}).get("verdict") if isinstance(ran, dict) else None
    if not isinstance(v, dict) or not isinstance(v.get("state"), str):
        return None
    return {"state": v["state"], "text": v.get("text") if isinstance(v.get("text"), str) else "",
            "facts": [{"text": str(f.get("text") or ""), "citation": f.get("citation"),
                       "openable": f.get("openable") is True}
                      for f in v.get("facts") or () if isinstance(f, dict)]}


def extract(family: str, ran: Any) -> dict[str, Any] | None:
    """What a family's section reads of its run, as ``plan_sections.item`` takes it: the verdict
    (late), or the plan's own computed entries (people). A run whose investigation carries an
    error answered nothing: None."""
    if family == "po_trail":
        return {"trail": ran} if isinstance(ran, dict) else None
    if family == "late_po":
        v = verdict_of(ran)
        return None if v is None else {"verdict": v}
    inv = (ran or {}).get("investigation") if isinstance(ran, dict) else None
    if not isinstance(inv, dict) or inv.get("error") or not isinstance(inv.get("answer"), list):
        return None
    if family == "landed_cost":
        verdict = ran.get("verdict") if isinstance(ran.get("verdict"), dict) else {}
        keep = ("name", "kind", "value", "currency", "unit", "citation", "inputs")
        return {"landed": {
            "entries": [{k: e[k] for k in keep if k in e} for e in inv["answer"]
                        if isinstance(e, dict)],
            "answerability": (inv.get("answerability") or {}).get("state"),
            "missing": [f.get("missing") for f in verdict.get("follow_ups") or ()
                        if isinstance(f, dict) and f.get("missing")],
            "verdict_text": verdict.get("text") if isinstance(verdict.get("text"), str) else None}}
    # the people are found through everything the plan read (a mail that links the order to a task,
    # a task's events), not only through the entries that name them: a reader who may not open
    # that evidence gets a different live answer, so every handle the whole answer cites is kept
    # as the row's dependency (``plan_sections._people``), and a reader missing one reads live
    read: list[str] = []
    for e in inv["answer"]:
        if not isinstance(e, dict):
            continue
        for c in (e.get("citation"), (e.get("value") or {}).get("origin", {}).get("citation")
                  if isinstance(e.get("value"), dict) and isinstance(e["value"].get("origin"), dict)
                  else None):
            h = c.get("l1_handle") if isinstance(c, dict) else c
            if isinstance(h, str) and h:
                read.append(h)
    return {"entries": [e for e in inv["answer"] if isinstance(e, dict) and e.get("kind") == "computed"
                        and any(isinstance(x, str) and x.startswith("roles: ")
                                for x in e.get("steps") or ())],
            "read": list(dict.fromkeys(read))}


def console_args(host: str, dsn: str) -> list[str]:
    """The app's own console arguments (the host config it serves with, the Postgres it reads),
    as ``case_console.main`` takes them."""
    from operator_api.server import build_parser
    from operator_api.cli import console_argv
    state = os.environ.get("PLX_WORKBENCH_STATE_DIR", "/data/plx-state")
    parser = build_parser()
    ns = parser.parse_args(["--dev", "--dev-ticket-login", "--state-dir", state,
                            "--host-config", host, "--pg-dsn", dsn, "--app-data-dir",
                            os.path.join(state, "app-data")])
    return console_argv(parser, ns, "dev")


def stamps_of(pg: Any, schema: str = "biz", quick: bool = False) -> dict[str, str]:
    """What an answer started now is computed from: the order views' build stamps and the two
    watermarks (``plan_sections.VER_SQL``, ``LINK_SQL``) of the database being read. ``quick``:
    the view stamps and the watermarks only (the resident runner takes them for every record;
    the record pages' links stamp, a scan of every order's links, is for a batch's one reading)."""
    from operating.pg import plan_sections, record_pages
    views = ({v: b for v, b in pg.execute(f"SELECT view, built_at FROM {schema}.state WHERE view = "
                                          "ANY(%s)", (list(record_pages.SOURCES),))}
             if quick else record_pages._stamps(pg, schema))
    return {**views,
            "ver": str(pg.execute(plan_sections.VER_SQL).fetchone()[0]),
            "link": str(pg.execute(plan_sections.LINK_SQL).fetchone()[0]),
            "xmin": str(pg.execute(plan_sections.XMIN_SQL).fetchone()[0])}


def answer(view: Any, oid: str, family: str) -> dict[str, Any] | None:
    """One record's answer of one family as a runner line. A plan that raised or gave no usable
    answer is a line with ``failed: <reason>`` (stored typed, run again), never dropped. The trail
    has no time limit here (a cut trail reads on)."""
    from operating.ontology_tools_policy import current_snapshot, opened_in, snapshot_reads
    from operating.pg import plan_sections
    from operator_api.backend import plan_runs
    began = time.monotonic()
    try:
        if family == "po_trail":
            from operating import evidence_trail as et
            with view._request(), snapshot_reads():
                ran = et.po_trail(lambda name, args: view._call(name, dict(args)), oid,
                                  seconds=None)
                opened = opened_in(current_snapshot())
        else:
            with view._request(), snapshot_reads():
                ran = plan_runs(view, "PurchaseOrder", oid, (family,)).get(family)
                opened = opened_in(current_snapshot())
    except Exception as exc:  # noqa: BLE001 - said: a failed line the runner stores and runs again
        LOG.warning("plan run failed: %s %s %s: %s", family, oid, type(exc).__name__, str(exc)[:200])  # guard-ok: a log line shortens the error text
        return {"record_id": oid, "family": family, "failed": f"the plan raised {type(exc).__name__}",
                "took": round(time.monotonic() - began, 2),
                "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    got = extract(family, ran)
    if got is None:
        # the run gave no usable answer (its investigation carries an error, no verdict): a FAILED
        # line with the exact reason, which ``plan_sections.stage`` stores typed and the runner
        # runs again (never stored as the plan's "no answer")
        got = {"failed": failure_of(family, ran)}
    # the run's status from its structure (``plan_sections.run_status``): what ``plan_sections.outcome``
    # reads to tell a failed run from a true "no answer", never the verdict's words. The trail
    # states its own in its gaps.
    status = {} if family == "po_trail" else {"run": plan_sections.run_status(ran)}
    # every handle the run asked the door about, cited or not: the row depends on all of them
    return {"record_id": oid, "family": family, **got, **status, "opened": opened, "took": round(time.monotonic() - began, 2),
            "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def failure_of(family: str, ran: Any) -> str:
    """The exact reason a family's run gave no usable answer (``extract`` returned None)."""
    if family == "po_trail":
        return "the trail run returned no trail"
    if family == "late_po":
        return "the run returned no verdict"
    inv = (ran or {}).get("investigation") if isinstance(ran, dict) else None
    if not isinstance(inv, dict):
        return "the run returned no investigation"
    if inv.get("error"):
        err = inv["error"]
        code = err.get("code") if isinstance(err, dict) else None
        return "the investigation failed: " + str(code or err)
    return "the investigation returned no answer list"


def run(args: argparse.Namespace) -> int:
    from operator_api.workspace import case_console
    host = args.host_config or os.environ.get(HOST_CONFIG_ENV)
    reader = os.environ.get(READER_ENV)
    dsn = os.environ.get("PLX_PG_DSN")
    if not host or not reader or not dsn:
        print(f"{HOST_CONFIG_ENV}, {READER_ENV} and PLX_PG_DSN are required", file=sys.stderr)
        return 2
    known = [f for f in args.family if f in FAMILIES]
    if len(known) != len(args.family) or not known:
        print(f"families: {', '.join(FAMILIES)}", file=sys.stderr)
        return 2
    index, count = (int(x) for x in args.shard.split("/"))
    if not 0 <= index < count:
        print("shard is I/N with 0 <= I < N", file=sys.stderr)
        return 2
    if args.ids:
        with open(args.ids, encoding="utf-8") as fh:
            ids, stamps = [x.rstrip("\n") for x in fh if x.strip()], targets(dsn)[1]
    else:
        ids, stamps = targets(dsn)
    ids = ids[index::count]
    argv = console_args(host, dsn)

    def serve(ctx: dict[str, Any]) -> int:
        from operating.session_identity import warm_session
        session = warm_session(ctx["resolver"], reader, expires_at=int(time.time()) + 6 * 3600)
        view = ctx["objects_for"](session)
        with open(args.out, "w", encoding="utf-8") as out:
            out.write(json.dumps({"stamps": stamps, "shard": args.shard}) + "\n")
            for oid in ids:
                for family in known:
                    line = answer(view, oid, family)
                    if line is not None:
                        out.write(json.dumps(line) + "\n")
                        out.flush()
        return 0
    return case_console.main(argv, serve_with=serve)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operator_api.plan_batch")
    ap.add_argument("command", choices=("run",))
    ap.add_argument("--out", required=True)
    ap.add_argument("--family", action="append", default=[])
    ap.add_argument("--ids")
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--host-config")
    args = ap.parse_args(argv)
    args.family = args.family or list(FAMILIES)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
