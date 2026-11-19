"""Start-up of the app API: flags -> authenticator + backend -> uvicorn on a checked bind.

Exactly one identity mode is chosen by flags (``operator_api.auth``); there is no default
principal and no default mode:

* ``--mock``: fixture payloads (``operator_api.mock``), a fixture demo identity behind
  dev-login. Loopback or Tailscale bind only.
* ``--dev`` (with the state flags): the real backend with a configured test identity
  (``--dev-ticket-login`` over live state, ``--dev-employee-oid`` over dev state,
  ``--dev-fixture-principal`` over fixture state). Loopback or Tailscale bind only.
* ``--entra-resource FILE`` (with the state flags): the real backend, each request's
  Entra access token verified. Loopback bind only (Caddy terminates TLS in front).

A deploy restarts the API without ever routing a reader to it cold (ARCH-T6,
``deploy/release/plx-release``): Caddy sends ``/api/*`` to 127.0.0.1:8780, and to the
standby's unix socket (``--unix-socket``, in a directory only the API's own user may write:
no stray listener can take its place) when 8780 takes no connection. An API started while
``<PLX_API_HOLD_DIR>/hold-<name>`` exists (``HOLD_DIR``; the deploy writes it; ``name``: the
port, or the socket's directory) opens its port only once warm (``hold_until_ready``), and
on SIGTERM it stops accepting, lets the requests under way end (``GRACE_SECONDS``) and exits,
so the other one takes the next request. The standby socket is for the real backend only:
the mock and the fixed dev identities are refused on it.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import time
from typing import Optional
import sys
from typing import Any


HOLD_DIR = "/run/plx-api"     # hold-<name> here: open that port or socket only once warm
HOLD_MAX_AGE_SECONDS = 900    # an older hold file is a deploy's leftover: not obeyed
HOLD_MAX_SECONDS = 600        # held longer (the deploy gives up at 660 s): open it anyway
GRACE_SECONDS = 60            # SIGTERM: the longest the requests under way may still run
HOLD_POLL_SECONDS = 0.5
HOLD_SAY_SECONDS = 60         # while held, a line this often


def hold_name(port: int, unix_socket: Optional[str]) -> str:
    """What a deploy's hold file names this API by: its socket's directory, else its port."""
    return Path(unix_socket).parent.name if unix_socket else str(port)


def held(name: str, now: Optional[float] = None) -> bool:
    """Whether a deploy asked the API ``name`` (``hold_name``) to open only once warm: its
    hold file exists and is not older than ``HOLD_MAX_AGE_SECONDS`` (a deploy interrupted
    before it removed the file never holds a later start)."""
    path = Path(os.environ.get("PLX_API_HOLD_DIR") or HOLD_DIR) / f"hold-{name}"
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age <= HOLD_MAX_AGE_SECONDS


def hold_until_ready(backend: Any, stop: Any = None, poll: float = HOLD_POLL_SECONDS,
                     limit: float = HOLD_MAX_SECONDS) -> bool:
    """Start the warm-up now and wait until the backend is ready (``WorkspaceBackend.ready``:
    the known readers' first read-ahead kept under this code version), at most ``limit``
    seconds (the deploy that asked stops an API that is not ready in its own 660 s; past
    ``limit`` nobody is waiting, so the port opens rather than stay shut). The first pass
    itself is time-boxed (``warm_process.FIRST_PASS_SECONDS``), so a slow or stuck read
    never keeps the API unready. True when ready,
    False at the limit or when ``stop`` (an Event) is set first."""
    log = logging.getLogger("operator_api")
    begin = getattr(backend, "start_warm_now", None)
    if callable(begin):
        begin()
    check = getattr(backend, "ready", None)
    started = time.monotonic()
    log.info("holding the port until warm (deploy)")
    said = started
    while callable(check) and not check():
        if time.monotonic() - said >= HOLD_SAY_SECONDS:   # never silent while held
            said = time.monotonic()
            log.info("holding the port: not warm yet after %.0f s", said - started)
        if time.monotonic() - started >= limit:
            log.warning("not warm after %.0f s: opening the port", limit)
            return False
        if stop is not None and stop.wait(poll):
            return False
        if stop is None:
            time.sleep(poll)
    log.info("warm after %.1f s: opening the port", time.monotonic() - started)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m operator_api serve",
                                     description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true", help="fixture payloads (demo/front end)")
    mode.add_argument("--dev", action="store_true",
                      help="DEMO ONLY: a configured test identity; loopback/Tailscale bind")
    mode.add_argument("--entra-resource", help="plx-entra-resource/v1 JSON (production)")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--unix-socket", metavar="PATH",
                        help="serve on this unix socket instead of --port (the deploy's "
                             "standby, plx-api@.service; never --mock or a fixed dev identity)")
    parser.add_argument("--allowed-origin", action="append", default=[],
                        help="browser Origin allowed to POST (default: same host only)")
    parser.add_argument("--insecure-cookie", action="store_true",
                        help="dev/mock over plain http on loopback: cookie without Secure")
    parser.add_argument("--mock-ticket-env", metavar="VAR",
                        help="--mock: sign-in takes the access ticket held in this variable "
                             "(the access-link flow, end to end, over fixtures)")
    parser.add_argument("--mock-step-delay", type=float, default=0.25,
                        help="--mock: seconds between streamed steps (realistic pacing)")
    parser.add_argument("--latency-log", help="append request latency JSON lines here")
    parser.add_argument("--check", action="store_true",
                        help="configuration preflight: parse the flags, check the bind, run "
                             "every start-up check (store, identity configuration) and exit "
                             "0 without serving or starting a worker (plx-release preflight)")
    add_backend_arguments(parser)
    return parser


def add_backend_arguments(parser: argparse.ArgumentParser) -> None:
    from .backend import add_arguments
    add_arguments(parser)


def mock_ticket(expected: str, identity: Any) -> Any:
    """The mock's access ticket: exactly ``expected`` opens the fixture identity."""
    import hmac
    from operating.session_identity import SessionIdentityRefused

    def check(ticket: str) -> Any:
        if not hmac.compare_digest(ticket.encode(), expected.encode()):
            raise SessionIdentityRefused("session_ticket_invalid")
        return identity()
    return check


def main(argv: list[str] | None = None, *, ready: Any = None, fixtures: Any = None,
         planner: Any = None) -> int:
    """``fixtures``/``planner``: the console's fixture world and a scripted planner, passed
    only by tests (as ``case_console.main`` takes them)."""
    from .auth import DevAuth, check_bind, fixture_identity

    parser = build_parser()
    args = parser.parse_args(argv)
    mode = "mock" if args.mock else "dev" if args.dev else "entra"
    try:
        check_bind(args.bind, mode)
    except ValueError:
        parser.error(f"--bind {args.bind} refused in {mode} mode: " +
                     ("loopback only (Caddy fronts the API)" if mode == "entra"
                      else "loopback or a Tailscale address only"))
    if args.unix_socket and (args.mock or getattr(args, "dev_employee_oid", None)
                             or getattr(args, "dev_fixture_principal", None)):
        # Caddy sends readers to the standby socket: it never signs anyone in as a fixed
        # identity or serves fixtures as the Workbench's data
        parser.error("--unix-socket serves readers: not with --mock, --dev-employee-oid or "
                     "--dev-fixture-principal")
    if args.check and args.mock:
        print('{"operator_api_check": "ok", "mode": "mock"}', flush=True)
        return 0
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(message)s")
    if args.latency_log:
        handler = logging.FileHandler(args.latency_log)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("operator_api.latency").addHandler(handler)
    def start(backend: Any, auth: Any, auth_config: dict[str, Any]) -> int:
        """Serve ``backend``. A real backend's workspace calls run on this thread
        (``backend.loop``) while uvicorn runs on another; ``ready`` (tests) gets the
        server instead of a socket and stops the backend when done."""
        from .app import create_app
        app = create_app(backend, auth, allowed_origins=frozenset(args.allowed_origin),
                         auth_config=auth_config, secure_cookie=not args.insecure_cookie)
        import threading
        import uvicorn
        where = ({"uds": args.unix_socket} if args.unix_socket
                 else {"host": args.bind, "port": args.port})
        config = uvicorn.Config(app, **where, log_level="warning",
                                access_log=False, proxy_headers=mode == "entra",
                                forwarded_allow_ips="127.0.0.1",
                                timeout_graceful_shutdown=GRACE_SECONDS)
        server = uvicorn.Server(config)
        from .feedback import start_nightly   # yesterday's feedback digest, once a night
        start_nightly(app.state.feedback)
        owner_loop = getattr(backend, "loop", None)
        if ready is not None:
            if owner_loop is None:
                ready(server)
                return 0
            threading.Thread(target=ready, args=(server,), daemon=True).start()
            owner_loop()
            return 0
        if owner_loop is not None:   # a real backend: the code fingerprint before a first read
            from . import preload
            preload.start()
        if held(hold_name(args.port, args.unix_socket)):
            hold_until_ready(backend)
        served = (f"unix:{args.unix_socket}" if args.unix_socket
                  else f"http://{args.bind}:{args.port}")
        print(f'{{"operator_api": "{served}", "mode": "{mode}"}}', file=sys.stderr, flush=True)
        if owner_loop is None:
            server.run()
            return 0
        if threading.current_thread() is threading.main_thread():
            # uvicorn on its own thread sets no signal handler: SIGTERM (systemctl stop)
            # closes the port and lets the requests under way end, then this returns
            signal.signal(signal.SIGTERM, lambda *_: setattr(server, "should_exit", True))
        thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
        thread.start()
        try:
            owner_loop(lambda: not thread.is_alive())
        except KeyboardInterrupt:
            pass
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        return 0

    if args.mock:
        from .mock import MockBackend
        identity = fixture_identity("plx", ["staff:demo"])
        auth = DevAuth(identity, mode="mock")
        if args.mock_ticket_env:
            expected = os.environ.get(args.mock_ticket_env) or ""
            if len(expected) < 16:
                parser.error(f"--mock-ticket-env: {args.mock_ticket_env} holds no ticket "
                             "(16 characters at least)")
            auth = DevAuth(ticket_identity=mock_ticket(expected, identity), mode="mock")
        from . import mock_studio
        backend = MockBackend(step_delay=max(0.0, min(args.mock_step_delay, 2.0)))
        mock_studio.install(backend)
        return start(backend, auth,
                     {"sign_in_available": True,
                      "message": "Demo data: sign in with the demo button."})
    from .backend import run
    return run(parser, args, mode, start, fixtures=fixtures, planner=planner)
