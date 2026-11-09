"""The app API's command line: its arguments, the console argv, auth from the arguments,
the optional question understanding and the default search mode.

Moved from ``operator_api.backend`` by concern (no behaviour change); every name is
re-exported there."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Optional

from .serving import (ASK_WORKERS, ASK_WORKERS_MOST, DEFAULT_DATA_DIR, READ_WORKERS,
    SECTION_WORKERS, WARM_HOSTS)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("real backend (--dev / --entra-resource)")
    group.add_argument("--state-dir", type=Path, help="the host's state directory")
    group.add_argument("--host-config", type=Path,
                       help="operating.host config: the console's own arguments are derived "
                            "from it (as `operating.host serve` starts the console)")
    group.add_argument("--console-arg", action="append", default=[],
                       help="one more argument for the console builder (repeatable)")
    group.add_argument("--ask", action="store_true",
                       help="serve Ask: the scoreboard's agent path for employees (one "
                            "Claude Code session per question, needs --host-config and the "
                            "session secret), else the employee agent loop (PLX_ASK_PLANNER)")
    group.add_argument("--ask-path", choices=("agent", "objective"), default="agent",
                       help="agent: the scoreboard's agent path where it can run (default); "
                            "objective: always the console's objective box")
    group.add_argument("--ask-seconds", type=int, default=180,
                       help="hard cap of one agent Ask's session (seconds); each question "
                            "gets deep_investigation.question_budget under it")
    group.add_argument("--ask-lookup-model", default=None,
                       help="the model an Ask about one named record runs on "
                            "(agent_ask.LOOKUP_MODEL; empty: the session's model)")
    group.add_argument("--ask-workers", type=int, default=ASK_WORKERS,
                       help="Asks answered at once (each its own runtime and thread), at "
                            f"most {ASK_WORKERS_MOST}; past one, only as many as the memory "
                            "available at start holds (ask_worker_count)")
    group.add_argument("--warm-hosts", type=int, default=WARM_HOSTS,
                       help="sessions' tool hosts kept started before any reader is known "
                            "(agent_session.HostSpares.start_warm; each bound to one "
                            "session's reader only); 0: none")
    group.add_argument("--read-workers", type=int, default=READ_WORKERS,
                       help="page reads served at once (each its own runtime and thread "
                            "on one queue); 1: the owner thread only")
    group.add_argument("--section-workers", type=int, default=SECTION_WORKERS,
                       help="page sections read at once (each its own process and "
                            "runtime); 0: the read worker reads them")
    group.add_argument("--warm", type=int, choices=(0, 1), default=1,
                       help="1 (default): a warm worker (its own runtime) reads each "
                            "signed-in reader's Home, briefing and first pages ahead, and the "
                            "served generation's facts at start; 0: none")
    group.add_argument("--keep-pages", type=int, choices=(0, 1), default=1,
                       help="1 (default): the readers' pages are kept on disk in the app's "
                            "data directory, shared with the warm-up process and the "
                            "section pool and kept across restarts; 0: in memory only")
    group.add_argument("--dev-ticket-login", action="store_true",
                       help="--dev over live state: dev-login takes a console session ticket "
                            "(python3 -m operator_ui.console_ticket mint)")
    group.add_argument("--dev-employee-oid", help="--dev over a dev state: this employee")
    group.add_argument("--dev-fixture-principal", action="append", default=[],
                       help="--dev over a fixture state: a fixture principal")
    group.add_argument("--app-data-dir", type=Path, default=DEFAULT_DATA_DIR,
                       help="the app's own files (feedback log); never the state directory")
    group.add_argument("--pg-dsn", default=os.environ.get("PLX_PG_DSN", "postgresql:///plx"),
                       help="search.entities (suggest) and search.feedback")


def console_argv(parser: argparse.ArgumentParser, args: argparse.Namespace,
                 mode: str) -> list[str]:
    if args.state_dir is None:
        parser.error("--state-dir is required for the real backend")
    argv: list[str] = []
    if args.host_config is not None:
        from operating.host import Host, HostConfig, console_arguments
        try:
            host = Host(args.state_dir, HostConfig.from_file(args.host_config))
            argv = console_arguments(host, 0)
        except Exception as error:  # noqa: BLE001 - named, never a trace
            parser.error(f"--host-config is unusable: {type(error).__name__}")
    else:
        argv = ["--state-dir", str(args.state_dir)]
    if mode == "entra":
        argv += ["--entra-resource", args.entra_resource]
    if args.dev_fixture_principal:
        argv += ["--fixture"] + [a for p in args.dev_fixture_principal
                                 for a in ("--principal", p)]
    if args.ask:
        argv += ["--ask"]
    return argv + list(args.console_arg)


def make_auth(parser: argparse.ArgumentParser, args: argparse.Namespace, mode: str,
              ctx: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    from operating import session_identity
    from .auth import DevAuth, EntraAuth, configured_employee, ticket_identity

    resolver, session = ctx.get("resolver"), ctx.get("session")
    if mode == "entra":
        resource = ctx.get("entra_resource")
        if resource is None or resolver is None:
            parser.error("--entra-resource needs the live employee policy")
        redirect = os.environ.get("PLX_APP_REDIRECT_URI") or None
        guid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        client_id = next((a for a in sorted(resource.audiences) if guid.match(a)), "")
        config = {"entra": {"tenant_id": resource.tenant_id, "client_id": client_id,
                            "authority": f"https://login.microsoftonline.com/"
                                         f"{resource.tenant_id}/v2.0",
                            "scopes": [resource.required_scope], "redirect_uri": redirect},
                  "sign_in_available": redirect is not None and bool(client_id),
                  "message": None if redirect else
                  "Microsoft sign-in is waiting for the app's redirect URI to be registered."}
        return EntraAuth(resource, resolver, ctx["replay_guard"]), config
    if args.dev_fixture_principal:
        if session is None:
            parser.error("--dev-fixture-principal needs a fixture state directory")
        return DevAuth(lambda: session, mode="dev"), {"sign_in_available": True}
    if resolver is None:
        parser.error("--dev with an employee identity needs --employee-policy (console args)")
    if args.dev_employee_oid:
        try:
            session_identity.bind_state_dir(ctx["state_dir"], fixture=False, dev=True)
        except session_identity.SessionIdentityRefused as error:
            parser.error("--dev-employee-oid never opens live state: " + error.reason)
        try:
            identity = configured_employee(resolver, args.dev_employee_oid)
        except session_identity.SessionIdentityRefused as error:
            parser.error("--dev-employee-oid refused: " + error.reason)
        return DevAuth(identity, mode="dev"), {"sign_in_available": True}
    secret = session_identity.session_secret(os.environ)
    return (DevAuth(ticket_identity=ticket_identity(resolver, secret, ctx["replay_guard"]),
                    sign_in_identity=ticket_identity(resolver, secret, ctx["replay_guard"],
                                                     single_use=True),
                    mode="dev"), {"sign_in_available": True,
                      "message": "Demo sign-in: paste the access link you were given."})


def optional_understanding() -> Optional[Callable]:
    """the ``understand`` when merged and switched on (PLX_APP_UNDERSTANDING=1)."""
    if os.environ.get("PLX_APP_UNDERSTANDING") != "1":
        return None
    try:
        from operating.query_understanding import understand
    except ImportError:
        return None
    return understand


def default_search(env: Any, mode: str, pg_dsn: Optional[str]) -> str:
    """The search the app's agent and tools use (``operating.search.plug``), set in this
    process's environment so every agent host it starts inherits it
    (``agent_session.SEARCH_ENV``): ``PLX_SEARCH`` when set (``hybrid``; any other value,
    e.g. ``keyword``, is the word index alone), else ``hybrid`` over live state (``--mock``
    keeps the word index: its fixtures have no search tables). ``PLX_PG_DSN`` defaults to
    ``--pg-dsn``. Returns the value in force."""
    if not (env.get("PLX_SEARCH") or "").strip() and mode != "mock":
        env["PLX_SEARCH"] = "hybrid"
    if pg_dsn and not env.get("PLX_PG_DSN"):
        env["PLX_PG_DSN"] = pg_dsn
    return env.get("PLX_SEARCH") or "keyword"
