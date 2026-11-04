"""A generation build that fails or is killed after its projection resumes there.

What must hold (``ontology/project_incremental.py`` "Resume"): a failure in a
``post_project`` step, a killed process or a failed digest leaves the committed generation
current and the projected build on disk; the next build with the same unit keys and base
goes on at the first step not done (a step done is never run again) and commits a
generation equal to an uninterrupted build of the same captures and steps; a build whose
keys changed meanwhile drops the kept one and builds as before; a step that fails one
document records it and goes on, and one that fails for most documents fails the build.
"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology import project_incremental as inc
from ontology import store as store_module
from ontology.document_failures import DocumentFailures, TooManyFailed
from ontology.project import build_store
from ontology.tests import synthetic_captures
from ontology.tests.test_project_incremental import T2, incremental_path, tables


class Boom(Exception):
    pass


class ResumeTests(unittest.TestCase):
    ROWS = 6

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.schema = load_schema()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def sources(self, changed=None):
        return synthetic_captures.sources(self.schema, self.ROWS, [changed] if changed else [])

    def steps(self):
        """Two steps that write rows (a link, then a value) and a third that reads them,
        each counting its runs and failing while named in ``self.fail``."""
        def link(builder, projector):
            self.calls.append("a_link")
            conn = builder.connection
            a, b = conn.execute("SELECT object_id FROM objects WHERE object_type = 'Supplier' "
                                "ORDER BY object_id LIMIT 2").fetchall()
            at = conn.execute("SELECT recorded_at, superseded_at FROM object_versions WHERE "
                              "object_id = ? ORDER BY recorded_at LIMIT 1", (a[0],)).fetchone()
            builder.add("links", [{
                "link_id": "test:resume", "link_type": "Supplier.test_resume",
                "from_object": a[0], "to_object": b[0], "tier": "lead",
                "binding_kind": "mention", "reason": '[{"test":"resume"}]', "why_not": "[]",
                "evidence_handles": '["test:h"]', "markings": "[]", "recorded_at": at[0],
                "superseded_at": at[1]}])
            if "a_link" in self.fail:   # rows written, then the step fails
                raise Boom("a_link")
            return {"counts": {"links": 1}}

        def count(builder, projector):
            self.calls.append("b_count")
            if "b_count" in self.fail:
                raise Boom("b_count")
            n = builder.connection.execute(
                "SELECT COUNT(*) FROM links WHERE link_id = 'test:resume'").fetchone()[0]
            return {"counts": {"seen": n}}
        return {"a_link": link, "b_count": count}

    def build(self, changed=None, **options):
        return inc.build_generation(self.state, self.schema, self.sources(changed),
                                    clock=lambda: T2, post_project=self.steps(), **options)

    def cold(self, name, changed=None):
        self.fail = set()
        return build_store(self.root / name, self.schema, self.sources(changed),
                           clock=lambda: T2, post_project=self.steps())

    def kept(self):
        return sorted(p.name for p in (self.state / "generations").iterdir()
                      if not (p / "units.json").exists())

    def assertEqualsCold(self, generation, name, changed=None):
        report = self.cold(name, changed)
        self.assertEqual(generation.content_digest, report["content_digest"])
        self.assertEqual(tables(generation.store), tables(self.root / name))
        with closing(sqlite3.connect(generation.store)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM store_meta WHERE key = ?",
                                           (store_module.CHECKPOINT_META,)).fetchone())
            self.assertEqual(store_module.content_digest(conn), generation.content_digest)

    def test_a_step_failure_resumes_at_that_step_cold_and_incremental(self):
        self.fail = {"b_count"}
        with self.assertRaises(Boom):
            self.build()
        self.assertIsNone(inc.current_generation(self.state))
        self.assertEqual(len(self.kept()), 1)
        self.assertEqual(self.calls, ["a_link", "b_count"])
        # A disk check before the next build counts the kept build as free (the host's
        # ``build_disk``): it is resumed or removed before anything is written.
        directory, held = inc._held_checkpoint(self.state)
        self.assertGreaterEqual(inc.kept_bytes(self.state),
                                (directory / held["store"]).stat().st_size)
        self.fail, self.calls = set(), []
        first = self.build()
        self.assertEqual(self.calls, ["b_count"])   # a_link is done: never run again
        self.assertEqual((first.mode, first.seconds.get("resumed")), ("cold", 1))
        self.assertEqual(first.report["post_project"]["b_count"]["counts"], {"seen": 1})
        self.assertEqual((self.kept(), inc.kept_bytes(self.state)), ([], 0))
        self.assertEqualsCold(first, "cold1.sqlite")

        # Incremental: a step that fails after writing its rows; they are rolled back.
        self.fail, self.calls = {"a_link"}, []
        with self.assertRaises(Boom):
            self.build("public.app_product", space=incremental_path)
        self.assertEqual(inc.current_generation(self.state).id, first.id)
        self.fail, self.calls = set(), []
        second = self.build("public.app_product", space=incremental_path)
        self.assertEqual(self.calls, ["a_link", "b_count"])
        self.assertEqual((second.mode, second.seconds.get("resumed")), ("incremental", 1))
        self.assertEqualsCold(second, "cold2.sqlite", "public.app_product")

    def test_a_killed_build_rolls_back_to_its_last_step(self):
        """A process killed inside a step (no exception, no cleanup: ``os._exit``) leaves a
        hot journal; the next build rolls its rows back and runs that step again."""
        pid = os.fork()
        if pid == 0:   # the build that is killed
            try:
                real = self.steps()

                def killed(builder, projector):
                    real["b_count"](builder, projector)
                    conn = builder.connection   # rows changed and spilled to the file
                    conn.execute("PRAGMA cache_size = 2")
                    conn.execute("DELETE FROM links")
                    conn.execute("UPDATE objects SET missing_from_source_since = 'x'")
                    os._exit(9)
                inc.build_generation(self.state, self.schema, self.sources(),
                                     clock=lambda: T2,
                                     post_project={"a_link": real["a_link"], "b_count": killed})
            finally:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 9)
        self.assertEqual(len(self.kept()), 1)
        directory, held = inc._held_checkpoint(self.state)
        self.assertTrue((directory / (held["store"] + "-journal")).is_file())   # hot
        resumed = self.build()
        self.assertEqual(self.calls, ["b_count"])
        self.assertEqual(resumed.report["post_project"]["b_count"]["counts"], {"seen": 1})
        self.assertEqualsCold(resumed, "cold.sqlite")

    def test_a_failed_digest_resumes_at_the_digest(self):
        real = store_module.content_digest_parts
        with mock.patch.object(store_module, "content_digest_parts", side_effect=Boom):
            with self.assertRaises(Boom):
                self.build()
        self.calls = []
        with mock.patch.object(store_module, "content_digest_parts", side_effect=real) as dig:
            resumed = self.build()
        self.assertEqual(self.calls, [])   # checked and committed: only digested again
        self.assertEqual(dig.call_count, 1)
        self.assertEqual(resumed.report["post_project"]["b_count"]["counts"], {"seen": 1})
        self.assertEqualsCold(resumed, "cold.sqlite")

    def test_changed_captures_drop_the_kept_build(self):
        first = self.build()
        self.fail = {"b_count"}
        with self.assertRaises(Boom):
            self.build("public.app_product", space=incremental_path)
        self.assertEqual(len(self.kept()), 1)
        self.fail, self.calls = set(), []
        other = self.build("public.app_purchase_order", space=incremental_path)
        self.assertEqual(self.calls, ["a_link", "b_count"])
        self.assertNotIn("resumed", other.seconds)
        self.assertEqual(self.kept(), [])
        self.assertNotEqual(other.id, first.id)
        self.assertEqualsCold(other, "cold.sqlite", "public.app_purchase_order")

    def test_a_kept_file_without_its_checkpoint_is_never_resumed(self):
        self.fail = {"b_count"}
        with self.assertRaises(Boom):
            self.build()
        [kept] = self.kept()
        directory = self.state / "generations" / kept
        held = inc._held_checkpoint(self.state)[1]
        with closing(sqlite3.connect(directory / held["store"])) as conn:
            conn.execute("DELETE FROM store_meta WHERE key = ?", (store_module.CHECKPOINT_META,))
            conn.commit()
        self.fail, self.calls = set(), []
        built = self.build()
        self.assertEqual(self.calls, ["a_link", "b_count"])
        self.assertNotIn("resumed", built.seconds)
        self.assertEqualsCold(built, "cold.sqlite")


class DocumentFailuresTests(unittest.TestCase):
    def setUp(self):
        logs = self.assertLogs("ontology.document_failures", "WARNING")
        logs.__enter__()
        self.addCleanup(logs.__exit__, None, None, None)

    def test_one_document_is_its_own_gap_and_most_failing_is_the_steps(self):
        for seen in (1, 10, 60, 10_000):
            with self.subTest(seen=seen):
                failures = DocumentFailures("mentions")
                done = [failures.run(f"h{n}", lambda n=n: n if n else 1 / n)
                        for n in range(seen)]
                self.assertEqual([ok for ok, _ in done], [False] + [True] * (seen - 1))
                self.assertEqual(failures.report(), {
                    "documents_failed": 1, "failed_errors": {"ZeroDivisionError": 1}})
        clean = DocumentFailures("text_index")
        clean.run("h", lambda: 1)
        self.assertEqual(clean.report(), {})   # a report as before
        many = DocumentFailures("mentions")
        for n in range(300):
            many.run(f"h{n}", (lambda: 1 / 0) if n < 101 else (lambda: 1))
        with self.assertRaises(TooManyFailed):
            many.report()
        with self.assertRaises(sqlite3.OperationalError):   # the build's, not a document's
            DocumentFailures("mentions").run("h", lambda: sqlite3.connect(":memory:").execute(
                "SELECT * FROM missing"))
        # A document's fields (text_index): each document whose field fails counts once,
        # and the bound holds over them.
        fields = DocumentFailures("text_index")
        fields.seen = 200
        for n in range(5000):
            fields.run(f"h{n % 150}", lambda: 1 / 0, part=True)
        self.assertEqual((fields.failed, fields.seen), (150, 200))
        with self.assertRaises(TooManyFailed):
            fields.report()
        early = DocumentFailures("mentions")
        with self.assertRaises(TooManyFailed):
            for n in range(5000):
                early.run(f"h{n}", lambda: 1 / 0)
        self.assertEqual(early.failed, 1000)


def mail_world(root: Path, messages: dict[str, str]):
    """A captured mailbox (``messages``: id -> body, each naming a PO) and a function
    ``sources(numbers)`` of the build's sources over it with Portal POs ``numbers``."""
    from integrations import graph_read
    from integrations.capture_mail import MailCapture, MailCaptureStore
    from integrations.graph_read import GraphMailReader
    from integrations.live_source_guard import BoundedBackoff
    from integrations.tests.test_capture_mail import (BOX_A, CAPTURE, ENV, SCOPE, Clock,
                                                       FakeGraph, capture_audience)
    from ontology.project import MailCaptures
    from ontology.tests.test_mail_documents import StaticPortal, po

    os.environ.pop("PLX_WORKBENCH_API_KEY", None)
    graph = FakeGraph()
    for name, body in messages.items():
        graph.put(BOX_A, name, f"CK-{name}", body=body, subject=f"About {name}")
    store = MailCaptureStore(root / "mail", clock=Clock())
    api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
        sleep=lambda s: None, jitter=lambda: 0.0))
    MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                sleep=lambda s: None).run(SCOPE)

    def sources(numbers):
        portal = StaticPortal([po("public.app_purchase_order", {"id": f"p{n}"},
                                  {"id": f"p{n}", "po_number": number})
                               for n, number in enumerate(numbers)])
        return {"portal_api": portal,
                "graph_mail": MailCaptures(root / "mail",
                                           internal_domains=SCOPE.internal_domains)}

    def citation(name):
        return graph_read.mail_citation(BOX_A, name, f"CK-{name}")
    return sources, citation


