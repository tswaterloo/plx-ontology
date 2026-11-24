"""The record page's data conflicts served from stored items (speed specs 8.3 ``conflicts``, the
default posted 11-23 7:21 PM): ``history_batch.conflicts_of`` stores the owner's raw conflicts per
value and the receipts dated before the order date; ``record_history.conflict_items`` classes each
item by what a reader must hold to open its handle; ``served.record_conflicts`` runs
``tool_conflicts`` over the values the reader's door let through. Every permission edge has a
DENIED case: a value the reader may not open is as absent as in ``backend._conflicts`` (one side
hidden: no card, and nothing says a hidden value exists), an item gate they fail drops its value,
an item emptied by the people exclusion is skipped, a record stored without conflicts says
``not_built`` and never an empty list."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import psycopg

from operating.pg import record_history as rh
from operator_api import served
from operator_api.tests import test_served_history as tsh
from operator_api.tests import test_served_read as tsr

FULL, OPS, NOBODY = tsr.FULL, tsr.OPS, tsr.NOBODY


def value(handle, text, rank=1, markings=None):
    return {"property": "order_date", "value_type": "text", "value": text, "authority_rank": rank,
            "citation": {"l1_handle": handle, "field_path": "$.x"}, "markings": markings or []}


def head(built=True, subject="PO 1"):
    return {"built": True, "subject": subject} if built else {"built": False}


def item(c, j, handle, text, rank=1, cites=(), kind=None):
    return ("conflict", {"c": c, "j": j, "kind": kind, "subject": "PurchaseOrder.order_date",
                         "tier": None, "as_of": None, "v": {k: v for k, v in value(
                             handle, text, rank).items() if k != "markings"},
                         "need": {"cites": list(cites)}})


class RecordConflicts(unittest.TestCase):
    """``served.record_conflicts`` alone: what a reader's received items become."""

    def test_two_values_held_make_one_card_with_each_source(self):
        got = served.record_conflicts((head(), [
            item(0, 0, "api://sage.po_header/1@a", "2026-01-02"),
            item(0, 1, "api://public.po/1@b", "2026-02-03", rank=2)]), set())
        self.assertEqual(got["status"], "ok")
        self.assertEqual(len(got["items"]), 1)
        card = got["items"][0]
        self.assertEqual(card["subject"], "PO 1")
        self.assertEqual(sorted(v["value"] for v in card["values"]), ["2026-01-02", "2026-02-03"])
        self.assertTrue(all(v["source"]["handle"] for v in card["values"]))

    def test_one_value_left_is_no_card_and_says_nothing_of_the_other(self):
        got = served.record_conflicts((head(), [
            item(0, 0, "api://sage.po_header/1@a", "2026-01-02")]), set())
        self.assertEqual(got, {"status": "ok", "items": []})

    def test_a_value_resting_on_a_message_the_reader_may_not_open_is_dropped(self):
        rows = [item(0, 0, "api://sage.po_header/1@a", "2026-01-02"),
                item(0, 1, "graph-mail://m/1", "2026-02-03", cites=["graph-mail://m/1"])]
        denied = served.record_conflicts((head(), rows), set())      # the item gate refused
        self.assertEqual(denied["items"], [])
        allowed = served.record_conflicts((head(), rows), {"graph-mail://m/1"})
        self.assertEqual(len(allowed["items"]), 1)

    def test_an_item_emptied_by_the_people_exclusion_is_skipped(self):
        rows = [item(0, 0, "api://sage.po_header/1@a", "2026-01-02"), ("conflict", None)]
        self.assertEqual(served.record_conflicts((head(), rows), set())["items"], [])

    def test_not_built_is_said_and_never_an_empty_list(self):
        self.assertEqual(served.record_conflicts((head(False), []), set())["status"], "not_built")
        self.assertEqual(served.record_conflicts((None, []), set())["status"], "not_built")

    def test_receipts_before_the_order_date_make_the_card_with_every_receipt_counted(self):
        base = {"ordered": "2026-03-01", "order_handle": "api://sage.po_header/1@a"}
        rows = [("received", {**base, "when": "2026-02-01", "handle": "api://sage.rc/1@a",
                              "need": {"cites": []}}),
                ("received", {**base, "when": "2026-02-10", "handle": "api://sage.rc/2@a",
                              "need": {"cites": []}})]
        got = served.record_conflicts((head(), rows), set())
        self.assertEqual(len(got["items"]), 1)
        values = got["items"][0]["values"]
        self.assertEqual([v["value"] for v in values], ["2026-03-01", "2026-02-01"])
        self.assertIn("2 receipts", values[0]["why"])
        # the earliest receipt is the one the reader may not open: no card (as the old page)
        rows[0] = ("received", {**base, "when": "2026-02-01", "handle": "graph-mail://m/1",
                                "need": {"cites": ["graph-mail://m/1"]}})
        got = served.record_conflicts((head(), rows), set())
        self.assertEqual([v["value"] for v in got["items"][0]["values"]],
                         ["2026-03-01", "2026-02-10"])      # only what they may open is compared
        self.assertIn("a receipt of this order", got["items"][0]["values"][0]["why"])


