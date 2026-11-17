"""Identifier mention leads raised to firm links (``ontology.mention_firm``).

Evidence label: ``isolated-fixture`` (made-up lead rows; the identifier grammar and the
judging rules; no store or source read).

* An anchor identifier written as normalized, holding a letter and enough digits, whose
  own-type candidates are one record, is firm; its candidates of other types are not.
* An assembled number, a bare number, a short code or several unrelated records stay leads.
* A receipt lab id (``26161-1``: no letter, the kind's declared ``firm_mention.shape``) is
  firm on the receipt line it names (a declared ``firm_mention.types`` own type), each own
  type judged on its own; a letterless value of a kind declaring no shape, an off-shape lab
  id, or an unknown kind stays a lead.
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


UNCORROBORATED = {"feature": "uncorroborated", "agrees": None, "detail": "x"}


def lead(n, to, kind, value, span, group, why_not=(UNCORROBORATED,)):
    reason = [{"feature": "identifier_mention", "agrees": True, "identifier_kind": kind,
               "normalized": value, "span": span, "mention_group": group}]
    return {"link_id": f"mention:{n}", "link_type": "Email.mentions_x", "from_object": "Email:m:1",
            "to_object": to, "reason": json.dumps(reason), "evidence_handles": "[]",
            "why_not": why_not if isinstance(why_not, str) else json.dumps(list(why_not)),
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

    def test_a_list_file_never_gets_a_firm_link(self):
        # a tracker naming five purchase orders, each one record: none is firm (each a
        # lead), while the same file's one sales order still is
        pos = [lead(n, f"PurchaseOrder:a:{n}", "petra_po", f"PO90771{n}", [40 * n, 40 * n + 8],
                    f"g{n}") for n in range(5)]
        so = lead(9, "SalesOrder:a:9", "so_number", "OR5550001", [900, 909], "g9")
        chosen, counts = self.judge(pos + [so])
        self.assertEqual(chosen, ["mention:9"])
        self.assertEqual(counts["list_file"], 5)
        # four: below the measured threshold, each still firm
        chosen, counts = self.judge(pos[:4])
        self.assertEqual(sorted(chosen), [f"mention:{n}" for n in range(4)])
        self.assertEqual(counts["list_file"], 0)
        # one resolved number, but the lead says its document's text lists many
        listed = dict(pos[0], why_not=json.dumps([{"feature": "list_file", "agrees": False,
                                                   "records": 57}]))
        chosen, counts = self.judge([listed])
        self.assertEqual((chosen, counts["list_file"]), ([], 1))

    def field_lead(self, n, value, field, source="Email:m:1", quoted=False):
        row = lead(n, f"PurchaseOrder:a:{value}", "petra_po", value, [0, len(value)], f"g{n}")
        reason = json.loads(row["reason"])
        reason[0]["citations"] = [{"l1_handle": "h://doc@1", "field_path": field}]
        why_not = [{"feature": "uncorroborated"}] + ([{"feature": "quoted_history"}]
                                                     if quoted else [])
        return dict(row, from_object=source, reason=json.dumps(reason),
                    why_not=json.dumps(why_not))

    def test_a_reply_keeps_its_own_po_when_its_quoted_history_names_five(self):
        # mail, Teams and meetings alike: quoted text is someone else's, not a list
        for source, own_field in (("Email:m:1", "row.subject"), ("Email:m:2", "row.body_text"),
                                  ("TeamsChatMessage:t:1", "row.body"),
                                  ("Meeting:x:1", "row.transcript")):
            rows = [self.field_lead(0, "PO1000200", own_field, source)]
            rows += [self.field_lead(n, f"PO100030{n}", "row.body_text", source, quoted=True)
                     for n in range(1, 6)]
            chosen, counts = self.judge(rows)
            self.assertIn("mention:0", chosen, source)
            self.assertEqual(counts["list_file"], 0, source)

    def test_a_po_named_file_keeps_its_name_number_when_its_text_lists_five(self):
        rows = [self.field_lead(0, "PO1000200", "row.name", "Attachment:a:1")]
        rows += [self.field_lead(n, f"PO100030{n}", "row.content_text", "Attachment:a:1")
                 for n in range(1, 6)]
        chosen, counts = self.judge(rows)
        self.assertEqual(chosen, ["mention:0"])
        self.assertEqual(counts["list_file"], 5)
        # the name's number is never vetoed, even when its lead were written list_file
        said = dict(rows[0], why_not=json.dumps([{"feature": "list_file", "records": 9}]))
        chosen, _ = self.judge([said])
        self.assertEqual(chosen, ["mention:0"])

    def test_numbers_in_other_fields_do_not_add_up_to_a_list(self):
        # three in the body, three in an attachment's name field: no field names five
        rows = [self.field_lead(n, f"PO100030{n}", "row.body_text") for n in range(3)]
        rows += [dict(self.field_lead(n, f"PO100030{n}", "row.content_text"))
                 for n in range(3, 6)]
        chosen, counts = self.judge(rows)
        self.assertEqual(len(chosen), 6)
        self.assertEqual(counts["list_file"], 0)

    def test_the_same_number_twice_is_one_record_named(self):
        # an order's own PDF names its number on every page: one value, not a list
        rows = [lead(n, "PurchaseOrder:a:1", "petra_po", "PO907711", [100 * n, 100 * n + 8],
                     f"g{n}") for n in range(8)]
        chosen, counts = self.judge(rows)
        self.assertEqual(len(chosen), 8)
        self.assertEqual(counts["list_file"], 0)


class ReceiptLabIds(unittest.TestCase):
    """The schema's own owner map: a lab id names the receipt line it was given to."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE link_rows (from_object, to_object, link_type, tier, "
                          "superseded_at)")
        self.owner = mf.anchor_types(["Receipt", "ReceiptLine", "SalesOrder", "PurchaseOrder"])

    def judge(self, leads):
        counts = Counter()
        return [l["link_id"] for l in mf.judge_groups(self.conn, leads, self.owner, counts)], \
            counts

    def test_the_owner_map_holds_the_line_beside_the_receipt(self):
        self.assertEqual(self.owner["receipt_lab_id"], ("Receipt", "ReceiptLine"))
        self.assertEqual(self.owner["petra_po"], ("PurchaseOrder",))
        self.assertNotIn("receipt_lab_id", mf.anchor_types(["SalesOrder"]))

    def test_a_lab_id_names_its_line_and_each_own_type_on_its_own(self):
        chosen, counts = self.judge([
            lead(1, "ReceiptLine:fm:1", "receipt_lab_id", "22082-1", [9, 16], "g1"),
            lead(2, "Receipt:app:1", "receipt_lab_id", "22082-1", [9, 16], "g1"),
            lead(3, "Lot:fm:1", "receipt_lab_id", "22082-1", [9, 16], "g1"),
            lead(4, "ReceiptLine:fm:2", "receipt_lab_id", "RP0008270", [0, 9], "g2")])
        self.assertEqual(chosen, ["mention:1", "mention:2", "mention:4"])
        self.assertEqual(counts["firm"], 3)

    def test_unshaped_ambiguous_or_unknown_values_stay_leads(self):
        chosen, counts = self.judge([
            # assembled by a label: the span is longer than the value
            lead(1, "ReceiptLine:fm:1", "receipt_lab_id", "22082-1", [0, 8], "g1"),
            # two lines that are no twins
            lead(2, "ReceiptLine:fm:2", "receipt_lab_id", "22083-4", [0, 7], "g2"),
            lead(3, "ReceiptLine:fm:3", "receipt_lab_id", "22083-4", [0, 7], "g2"),
            # a bare number of a kind that declares no shape
            lead(4, "SalesOrder:a:1", "so_number", "5550001", [0, 7], "g3"),
            # off the lab id shape (six digits before the dash) and too few digits
            lead(5, "ReceiptLine:fm:4", "receipt_lab_id", "220821-1", [0, 8], "g4"),
            lead(6, "ReceiptLine:fm:5", "receipt_lab_id", "22-1", [0, 4], "g5")])
        self.assertEqual(chosen, [])
        self.assertEqual((counts["not_exact"], counts["several_records"]), (4, 1))
        # a kind the schema does not declare is never firm, written how it may be
        self.assertFalse(mf.written_exactly({"identifier_kind": "no_such_kind",
                                             "normalized": "22082-1", "span": [0, 7]}))

    def test_a_malformed_firm_mention_is_refused(self):
        from ontology import identifiers as ids
        data = ids.schema()
        base = data["kinds"]["receipt_lab_id"]["firm_mention"]
        for bad in ({**base, "shape": "(["}, {**base, "types": "ReceiptLine"},
                    {"why": base["why"], "upstream": base["upstream"]},
                    {**base, "upstream": " "}, {**base, "extra": 1}):
            broken = {**data, "kinds": {**data["kinds"], "receipt_lab_id": {
                **data["kinds"]["receipt_lab_id"], "firm_mention": bad}}}
            with self.subTest(bad=sorted(bad)), self.assertRaises(ids.IdentifierSchemaError):
                ids._Compiled(broken)


    def test_a_number_only_in_quoted_reply_history_stays_a_lead(self):
        """CC-NY-10: quoted history was written by someone else (``ontology.mentions``
        marks it ``quoted_history``): a number seen only there is a lead; the same number
        in the message's own text is its own mention group and is firm. A secondary
        reading, a number split from its label or a why_not that cannot be read is never
        firm either."""
        quoted = (UNCORROBORATED, {"feature": "quoted_history", "agrees": False})
        chosen, counts = self.judge([
            lead(1, "PurchaseOrder:a:1", "petra_po", "PO907711", [90, 98], "g1", quoted),
            lead(2, "PurchaseOrder:b:1", "petra_po", "PO907711", [90, 98], "g1", quoted)])
        self.assertEqual((chosen, counts["veto_quoted_history"]), ([], 1))
        chosen, _ = self.judge([
            lead(1, "PurchaseOrder:a:1", "petra_po", "PO907711", [90, 98], "g1", quoted),
            lead(3, "PurchaseOrder:a:1", "petra_po", "PO907711", [4, 12], "g2")])
        self.assertEqual(chosen, ["mention:3"])
        # one candidate of the group vetoed vetoes the group
        chosen, _ = self.judge([
            lead(1, "PurchaseOrder:a:1", "petra_po", "PO907711", [4, 12], "g1"),
            lead(2, "PurchaseOrder:b:1", "petra_po", "PO907711", [4, 12], "g1", quoted)])
        self.assertEqual(chosen, [])
        for why_not, veto in (
                ([{"feature": "label_kind_conflict", "agrees": False}], "label_kind_conflict"),
                ([{"feature": "split_at_gap"}], "split_at_gap"),
                ("not json", "unreadable"), ('{"feature": "quoted_history"}', "unreadable"),
                ('["quoted_history"]', "unreadable")):
            chosen, counts = self.judge([lead(1, "PurchaseOrder:a:1", "petra_po", "PO907711",
                                              [4, 12], "g1", why_not)])
            self.assertEqual((chosen, counts[f"veto_{veto}"]), ([], 1), why_not)
        # a row without the column (an unknown shape) is never firm
        row = lead(1, "PurchaseOrder:a:1", "petra_po", "PO907711", [4, 12], "g1")
        del row["why_not"]
        self.assertEqual(self.judge([row])[0], [])
        # an empty or null why_not states no veto
        for why_not in ("[]", None):
            row = lead(1, "PurchaseOrder:a:1", "petra_po", "PO907711", [4, 12], "g1")
            row["why_not"] = why_not
            self.assertEqual(self.judge([row])[0], ["mention:1"])

    def test_cardinality_one_ten_sixty_messages_quoted_never_firm(self):
        quoted = (UNCORROBORATED, {"feature": "quoted_history", "agrees": False})
        for n in (1, 10, 60):
            # one judge per message (judge_groups judges one object's leads): n numbers in
            # one message's text would be a list file (``mentions.LIST_FILE_MIN``), not n
            # messages
            chosen, counts = [], Counter()
            for m in range(n):
                got, found = self.judge([
                    lead(f"o{m}", f"PurchaseOrder:o:{m}", "petra_po", f"PO8{m:05d}",
                         [0, 8], f"own{m}"),
                    lead(f"q{m}", f"PurchaseOrder:q:{m}", "petra_po", f"PO7{m:05d}",
                         [50, 58], f"quoted{m}", quoted)])
                chosen += got
                counts += found
            self.assertEqual(sorted(chosen), sorted(f"mention:o{m}" for m in range(n)))
            self.assertEqual((counts["firm"], counts["veto_quoted_history"]), (n, n))


class ParallelEqualsSerial(unittest.TestCase):
    """Made-up leads of many messages in a file store; workers read it read-only."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "store.sqlite"
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE links (link_id, link_type, from_object, to_object, tier, "
                     "reason, evidence_handles, why_not, markings, recorded_at, "
                     "superseded_at)")
        conn.execute("CREATE VIEW link_rows AS SELECT * FROM links")
        for m in range(40):
            for n, (to, value) in enumerate([(f"PurchaseOrder:a:{m}", f"PO9{m:05d}"),
                                             (f"SalesOrder:a:{m}", f"OR77{m:04d}")]):
                row = lead(f"{m:03d}-{n}", to, "petra_po" if n == 0 else "so_number", value,
                           [0, len(value) - (m % 3 == 0)], f"g{n}")
                conn.execute("INSERT INTO links VALUES (?, ?, ?, ?, 'lead', ?, ?, ?, ?, ?, NULL)",
                             (row["link_id"], row["link_type"], f"Email:m:{m:03d}", to,
                              row["reason"], row["evidence_handles"], row["why_not"],
                              row["markings"], row["recorded_at"]))
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
