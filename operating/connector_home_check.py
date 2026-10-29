"""Live check of the connector's entry point: plx.find's home mode (operating/mcp_server.py).

``python3 -m operating.connector_home_check`` is ``devloop/live-checks/connector.home.json``'s
command: one host capture cycle into a state directory and one run of the scheduled
proactive engine over its projection into the gap inbox (as ``operating.proactive_engine``),
then, per reader, an MCP session over the stdio transport (``serve_stdio``): initialize
(the business primer), tools/list and tools/call ``plx.find`` with the home question,
timed. It prints counts, kinds and priorities only, never an identifier or a value.
With ``--portfolio`` (``devloop/live-checks/connector.portfolio.json``) each reader's
session also asks plx.find the portfolio questions (``PORTFOLIO_QUESTIONS``), scoped to
purchase orders dated in the ``PORTFOLIO_SCOPE_DAYS`` before the check's day (an unscoped
portfolio question is never scanned on the request), and, for up to
``AGREE_CAP`` of the late_po rows, plx.investigate F14, whose readable verdict is set
against the status card's (``status_card.verdict``) and the row's: counts only.
Evidence label: that of the captures it reads (live-unverified until the verifier runs it).
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import io
import json
import os
import re
from pathlib import Path
import sys
from time import monotonic
from typing import Any, Callable, Mapping, Sequence

from operating import po_status

SCHEMA = "plx-connector-home-check/v2"
TOP = 3
QUESTION = "What needs my attention?"
# plx.find's portfolio phrases (operating/mcp_portfolio.py), one per investigate plan
PORTFOLIO_QUESTIONS = ("Which POs are late and why?",
                       "What does this product really cost landed?")
AGREE_CAP = 10
# The scope the portfolio questions name (a date range on the plans' root type): a fresh
# scan runs only on an explicitly scoped question (operating/mcp_server.py _portfolio_mode).
PORTFOLIO_SCOPE_DAYS = 365


def _today() -> date:
    """The check's day when ``--as-of`` is not given (its clock; tests move it)."""
    return po_status.business_today()


def portfolio_scope(as_of: date) -> list[dict[str, Any]]:
    """The date range the portfolio questions are asked over: purchase orders dated in
    the ``PORTFOLIO_SCOPE_DAYS`` up to and including ``as_of``."""
    return [{"property": "order_date", "op": "gte",
             "value": (as_of - timedelta(days=PORTFOLIO_SCOPE_DAYS)).isoformat()},
            {"property": "order_date", "op": "lt",
             "value": (as_of + timedelta(days=1)).isoformat()}]


