"""The gap inbox's per-reader "not evaluated" counts (``proactive_engine._rule_coverage``):
the set-based query counts exactly what the per-record correlated query counted, for
readers of mixed grants, stays fast on one large skip, and a kept count is never served
across readers or after the inbox or store changed."""

from __future__ import annotations

import json
import os
import random
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
from contextlib import closing
from pathlib import Path

from ontology import store as ostore
from operating import proactive_engine as pe

# The query as it stood before the set-based rewrite: per counted record, every record of
# its skip re-checked. The reference the rewrite must equal.
_OLD_VISIBLE = (
    "SELECT u.detector, count(DISTINCT u.object_id) FROM unjudged_causes u "
    "WHERE u.subject = 1 AND NOT EXISTS (SELECT 1 FROM unjudged_causes c WHERE "
    "c.detector = u.detector AND c.skip = u.skip AND NOT (c.markings IN (SELECT "
    "value FROM json_each(:marks)) AND " + pe._OBJECT_VISIBLE.format(o="c.object_id")
    + ")) GROUP BY u.detector")
_OLD_BY_REASON = (
    "SELECT u.detector, r.reason, count(DISTINCT u.object_id) FROM unjudged_causes u "
    "JOIN unjudged_reasons r ON r.detector = u.detector AND r.skip = u.skip "
    "WHERE u.subject = 1 AND NOT EXISTS (SELECT 1 FROM unjudged_causes c WHERE "
    "c.detector = u.detector AND c.skip = u.skip AND NOT (c.markings IN (SELECT "
    "value FROM json_each(:marks)) AND " + pe._OBJECT_VISIBLE.format(o="c.object_id")
    + ")) GROUP BY u.detector, r.reason")

AUDIENCES = ["aud:a", "aud:b", "aud:c"]
MARKINGS = ['[]', '["finance"]', '["pii"]', '["finance","pii"]']
READERS = {   # (audiences, markings held)
    "owner": (AUDIENCES, ["finance", "pii"]),
    "finance": (["aud:a", "aud:b"], ["finance"]),
    "plain": (["aud:a"], []),
    "pii": (["aud:a", "aud:c"], ["pii"]),
    "nothing": ([], []),
}


def _inbox(tmp: Path, causes: list[tuple[str, int, str, str, int]],
           objects: dict[str, tuple[str, str | None]], reasons: dict[tuple[str, int], str]
           ) -> tuple[Path, Path]:
    """An inbox and a store holding ``causes`` (detector, skip, object, markings,
    subject) over ``objects`` (id -> audience, current markings or None: no version)."""
    store, inbox = tmp / "store.sqlite", tmp / "inbox.sqlite"
    with closing(sqlite3.connect(store)) as st:
        st.executescript(
            "CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE objects (object_id TEXT PRIMARY KEY, audience_key TEXT);"
            "CREATE TABLE object_versions (object_id TEXT, superseded_at TEXT, markings TEXT);"
            "CREATE INDEX object_versions_by_object ON object_versions (object_id);")
        st.executemany("INSERT INTO store_meta VALUES (?, ?)",
                       [("content_digest", "d1"), ("audience_keys", json.dumps(AUDIENCES)),
                        ("format", ostore.FORMAT)])
        st.executemany("INSERT INTO objects VALUES (?, ?)",
                       [(o, a) for o, (a, _) in objects.items()])
        st.executemany("INSERT INTO object_versions VALUES (?, NULL, ?)",
                       [(o, m) for o, (_, m) in objects.items() if m is not None])
        # a superseded version at open markings never makes a record visible
        st.executemany("INSERT INTO object_versions VALUES (?, '2026-01-01', '[]')",
                       [(o,) for o in list(objects)[::5]])
        st.commit()
    with closing(sqlite3.connect(inbox)) as conn:
        conn.executescript(_DDL)
        conn.execute("INSERT INTO meta VALUES ('schema', ?)", (pe.INBOX_SCHEMA,))
        conn.executemany("INSERT OR IGNORE INTO unjudged_causes VALUES (?, ?, ?, ?, ?)", causes)
        conn.executemany("INSERT INTO unjudged_reasons VALUES (?, ?, ?)",
                         [(d, s, r) for (d, s), r in reasons.items()])
        for name in sorted({c[0] for c in causes}):
            conn.execute("INSERT INTO rule_coverage VALUES (?, 0, '2026-11-01T00:00:00Z')", (name,))
            conn.execute("INSERT INTO detector_runs VALUES (?, 'complete', '[]', 0, ?, "
                         "'2026-11-01T00:00:00Z')", (name, json.dumps({"x": 1})))
        conn.commit()
    return inbox, store


