"""The post-project steps read a batch of objects per statement, never one (NW-13).

Evidence label: ``isolated-fixture`` (made-up rows in an in-memory store; the real step
functions; no Postgres). Live-unverified: the in-place run over Postgres is proven by
``/data/scratch/steps-b/proof``-style row-for-row comparison, not here.

* ``rejected_pairs``, ``unique_mentions`` (``mention_firm.firm_links``) and the records a
  message names (``sender_parties.named_parties_batch``) return what the per-object reads
  they replace returned, for 1, 10 and 60 objects and across batch boundaries.
* The number of statements grows with the batches, not with the objects.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from collections import Counter
from types import SimpleNamespace

from ontology import mention_firm as mf
from ontology import sender_parties as sp
from ontology import step_reads as SR

OWNER = {"petra_po": "PurchaseOrder", "so_number": "SalesOrder"}


def _lead(n, frm, to, kind, value, span, group):
    reason = [{"feature": "identifier_mention", "agrees": True, "identifier_kind": kind,
               "normalized": value, "span": span, "mention_group": group}]
    return (f"mention:{n}", "Email.mentions_x", frm, to, "lead", json.dumps(reason),
            "[]", "[]", "2026-10-01T00:00:00Z", None)


INSERT = ("INSERT INTO links (link_id, link_type, from_object, to_object, tier, reason, "
          "evidence_handles, markings, recorded_at, superseded_at) VALUES (?,?,?,?,?,?,?,?,?,?)")


def mention_store(messages):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE links (link_id, link_type, from_object, to_object, tier, "
                 "reason, evidence_handles, markings, recorded_at, superseded_at, "
                 "why_not DEFAULT '[]')")   # as every store's link table carries it
    conn.execute("CREATE VIEW link_rows AS SELECT * FROM links")
    for m in range(messages):
        frm = f"Email:m:{m:03d}"
        for n, (to, kind, value) in enumerate([(f"PurchaseOrder:a:{m}", "petra_po", f"PO9{m:05d}"),
                                               (f"SalesOrder:a:{m}", "so_number", f"OR77{m:04d}")]):
            conn.execute(INSERT,
                         _lead(f"{m:03d}-{n}", frm, to, kind, value,
                               [0, len(value) - (m % 3 == 0)], f"g{n}"))
        twins = m % 6   # a third lead of the order group: its twin, joined or not
        if twins in (1, 2, 3, 4):
            twin = f"PurchaseOrder:fm:{m}"
            conn.execute(INSERT,
                         _lead(f"{m:03d}-2", frm, twin, "petra_po", f"PO9{m:05d}",
                               [0, len(f"PO9{m:05d}") - (m % 3 == 0)], "g0"))
            ends = {1: (f"PurchaseOrder:a:{m}", twin, "verified", None),   # one record
                    2: (twin, f"PurchaseOrder:a:{m}", "reviewed", None),   # the other way
                    3: (f"PurchaseOrder:a:{m}", twin, "lead", None),       # not confirming
                    4: (f"PurchaseOrder:a:{m}", twin, "verified", "2026-10-02")}[twins]
            conn.execute(INSERT,
                         (f"same:{m}", "PurchaseOrder.same_record_as", ends[0], ends[1],
                          ends[2], "[]", "[]", "[]", "2026-10-01T00:00:00Z", ends[3]))
        if m % 4 == 0:   # a person rejected the pair of this message's purchase order
            conn.execute(INSERT,
                         (f"rej:{m}", "Email.mentions_x", frm, f"PurchaseOrder:a:{m}",
                          "contradicted", "[]", "[]", "[]", "2026-10-01T00:00:00Z", None))
    conn.commit()
    return conn


def per_object_firm_links(conn, object_types, counts):
    """The step before NW-13: one leads read and one rejections read per object."""
    owner = mf.anchor_types(object_types)
    pending = []
    for oid in mf._from_objects(conn):
        leads = conn.execute(
            "SELECT * FROM links WHERE from_object = ? AND link_id >= ? AND link_id < ? AND "
            "tier = 'lead' AND superseded_at IS NULL ORDER BY link_id",
            [oid, mf.LEAD_PREFIX, mf._end(mf.LEAD_PREFIX)]).fetchall()
        if not leads:
            continue
        cursor = conn.execute("SELECT * FROM links LIMIT 0")
        names = [d[0] for d in cursor.description]
        leads = [dict(zip(names, r)) for r in leads]
        chosen = mf.judge_groups(conn, leads, owner, counts, pending)
        rejected = {(r[0], r[1]) for r in conn.execute(
            "SELECT to_object, link_type FROM link_rows WHERE from_object = ? AND "
            "tier = 'contradicted'", [oid])}
        for lead in chosen:
            if (lead["to_object"], lead["link_type"]) in rejected:
                counts["rejected"] += 1
                continue
            yield mf.firm_row(lead)
    yield from mf.resolved_rows(conn, pending, counts, ())


class Counting:
    """Counts the statements a connection runs."""

    def __init__(self, conn):
        self.statements = []
        conn.set_trace_callback(self.statements.append)

    @property
    def reads(self):
        return [s for s in self.statements if s.lstrip().upper().startswith(("SELECT", "WITH"))]


class UniqueMentions(unittest.TestCase):
    def run_both(self, messages, window):
        conn = mention_store(messages)
        types = ["PurchaseOrder", "SalesOrder"]
        old = Counter()
        expected = list(per_object_firm_links(conn, types, old))
        saved = mf.WINDOW
        mf.WINDOW = window
        try:
            counting = Counting(conn)
            new = Counter()
            rows = list(mf.firm_links(conn, types, new))
        finally:
            mf.WINDOW = saved
        return expected, old, rows, new, counting

    def test_the_rows_and_counts_are_the_per_object_steps_at_1_10_and_60_messages(self):
        for messages in (1, 10, 60):
            for window in (1, 7, 500):
                with self.subTest(messages=messages, window=window):
                    expected, old, rows, new, _c = self.run_both(messages, window)
                    key = lambda r: r["link_id"]
                    self.assertEqual(sorted(rows, key=key), sorted(expected, key=key))
                    self.assertEqual(new, old)
        self.assertTrue(expected)   # the 60-message case produced firm rows

    def test_twin_candidates_are_one_record_only_when_a_confirming_link_joins_them(self):
        _e, _o, rows, counts, _c = self.run_both(12, 500)
        written = {r["to_object"] for r in rows}
        self.assertTrue({"PurchaseOrder:fm:1", "PurchaseOrder:fm:7",       # verified a -> fm
                         "PurchaseOrder:fm:2", "PurchaseOrder:fm:8"} <= written)   # reviewed
        for m in (3, 4, 9, 10):   # a lead join, a superseded join: several records
            self.assertNotIn(f"PurchaseOrder:fm:{m}", written)
            self.assertNotIn(f"PurchaseOrder:a:{m}", written)
        self.assertGreater(counts["several_records"], 0)

    def test_a_rejected_pair_is_not_written_and_is_counted(self):
        _e, _o, rows, counts, _c = self.run_both(10, 500)
        self.assertGreater(counts["rejected"], 0)
        written = {r["to_object"] for r in rows}
        self.assertNotIn("PurchaseOrder:a:0", written)   # message 0 was rejected
        self.assertIn("PurchaseOrder:a:1", written)

    def test_the_statements_follow_the_batches_not_the_messages(self):
        _e, _o, _rows, _n, counting = self.run_both(60, 25)
        # 1 enumeration + (leads + rejections) per window of 25 objects (3 windows)
        # + one read of the candidates' same_record joins per window: never one per record
        link_reads = [s for s in counting.reads if "from_object IN" in s]
        self.assertEqual(len(link_reads), 3 * 3)
        self.assertFalse([s for s in counting.reads if "from_object = ?" in s])


class RejectedPairs(unittest.TestCase):
    def test_pairs_are_grouped_by_source_in_batches_and_unknown_sources_are_absent(self):
        conn = mention_store(60)
        counting = Counting(conn)
        got = SR.rejected_pairs(conn, [f"Email:m:{m:03d}" for m in range(60)] + ["Email:none"])
        self.assertEqual(sorted(got), [f"Email:m:{m:03d}" for m in range(0, 60, 4)])
        self.assertEqual(got["Email:m:000"], {("PurchaseOrder:a:0", "Email.mentions_x")})
        self.assertEqual(len(counting.reads), 1)          # 61 sources: one statement
        SR.CHUNK, saved = 20, SR.CHUNK
        try:
            counting.statements.clear()
            SR.rejected_pairs(conn, [f"Email:m:{m:03d}" for m in range(60)])
            self.assertEqual(len(counting.reads), 3)
        finally:
            SR.CHUNK = saved

    def test_no_sources_runs_no_statement(self):
        conn = mention_store(1)
        counting = Counting(conn)
        self.assertEqual(SR.rejected_pairs(conn, []), {})
        self.assertEqual(counting.reads, [])


def named_store(messages):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE link_rows (link_id, link_type, from_object, to_object, tier, "
                 "markings, superseded_at)")
    conn.execute("CREATE TABLE object_versions (object_id, l1_handle, markings, recorded_at, "
                 "superseded_at)")
    n = 0

    def link(frm, to, tier, link_type="x"):
        nonlocal n
        n += 1
        conn.execute("INSERT INTO link_rows VALUES (?,?,?,?,?,?,NULL)",
                     (f"l{n:05d}", link_type, frm, to, tier, "[]"))

    for m in range(messages):
        msg = f"Email:m:{m:03d}"
        if m % 3 == 0:     # names an order whose confirmed party is a supplier
            link(msg, f"PurchaseOrder:p:{m}", "lead")
            link(f"PurchaseOrder:p:{m}", f"Supplier:s:{m % 5}", "corroborated")
            conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,NULL)",
                         (f"PurchaseOrder:p:{m}", f"h-po-{m}", "[]", "2026-10-01"))
        elif m % 3 == 1:   # names a customer straight
            link(msg, f"Customer:c:{m % 4}", "lead")
            conn.execute("INSERT OR IGNORE INTO object_versions VALUES (?,?,?,?,NULL)",
                         (f"Customer:c:{m % 4}", f"h-c-{m % 4}", "[]", "2026-10-01"))
        else:              # names an order whose party is only a lead: no party
            link(msg, f"SalesOrder:o:{m}", "lead")
            link(f"SalesOrder:o:{m}", f"Customer:c:{m % 4}", "lead")
            conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,NULL)",
                         (f"SalesOrder:o:{m}", f"h-so-{m}", "[]", "2026-10-01"))
        if m % 5 == 0:     # a link of the step's own prefix is never read as naming
            link(msg, "Supplier:s:own", "lead")
            conn.execute("UPDATE link_rows SET link_id = ? WHERE link_id = ?",
                         (sp.PREFIX + f"x{m}", f"l{n:05d}"))
    conn.commit()
    return conn


def per_message_named_parties(conn, message, parties):
    """``named_parties`` before NW-13: one read per message and one per named record."""
    found = {}
    for (target,) in conn.execute(
            "SELECT to_object FROM link_rows WHERE from_object = ? AND tier <> 'contradicted' "
            "AND superseded_at IS NULL AND NOT (link_id >= ? AND link_id < ?)",
            [message, sp.PREFIX, sp._PREFIX_END]):
        kind = target.split(":", 1)[0]
        if kind in sp.PARTY_TYPES:
            found.setdefault(target, target)
        elif kind in sp.NAMED_TYPES:
            for (party,) in conn.execute(
                    "SELECT to_object FROM link_rows WHERE from_object = ? AND tier IN "
                    f"({','.join('?' * len(sp.NAMED_PARTY_TIERS))}) AND superseded_at IS NULL",
                    [target, *sp.NAMED_PARTY_TIERS]):
                if party.split(":", 1)[0] in sp.PARTY_TYPES:
                    found.setdefault(party, target)
    versions = sp._versions(conn, set(found.values()))
    out = []
    for party, record in sorted(found.items()):
        version = versions.get(record)
        if version is None:
            continue
        sp._component(conn, party, parties.roots, parties.twins)
        out.append(sp.Holding(party, party.split(":", 1)[0], sp.NAMED, "", None,
                              version["l1_handle"], {"l1_handle": version["l1_handle"]},
                              version["markings"], version["recorded_at"]))
    return out


class NamedParties(unittest.TestCase):
    def test_the_batch_names_what_each_message_alone_names_at_1_10_and_60_messages(self):
        for messages in (1, 10, 60):
            with self.subTest(messages=messages):
                conn = named_store(messages)
                ids = [f"Email:m:{m:03d}" for m in range(messages)]
                old = SimpleNamespace(roots={}, twins={})
                new = SimpleNamespace(roots={}, twins={})
                expected = {m: per_message_named_parties(conn, m, old) for m in ids}
                got = sp.named_parties_batch(conn, ids, new)
                self.assertEqual({m: h for m, h in expected.items() if h}, got)
                for m in ids:   # the one-message form is the same function
                    self.assertEqual(sp.named_parties(conn, m, SimpleNamespace(roots={}, twins={})),
                                     expected[m])
                self.assertEqual((old.roots, old.twins), (new.roots, new.twins))
        self.assertTrue(any(got.values()))

    def test_statements_do_not_grow_with_the_messages(self):
        counts = {}
        for messages in (10, 60):
            conn = named_store(messages)
            counting = Counting(conn)
            sp.named_parties_batch(conn, [f"Email:m:{m:03d}" for m in range(messages)],
                                   SimpleNamespace(roots={}, twins={}))
            link_reads = [s for s in counting.reads if "FROM link_rows" in s
                          and "same_record_as" not in s and "UNION" not in s]
            counts[messages] = len(link_reads)
        self.assertEqual(counts[10], counts[60])
        self.assertLessEqual(counts[60], 2)

    def test_the_steps_own_links_never_name_a_party(self):
        conn = named_store(6)
        got = sp.named_parties_batch(conn, ["Email:m:000"], SimpleNamespace(roots={}, twins={}))
        self.assertNotIn("Supplier:s:own", {h.party for h in got.get("Email:m:000", [])})


if __name__ == "__main__":
    unittest.main()
