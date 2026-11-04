"""The ``sender_corroboration`` step whose reads are made by worker processes
(``ontology.sender_parallel``) writes what the serial step writes: every table of the
generation, its content digest and the step's report, over the firm, carrier, forward,
predating, shared-domain and two-number cases of ``test_sender_corroboration``, once and
nine times over (63 messages, so a message's sender is read for several leads); and a
worker that dies leaves the work to the build's process, the same rows again. No network,
no credentials. Evidence: injected-transport fixture.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from integrations.tests.test_capture_mail import SCOPE
from ontology import document_fields, identifiers, mentions, sender_parallel
from ontology import sender_corroboration as sc
from ontology.project import MailCaptures, build_store
from ontology.tests.test_po_identity_firm import Source
from ontology.tests.test_project_incremental import tables
from ontology.tests.test_sender_corroboration import LINK, NOW, SCHEMA, world

_DIE_ENV = "PLX_TEST_SENDER_WORKER_DIES"
# Loaded by the workers (``spawn``) only: their task kills the worker.
_SITE = textwrap.dedent(f"""
    import os
    if os.environ.get({_DIE_ENV!r}):
        from ontology import sender_parallel as _p

        def _dies(*args):
            os._exit(3)
        _p._task = _dies
""")


def _serial(builder, projector):
    return sender_parallel.sender_corroboration_step(builder, projector, processes=1)


def _parallel(builder, projector):
    return sender_parallel.sender_corroboration_step(builder, projector, processes=3,
                                                     min_items=1)


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "confirmations are read from PDFs: needs pypdf and reportlab")
class ParallelSenderTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        for key in [k for k in os.environ if k.startswith(("PLX_WORKBENCH_API", "BC_",
                                                           "MICROSOFT_GRAPH"))]:
            os.environ.pop(key)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.worlds = {}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, copies: int, step, name: str) -> tuple:
        if copies not in self.worlds:
            self.worlds[copies] = world(self.root / f"world-{copies}", copies)
        state, rows = self.worlds[copies]
        path = self.root / f"{name}-{copies}.sqlite"
        with MailCaptures(state, internal_domains=SCOPE.internal_domains) as captures:
            report = build_store(
                path, SCHEMA, {"portal_api": Source("portal_api", rows), "graph_mail": captures},
                normalizer=identifiers.normalize, clock=lambda: NOW,
                post_project={"document_fields": document_fields.store_document_links,
                              "mentions": mentions.store_mention_links,
                              "sender_corroboration": step})
        return (report["content_digest"], {**report, "store": None}, tables(path),
                dict(sender_parallel.LAST))

    def test_workers_judge_the_serial_links_at_every_size(self):
        for copies in (1, 9):
            with self.subTest(copies=copies):
                serial = self.build(copies, _serial, "serial")
                parallel = self.build(copies, _parallel, "parallel")
                self.assertEqual(serial[3]["mode"], "serial")
                self.assertEqual((parallel[3]["mode"], parallel[3]["reason"]),
                                 ("parallel", None), parallel[3])
                self.assertGreater(parallel[3]["items"], 7 * copies)
                self.assertEqual(parallel[:3], serial[:3])
                counts = serial[1]["post_project"]["sender_corroboration"]["counts"][LINK]
                # every case of the fixture, as often as it is copied
                self.assertEqual(counts, {"carrier_leads": copies, "firm": 3 * copies,
                                          "message_predates_target": 2 * copies,
                                          "no_party_domain": 6 * copies})
                links = [row for row in serial[2]["links"] if row[0].startswith(sc.FIRM_PREFIX)]
                self.assertEqual(len(links), 3 * copies)

    def test_a_worker_that_dies_leaves_the_same_rows(self):
        site = self.root / "site"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(_SITE)
        path = os.pathsep.join([str(site), str(Path(__file__).resolve().parents[2])])
        serial = self.build(1, _serial, "serial")
        with mock.patch.dict(os.environ, {_DIE_ENV: "1", "PYTHONPATH": path}):
            died = self.build(1, _parallel, "died")
        self.assertTrue(died[3]["reason"].startswith("workers_failed:"), died[3])
        self.assertEqual(died[3]["mode"], "serial")
        self.assertEqual(died[:3], serial[:3])

    def test_a_build_script_is_not_run_again_by_the_workers(self):
        """A script that builds runs once: its workers start without it (``spawn`` would
        otherwise run an unguarded script's top level in each of them)."""
        script = self.root / "build_once.py"
        marks = self.root / "runs.txt"
        script.write_text(textwrap.dedent(f"""
            import json, sys
            with open({str(marks)!r}, "a") as out:
                out.write("run\\n")
            from ontology.tests import test_sender_parallel as t
            case = t.ParallelSenderTests("test_a_build_script_is_not_run_again_by_the_workers")
            t.ParallelSenderTests.setUpClass()
            try:
                found = case.build(1, t._parallel, "script")
            finally:
                t.ParallelSenderTests.tearDownClass()
            print(json.dumps(found[3]))
        """))
        env = {k: v for k, v in os.environ.items() if not k.startswith(
            ("PLX_WORKBENCH_API", "BC_", "MICROSOFT_GRAPH"))}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                              env=env, timeout=600)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        last = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((last["mode"], last["reason"]), ("parallel", None), last)
        self.assertEqual(marks.read_text(), "run\n")


if __name__ == "__main__":
    unittest.main()
