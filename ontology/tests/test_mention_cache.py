"""The ``mentions`` step with the extraction cache (``ontology.mention_cache``) writes what
the step writes without it, and a later run whose cache holds every document reads none:
cold (every document read, extracted and kept), warm (nothing extracted, every group
held, the resolver's code changed meanwhile), after an extractor change (every entry
missed and kept again), and after a prefill that resolved nothing; at 1, 10 and 60
messages, the document workers on. No network, no credentials. Evidence:
injected-transport fixture.
"""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from integrations.tests.test_capture_mail import SCOPE
from ontology import finish_parallel, mention_cache, mentions
from ontology import project_incremental as inc
from ontology import schema as schema_loader
from ontology.project import MailCaptures
from ontology.tests.test_finish_parallel import NOW, _captures
from ontology.tests.test_mail_documents import StaticPortal
from ontology.tests.test_project_incremental import incremental_path, tables


def _step(builder, projector):
    return finish_parallel.store_mention_links(builder, projector, processes=3,
                                               min_documents=1, min_groups=1)


def _never(document):
    raise AssertionError(f"extracted again: {document.citation}")


def _held(path: Path) -> int:
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT count(*) FROM extracted").fetchone()[0]


class MentionCacheTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.schema = schema_loader.load_schema()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, root: Path, rows, name: str, step, workers: int = 1):
        with MailCaptures(root / "state", internal_domains=SCOPE.internal_domains) as mail:
            generation = inc.build_generation(
                root / name, self.schema, {"portal_api": StaticPortal(rows),
                                           "graph_mail": mail},
                clock=lambda: NOW, post_project={"mentions": step}, workers=workers,
                space=incremental_path)
        report = generation.report["post_project"]["mentions"]
        return (generation.content_digest, tables(generation.store),
                report["counts"], report.get("documents_failed", 0))

    def test_cached_runs_write_the_plain_step_and_a_warm_one_reads_nothing(self):
        for messages in (1, 10, 60):
            with self.subTest(messages=messages):
                root = self.root / f"m{messages}"
                _, rows = _captures(root, messages)
                workers = 3 if messages == 60 else 1
                cache = root / "extract.sqlite"
                plain = self.build(root, rows, "plain", _step, workers)
                cold = self.build(root, rows, "cold",
                                  mention_cache.cached_step(_step, cache), workers)
                self.assertEqual(cold, plain)
                self.assertEqual(_held(cache), messages)
                self.assertEqual(mention_cache.LAST["kept"], messages)
                # the resolver's code changed since (another migration): the mentions
                # are still the cache's, no document is read or extracted
                with mock.patch.object(mentions, "document_mentions", _never), \
                        mock.patch.object(mentions, "_memo_code",
                                          lambda declared: "0" * 64):
                    warm = self.build(root, rows, "warm",
                                      mention_cache.cached_step(_step, cache), workers)
                self.assertEqual(warm, plain)
                self.assertEqual(finish_parallel.LAST["links.groups_held"], messages)
                self.assertEqual(mention_cache.LAST["kept"], 0)
                # another extractor: every entry missed, extracted and kept again
                with mock.patch.object(mention_cache, "extractor_code",
                                       lambda: "ab" * 32):
                    other = self.build(root, rows, "other",
                                       mention_cache.cached_step(_step, cache), workers)
                self.assertEqual(other, plain)
                self.assertEqual(mention_cache.LAST["kept"], messages)
                self.assertEqual(_held(cache), messages)   # one entry per document

    def test_a_prefill_resolves_nothing_and_leaves_the_step_nothing_to_read(self):
        root = self.root / "prefill"
        _, rows = _captures(root, 60)
        cache = root / "extract.sqlite"
        plain = self.build(root, rows, "plain", _step, 3)
        for workers in (1, 3):
            cache.unlink(missing_ok=True)
            with MailCaptures(root / "state", internal_domains=SCOPE.internal_domains) as mail:
                mail.records([])
                report = mention_cache.prefill({"graph_mail": mail}, self.schema, cache,
                                               workers=workers)
            self.assertEqual((report["documents"], report["extracted"],
                              report["failed_documents"]), (60, 60, 0), report)
            if workers > 1:
                self.assertGreater(report["pool"]["tasks"], 0, report)
            with mock.patch.object(mentions, "document_mentions", _never):
                warm = self.build(root, rows, f"warm{workers}",
                                  mention_cache.cached_step(_step, cache), 3)
            self.assertEqual(warm, plain)
            self.assertEqual(mention_cache.LAST["kept"], 0)
        # a second prefill over a full cache reads no group
        with MailCaptures(root / "state", internal_domains=SCOPE.internal_domains) as mail:
            mail.records([])
            again = mention_cache.prefill({"graph_mail": mail}, self.schema, cache)
        self.assertEqual((again["groups_held"], again["extracted"]), (60, 0))

    def test_an_unreadable_cache_entry_is_a_miss_and_the_file_is_private(self):
        root = self.root / "broken"
        _, rows = _captures(root, 10)
        cache = root / "extract.sqlite"
        plain = self.build(root, rows, "plain", _step)
        self.build(root, rows, "cold", mention_cache.cached_step(_step, cache))
        self.assertEqual(os.stat(cache).st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(cache)) as db:
            db.execute("UPDATE extracted SET value = x'00' WHERE rowid IN "
                       "(SELECT rowid FROM extracted LIMIT 3)")
            db.commit()
        again = self.build(root, rows, "again", mention_cache.cached_step(_step, cache))
        self.assertEqual(again, plain)


if __name__ == "__main__":
    unittest.main()
