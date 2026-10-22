"""One filter decides which live-reader credentials are read: ``build_live_readers``.

``case_runtime.build_live_readers`` builds a source's reader (mail, the SharePoint drives,
BC), and reads its variables, only for a source the live audience grants a principal and,
when the caller declares its host sources, one they name. No caller filters the environment
itself, so a BC-only audience never reads a Graph credential whatever the environment holds
(the ``case.unfamiliar_order`` spec under the verifier's guard:
``operating/tests/test_unfamiliar_order_case.py``), and a SharePoint-only one builds no mail
reader, so its mail citations answer ``source_not_configured`` for every reader. Evidence
label: ``injected-transport`` (no network, no credential; no environment value is printed).
"""

from __future__ import annotations

from collections.abc import MutableMapping
import json
from pathlib import Path
import tempfile
import unittest

from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_live_source_adapters import DRIVE, ENV, VINCE_ID
from ontology.core import Actor
from operating.case_runtime import CaseRuntime, build_live_readers

GRAPH_PREFIXES = ("MICROSOFT_GRAPH_", "SHAREPOINT_")
PRINCIPALS = ["staff:sales"]


class Undeclared(BaseException):
    """A guarded read: ``live_verify.ENV_GUARD`` ends the command there (exit 86)."""


class GuardedEnv(MutableMapping):
    """``os.environ`` as ``live_verify.ENV_GUARD`` leaves it: ``get`` and ``in`` go through
    ``__getitem__``, a read of a ``hidden`` name stops, and every name read is recorded."""

    def __init__(self, env, hidden=frozenset()) -> None:
        self.data, self.hidden, self.read = dict(env), frozenset(hidden), set()

    def __getitem__(self, key):
        self.read.add(key)
        if key in self.hidden:
            raise Undeclared(key)
        return self.data[key]

    def __contains__(self, key):
        try:
            self[key]
        except KeyError:
            return False
        return True

    def __setitem__(self, key, value):
        self.data[key] = value

    def __delitem__(self, key):
        del self.data[key]

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)


class BuildLiveReadersFilterTest(unittest.TestCase):

    def build(self, audiences, sources=None, **senders):
        env = GuardedEnv(ENV)
        readers = build_live_readers(LiveSourceAudience(audiences, tenant="plx"), env,
                                     sharepoint_drives=[DRIVE], sources=sources, **senders)
        return readers, {name for name in env.read if name.startswith(GRAPH_PREFIXES)}, {
            name for name in env.read if name.startswith("BC_")}

    def test_a_bc_only_audience_builds_no_graph_reader_with_graph_variables_set(self):
        self.assertIn("MICROSOFT_GRAPH_CLIENT_SECRET", ENV)
        for sources in (None, {"bc", "mail", "sharepoint", "teams"}):
            with self.subTest(sources=sources):
                readers, graph_read, bc_read = self.build({"bc:production:*": PRINCIPALS},
                                                          sources)
                self.assertEqual(graph_read, set())
                self.assertEqual((readers.mail, readers.sharepoint, readers.graph_transport),
                                 (None, {}, None))
                self.assertIsNotNone(readers.bc)
                self.assertIn("BC_CLIENT_SECRET", bc_read)

    def test_a_reader_is_built_only_where_audience_and_declared_sources_both_name_it(self):
        # Declared host sources narrow the audience: a mail grant on a BC-only host.
        readers, graph_read, _ = self.build(
            {"mail:*": PRINCIPALS, "bc:production:*": PRINCIPALS}, {"bc"})
        self.assertEqual((graph_read, readers.mail), (set(), None))
        self.assertIsNotNone(readers.bc)
        # A mail-only audience never reads a BC credential, nor builds a drive reader.
        readers, graph_read, bc_read = self.build({"mail:*": PRINCIPALS})
        self.assertEqual((bc_read, readers.bc, readers.sharepoint), (set(), None, {}))
        self.assertIsNotNone(readers.mail)
        self.assertIn("MICROSOFT_GRAPH_CLIENT_SECRET", graph_read)
        self.assertNotIn("SHAREPOINT_DRIVE_ID", graph_read)
        # Fails closed: a pattern granting nobody, a family with no live reader here, or
        # a declared source the audience does not grant reads no credential at all.
        for audiences, sources in (({"mail:*": [], "bc:production:*": []}, None),
                                   ({"teams:*": PRINCIPALS, "mc:*": PRINCIPALS}, None),
                                   ({"sharepoint:*": PRINCIPALS}, {"bc"})):
            with self.subTest(audiences=audiences, sources=sources):
                readers, graph_read, bc_read = self.build(audiences, sources)
                self.assertEqual(readers.configured(), {"graph-mail": False,
                                                        "sharepoint_drives": [], "bc": False})
                self.assertEqual((graph_read, bc_read), (set(), set()))
        with self.assertRaises(ValueError):
            self.build({"bc:production:*": PRINCIPALS}, {"bc", "erp"})

    def test_a_sharepoint_only_audience_builds_no_mail_reader_and_mail_is_not_configured(self):
        sent = []
        for sources in (None, {"mail", "sharepoint"}):
            with self.subTest(sources=sources):
                readers, graph_read, bc_read = self.build(
                    {f"sharepoint:{DRIVE}:/*": PRINCIPALS}, sources,
                    graph_sender=lambda *request: sent.append(request))
                self.assertEqual((readers.mail, sorted(readers.sharepoint), bc_read),
                                 (None, [DRIVE], set()))
                self.assertIn("MICROSOFT_GRAPH_CLIENT_SECRET", graph_read)
        # A kind the audience grants no principal answers the same for every reader, the
        # one holding a SharePoint grant and one holding nothing, and nothing is read.
        with tempfile.TemporaryDirectory() as temp:
            mirror_audience = Path(temp) / "mirror-audience.json"
            mirror_audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                                   "audiences": {"public.*": PRINCIPALS}}))
            runtime = CaseRuntime(Path(temp) / "state", client=None, audience=mirror_audience,
                                  tenant="plx", live=readers)
            try:
                handle = f"graph-mail://{VINCE_ID}/m1@ck1"
                for principals in (PRINCIPALS, ["staff:nobody"]):
                    self.assertEqual(
                        runtime.read_live(handle, Actor("plx", frozenset(principals))),
                        {"ok": False, "error": "source_not_configured", "source": "graph-mail",
                         "citation": handle})
            finally:
                runtime.close()
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
