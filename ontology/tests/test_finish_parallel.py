"""The ``mentions`` step corroborated by worker processes writes what the serial step writes
(``ontology.finish_parallel``): every link row, the step's report and the generation's
content digest, over a mail capture with party-domain, conversation (petra thread), veto
and twin cases. No network, no credentials. Evidence: injected-transport fixture.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import unittest
from unittest import mock

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import (BOX_A, CAPTURE, ENV, SCOPE, Clock, FakeGraph,
                                                   capture_audience)
from ontology import finish_parallel, mentions
from ontology import schema as schema_loader
from ontology.project import CapturedRecord, MailCaptures, build_store
from ontology.tests.test_mail_documents import StaticPortal, po

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2026-09-01T10:00:00.000000Z"
RESTATED = "2026-10-05T10:00:00.000000Z"
DOMAINS = ("acme-bottles.example", "birch-caps.example", "gmail.com", "example-plx.com")


def _captures(root: Path, messages: int) -> tuple[MailCaptureStore, list[CapturedRecord]]:
    """A mail capture of ``messages`` generated messages over five suppliers' orders (some
    sent from the supplier's own domain, some from free mail, some internal, in shared
    conversations, some naming two orders or an order dated after the mail) and the Portal
    rows they name."""
    graph = FakeGraph()
    day = "2026-09-20T09:00:00Z"
    for n in range(messages):
        domain = DOMAINS[n % len(DOMAINS)]
        number = 100301 + n % 6
        body = (f"PO{number} and PO{100301 + (n + 1) % 6} ship." if n % 7 == 3
                else f"About PO{number}.")
        mid = f"M-{n:05d}"
        graph.put(BOX_A, mid, "CK-" + mid, body=body, subject="Order", received=day)
        graph.messages[BOX_A][mid]["from"] = {"emailAddress": {"address": f"s{n}@{domain}"}}
        graph.messages[BOX_A][mid]["conversationId"] = f"CONV-{n % 9}"
        if domain == "example-plx.com":
            graph.messages[BOX_A][mid]["internetMessageHeaders"] = [
                {"name": "Authentication-Results",
                 "value": "mx.example; dmarc=pass header.from=example-plx.com"}]
    store = MailCaptureStore(root / "state", clock=Clock())
    api, _ = graph_read.build_graph(ENV, sender=graph,
                                    retry=BoundedBackoff(sleep=lambda s: None,
                                                         jitter=lambda: 0.0))
    run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                      sleep=lambda s: None).run(SCOPE)
    assert run["complete"], run
    rows = [po("public.app_supplier", {"id": "s1"},
               {"id": "s1", "name": "Acme", "website": "https://www.acme-bottles.example"})]
    for version, since, until in (("g-1", T0, RESTATED), ("g-2", RESTATED, None)):
        rows.append(CapturedRecord(
            "portal_api", "public.app_supplier", '{"id":"s3"}',
            {"id": "s3", "name": "Birch", "website": "https://birch-caps.example"},
            f"api://public.app_supplier/s3@{version}", "row", version[-1] * 64, since, until,
            until or RESTATED, "api:public.app_supplier"))
    for index, (pid, dated) in enumerate((("p1", "2026-09-01"), ("p2", "2026-10-20"),
                                          ("p3", "2026-09-01"), ("p4", "2026-09-01"),
                                          ("p5", "2026-09-01"), ("p6", "2026-09-01"))):
        rows.append(po("public.app_purchase_order", {"id": pid},
                       {"id": pid, "po_number": f"PO{100301 + index}",
                        "supplier_id": "s3" if index % 2 else "s1", "date_generated": dated}))
    return store, rows


def _build(root: Path, name: str, store_rows: tuple[Any, ...], step) -> tuple[dict, list]:
    path = root / name
    with MailCaptures(root / "state", internal_domains=SCOPE.internal_domains) as mail:
        report = build_store(path, schema_loader.load_schema(),
                             {"portal_api": StaticPortal(store_rows), "graph_mail": mail},
                             clock=lambda: NOW, post_project={"mentions": step})
    with sqlite3.connect(path) as db:
        links = db.execute("SELECT * FROM links ORDER BY link_id").fetchall()
        skeletons = db.execute("SELECT * FROM link_skeletons ORDER BY id").fetchall()
    return report, [links, skeletons]


class ParallelCorroborationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.store, cls.rows = _captures(cls.root, 60)
        cls.serial = _build(cls.root, "serial.sqlite", tuple(cls.rows),
                            mentions.store_mention_links)

        def parallel(builder, projector):
            return finish_parallel.store_mention_links(builder, projector, processes=3,
                                                       min_documents=1)
        cls.parallel = _build(cls.root, "parallel.sqlite", tuple(cls.rows), parallel)
        cls.last = dict(finish_parallel.LAST)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_workers_write_the_serial_rows_report_and_digest(self):
        self.assertEqual(self.last.get("mode"), "parallel", self.last)
        serial, parallel = self.serial, self.parallel
        step = serial[0]["post_project"]["mentions"]
        # the fixture exercises raises of several kinds and leads that stay leads
        self.assertGreater(step["counts"]["corroborated"], 3, step["counts"])
        self.assertGreater(step["link_tiers"]["lead"], 10)
        raised = [row for row in serial[1][0] if row[4] == "corroborated"]
        features = {f["feature"] for row in raised for f in json.loads(row[7])}
        self.assertTrue({"party_domain", "petra_thread"} <= features, features)
        self.assertEqual(parallel[1], serial[1])
        self.assertEqual(parallel[0]["post_project"], serial[0]["post_project"])
        self.assertEqual(parallel[0]["content_digest"], serial[0]["content_digest"])
        self.assertEqual(self.last["raised"], step["counts"]["corroborated"])
        # the working file and its spill are gone
        self.assertEqual(sorted(p.name for p in self.root.iterdir()
                                if ".mentions-" in p.name), [])

    def test_a_failing_worker_pool_falls_back_to_the_serial_corroboration(self):
        def parallel(builder, projector):
            return finish_parallel.store_mention_links(builder, projector, processes=2,
                                                       min_documents=1)
        with mock.patch.object(finish_parallel.futures, "ProcessPoolExecutor",
                               side_effect=OSError("no processes")):
            found = _build(self.root, "fallback.sqlite", tuple(self.rows), parallel)
        self.assertEqual(finish_parallel.LAST["mode"], "serial")
        self.assertEqual(finish_parallel.LAST["reason"], "workers_failed:OSError")
        self.assertEqual(found[1], self.serial[1])
        self.assertEqual(found[0]["content_digest"], self.serial[0]["content_digest"])

    def test_few_documents_corroborate_serially_with_the_same_rows(self):
        def few(builder, projector):
            return finish_parallel.store_mention_links(builder, projector, processes=2)
        found = _build(self.root, "few.sqlite", tuple(self.rows), few)
        self.assertEqual(finish_parallel.LAST["reason"], "few_documents")
        self.assertEqual(found[0]["content_digest"], self.serial[0]["content_digest"])


class WorkerViewTests(unittest.TestCase):
    """A worker's links (the build file's and the step's) are read by the document's index:
    the conversation lookup by the target's index read every mention of a busy order
    (measured: 3x a document's time)."""

    def test_the_conversation_lookup_reads_by_the_document(self):
        from ontology import store_ddl
        root = Path(tempfile.mkdtemp())
        try:
            with sqlite3.connect(root / "store.sqlite") as db:
                store_ddl.create(db)
            side = finish_parallel._Scratch(root / "step.sqlite")
            with sqlite3.connect(":memory:") as build:
                store_ddl.create(build)
                side.prepare(build, "conversation_id")
            side.conn.close()
            side.conn = None
            reader = finish_parallel._reader(str(root / "store.sqlite"), str(side.path))
            plan = " | ".join(row[3] for row in reader.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM links WHERE from_object = ? AND "
                "to_object = ? AND binding_kind = ? ORDER BY link_id", ("a", "b", "mention")))
            reader.close()
            self.assertIn("SEARCH l USING INDEX links_from (from_object=?)", plan)
            self.assertIn("SEARCH plx_step.links USING INDEX links_from (from_object=?)", plan)
            self.assertNotIn("links_to", plan)
        finally:
            import shutil
            shutil.rmtree(root)


