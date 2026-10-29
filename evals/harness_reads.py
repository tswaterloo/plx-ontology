"""The live eval harness's reads on the persistent host, as the named reader.

``python3 -m evals.harness_reads --state-dir S --config C --session-ticket-env VAR
[--agent ID]`` starts
the session ``operating.host mcp`` starts for the employee a fresh single-use agent ticket
names (``operating.host.mcp_arguments``, its Entra directory provider and
``operating.mcp_server.session_server``: the same resolver, trimming and start-up refusals
on stderr; with ``--eval-principal`` the declared eval principal's session,
``operating.eval_principal``) and serves it to the harness only
(``scripts.eval_real_agent.LiveHost``), one JSON-RPC message per line. No agent is given
this command: an agent's server is ``operating.host mcp``, whose catalog this one lists
unchanged, so ``plx.source_citation`` (a host-code read, never listed) is not a tool any
agent sees.

* MCP messages (``initialize``, ``tools/list``, ``tools/call``) are the session's own, as
  a client gets them (through the output boundary): ``evals.runner.CaseHost.call``.
* ``harness/read`` (params ``name``, ``arguments``) is one host-code read
  (``MCPServer.handle(host_code=True)``, the result as the tool built it):
  ``evals.runner.CaseHost.read``. A ``plx.source_citation`` of an ``api://`` or
  ``original://`` citation is the case runtime's own (its column and text paging
  included). One of a record outside the Portal mirror (``evals.score.RECORD_SCHEMES``:
  ``bc://``, ``graph-mail://``, ``sharepoint://``), which the case runtime reads live as
  metadata only, is ``record_read``: the reader's own ``plx.read`` decides it (the
  employee gate, the live audience, the mail scope and item ACL, the SharePoint item
  gate), and only a ``resolved`` decision is read, from the host's capture stores at
  exactly the version the citation names, as that reader (``evals.capture_evidence``:
  the row shape the scorer checks). Anything else (withheld, changed since, not
  captured, a paging argument) is ``{"ok": false}``: fail closed.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys
from typing import Any, BinaryIO, Sequence

from evals.score import RECORD_SCHEMES

HARNESS_READ = "harness/read"
RECORD_PREFIXES = tuple(f"{scheme}://" for scheme in RECORD_SCHEMES)


class Session:
    """One harness session over the host's ``MCPServer`` (``server``)."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._ids = 0

    def host_code(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """One host-code tool call: its result as built, or ``tool_refused``."""
        self._ids += 1   # an id space of its own beside the client's requests
        response = self.server.handle({
            "jsonrpc": "2.0", "id": f"harness-{self._ids}", "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments)}}, host_code=True) or {}
        result = response.get("result") or {}
        if "error" in response or result.get("isError"):
            return {"ok": False, "error": "tool_refused"}
        return result.get("structuredContent")

    def read(self, name: Any, arguments: Any) -> Any:
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return {"ok": False, "error": "invalid_read"}
        if name == "plx.source_citation" and \
                str(arguments.get("citation") or "").startswith(RECORD_PREFIXES):
            return self.record_read(arguments)
        return self.host_code(name, arguments)

    def record_read(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """A ``RECORD_SCHEMES`` citation as the reader may read it now (module docstring)."""
        if set(arguments) != {"citation"}:
            return {"ok": False, "error": "record_paging_not_supported"}
        handle = str(arguments["citation"])
        decided = self.host_code("plx.read", {"citation": handle})
        status = decided.get("status") if isinstance(decided, Mapping) else None
        if status != "resolved":
            return {"ok": False, "error": str(status or "unreadable")[:40]}
        from evals.capture_evidence import reader
        try:
            found = reader(**self._stores())(handle)
        except Exception as error:  # noqa: BLE001 - an unreadable store reads nothing
            return {"ok": False, "error": type(error).__name__}
        return {"ok": False, "error": "not_captured"} if found is None else found

    def _stores(self) -> dict[str, Any]:
        """The host's BC, mail and SharePoint capture resolvers, bound to the session's
        reader (employee-gated, as ``plx.read`` reads) and the live audience the session
        serves them under."""
        from integrations.capture_mail import MailCaptureStore, store_exists as mail_held
        from integrations.capture_sharepoint import (SharePointCaptureStore,
                                                     store_exists as sharepoint_held)
        from integrations.capture_store import CaptureStore
        from operating.host import BC_DIR, BC_FILE

        runtime = self.server.case_runtime
        audience = getattr(runtime.live, "audience", None)
        if audience is None:
            return {}
        gated = runtime.employee_gate(self.server.actor)   # the reader plx.read reads as
        stores: dict[str, Any] = {}
        bc = Path(runtime.state_dir) / BC_DIR / BC_FILE
        if bc.is_file():
            def resolve_bc(handle: str) -> Any:
                store = CaptureStore.open_read_only(bc, audience)
                try:
                    return store.resolve(handle, gated)
                finally:
                    store.close()
            stores["bc"] = resolve_bc
        mail = getattr(self.server.ontology, "mail_capture_dir", None)
        if mail is not None and mail_held(Path(mail)):
            def resolve_mail(handle: str) -> Any:
                store = MailCaptureStore(Path(mail))
                try:
                    return store.resolve(handle, gated, audience, text=True)
                finally:
                    store.close()
            stores["mail"] = resolve_mail
        drive = getattr(self.server.ontology, "sharepoint_capture_dir", None)
        if drive is not None and sharepoint_held(Path(drive)):
            def resolve_sharepoint(handle: str) -> Any:
                store = SharePointCaptureStore.open_read_only(Path(drive))
                try:
                    return store.resolve(handle, gated, audience, text=True)
                finally:
                    store.close()
            stores["sharepoint"] = resolve_sharepoint
        return stores


def serve(session: Session, reader: BinaryIO, writer: BinaryIO) -> None:
    """One message per line until the harness closes its end."""
    server = session.server
    while True:
        line = reader.readline()
        if not line:
            return
        message = json.loads(line.decode("utf-8"))
        if isinstance(message, dict) and message.get("method") == HARNESS_READ:
            params = message.get("params") or {}
            response = {"jsonrpc": "2.0", "id": message.get("id"), "result": session.read(
                params.get("name"), params.get("arguments"))}
        else:
            response = server.handle(message)
        # Server-to-client requests (a confirmation form) are never answered here.
        outbound, server.outbound[:] = list(server.outbound), []
        for out in outbound + ([response] if response is not None else []):
            writer.write(json.dumps(out, ensure_ascii=False, separators=(",", ":"),
                                    default=str).encode("utf-8") + b"\n")
            writer.flush()


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None, input_stream: BinaryIO | None = None,
         output_stream: BinaryIO | None = None) -> int:
    """``environ`` and ``sender`` (tests): ``operating.host.Host``'s, for the directory."""
    from operating import host as plx_host
    from operating import mcp_server

    parser = argparse.ArgumentParser(prog="python3 -m evals.harness_reads",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--session-ticket-env", required=True)
    parser.add_argument("--eval-principal", action="store_true",
                        help="the ticket names the declared eval principal "
                             "(operating.eval_principal)")
    parser.add_argument("--agent", default=None,
                        help="the agent definition the agent's session runs "
                             "(operating.host mcp --agent): this one lists its catalog")
    args = parser.parse_args(argv)
    try:
        host = plx_host.Host(args.state_dir, plx_host.HostConfig.from_file(args.config),
                             environ=environ, sender=sender)
        _host, server = mcp_server.session_server(
            plx_host.mcp_arguments(host, session_ticket_env=args.session_ticket_env,
                                   eval_principal=args.eval_principal, agent=args.agent),
            directory=None if args.eval_principal else plx_host.directory_provider(host))
    except plx_host.HostError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 2
    try:
        serve(Session(server), input_stream or sys.stdin.buffer,
              output_stream or sys.stdout.buffer)
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
