"""The ``mentions`` step whose documents are read and whose links are found by worker
processes (``ontology.finish_documents``) writes what the serial step writes: every table
of the generation, its report and content digest, and every entry of the document memo,
over a cold build (memo misses), a refresh of the same captures (memo hits) and a refresh
where a purchase order's number moved (changed identifier keys), at 1, 10 and 60 messages;
and a document that fails is the same failure, counted the same way. No network, no
credentials. Evidence: injected-transport fixture.
"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import textwrap
import unittest
from unittest import mock

from integrations.tests.test_capture_mail import SCOPE
from ontology import finish_parallel, mentions
from ontology import project_incremental as inc
from ontology import schema as schema_loader
from ontology.project import MailCaptures
from ontology.tests.test_finish_parallel import NOW, _captures
from ontology.tests.test_mail_documents import StaticPortal, po
from ontology.tests.test_project_incremental import incremental_path, tables

_FAIL_ENV = "PLX_TEST_FAIL_MENTIONS_OF"
# Loaded by the workers (``spawn``) only: their ``document_mentions`` fails for the
# documents the build's process fails for too (``mock.patch`` in the test).
_SITE = textwrap.dedent(f"""
    import os
    if os.environ.get({_FAIL_ENV!r}):
        from ontology import mentions as _m
        _real = _m.document_mentions

        def _failing(document):
            if os.environ[{_FAIL_ENV!r}] in document.citation:
                if os.environ.get("PLX_TEST_DIE"):
                    os._exit(3)
                raise ValueError("fixture")
            return _real(document)
        _m.document_mentions = _failing
