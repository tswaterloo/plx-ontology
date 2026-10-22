"""``host mcp`` and ``host mcp-http`` build only the live readers of the sources the host
config names.

The host passes its configured live sources to ``operating.mcp_server``
(``--live-sources``, ``operating.host._live_arguments``), whose session host (the stdio and
the remote host's) builds the same reduced readers as ``Host.served_tools``
(``case_runtime.build_live_readers(sources=)``): a BC-only host's real MCP session never
reads a Graph credential, at start-up or on a BC read. Every read of the process environment
is recorded. Evidence label: ``injected-transport`` (the start-up path of
``operating/tests/test_host.py``, its fake BC behind the guard; no network, no credential).
"""

from __future__ import annotations

from collections.abc import MutableMapping
import os
import unittest
from unittest.mock import patch

from ontology.core import Actor
from operating import host as host_module
from operating import mcp_http_host, mcp_server
from operating import session_identity as si
from operating.ontology_host import live_lookups
from operating.tests.test_host import ENV, SVC, HostCase
from operating.tests.test_session_identity import SECRET, TID, Fixture

GRAPH_PREFIXES = ("MICROSOFT_GRAPH_", "SHAREPOINT_")


class RecordingEnv(MutableMapping):
    """``os.environ`` recording every name read (``get``, ``in`` and a copy read through
    ``__getitem__``)."""

    def __init__(self, values) -> None:
        self.values, self.read = dict(values), set()

    def __getitem__(self, key):
        self.read.add(key)
        return self.values[key]

    def __setitem__(self, key, value) -> None:
        self.values[key] = value

    def __delitem__(self, key) -> None:
        del self.values[key]

    def __iter__(self):
        return iter(list(self.values))

    def __len__(self) -> int:
        return len(self.values)

    def copy(self) -> dict:
        return {key: self[key] for key in self}


class Started(Exception):
    """Stops the remote host once its session host is built (before it serves)."""


class McpLiveSourcesTests(HostCase):
    def session(self, *configured: str, remote: bool = False):
        """``host mcp`` (``remote``: ``host mcp-http``) on a config naming ``configured``
        of the live sources (the Portal too): the case runtime its sessions share and the
        environment names the process read."""
        off = {name: None for name in ("bc", "mail", "sharepoint", "teams")
               if name not in configured}
        config = self.write_config(**off)
        env = RecordingEnv({**ENV, "T": Fixture.ticket(author=si.AUTHOR_AGENT),
                            si.SESSION_TICKET_SECRET_ENV: SECRET})
        built, self.env = [], env

        def remote_services(*args, **kwargs):
            built.append(mcp_server.SessionHost(*args, **kwargs))
            raise Started
        if remote:
            from operating.tests import test_mcp_http_host as th
            resource = self.write("entra.json", {
                "schema": "plx-entra-resource/v1", "tenant_id": TID,
                "audiences": [th.AUDIENCE], "required_scope": th.SCOPE,
                "resource": "http://127.0.0.1:8766/mcp", "jwks_file": "absent-in-tests.json"})
            argv = ["mcp-http", "--state-dir", str(self.state), "--config", str(config),
                    "--entra-resource", str(resource), "--port", "0"]
            start = patch.object(mcp_http_host, "SessionHost", remote_services)
            options = {"jwks": th.StaticJwks(th.jwks(th.KEY))}
        else:
            argv = ["mcp", "--state-dir", str(self.state), "--config", str(config),
                    "--session-ticket-env", "T"]
            start = patch.object(mcp_server, "serve_stdio",
                                 lambda server, **_: built.append(server.case_runtime))
            options = {}
        with patch.object(os, "environ", env), start:
            try:
                code, _out, err = self.main(*argv, **options)
            except Started:
                code, err = 0, ""
        self.assertEqual((code, len(built)), (0, 1), err)
        self.addCleanup(built[0].close)
        argv = host_module.mcp_arguments(host_module.Host(
            self.state, host_module.HostConfig.from_file(config), environ=ENV),
            session_ticket_env="T")
        self.assertEqual(argv[argv.index("--live-sources") + 1], ",".join(configured))
        return getattr(built[0], "case_runtime", built[0]), env.read

    def test_a_bc_only_host_reads_no_graph_variable_and_serves_bc(self):
        runtime, read = self.session("bc")
        self.assertEqual([n for n in read if n.startswith(GRAPH_PREFIXES)], [], read)
        self.assertIn("BC_CLIENT_SECRET", read)
        self.assertEqual(runtime.live.configured(),
                         {"graph-mail": False, "sharepoint_drives": [], "bc": True})
        self.assertIn("bc", live_lookups(runtime))
        # A BC read (token POST and GET, every request through the guard) reads none either.
        read.clear()
        runtime.live.bc_transport._sender = self.router   # the fake BC behind the guard
        with patch.object(os, "environ", self.env):
            listed = runtime.live.bc.companies(Actor("plx", frozenset({SVC}), "restricted"))
        self.assertTrue(listed["items"], listed)
        self.assertEqual([n for n in read if n.startswith(GRAPH_PREFIXES)], [], read)

    def test_a_host_naming_bc_and_mail_builds_both(self):
        # ... and no drive reader: the filter is per source, not per Graph family.
        runtime, read = self.session("bc", "mail")
        self.assertTrue({"MICROSOFT_GRAPH_CLIENT_SECRET", "BC_CLIENT_SECRET"} <= read, read)
        self.assertEqual(runtime.live.configured(),
                         {"graph-mail": True, "sharepoint_drives": [], "bc": True})
        self.assertIn("bc", live_lookups(runtime))

    def test_the_remote_host_builds_the_same_reduced_readers(self):
        runtime, read = self.session("bc", remote=True)
        self.assertEqual([n for n in read if n.startswith(GRAPH_PREFIXES)], [], read)
        self.assertEqual(runtime.live.configured(),
                         {"graph-mail": False, "sharepoint_drives": [], "bc": True})

    def test_an_unknown_live_source_is_refused(self):
        config = host_module.HostConfig.from_file(self.config)
        argv = host_module.mcp_arguments(host_module.Host(self.state, config, environ=ENV),
                                         session_ticket_env="T")
        argv[argv.index("--live-sources") + 1] = "bc,portal"
        with patch.dict(os.environ, {"T": Fixture.ticket(author=si.AUTHOR_AGENT),
                                     si.SESSION_TICKET_SECRET_ENV: SECRET}), \
                self.assertRaises(SystemExit) as refused, patch("sys.stderr"):
            mcp_server.session_server(argv)
        self.assertEqual(refused.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