def _stdio(server: Any, calls: Sequence[tuple[str, dict[str, Any]]] | None = None
           ) -> list[dict[str, Any]]:
    """initialize, initialized, tools/list and plx.find's home question over
    ``serve_stdio`` (or ``calls``, numbered from 3)."""
    from .mcp_server import PROTOCOL_VERSION, serve_stdio

    calls = [("plx.find", {"query": QUESTION})] if calls is None else calls
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "connector-home-check", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        *({"jsonrpc": "2.0", "id": 3 + i, "method": "tools/call",
           "params": {"name": name, "arguments": arguments}}
          for i, (name, arguments) in enumerate(calls))]
    stdin = io.BytesIO(b"".join(json.dumps(r).encode() + b"\n" for r in requests))
    stdout = io.BytesIO()
    serve_stdio(server, input_stream=stdin, output_stream=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def _text(value: Any) -> Any:
    return value.get("untrusted_text") if isinstance(value, dict) else value


def _reader_run(server: Any) -> dict[str, Any]:
    started = monotonic()
    replies = {reply.get("id"): reply for reply in _stdio(server)}
    seconds = round(monotonic() - started, 3)
    init = (replies.get(1) or {}).get("result") or {}
    listed = ((replies.get(2) or {}).get("result") or {}).get("tools") or []
    called = (replies.get(3) or {}).get("result") or {}
    found = called.get("structuredContent") if not called.get("isError") else None
    home = (found or {}).get("home") if (found or {}).get("status") == "home" else None
    row: dict[str, Any] = {
        "transport": "stdio", "seconds": seconds,
        "primer": "personal care contract manufacturer" in str(init.get("instructions")),
        "tools": sorted(tool["name"] for tool in listed), "tool_count": len(listed),
        "listing_bytes": len(json.dumps(listed, separators=(",", ":")).encode()),
        "home_error": None if home is not None else "home_mode_not_answered"}
    home = home or {}
    gaps = home.get("gaps") or []
    row.update(inbox=_text(home.get("inbox")), gaps_returned=len(gaps),
               gaps_cited=sum(1 for gap in gaps if gap.get("citations")),
               more=bool(home.get("more")), complete=bool(home.get("complete")),
               stale=bool(home.get("stale")), not_run=home.get("not_run") or [],
               scanned=home.get("scanned", 0), unrecognized=home.get("unrecognized", 0),
               top=[{"kind": _text(gap.get("kind")), "priority": _text(gap.get("priority")),
                     "object_type": _text(gap.get("object_type"))} for gap in gaps[:TOP]])
    rules = home.get("rules") or []
    row.update(inbox_age_seconds=home.get("age_seconds"),
               rules_not_complete=sum(1 for r in rules if r.get("status") != "complete"),
               rules_untyped=sum(1 for r in rules
                                 if r.get("status") != "complete" and not r.get("reasons")))
    return row


def _verdict_state(text: Any) -> str | None:
    """The state a plx.investigate F14 readable answer states on its first line
    (``mcp_portfolio.verdict_text``), or None when it gives no verdict."""
    from .mcp_portfolio import _STATE_WORDS

    first = text.split("\n", 1)[0] if isinstance(text, str) else ""
    _name, said, rest = first.partition("): ")
    if not said:
        return None
    words = {word: state for state, word in _STATE_WORDS.items()}
    for word in sorted(words, key=len, reverse=True):
        if rest.startswith(word):
            return words[word]
    return None


def _f14_state(structured: Any) -> str | None:
    """The state a plx.investigate F14 structured result gives: its ``late`` answers
    (``late_<n>`` per PO, the plan's own select of plx.process's verdict): any true,
    all false, else undecided."""
    if not isinstance(structured, dict) or structured.get("error"):
        return None
    said = {a.get("value") for a in structured.get("answer") or ()
            if isinstance(a, dict) and isinstance(a.get("name"), str)
            and re.fullmatch(r"late(?:_[0-9]+)?", a["name"])}
    if True in said:
        return "late"
    return "on_time" if said == {False} else "undecided"


def _portfolio_run(server: Callable[[], Any], host: Any, gated: Any,
                   as_of: date) -> dict[str, Any]:
    """The portfolio questions over stdio, scoped (``portfolio_scope``), then F14 over stdio for up to ``AGREE_CAP``
    late_po rows, each F14 verdict set against the status card's for the same record and
    reader (``status_card.verdict`` through ``host`` under ``gated``, citations kept as
    the console keeps them, ``HostAccess._decide``) and the row's own; they agree when
    the F14 text, the card and the row give one state and F14's structured ``late``
    answer (``_f14_state``) is late exactly when they are (a draft or closed PO the process
    calls not late, false, is undecided on a card with no dated stage to cite: neither
    says late). ``server()``: a new
    MCP session over the reader's host per batch (a session refuses a request id twice).
    Counts only."""
    from . import status_card
    from .mcp_server import MCPServer

    started = monotonic()
    scope = portfolio_scope(as_of)
    replies = {r.get("id"): r for r in _stdio(server(), [
        ("plx.find", {"query": q, "object_type": "PurchaseOrder", "filters": scope})
        for q in PORTFOLIO_QUESTIONS])}
    asked: list[dict[str, Any]] = []
    late_rows: list[dict[str, Any]] = []
    for i, question in enumerate(PORTFOLIO_QUESTIONS):
        called = (replies.get(3 + i) or {}).get("result") or {}
        found = (called.get("structuredContent") or {}) if not called.get("isError") else {}
        portfolio = found.get("portfolio") if found.get("status") == "portfolio" else None
        portfolio = portfolio or {}
        rows = portfolio.get("rows") or []
        text = ((called.get("content") or [{}])[0]).get("text") or ""
        asked.append({
            "plan": portfolio.get("plan"), "status": found.get("status") or "error",
            "in_scope": portfolio.get("in_scope", 0),
            "not_evaluated": portfolio.get("not_evaluated", 0),
            "withheld": portfolio.get("withheld", 0), "rows": len(rows),
            "rows_cited": sum(1 for r in rows if r.get("citations")),
            "rows_with_reason": sum(1 for r in rows if not r.get("missing")),
            "rows_missing_field": sum(1 for r in rows if r.get("missing")),
            "text_bytes": len(text.encode())})
        if portfolio.get("plan") == "late_po":
            late_rows = rows
    agree = {"compared": 0, "agree": 0, "disagree": 0, "no_verdict": 0}
    ids = [r["object_id"] for r in late_rows[:AGREE_CAP] if isinstance(r.get("object_id"), str)]
    if ids:
        _policy, decide = MCPServer._decider(host, gated)
        f14 = {r.get("id"): r for r in _stdio(server(), [
            ("plx.investigate", {"family": "F14", "root": object_id}) for object_id in ids])}
        for i, (object_id, row) in enumerate(zip(ids, late_rows)):
            called = (f14.get(3 + i) or {}).get("result") or {}
            said = None if called.get("isError") else _verdict_state(
                ((called.get("content") or [{}])[0]).get("text"))
            structured = None if called.get("isError") else _f14_state(
                called.get("structuredContent"))
            card = status_card.verdict(
                lambda name, arguments: host.call(name, arguments, gated), object_id,
                as_of=_today(),
                readable=lambda hs: [h for h, why in decide(hs).items() if why is None])
            agree["compared"] += 1
            if said is None or card.get("status") != "read":
                agree["no_verdict"] += 1
            elif said == card.get("state") == row.get("state") and (
                    (structured == "late") == (said == "late")):
                agree["agree"] += 1
            else:
                agree["disagree"] += 1
    return {"questions": asked,
            "questions_answered": sum(1 for q in asked if q["rows_cited"] >= 1),
            "rows_with_reason": sum(q["rows_with_reason"] for q in asked),
            "rows_missing_field": sum(q["rows_missing_field"] for q in asked),
            "verdicts": agree, "seconds": round(monotonic() - started, 3)}


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None, graph_sender: Any = None) -> int:
    from integrations.source_mirror import AudienceMap, MirrorError
    from ontology.core import Actor

    from . import case_workspace_live as live
    from . import customer_po_coverage, proactive_engine
    from .mcp_server import MCPServer

    parser = argparse.ArgumentParser(prog="python3 -m operating.connector_home_check",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--reader", action="append", default=[], metavar="P1,P2")
    parser.add_argument("--host-config", type=Path, default=None)
    parser.add_argument("--live-source-audience", type=Path, default=None)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--portfolio", action="store_true",
                        help="also ask the portfolio questions and compare F14 verdicts")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        audience = AudienceMap.from_file(args.mirror_audience)
        readers = [live._reader(text, args.tenant, args.clearance) for text in args.reader]
    except (OSError, ValueError) as error:
        parser.error(f"invalid input: {type(error).__name__}")
    if audience.tenant != args.tenant:
        parser.error("mirror audience tenant must equal --tenant")
    as_of = args.as_of or _today()
    summary: dict[str, Any] = {"schema": SCHEMA, "evidence": live.MIRROR_EVIDENCE["label"],
                               "as_of": as_of.isoformat(), "capture": None}
    if args.host_config is not None:
        code, summary["capture"] = live._host_cycle(args, env, host_options or {})
        if code != 0:
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    investigator = Actor(args.tenant, frozenset(args.principal), args.clearance)
    readers_live = None
    if args.live_source_audience is not None:
        from .case_runtime import build_live_readers
        from .ingestion import IngestError, load_live_audience
        try:
            readers_live = build_live_readers(
                load_live_audience(args.live_source_audience, env), env,
                graph_sender=graph_sender, bc_sender=(host_options or {}).get("sender"))
        except IngestError as error:
            parser.error(f"live source audience is invalid: {error.code}")
    runtime = live.offline_runtime(args.state_dir, args.mirror_audience, args.tenant,
                                   live=readers_live, environ=env)
    projection = live.projection_file(args.state_dir, args.tenant)
    rendered = []
    try:
        live.project_state(args.state_dir, projection, tenant=args.tenant)
        summary["blocked_upstream"] = live.upstream_parts(live.portal_upstream(args.state_dir))
        # The scheduled engine's run (as operating.proactive_engine): the inbox the home
        # mode reads; plx.find never scans on the request.
        try:
            coverage = customer_po_coverage.coverage(runtime.mirror, investigator,
                                                     statuses=("absent_in_this_source",))
        except MirrorError as error:
            if error.code != "not_visible" or not args.portfolio:
                raise   # connector.home: an investigator who may not read them fails
            # --portfolio only (connector.portfolio's purchasing investigator may not read
            # the sales orders; its check is the POs): the coverage rule is not run and
            # home names why (coverage_not_given), never a clean inbox
            coverage = None
        session = live._Session(runtime, investigator, audience, projection,
                                captures=args.state_dir)
        try:
            engine = proactive_engine.run(projection, proactive_engine.inbox_path(projection),
                                          as_of, coverage=coverage, call=session)
        finally:
            session.close()
        summary["detectors"] = engine.get("detectors", {})
        for actor in (investigator, *readers):
            session = live._Session(runtime, actor, audience, projection,
                                    captures=args.state_dir)
            try:
                def server(actor: Any = actor, session: Any = session) -> Any:
                    return MCPServer(case_runtime=runtime, actor=actor, ontology=session.host)

                row = _reader_run(server())
                if args.portfolio:
                    row["portfolio"] = _portfolio_run(server, session.host,
                                                      runtime.employee_gate(actor), as_of)
            finally:
                session.close()
            rendered.append({"reader": sorted(actor.principals),
                             "investigator": actor is investigator, **row})
    finally:
        runtime.close()
    summary.update(rendered[0], readers=rendered)
    summary.pop("reader"), summary.pop("investigator")
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 1 if "failed" in summary["detectors"].values() else 0


if __name__ == "__main__":
    sys.exit(main())