""")


def _serial(builder, projector):
    return finish_parallel.store_mention_links(builder, projector, processes=1)


def _parallel(builder, projector):
    return finish_parallel.store_mention_links(builder, projector, processes=3,
                                               min_documents=1, min_groups=1)


def _kept(state: Path) -> list:
    """Every memo entry; ``valid_at`` (a build token: its generation and a random part)
    as its generation."""
    with closing(sqlite3.connect(state / "mentions.sqlite")) as db:
        return sorted(db.execute(
            "SELECT 'm', key, code, substr(valid_at, 1, instr(valid_at, '-')), value, used "
            "FROM mentions UNION ALL "
            "SELECT 'g', key, '', '', value, used FROM groups").fetchall())


class ParallelLinksTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.schema = schema_loader.load_schema()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def chain(self, messages: int, step, name: str, moved: str = "PO100306",
              workers: int = 1):
        """Three generations of a ``messages``-message capture: cold, the same again, and
        one where order p6's number moved; their records and the memo after each."""
        root = self.root / f"{messages}"
        if not (root / "state").exists():
            self.__class__.captured = getattr(self.__class__, "captured", {})
            self.captured[messages] = _captures(root, messages)
        store, rows = self.captured[messages]
        out = []
        for number in ("PO100306", "PO100306", moved):
            portal = [r for r in rows if r.relation != "public.app_purchase_order"
                      or r.row["id"] != "p6"]
            portal.append(po("public.app_purchase_order", {"id": "p6"},
                             {"id": "p6", "po_number": number, "supplier_id": "s3",
                              "date_generated": "2026-09-01"}))
            with MailCaptures(root / "state", internal_domains=SCOPE.internal_domains) as mail:
                generation = inc.build_generation(
                    root / name, self.schema, {"portal_api": StaticPortal(portal),
                                               "graph_mail": mail},
                    clock=lambda: NOW, post_project={"mentions": step}, workers=workers,
                    space=incremental_path)
            out.append((generation.mode, generation.content_digest,
                        {**generation.report, "store": None}, tables(generation.store),
                        _kept(root / name), dict(finish_parallel.LAST)))
        return out

    def test_workers_find_the_serial_links_and_memo_at_every_size(self):
        for messages in (1, 10, 60):
            with self.subTest(messages=messages):
                serial = self.chain(messages, _serial, "serial", moved="PO100399")
                # with the build's document workers (the host's build: their sources'
                # specs) at the largest size
                parallel = self.chain(messages, _parallel, "parallel", moved="PO100399",
                                      workers=3 if messages == 60 else 1)
                self.assertEqual([g[0] for g in serial], ["cold", "incremental", "incremental"])
                for at, (one, other) in enumerate(zip(serial, parallel)):
                    self.assertEqual(other[5].get("links.mode"), "parallel", other[5])
                    self.assertEqual(other[5]["links.groups"], messages)
                    self.assertEqual(other[5]["links.groups_here"], 0)
                    self.assertEqual(other[:5], one[:5], (messages, at))
                # the refresh of the same captures: every group from the memo, unread
                self.assertEqual(parallel[1][5]["links.groups_held"], messages)
                self.assertEqual(parallel[0][5]["links.groups_held"], 0)
                counts = serial[0][2]["post_project"]["mentions"]["counts"]
                self.assertEqual(counts["documents"], messages)
                if messages == 60:   # leads, raises and the moved number's links change
                    self.assertGreater(counts["links"], 60)
                    self.assertNotEqual(serial[2][1], serial[1][1])

    def test_a_document_that_fails_is_the_same_failure(self):
        site = self.root / "site"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(_SITE)
        failing = "M-00003"
        real = mentions.document_mentions

        def fails(document):
            if failing in document.citation:
                raise ValueError("fixture")
            return real(document)
        path = os.pathsep.join([str(site), str(Path(__file__).resolve().parents[2])])
        with mock.patch.object(mentions, "document_mentions", fails), \
                mock.patch.dict(os.environ, {_FAIL_ENV: failing, "PYTHONPATH": path}):
            serial = self.chain(10, _serial, "serial-failed")
            parallel = self.chain(10, _parallel, "parallel-failed")
        step = serial[0][2]["post_project"]["mentions"]
        self.assertEqual((step["documents_failed"], step["failed_errors"]),
                         (1, {"ValueError": 1}))
        for one, other in zip(serial, parallel):
            self.assertEqual(other[:5], one[:5])
        # the failed group was read and derived by the build's process, the others not
        self.assertEqual((parallel[0][5]["links.groups_here"], parallel[0][5]["links.groups"]),
                         (1, 9))
        # a worker that dies: its task's groups and every later one are the build's own
        with mock.patch.object(mentions, "document_mentions", fails), \
                mock.patch.dict(os.environ, {_FAIL_ENV: failing, "PYTHONPATH": path,
                                             "PLX_TEST_DIE": "1"}):
            died = self.chain(10, _parallel, "parallel-died")
        # (every task in flight when it died fails with it)
        self.assertGreaterEqual(died[0][5]["links.failed_tasks"], 1)
        self.assertGreater(died[0][5]["links.groups_here"], 1)
        for one, other in zip(serial, died):
            self.assertEqual(other[:5], one[:5])


    def test_attachments_and_members_read_by_workers_through_the_cleaning_step(self):
        """The host's step (``operating.build_steps``: documents cleaned of lone surrogates,
        a group's load wrapped) over mail with PDF attachments and zips (their members):
        the workers read each group with the build's kept readings; the generation, the
        memo and the kept readings equal the serial step's."""
        from operating.build_steps import _documents_cleaned
        from ontology.tests.test_mail_documents import captured_mail_with_documents

        root = captured_mail_with_documents(self)
        portal = StaticPortal([po("public.app_purchase_order", {"id": f"p{n}"},
                                  {"id": f"p{n}", "po_number": f"PO10023{n}"})
                               for n in range(4)])

        def build(name, step):
            with MailCaptures(root / "capture", internal_domains=SCOPE.internal_domains) \
                    as mail:
                generation = inc.build_generation(
                    root / name, self.schema, {"portal_api": portal, "graph_mail": mail},
                    clock=lambda: NOW, post_project={"mentions": _documents_cleaned(step)},
                    workers=3)
            with closing(sqlite3.connect(root / name / "readings.sqlite")) as db:
                readings = sorted(db.execute("SELECT key, code, value FROM readings"))
            return (generation.content_digest, {**generation.report, "store": None},
                    tables(generation.store), _kept(root / name), readings,
                    dict(finish_parallel.LAST))
        serial, parallel = build("serial", _serial), build("parallel", _parallel)
        self.assertEqual(parallel[5]["links.groups"], 4, parallel[5])
        self.assertTrue(serial[4])
        self.assertGreater(serial[1]["post_project"]["mentions"]["counts"]["documents"], 8)
        self.assertEqual(parallel[:5], serial[:5])


if __name__ == "__main__":
    unittest.main()
