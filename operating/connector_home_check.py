"""Live check of the connector's entry point: plx.find's home mode (operating/mcp_server.py).

``python3 -m operating.connector_home_check`` is ``verify/live-checks/connector.home.json``'s
command: one host capture cycle into a state directory and one run of the scheduled
proactive engine over its projection into the gap inbox (as ``operating.proactive_engine``),
then, per reader, an MCP session over the stdio transport (``serve_stdio``): initialize
(the business primer), tools/list and tools/call ``plx.find`` with the home question,
timed. It prints counts, kinds and priorities only, never an identifier or a value.
Evidence label: that of the captures it reads (live-unverified until the verifier runs it).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
from time import monotonic
from typing import Any, Mapping, Sequence

SCHEMA = "plx-connector-home-check/v2"
TOP = 3
QUESTION = "What needs my attention?"


def _stdio(server: Any) -> list[dict[str, Any]]:
    """initialize, initialized, tools/list and plx.find's home question over
    ``serve_stdio``."""
    from .mcp_server import PROTOCOL_VERSION, serve_stdio

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "connector-home-check", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "plx.find", "arguments": {"query": QUESTION}}}]
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
    return row


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None, graph_sender: Any = None) -> int:
    from integrations.source_mirror import AudienceMap
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
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        audience = AudienceMap.from_file(args.mirror_audience)
        readers = [live._reader(text, args.tenant, args.clearance) for text in args.reader]
    except (OSError, ValueError) as error:
        parser.error(f"invalid input: {type(error).__name__}")
    if audience.tenant != args.tenant:
        parser.error("mirror audience tenant must equal --tenant")
    as_of = args.as_of or datetime.now(timezone.utc).date()
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
        coverage = customer_po_coverage.coverage(runtime.mirror, investigator,
                                                 statuses=("absent_in_this_source",))
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
                row = _reader_run(MCPServer(case_runtime=runtime, actor=actor,
                                            ontology=session.host))
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