class MailStepTests(unittest.TestCase):
    """The mentions and text_index steps over captured mail."""

    def steps(self):
        from ontology import mentions, text_index
        return {"mentions": mentions.store_mention_links,
                "text_index": text_index.store_text_index}

    def test_one_failing_message_does_not_fail_the_generation(self):
        """One message whose derivation raises is that message's gap; the generation
        commits with the others."""
        from ontology import mentions, text_index
        from ontology.schema import load_schema
        from ontology.tests.test_mail_documents import NOW

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources, citation = mail_world(root, {
                "MSG-1": "Please confirm PO100234.", "MSG-2": "Re: PO100236 freight.",
                "MSG-3": "PO100236 shipped."})
            bad = citation("MSG-2")
            extract, facts = mentions.document_mentions, text_index._text_facts

            def failing(real):
                def run(document):
                    if document.citation == bad:
                        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")
                    return real(document)
                return run
            built_sources = sources(["PO100234", "PO100236"])
            with built_sources["graph_mail"], \
                    mock.patch.object(mentions, "document_mentions", failing(extract)), \
                    mock.patch.object(text_index, "_text_facts", failing(facts)), \
                    self.assertLogs("ontology.document_failures", "WARNING") as logged:
                built = inc.build_generation(root / "projection", load_schema(),
                                             built_sources, clock=lambda: NOW,
                                             post_project=self.steps())
            steps = built.report["post_project"]
            for name in ("mentions", "text_index"):
                self.assertEqual(steps[name]["documents_failed"], 1, name)
                self.assertEqual(steps[name]["failed_errors"], {"UnicodeEncodeError": 1})
            self.assertEqual(steps["mentions"]["counts"]["documents"], 2)
            self.assertTrue(any(bad in line for line in logged.output))
            with closing(sqlite3.connect(built.store)) as conn:
                linked = {row[0] for row in conn.execute(
                    "SELECT DISTINCT json_extract(evidence_handles, '$[0]') FROM links "
                    "WHERE binding_kind = 'mention'")}
                indexed = {row[0] for row in conn.execute("SELECT citation FROM text_fields")}
            self.assertNotIn(bad, linked | indexed)
            self.assertIn(citation("MSG-3"), indexed)
            self.assertTrue(linked)

    def test_a_field_whose_chunking_fails_is_its_documents_gap(self):
        """text_index: a field whose text cannot be chunked fails its document, counted
        in the step's report (not only its group's reading)."""
        from ontology import text_index
        from ontology.schema import load_schema
        from ontology.tests.test_mail_documents import NOW

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources, citation = mail_world(root, {
                "MSG-1": "Please confirm PO100234.", "MSG-2": "Re: PO100236 freight."})
            pieces = text_index._pieces

            def failing(text):
                if "freight" in text:
                    raise ValueError("chunk")
                return pieces(text)
            built_sources = sources(["PO100234", "PO100236"])
            with built_sources["graph_mail"], \
                    mock.patch.object(text_index, "_pieces", failing), \
                    self.assertLogs("ontology.document_failures", "WARNING"):
                built = inc.build_generation(root / "projection", load_schema(),
                                             built_sources, clock=lambda: NOW,
                                             post_project=self.steps(), workers=1)
            step = built.report["post_project"]["text_index"]
            self.assertEqual((step["documents_failed"], step["failed_errors"]),
                             (1, {"ValueError": 1}))
            with closing(sqlite3.connect(built.store)) as conn:
                indexed = {row[0] for row in conn.execute("SELECT citation FROM text_fields")}
            self.assertNotIn(citation("MSG-2"), indexed)
            self.assertIn(citation("MSG-1"), indexed)

    def test_a_cold_build_over_a_generation_takes_what_its_document_steps_derived(self):
        """A cold build (a code change re-projects every unit) over a committed
        generation resolves again only the documents whose identifier keys or version
        rows changed between the two (``_changed_keys_against``) and takes the text index
        chunks of every unchanged field from it, reading no document text, and equals a
        build that derives every one."""
        from ontology import mentions
        from ontology.project import MailCaptures
        from ontology.schema import load_schema
        from ontology.tests.test_mail_documents import NOW

        def cold_mode(need):   # the build a code change makes: every unit again
            if need["mode"] == "incremental":
                raise OSError("cold_pinned")
        schema = load_schema()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources, citation = mail_world(root, {
                f"MSG-{n}": f"Please confirm PO1002{n:02d} today." for n in range(12)})
            numbers = [f"PO1002{n:02d}" for n in range(12)]
            resolved: list[str] = []
            real = mentions._document_links

            def counted(conn, declared, document, *args):
                resolved.append(document.citation)
                return real(conn, declared, document, *args)

            read: list[str] = []
            load = MailCaptures._message_documents

            def counted_load(mail, row, attached):
                read.append(row["l1_handle"])
                return load(mail, row, attached)

            def build(numbers, state="projection", **options):
                resolved.clear(), read.clear()
                built = sources(numbers)
                with built["graph_mail"], \
                        mock.patch.object(mentions, "_document_links", counted), \
                        mock.patch.object(MailCaptures, "_message_documents", counted_load):
                    return inc.build_generation(root / state, schema, built,
                                                clock=lambda: NOW, post_project=self.steps(),
                                                **options)
            first = build(numbers)
            self.assertEqual(len(resolved), 12)
            again = build(numbers, space=cold_mode)
            self.assertEqual((again.mode, resolved, read), ("cold", [], []))
            self.assertEqual(again.content_digest, first.content_digest)
            moved = list(numbers)
            moved[3] = "PO100999"   # one PO's number changes: its mention resolves again
            third = build(moved, space=cold_mode)
            self.assertEqual((third.mode, resolved, read), ("cold", [citation("MSG-3")], []))
            fresh = build(moved, state="fresh")   # no generation before: every one
            self.assertEqual((len(resolved), len(read)), (12, 24))   # both steps read
            self.assertEqual(third.content_digest, fresh.content_digest)
            self.assertEqual(tables(third.store), tables(fresh.store))

if __name__ == "__main__":
    unittest.main()