class StoredConflicts(tsr.Fixture, unittest.TestCase):
    """The items through the real loader and the real door: classes decide, per reader."""
    decided = tsh.ServedHistory.decided
    new = tsh.ServedHistory.new
    backend = tsh.ServedHistory.backend

    def setUp(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            rows = c.execute(
                "SELECT l1_handle FROM ontology.object_versions WHERE superseded_at IS NULL AND "
                "l1_handle IS NOT NULL ORDER BY ver LIMIT 2").fetchall()
        self.h_api, self.h_bc = rows[0][0], rows[1][0]
        # a unit FULL holds and OPS does not, and one both hold (concrete keys the store has)
        full, ops = self.door(FULL).grant.keys, self.door(OPS).grant.keys
        self.unit_both = sorted(set(full) & set(ops))[0]
        self.unit_full = sorted(set(full) - set(ops))[0]

    def block(self):
        v1 = value(self.h_api, "2026-01-02", rank=1)
        v2 = value(self.h_bc, "2026-02-03", rank=2)
        return {"label": "PO 1", "received": None, "raw": [
            {"kind": "value_disagreement", "subject": "PurchaseOrder.order_date", "tier": None,
             "from": "plx.object", "values": [
                 {"value": v1, "as_of": None, "unit": self.unit_both},
                 {"value": v2, "as_of": None, "unit": self.unit_full}]}]}

    def stored(self, with_conflicts=True):
        tools = self.base()
        key = self.keys_for_ops()[0]
        line = {**tsh.line_of(tools, key), **({"conflicts": self.block()} if with_conflicts else {})}
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(line, default=str) + "\n")
        self.addCleanup(os.unlink, fh.name)
        got = rh.load(self.dsn, [fh.name])
        self.assertEqual((got["failed"], got["refused"]), (0, {}))
        self.decided()
        return key

    def keys_for_ops(self):
        """Records a restricted reader may open (the rest answer them 404)."""
        from operator_api.errors import UserFacing
        out = []
        for key in self.keys():
            try:
                self.read(self.door(OPS), key)
                out.append(key)
            except UserFacing:
                pass
        self.assertTrue(out)
        return out

    def conflicts(self, actor, key):
        body, _ = served.read(self.backend(self.door(actor)), object(), "record", key)
        return json.loads(body)["conflicts"]

    def test_each_reader_receives_only_the_values_whose_unit_they_hold(self):
        key = self.stored()
        full = self.conflicts(FULL, key)
        self.assertEqual(full["status"], "ok", self.truth(
            "SELECT i.section, i.seq, i.access_class, c.audience_keys, c.blocked, "
            "i.payload->'need' FROM {biz}.page_items i LEFT JOIN {biz}.access_classes c ON "
            "c.id = i.access_class WHERE i.page_kind = 'history' AND i.page_key = %s AND "
            "i.section IN ('conflict', 'history_head') ORDER BY 1, 2", (key,)))
        self.assertEqual(len(full["items"]), 1, (full, self.truth(
            "SELECT i.section, i.seq, i.access_class, c.audience_keys, c.blocked, "
            "i.payload->'need' FROM {biz}.page_items i LEFT JOIN {biz}.access_classes c ON "
            "c.id = i.access_class WHERE i.page_kind = 'history' AND i.page_key = %s AND "
            "i.section IN ('conflict', 'history_head') ORDER BY 1, 2", (key,))))
        self.assertEqual(len(full["items"][0]["values"]), 2)
        # OPS holds api:* but not bc:*: the bc value is no row for them, one value left: no card
        self.assertEqual(self.conflicts(OPS, key), {"status": "ok", "items": []})
        # no grant: the record is not theirs at all (404, as an unknown record)
        from operator_api.errors import UserFacing
        with self.assertRaises(UserFacing):
            self.conflicts(NOBODY, key)

    def test_the_stored_class_of_each_value_names_its_unit_and_markings(self):
        key = self.stored()
        rows = self.truth(
            "SELECT i.seq, c.audience_keys, c.rec_marks FROM {biz}.page_items i JOIN "
            "{biz}.access_classes c ON c.id = i.access_class WHERE i.page_kind = 'history' AND "
            "i.page_key = %s AND i.section = 'conflict' ORDER BY i.seq", (key,))
        self.assertEqual(len(rows), 2)
        self.assertIn(self.unit_both, rows[0][1])
        self.assertNotIn(self.unit_full, rows[0][1])
        self.assertIn(self.unit_full, rows[1][1])

    def test_the_history_tab_is_unchanged_by_the_conflict_items(self):
        key = self.stored(with_conflicts=False)
        before = {a: self.new(self.door(a), key) for a in (FULL, OPS)}
        self.assertEqual(self.conflicts(FULL, key)["status"], "not_built")   # never "none"
        self.stored()
        after = {a: self.new(self.door(a), key) for a in (FULL, OPS)}
        self.assertEqual(after, before)