_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE detector_runs (detector TEXT PRIMARY KEY, status TEXT NOT NULL,
  reasons TEXT NOT NULL, found INTEGER NOT NULL, not_evaluated TEXT NOT NULL,
  run_at TEXT NOT NULL);
CREATE TABLE rule_coverage (detector TEXT PRIMARY KEY,
  unattributed INTEGER NOT NULL, run_at TEXT NOT NULL);
CREATE TABLE unjudged_causes (detector TEXT NOT NULL, skip INTEGER NOT NULL,
  object_id TEXT NOT NULL, markings TEXT NOT NULL, subject INTEGER NOT NULL,
  PRIMARY KEY (detector, skip, object_id, markings));
CREATE TABLE unjudged_reasons (detector TEXT NOT NULL, skip INTEGER NOT NULL,
  reason TEXT NOT NULL, PRIMARY KEY (detector, skip));
"""


def _open(inbox: Path, store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{inbox}?mode=ro", uri=True, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("ATTACH DATABASE ? AS st", (f"file:{store}?mode=ro",))
    return conn


def _params(reader: str) -> dict[str, str]:
    aud, held = READERS[reader]
    return {"aud": json.dumps(aud), "marks": json.dumps(ostore.allowed_marking_sets(held))}


def _old(conn: sqlite3.Connection, params: dict[str, str]
         ) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    visible = {r[0]: r[1] for r in conn.execute(_OLD_VISIBLE, params)}
    by_reason: dict[str, dict[str, int]] = {}
    for name, reason, n in conn.execute(_OLD_BY_REASON, params):
        by_reason.setdefault(name, {})[reason] = n
    return visible, by_reason


def _new(conn: sqlite3.Connection, params: dict[str, str]
         ) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return pe._visible_unjudged(conn, params, tables)


def _mixed(seed: int) -> tuple[list, dict, dict]:
    """Skips of 1..40 records, records at every audience and markings, some records
    the store no longer holds or holds without a current version, the same record
    under two markings, non-subject causes."""
    rnd = random.Random(seed)
    objects = {}
    for i in range(400):
        roll = rnd.random()
        objects[f"o{i}"] = (rnd.choice(AUDIENCES),
                            None if roll < 0.05 else rnd.choice(MARKINGS))
    ids = list(objects) + ["gone1", "gone2"]   # gone: not in the store at all
    causes, reasons = [], {}
    for det in ("rule_a", "rule_b", "rule_c"):
        for skip in range(60):
            size = rnd.choice([1, 1, 2, 3, 5, 40])
            for _ in range(size):
                obj = rnd.choice(ids)
                at = objects.get(obj, ("", "[]"))[1] or "[]"
                if rnd.random() < 0.1:
                    at = rnd.choice(MARKINGS)   # cause recorded at other markings
                causes.append((det, skip, obj, at, int(rnd.random() < 0.7)))
            if rnd.random() < 0.8:
                reasons[(det, skip)] = rnd.choice(["no lines", "no date", "no vendor"])
    return causes, objects, reasons


class RuleCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        pe._coverage_kept.clear()
        self.addCleanup(pe._coverage_kept.clear)
        pe._tables_kept.clear()
        self.addCleanup(pe._tables_kept.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        kept = mock.patch.dict(os.environ, {"PLX_RULE_COVERAGE_DIR": self.tmp.name + "/kept"})
        kept.start()
        self.addCleanup(kept.stop)

    def test_another_process_reads_the_table_kept_on_disk_and_counts_exactly(self) -> None:
        """A restart, the warm-up or an Ask host (no table in memory) reads the table one
        process built from disk, with no statement over the store, and every reader's
        counts are exactly the per-record query's; a kept file that is unreadable or of
        another table is never read: the table is built again, exact."""
        inbox, store = _inbox(Path(self.tmp.name), *_mixed(5))
        with closing(_open(inbox, store)) as conn:
            _new(conn, _params("owner"))
            files = list((Path(self.tmp.name) / "kept").glob("table-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            other = json.loads(files[0].read_text(encoding="utf-8"))
            other["key"] = "('another inbox',)"   # a whole table, kept for another key
            for broken in (None, "{not json", json.dumps(other)):
                for reader in READERS:
                    pe._coverage_kept.clear()
                    pe._tables_kept.clear()
                    if broken is not None:
                        files[0].write_text(broken, encoding="utf-8")
                    statements: list[str] = []
                    conn.set_trace_callback(statements.append)
                    expected = _old(conn, _params(reader))
                    statements.clear()
                    self.assertEqual(_new(conn, _params(reader)), expected, (broken, reader))
                    store_reads = [s for s in statements if "st.object" in s]
                    if broken is None:
                        self.assertFalse(store_reads, reader)   # read from disk
                    else:
                        self.assertTrue(store_reads, reader)    # built again
                    conn.set_trace_callback(None)
                    if broken is None:
                        continue
                    break

    def test_set_query_counts_exactly_what_the_per_record_query_counted(self) -> None:
        for seed in range(4):
            sub = Path(self.tmp.name) / str(seed)
            sub.mkdir()
            inbox, store = _inbox(sub, *_mixed(seed))
            with closing(_open(inbox, store)) as conn:
                seen = set()
                for reader in READERS:
                    params = _params(reader)
                    old = _old(conn, params)
                    pe._coverage_kept.clear()
                    self.assertEqual(_new(conn, params), old, (seed, reader))
                    # kept: the second read is the same, never another reader's
                    self.assertEqual(_new(conn, params), old, (seed, reader))
                    seen.add(json.dumps(old, sort_keys=True))
                # the fixture really mixes visibility: readers see different counts
                self.assertGreater(len(seen), 2)

    def test_listed_records_are_exactly_the_ones_counted_for_each_reader(self) -> None:
        """CAP-L167: a reader's unjudged list per rule and per reason names exactly the
        records the per-record reference counts for them (never one whose skip cause
        they may not read), its total is their count, and paging loses or repeats none."""
        listed_ids = (
            "SELECT DISTINCT u.object_id FROM unjudged_causes u {join}"
            "WHERE u.detector = :rule AND u.subject = 1 AND NOT EXISTS (SELECT 1 FROM "
            "unjudged_causes c WHERE c.detector = u.detector AND c.skip = u.skip AND NOT "
            "(c.markings IN (SELECT value FROM json_each(:marks)) AND "
            + pe._OBJECT_VISIBLE.format(o="c.object_id") + "))")
        by_reason = ("JOIN unjudged_reasons r ON r.detector = u.detector AND "
                     "r.skip = u.skip AND r.reason = :reason ")
        rules = {"rule_a": "late_or_unconfirmed_po", "rule_b": "late_customer_order"}
        for seed in range(2):
            sub = Path(self.tmp.name) / f"l{seed}"
            sub.mkdir()
            causes, objects, reasons = _mixed(seed)
            causes = [(rules.get(c[0], c[0]), *c[1:]) for c in causes if c[0] in rules]
            reasons = {(rules[d], k): r.replace(" ", "_") for (d, k), r in reasons.items()
                       if d in rules}
            inbox, store = _inbox(sub, causes, objects, reasons)
            for reader, (auds, held) in READERS.items():
                params = _params(reader)
                with closing(_open(inbox, store)) as conn:
                    counts, per_reason = _new(conn, params)
                    for rule in rules.values():
                        for reason in (None, "no_lines", "no_vendor"):
                            want = sorted(r[0] for r in conn.execute(
                                listed_ids.format(join=by_reason if reason else ""),
                                {**params, "rule": rule, "reason": reason}))
                            got, offset = [], 0
                            while True:
                                page = pe.unjudged_records(
                                    inbox, store, rule=rule, reason=reason,
                                    may_read=lambda a, auds=auds: a in auds, held=held,
                                    offset=offset, limit=7)
                                got += page["records"]
                                offset += 7
                                if offset >= page["total"]:
                                    break
                            self.assertEqual(got, want, (seed, reader, rule, reason))
                            count = counts.get(rule, 0) if reason is None \
                                else per_reason.get(rule, {}).get(reason, 0)
                            self.assertEqual(page["total"], count, (seed, reader, rule, reason))
                            self.assertTrue(page["complete"])
        # an unknown rule or a reason that is no reason name is refused, not read
        for bad in ({"rule": "no_such_rule"},
                    {"rule": "late_customer_order", "reason": "x' OR 1=1 --"}):
            with self.assertRaises(pe.UnknownCheck):
                pe.unjudged_records(inbox, store, may_read=lambda a: True, held=(), **bad)

    def test_kept_counts_are_per_reader_and_dropped_when_the_inbox_changes(self) -> None:
        objects = {"open1": ("aud:a", "[]"), "open2": ("aud:a", "[]"),
                   "fin": ("aud:a", '["finance"]')}
        causes = [("rule_a", 1, "open1", "[]", 1), ("rule_a", 2, "open2", "[]", 1)]
        inbox, store = _inbox(Path(self.tmp.name), causes, objects, {})
        with closing(_open(inbox, store)) as conn:
            self.assertEqual(_new(conn, _params("plain"))[0], {"rule_a": 2})
            self.assertEqual(_new(conn, _params("owner"))[0], {"rule_a": 2})
        # a run records a finance record in skip 2's cause: the plain reader may no
        # longer read that skip's whole cause, so it is no longer counted for them
        with closing(sqlite3.connect(inbox)) as w:
            w.execute("INSERT INTO unjudged_causes VALUES ('rule_a', 2, 'fin', '[\"finance\"]', 0)")
            w.commit()
        with closing(_open(inbox, store)) as conn:
            self.assertEqual(_new(conn, _params("plain"))[0], {"rule_a": 1})
            self.assertEqual(_new(conn, _params("finance"))[0], {"rule_a": 2})
        # the store moves the record to an audience the finance reader lacks
        with closing(sqlite3.connect(store)) as w:
            w.execute("UPDATE objects SET audience_key = 'aud:c' WHERE object_id = 'fin'")
            w.commit()
        with closing(_open(inbox, store)) as conn:
            self.assertEqual(_new(conn, _params("finance"))[0], {"rule_a": 1})
            self.assertEqual(_new(conn, _params("owner"))[0], {"rule_a": 2})

    def test_an_in_memory_store_without_a_change_stamp_is_never_kept(self) -> None:
        inbox, store = _inbox(Path(self.tmp.name), [("rule_a", 1, "o", "[]", 1)],
                              {"o": ("aud:a", "[]")}, {})
        conn = sqlite3.connect(f"file:{inbox}?mode=ro", uri=True, isolation_level=None)
        with closing(conn):
            conn.execute("ATTACH DATABASE ':memory:' AS st")
            conn.execute("CREATE TABLE st.store_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE st.objects (object_id TEXT PRIMARY KEY, audience_key TEXT)")
            conn.execute("CREATE TABLE st.object_versions (object_id TEXT, superseded_at TEXT, "
                         "markings TEXT)")
            conn.execute("INSERT INTO st.store_meta VALUES ('content_digest', 'gen')")
            self.assertIsNone(pe._coverage_key(conn, _params("plain")))
            conn.execute("INSERT INTO st.store_meta VALUES ('changes_stamp', '1')")
            self.assertIsNotNone(pe._coverage_key(conn, _params("plain")))

    def test_the_records_are_read_once_for_every_reader_of_one_inbox_and_store(self) -> None:
        # what the store holds of the unjudged records is read once; each further reader's
        # counts are its grant over that, exact, with no statement over the store
        inbox, store = _inbox(Path(self.tmp.name), *_mixed(7))
        with closing(_open(inbox, store)) as conn:
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            first = _new(conn, _params("owner"))
            self.assertTrue(any("st.objects" in s for s in statements))
            for reader in READERS:
                if reader == "owner":
                    continue
                statements.clear()
                expected = _old(conn, _params(reader))
                statements.clear()
                self.assertEqual(_new(conn, _params(reader)), expected, reader)
                self.assertFalse([s for s in statements if "st.object" in s], reader)
            self.assertEqual(first, _old(conn, _params("owner")))

    def test_one_large_skip_is_counted_fast(self) -> None:
        # one skip of 10,000 records (a rule that skipped everything for one cause):
        # the per-record query made 10^8 visibility checks of it
        n = 10_000
        objects = {f"o{i}": ("aud:a", "[]") for i in range(n)}
        causes = [("rule_a", 1, f"o{i}", "[]", 1) for i in range(n)]
        hidden = dict(objects, h=("aud:c", '["pii"]'))
        inbox, store = _inbox(Path(self.tmp.name), causes + [("rule_a", 1, "h", '["pii"]', 0)],
                              hidden, {("rule_a", 1): "no lines"})
        with closing(_open(inbox, store)) as conn:
            started = time.perf_counter()
            plain = _new(conn, _params("plain"))
            owner = _new(conn, _params("owner"))
            took = time.perf_counter() - started
        self.assertEqual(plain, ({}, {}))   # one hidden record hides the whole skip
        self.assertEqual(owner, ({"rule_a": n}, {"rule_a": {"no lines": n}}))
        self.assertLess(took, 3.0)


if __name__ == "__main__":
    unittest.main()
