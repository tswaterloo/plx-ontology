"""Firm BC copies of parties (``ontology.identity_scoring.corroborate_party_twins``, the
``twins_parties`` build step): a customer joined to a BC customer only on its code is a
``lead`` (the BC company cap) and is raised by a separate ``corroborated`` ``same_record``
link when the code names one record on each side and the names agree; never on the code
alone, never with a differing name, never when a code is shared by several records.

Evidence label: ``isolated-fixture`` (synthetic Sage, Portal and BC captures through the
real projector and the compiled repository schema).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from ontology import identifiers
from ontology import identity_scoring as ids
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema

T = "2026-10-01T00:00:00+00:00"
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
KEYS = {"sage.ar_customer": "idcust"}


class Source:
    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        digest = hashlib.sha256(json.dumps([rel, row], sort_keys=True).encode()).hexdigest()
        if self.source == "bc":
            book = row.pop("_book", "bc_manufacturing")
            return CapturedRecord("bc", rel, f"{book}/{row['id']}", row,
                                  f"bc://production/{book}/{rel}/{row['id']}@X", "row",
                                  digest, T, None, T, f"bc:production:{book}:{rel}",
                                  capture_lineage=book)
        name = KEYS.get(rel, "id")
        key = json.dumps({name: row[name]})
        return CapturedRecord("portal_api", rel, key, row, f"api://{rel}/{row[name]}@X", "row",
                              digest, T, None, T, f"api:{rel}")


ROWS = {
    "portal_api": {
        "sage.ar_customer": [
            {"idcust": "ALPH01", "namecust": "Alpha Soap Co."},
            {"idcust": "BETA01", "namecust": "Beta Labs"},
            {"idcust": "DELT01", "namecust": "Delta Hospitality Group"}],
        # two Portal customers carry one bill-to code: it names neither alone
        "public.app_customer": [
            {"id": "c-1", "erp_code": "GRP01", "company_name": "Group Hotel East",
             "external_id": "sage-cust-GRP01"},
            {"id": "c-2", "erp_code": "GRP01", "company_name": "Group Hotel East",
             "external_id": "sage-cust-GRP01-2"}]},
    "bc": {"customers": [
        {"id": "b1", "number": "ALPH01", "displayName": "ALPHA SOAP"},
        {"id": "b2", "number": "BETA01", "displayName": "Gamma Industries"},
        # one customer synced to two books: each book's copy is firm
        {"id": "b3", "number": "DELT01", "displayName": "Delta Hospitality", "_book":
         "bc_manufacturing"},
        {"id": "b4", "number": "DELT01", "displayName": "Delta Hospitality Group Inc",
         "_book": "bc_distribution"},
        {"id": "b5", "number": "GRP01", "displayName": "Group Hotel East"}]},
}


def sage(code):
    return f'Customer:portal.sage_ar_customer:{{"idcust":"{code}"}}'


def bc(i):
    return f'Customer:bc.customers:{{"id":"b{i}"}}'


class PartyTwins(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        rows = json.loads(json.dumps(ROWS))
        report = build_store(path, load_schema(), {n: Source(n, r) for n, r in rows.items()},
                             normalizer=identifiers.normalize, clock=lambda: NOW,
                             post_project={"twins_parties": ids.party_twin_step})
        cls.report = report["post_project"]["twins_parties"]
        cls.conn = sqlite3.connect(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def tiers(self, a, b):
        return sorted(r[0] for r in self.conn.execute(
            "SELECT tier FROM links WHERE link_type = 'Customer.same_record_as' AND "
            "superseded_at IS NULL AND ((from_object = ? AND to_object = ?) OR "
            "(from_object = ? AND to_object = ?))", (a, b, b, a)))

    def test_a_unique_code_with_agreeing_names_is_firm_beside_its_lead(self):
        self.assertEqual(self.tiers(sage("ALPH01"), bc(1)), ["corroborated", "lead"])
        (reason,) = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT reason FROM links WHERE tier = 'corroborated' AND from_object = ?",
            (sage("ALPH01"),))]
        self.assertEqual([f["feature"] for f in reason],
                         ["pair", "name", "unique", "corroborates"])

    def test_a_copy_in_each_book_is_firm_when_its_name_agrees(self):
        self.assertEqual(self.tiers(sage("DELT01"), bc(3)), ["corroborated", "lead"])
        self.assertEqual(self.tiers(sage("DELT01"), bc(4)), ["corroborated", "lead"])

    def test_a_differing_name_keeps_the_lead(self):
        self.assertEqual(self.tiers(sage("BETA01"), bc(2)), ["lead"])

    def test_a_shared_code_is_never_firm(self):
        for app in ("c-1", "c-2"):
            self.assertNotIn("corroborated",
                             self.tiers(f'Customer:portal.app_customer:{{"id":"{app}"}}', bc(5)))
        counts = self.report["counts"]["Customer.same_record_as"]
        self.assertEqual((counts["firm"], counts["name_conflict"], counts["ambiguous"]),
                         (3, 1, 2))

    def test_names_agree_word_for_word_or_cut_short_never_by_likeness(self):
        agree = ids.party_names_agree
        self.assertEqual(agree("Northwind Chemical Co.", "NORTHWIND CHEMICAL"), "exact")
        self.assertEqual(agree("Northwind Chemcial", "Northwind Chemical"), "close")
        self.assertEqual(agree("Northwind Chemical", "Northwind Chemical Supply West"), "prefix")
        self.assertNotIn(agree("Supplier One", "Supplier Two"), ("exact", "close", "prefix"))
        self.assertNotIn(agree("Northwind", "Northwind Chemical"), ("exact", "close", "prefix"))
        self.assertEqual(agree("", "Northwind"), "unknown")


if __name__ == "__main__":
    unittest.main()
