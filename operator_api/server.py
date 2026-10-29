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
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any


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
    parser.add_argument("--allowed-origin", action="append", default=[],
                        help="browser Origin allowed to POST (default: same host only)")
    parser.add_argument("--insecure-cookie", action="store_true",
                        help="dev/mock over plain http on loopback: cookie without Secure")
    parser.add_argument("--mock-step-delay", type=float, default=0.25,
                        help="--mock: seconds between streamed steps (realistic pacing)")
    parser.add_argument("--latency-log", help="append request latency JSON lines here")
    add_backend_arguments(parser)
    return parser


def add_backend_arguments(parser: argparse.ArgumentParser) -> None:
    from .backend import add_arguments
    add_arguments(parser)


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
        config = uvicorn.Config(app, host=args.bind, port=args.port, log_level="warning",
                                access_log=False, proxy_headers=mode == "entra",
                                forwarded_allow_ips="127.0.0.1")
        server = uvicorn.Server(config)
        owner_loop = getattr(backend, "loop", None)
        if ready is not None:
            if owner_loop is None:
                ready(server)
                return 0
            threading.Thread(target=ready, args=(server,), daemon=True).start()
            owner_loop()
            return 0
        print(f'{{"operator_api": "http://{args.bind}:{args.port}", "mode": "{mode}"}}',
              file=sys.stderr, flush=True)
        if owner_loop is None:
            server.run()
            return 0
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
        return start(MockBackend(step_delay=max(0.0, min(args.mock_step_delay, 2.0))),
                     DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
                     {"sign_in_available": True,
                      "message": "Demo data: sign in with the demo button."})
    from .backend import run
    return run(parser, args, mode, start, fixtures=fixtures, planner=planner)