class HistoryFreshness(StoredConflicts):
    """An audience changed IN PLACE (no new version) and a source that moved: the stored tab is
    found, taken out for a narrowed reader (fail closed) or shown with its age."""
    block = StoredConflicts.block

    def aud_moved(self, key):
        content, aud = rh.moved_parts("biz")
        with psycopg.connect(self.dsn, autocommit=True) as c:
            return c.execute(f"SELECT {aud} FROM biz.page_items h WHERE h.page_kind = 'history' "
                             "AND h.section = 'history_head' AND h.page_key = %s", (key,)
                             ).fetchone()[0]

    def marker(self, **lines):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(lines) + "\n")
        self.addCleanup(os.unlink, fh.name)
        return rh.load(self.dsn, [fh.name])

    def narrow(self, key):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            was = c.execute("SELECT audience_key FROM ontology.objects WHERE object_id = %s",
                            (key,)).fetchone()[0]
            c.execute("UPDATE ontology.objects SET audience_key = 'owner:only' WHERE object_id "
                      "= %s", (key,))

        def undo():
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute("UPDATE ontology.objects SET audience_key = %s WHERE object_id = %s",
                          (was, key))
        self.addCleanup(undo)

    def test_an_audience_narrowed_in_place_is_found_and_the_tab_taken_out(self):
        from operator_api.served import Unavailable
        key = self.stored()
        self.assertFalse(self.aud_moved(key))          # stamped when stored: not moved
        self.assertEqual(self.new(self.door(OPS), key)[0] != [], True)   # a restricted reader reads it
        self.narrow(key)                               # no version, no link row: only the audience
        self.assertTrue(self.aud_moved(key))
        got = self.marker(reset=[key])                 # the resident's sweep says so; the box takes it out
        self.assertEqual(got["reset"], 1)
        for actor in (FULL, OPS):                      # DENIED: nobody reads the old class's items
            with self.assertRaises(Unavailable):
                served.history(self.backend(self.door(actor)), object(), key)
        self.assertEqual(self.conflicts(FULL, key)["status"], "not_built")
        self.assertEqual(self.truth("SELECT count(*) FROM {biz}.page_requests WHERE page_kind = "
                                    "'history' AND page_key = %s", (key,))[0][0], 1)

    def test_a_source_that_moved_is_shown_with_its_age_and_a_rebuild_is_asked(self):
        key = self.stored()
        fresh = served.history(self.backend(self.door(FULL)), object(), key)
        self.assertNotIn("stored", fresh.message or "")
        self.assertGreaterEqual(self.marker(stale=[key])["stale"], 1)
        served.PENDING.set(None)
        late = served.history(self.backend(self.door(FULL)), object(), key)
        self.assertEqual([e.model_dump() for e in late.entries], [e.model_dump() for e in fresh.entries])
        self.assertIn("newer changes are being prepared", late.message)
        self.assertEqual(served.PENDING.get()[1:], ("history", key))
        self.assertEqual(served.SRC.get(), "stale")


if __name__ == "__main__":
    unittest.main()
