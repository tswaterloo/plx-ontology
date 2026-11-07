"""Ledger entries linked to the batch or assembly order their posting names
(``ontology.posting_links``).

Evidence label: ``isolated-fixture`` (made-up FileMaker batches and assembly orders, made-up
BC item ledger entries, the real projector, the compiled schema and the step; no source read).

* A document number in a declared Portal posting form links the entry to the one record it
  names: by the FileMaker record id (``PLX-BAT-<id>``) or by the record's own code
  (``PLX-AO-<AO name>``).
* A number naming no record, a form naming a day, and any other number link nothing.
* A re-run replaces its own rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ontology import identifiers, schema
from ontology import posting_links as pl
from ontology.project import CapturedRecord, build_store

T = "2026-10-01T00:00:00Z"


class Source:
    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        key = json.dumps({"id": row["id"]})
        digest = hashlib.sha256(f"{rel}/{key}".encode()).hexdigest()
        if self.source == "bc":
            return CapturedRecord("bc", rel, f"bc_zeta/{row['id']}", row,
                                  f"bc://production/bc_zeta/{rel}/{row['id']}@X", "row",
                                  digest, T, None, T, f"bc:production:bc_zeta:{rel}",
                                  capture_lineage="bc_zeta")
        return CapturedRecord("portal_api", rel, key, row, f"api://{rel}/{row['id']}@X", "row",
                              digest, T, None, T, f"api:{rel}")


PORTAL = {
    "public.batch": [{"id": 70101, "namebatch_c": "BC-99001Q"},
                     {"id": 70102, "namebatch_c": "BC-99002Q"}],
    "public.assembly_order": [{"id": 8801, "nameamo_c": "AO-99123K"}],
}


def entry(i, number):
    return {"id": f"e{i}", "entryNumber": i, "itemNumber": f"ZZ-{i:03d}",
            "entryType": "Negative_x0020_Adjmt_x002E_", "documentNumber": number,
            "quantity": -1}


BC = {"itemLedgerEntries": [
    entry(1, "PLX-BAT-70101"),        # the batch by its FileMaker id
    entry(2, "PLX-AO-AO-99123K"),     # the assembly order by its name
    entry(3, "PLX-BAT-70999"),        # no such batch
    entry(4, "PLX-AO-2026-10-04"),    # a day, no record
    entry(5, "PLX-OPEN-20261001-01"),  # not a posting form that names a record
]}


def oid(type_id, binding, key):
    return f"{type_id}:{binding}:{json.dumps(key, separators=(',', ':'))}"


def ile(i):
    return oid("ItemLedgerEntry", "bc.item_ledger_entries", {"id": f"e{i}"})


BATCH = oid("Batch", "portal.fm_batch", {"id": 70101})
AO = oid("AssemblyOrder", "portal.fm_assembly_order", {"id": 8801})


class PostingLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        cls.report = build_store(
            path, schema.load_schema(),
            {"portal_api": Source("portal_api", PORTAL), "bc": Source("bc", BC)},
            normalizer=identifiers.normalize,
            clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
            post_project={"trace_postings": pl.trace_postings_step})
        cls.conn = sqlite3.connect(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def links(self):
        return sorted(self.conn.execute(
            "SELECT from_object, to_object, link_type, tier FROM links WHERE link_id >= ? AND "
            "link_id < ?", (pl.PREFIX, pl._end(pl.PREFIX))).fetchall())

    def test_a_posting_links_the_one_record_its_number_names(self):
        self.assertEqual(self.links(), sorted([
            (ile(1), BATCH, "ItemLedgerEntry.posted_by_batch", "corroborated"),
            (ile(2), AO, "ItemLedgerEntry.posted_by_assembly_order", "corroborated")]))
        counts = self.report["post_project"]["trace_postings"]["counts"]
        self.assertEqual((counts["firm"], counts["no_record"], counts["names_no_record"]),
                         (2, 1, 1))

    def test_a_rerun_replaces_its_own_rows(self):
        class Builder:
            connection = self.conn

            def add(self, table, rows):
                for r in rows:
                    self.connection.execute(
                        "UPDATE link_rows SET recorded_at = recorded_at WHERE link_id = ?",
                        (r["link_id"],))
                self.added = len(rows)

        compiled = schema.load_schema()

        class Projector:
            links = compiled.link_types
            bindings = {b["id"]: (tid, b) for tid, t in compiled.object_types.items()
                        for b in t.get("bindings", ())}

        builder = Builder()
        result = pl.trace_postings_step(builder, Projector())
        self.assertEqual(result["link_tiers"], {"corroborated": 2})
        self.assertEqual(self.links(), [])
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