class EarlierRaiseMarkingsTests(unittest.TestCase):
    """A raise that reads another document's mention link (its conversation's message)
    joins the markings that link holds when the serial step reaches it: those an earlier
    document's raise added, never those of a later document's raise."""

    def test_markings_follow_the_serial_order(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE links (link_id PRIMARY KEY, tier, reason, why_not, "
                   "evidence_handles, markings)")
        db.executemany("INSERT INTO links VALUES (?, 'lead', '[]', '[]', '[]', '[]')",
                       [("a1",), ("b1",), ("c1",)])
        tag = finish_parallel._TAG
        spill = Path(tempfile.mkdtemp()) / "spill"
        import pickle
        with open(spill, "wb") as out:
            # a: raised, its markings grow by "hr"; b reads a1 (earlier) and c1 (later);
            # c: raised, grows by "fin".
            pickle.dump(("a", [("a1", "r", "w", "e", ["hr", tag + "a1"], [])]), out)
            pickle.dump(("b", [("b1", "r", "w", "e", [tag + "a1", tag + "b1", tag + "c1"],
                                [])]), out)
            pickle.dump(("c", [("c1", "r", "w", "e", ["fin", tag + "c1"], [])]), out)
        self.assertEqual(finish_parallel._apply(db, spill), 3)
        marks = dict(db.execute("SELECT link_id, markings FROM links"))
        self.assertEqual(marks, {"a1": '["hr"]', "b1": '["hr"]', "c1": '["fin"]'})
        spill.unlink()
        spill.parent.rmdir()


if __name__ == "__main__":
    unittest.main()
