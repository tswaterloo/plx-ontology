"""Identifier mention leads raised to firm links (``ontology.mention_firm``).

Evidence label: ``isolated-fixture`` (made-up lead rows; the identifier grammar and the
judging rules; no store or source read).

* An anchor identifier written as normalized, holding a letter and enough digits, whose
  own-type candidates are one record, is firm; its candidates of other types are not.
* An assembled number, a bare number, a short code or several unrelated records stay leads.
* Judged by worker processes (``ontology.mention_firm_parallel``), the step writes the
  serial step's rows, in its order.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from collections import Counter

from ontology import mention_firm as mf

OWNER = {"petra_po": "PurchaseOrder", "so_number": "SalesOrder"}


def lead(n, to, kind, value, span, group):
    reason = [{"feature": "identifier_mention", "agrees": True, "identifier_kind": kind,
               "normalized": value, "span": span, "mention_group": group}]
    return {"link_id": f"mention:{n}", "link_type": "Email.mentions_x", "from_object": "Email:m:1",
            "to_object": to, "reason": json.dumps(reason), "evidence_handles": "[]",
            "markings": "[]", "recorded_at": "2026-10-01T00:00:00Z"}


class MentionFirm(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE link_rows (from_object, to_object, link_type, tier, "
                          "superseded_at)")
        self.conn.execute("INSERT INTO link_rows VALUES ('PurchaseOrder:a:1', "
                          "'PurchaseOrder:b:1', 'PurchaseOrder.same_record_as', 'verified', NULL)")

    def judge(self, leads):
        counts = Counter()
        return [l["link_id"] for l in mf.judge_groups(self.conn, leads, OWNER, counts)], counts

    def test_exact_unique_anchor_is_firm_on_its_own_type_only(self):
        chosen, _ = self.judge([
            lead(1, "PurchaseOrder:a:1", "petra_po", "PO907711", [4, 12], "g1"),
            lead(2, "PurchaseOrder:b:1", "petra_po", "PO907711", [4, 12], "g1"),
            lead(3, "SupplierInvoice:c:1", "petra_po", "PO907711", [4, 12], "g1")])
        self.assertEqual(chosen, ["mention:1", "mention:2"])

    def test_assembled_short_or_ambiguous_numbers_stay_leads(self):
        chosen, counts = self.judge([
            lead(1, "PurchaseOrder:a:1", "petra_po", "PO907711", [0, 9], "g1"),   # "PO 907711"
            lead(2, "SalesOrder:a:2", "so_number", "OR12", [0, 4], "g2"),
            lead(3, "SalesOrder:a:3", "so_number", "OR5550001", [0, 9], "g3"),
            lead(4, "SalesOrder:a:4", "so_number", "OR5550001", [0, 9], "g3"),
            lead(5, "SalesOrder:a:5", "customer_po", "PX88123", [0, 7], "g4")])
        self.assertEqual(chosen, [])
        self.assertEqual((counts["not_exact"], counts["several_records"], counts["not_anchor"]),
                         (2, 1, 1))


class ParallelEqualsSerial(unittest.TestCase):
    """Made-up leads of many messages in a file store; workers read it read-only."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "store.sqlite"
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE links (link_id, link_type, from_object, to_object, tier, "
                     "reason, evidence_handles, markings, recorded_at, superseded_at)")
        conn.execute("CREATE VIEW link_rows AS SELECT * FROM links")
        for m in range(40):
            for n, (to, value) in enumerate([(f"PurchaseOrder:a:{m}", f"PO9{m:05d}"),
                                             (f"SalesOrder:a:{m}", f"OR77{m:04d}")]):
                row = lead(f"{m:03d}-{n}", to, "petra_po" if n == 0 else "so_number", value,
                           [0, len(value) - (m % 3 == 0)], f"g{n}")
                conn.execute("INSERT INTO links VALUES (?, ?, ?, ?, 'lead', ?, ?, ?, ?, NULL)",
                             (row["link_id"], row["link_type"], f"Email:m:{m:03d}", to,
                              row["reason"], row["evidence_handles"], row["markings"],
                              row["recorded_at"]))
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_workers_write_the_serial_rows(self):
        from ontology import mention_firm_parallel as mp
        types = ["PurchaseOrder", "SalesOrder"]
        serial = Counter()
        expected = list(mf.firm_links(self.conn, types, serial))
        self.assertTrue(expected)

        class Builder:
            connection, temp = self.conn, str(self.path)

        old = (mp.CHUNK, mp.MIN_OBJECTS)
        mp.CHUNK, mp.MIN_OBJECTS = 7, 0
        try:
            counts = Counter()
            rows = mp.parallel_firm_links(Builder(), types, counts, processes=3)
        finally:
            mp.CHUNK, mp.MIN_OBJECTS = old
        self.assertEqual(mp.LAST.get("mode"), "parallel", mp.LAST)
        self.assertEqual(rows, expected)
        self.assertEqual(counts, serial)


if __name__ == "__main__":
    unittest.main()
