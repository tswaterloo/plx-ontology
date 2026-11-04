"""The ``text_index`` step whose documents are read by worker processes
(``ontology.text_parallel``) writes what the serial step writes: every table of the
generation, every row of the text tables, every (word, chunk) pair of the word index, the
step's report and the content digest, every entry of the document memo and the kept
readings, over a cold build, a refresh of the same captures and a refresh after a message
changed and another arrived, at 1, 10 and 60 messages (long bodies cut in several chunks,
PDF attachments, a zip's member); and a document that fails is the same failure, counted
the same way, a dead worker included. No network, no credentials. Evidence:
injected-transport fixture.
"""

from __future__ import annotations

from contextlib import closing
import copy
import importlib
import io
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import textwrap
import unittest
from unittest import mock
import zipfile

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import (BOX_A, CAPTURE, ENV, SCOPE, Clock, FakeGraph,
                                                   capture_audience, file_attachment)
from ontology import project_incremental as inc
from ontology import project_parallel
from ontology import schema as schema_loader
from ontology import text_index, text_parallel
from ontology.project import MailCaptures
from ontology.tests.test_document_fields import SLIP, pdf
from ontology.tests.test_finish_parallel import NOW
from ontology.tests.test_mail_documents import StaticPortal, po
from ontology.tests.test_project_incremental import incremental_path, tables
from operating.build_steps import _documents_cleaned

_FAIL_ENV = "PLX_TEST_FAIL_TEXT_OF"
# Loaded by the workers (``spawn``) only: their ``_pieces`` fails for the text the build's
# process fails for too (``mock.patch`` in the test).
_SITE = textwrap.dedent(f"""
    import os
    if os.environ.get({_FAIL_ENV!r}):
        from ontology import text_index as _t
        _real = _t._pieces

        def _failing(text):
            if os.environ[{_FAIL_ENV!r}] in text:
                if os.environ.get("PLX_TEST_DIE"):
                    os._exit(3)
                raise ValueError("fixture")
            return _real(text)
        _t._pieces = _failing
""")
_WORDS = ("lanolin", "marigold", "cartons", "freight", "pallet", "quayside", "tariff",
          "invoice", "ledger", "booking", "customs", "voyage", "container", "manifest")


def _body(n: int, size: int) -> str:
    words = [_WORDS[(n * 7 + i * 3) % len(_WORDS)] + str(i % 97) for i in range(size)]
    return f"About PO{100301 + n % 6}, ref{n:05d}. " + " ".join(words)


def _serial(builder, projector):
    return text_parallel.store_text_index(builder, projector, processes=1)


def _parallel(builder, projector):
    return text_parallel.store_text_index(builder, projector, processes=3, min_groups=1)


def _text_rows(path: Path) -> dict[str, list]:
    """The text tables' rows and the word index's (word, chunk) pairs."""
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE VIRTUAL TABLE temp.words USING fts5vocab(main, chunk_text, "
                   "'instance')")
        return {"fields": db.execute("SELECT * FROM text_fields ORDER BY field_id").fetchall(),
                "chunks": db.execute("SELECT * FROM text_chunks ORDER BY chunk_id").fetchall(),
                "words": db.execute("SELECT term, doc FROM temp.words ORDER BY term, doc"
                                    ).fetchall()}


def _kept(directory: Path) -> list:
    """Every memo entry (texts and groups) and every kept reading."""
    with closing(sqlite3.connect(directory / "mentions.sqlite")) as db:
        memo = sorted(db.execute("SELECT 't', key, code, value, used FROM texts UNION ALL "
                                 "SELECT 'g', key, '', value, used FROM groups").fetchall())
    readings = []
    if (directory / "readings.sqlite").is_file():
        with closing(sqlite3.connect(directory / "readings.sqlite")) as db:
            readings = sorted(db.execute("SELECT key, code, value FROM readings"))
    return [memo, readings]


class ParallelTextTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.schema = schema_loader.load_schema()
        cls.captured = {}
        cls.portal = StaticPortal([po("public.app_purchase_order", {"id": f"p{n}"},
                                      {"id": f"p{n}", "po_number": f"PO{100301 + n}"})
                                   for n in range(6)])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def capture(self, root: Path, messages: int) -> tuple[FakeGraph, MailCaptureStore]:
        """A copy of the capture of ``messages`` messages (made once: a PDF carries the
        time it was made): bodies of 1 to ~4 chunks, a PDF attachment on every fourth, a
        zip holding a PDF on every ninth; the graph and the clock as they were after it."""
        made = self.captured.get(messages)
        if made is None:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as packed:
                packed.writestr("slip.pdf", pdf(SLIP))
            graph = FakeGraph()
            for n in range(messages):
                attachments = []
                if n % 4 == 0:
                    attachments.append(file_attachment(
                        f"A-{n}", pdf([[_body(n + 1, 60)], [_body(n + 2, 40)]]), f"a{n}.pdf"))
                if n % 9 == 0:
                    attachments.append(file_attachment(f"Z-{n}", archive.getvalue(),
                                                       f"z{n}.zip"))
                graph.put(BOX_A, f"M-{n:05d}", f"CK-{n}", subject=f"Order {n}",
                          body=_body(n, 40 + (n * 211) % 900), attachments=attachments or None)
            template = self.root / f"capture-{messages}"
            clock = Clock()
            store = MailCaptureStore(template, clock=clock)
            self.run_capture(graph, store)
            store.close()
            made = self.captured[messages] = (template, graph, clock)
        template, graph, clock = made
        shutil.copytree(template, root / "state")
        store = MailCaptureStore(root / "state", clock=copy.deepcopy(clock))
        self.addCleanup(store.close)
        return copy.deepcopy(graph), store

    def run_capture(self, graph: FakeGraph, store: MailCaptureStore) -> None:
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        self.assertTrue(run["complete"], run)

    def chain(self, messages: int, step, name: str, *, workers: int = 1) -> list:
        """Three generations over a fresh capture of ``messages`` messages: cold, the same
        again, and one after a message changed and another arrived."""
        root = self.root / f"{name}-{messages}"
        graph, store = self.capture(root, messages)
        # the process's reading memos as a new process's (a reading this process made
        # for another chain is otherwise never kept in this one's readings)
        for module, memo in project_parallel._KEPT:
            owner = importlib.import_module(module)
            setattr(owner, memo, getattr(owner, memo).empty())
        out = []
        for at in range(3):
            if at == 2:
                graph.put(BOX_A, "M-00000", "CK-0-2", subject="Order 0 restated",
                          body=_body(5, 700))
                graph.put(BOX_A, "M-NEW", "CK-NEW", subject="Late", body=_body(3, 300))
                self.run_capture(graph, store)
            with MailCaptures(root / "state", internal_domains=SCOPE.internal_domains) \
                    as mail:
                generation = inc.build_generation(
                    root / "gen", self.schema, {"portal_api": self.portal,
                                                "graph_mail": mail},
                    clock=lambda: NOW, post_project={"text_index": _documents_cleaned(step)},
                    workers=workers, space=incremental_path)
            out.append((generation.mode, generation.content_digest,
                        {**generation.report, "store": None}, tables(generation.store),
                        _text_rows(generation.store), _kept(root / "gen"),
                        dict(text_parallel.LAST)))
        return out

    def test_workers_index_the_serial_rows_and_memo_at_every_size(self):
        for messages in (1, 10, 60):
            with self.subTest(messages=messages):
                serial = self.chain(messages, _serial, "serial")
                parallel = self.chain(messages, _parallel, "parallel",
                                      workers=3 if messages == 60 else 1)
                self.assertEqual([g[0] for g in serial], ["cold", "incremental", "incremental"])
                for at, (one, other) in enumerate(zip(serial, parallel)):
                    self.assertEqual(one[6]["mode"], "serial")
                    self.assertEqual(other[:6], one[:6], (messages, at))
                cold, same, changed = (g[6] for g in parallel)
                # every group read by the workers when cold, none read again for the same
                # captures, the changed message and the new one after the change
                self.assertEqual((cold["mode"], cold["groups"], cold["groups_here"]),
                                 ("parallel", messages, 0), cold)
                self.assertEqual((same["groups"], same["groups_held"]), (0, messages), same)
                # (the restated message's versions and the new one)
                self.assertEqual((changed["groups"], changed["groups_held"],
                                  changed["groups_here"]), (3, messages - 1, 0), changed)
                step = serial[0][2]["post_project"]["text_index"]
                self.assertGreaterEqual(step["documents"], messages)
                self.assertNotIn("documents_failed", step)
                rows = serial[0][4]
                if messages == 60:   # long bodies in several chunks; attachments' pages
                    self.assertGreater(len(rows["chunks"]), len(rows["fields"]) + 20)
                    self.assertTrue(any("&page=" in row[3] for row in rows["fields"]))
                    self.assertGreater(len({row[1] for row in rows["fields"]}), 60)

    def test_a_document_that_fails_is_the_same_failure(self):
        site = self.root / "site"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(_SITE)
        failing = "ref00007"   # the body of message 7
        real = text_index._pieces

        def fails(text):
            if failing in text:
                raise ValueError("fixture")
            return real(text)
        path = os.pathsep.join([str(site), str(Path(__file__).resolve().parents[2])])
        with mock.patch.object(text_index, "_pieces", fails), \
                mock.patch.dict(os.environ, {_FAIL_ENV: failing, "PYTHONPATH": path}):
            serial = self.chain(10, _serial, "serial-failed")
            parallel = self.chain(10, _parallel, "parallel-failed")
            with mock.patch.dict(os.environ, {"PLX_TEST_DIE": "1"}):
                died = self.chain(10, _parallel, "parallel-died")
        step = serial[0][2]["post_project"]["text_index"]
        self.assertEqual((step["documents_failed"], step["failed_errors"]),
                         (1, {"ValueError": 1}), step)
        for one, other, dead in zip(serial, parallel, died):
            self.assertEqual(other[:6], one[:6])
            self.assertEqual(dead[:6], one[:6])
        # the failing group was read by the build's process, the others by the workers
        self.assertEqual((parallel[0][6]["groups_here"], parallel[0][6]["groups"]), (1, 9))
        # a worker that dies: its task's groups and every later one are the step's own
        self.assertGreaterEqual(died[0][6]["failed_tasks"], 1)
        self.assertGreater(died[0][6]["groups_here"], 1)


if __name__ == "__main__":
    unittest.main()
