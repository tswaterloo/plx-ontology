"""Portal supplier <-> FM supplier identity (``Supplier.same_record_as``): an
``app_supplier`` row whose ``external_id`` is ``fm-supp-{N}`` (the silver-to-gold ETL's
stored key, read by the Portal's ``parseFmSupplierId``) is the FM ``public.supplier`` row
with id N, a ``verified`` ``same_record`` link through the ``fm_supplier_id`` identifier
kind. A malformed external id, another prefix or a missing FM row joins nothing; the name
pair stays a ``lead`` only.

Evidence label: ``isolated-fixture`` (synthetic Portal captures through the real
projector and the compiled repository schema; no source is read).
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from ontology import identifiers
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from ontology import store_ddl
from ontology.store import ProjectionStore, ReadPolicy
from ontology.types import Actor

T = "2026-10-01T00:00:00+00:00"
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
SCHEMA = load_schema()
LINK = "Supplier.same_record_as"
GOLD, FM = "public.app_supplier", "public.supplier"


class Source:
    source = "portal_api"

    def __init__(self, rows):
        self.rows = rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        digest = hashlib.sha256(json.dumps([rel, row], sort_keys=True).encode()).hexdigest()
        return CapturedRecord("portal_api", rel, json.dumps({"id": row["id"]}), row,
                              f"api://{rel}/{row['id']}@X", "row", digest, T, None, T,
                              f"api:{rel}")


def gold(i, external_id, name=None):
    return {"id": f"g{i}", "code": f"ZZ{i:04d}", "name": name or f"Synthetic Vendor {i}",
            "external_id": external_id}


def fm(n, name=None):
    return {"id": n, "name": name or f"Synthetic Vendor {n}"}


def gold_id(i):
    return f'Supplier:portal.app_supplier:{{"id":"g{i}"}}'


def fm_id(n):
    return f'Supplier:portal.fm_supplier:{{"id":{n}}}'


class SupplierFmIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir, self.n = Path(tmp.name), 0

    def build(self, gold_rows, fm_rows):
        self.n += 1
        path = self.dir / f"s{self.n}.sqlite"
        build_store(path, SCHEMA, {"portal_api": Source({GOLD: gold_rows, FM: fm_rows})},
                    normalizer=identifiers.normalize, clock=lambda: NOW)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        links = [dict(r) for r in conn.execute(
            "SELECT * FROM links WHERE link_type = ? ORDER BY link_id", (LINK,))]
        return path, links

    @staticmethod
    def verified(links):
        return {(l["from_object"], l["to_object"]) for l in links
                if l["tier"] == "verified" and l["binding_kind"] == "same_record"}

    def test_an_fm_supp_external_id_joins_its_fm_supplier_verified_with_evidence(self):
        # Names differ on purpose: the stored key decides, not the name.
        path, links = self.build([gold(1, "fm-supp-7", "Renamed In Portal")],
                                 [fm(7, "Old FM Name"), fm(8)])
        self.assertEqual(self.verified(links), {(gold_id(1), fm_id(7))})
        [link] = [l for l in links if l["tier"] == "verified"]
        self.assertEqual(json.loads(link["evidence_handles"]),
                         ["api://public.app_supplier/g1@X", "api://public.supplier/7@X"])
        reason = json.dumps(json.loads(link["reason"]))
        self.assertIn("external_id", reason)
        self.assertNotIn("lead", {l["tier"] for l in links})   # names differ: no lead

    def test_spellings_of_one_stored_key_join_and_the_name_lead_stays_a_lead(self):
        _, links = self.build([gold(1, " FM-SUPP-12 "), gold(2, "fm-supp-0013")],
                              [fm(12), fm(13)])
        self.assertEqual(self.verified(links),
                         {(gold_id(1), fm_id(12)), (gold_id(2), fm_id(13))})
        for l in links:   # an agreeing name never adds more than a lead
            self.assertIn((l["tier"], l["binding_kind"]),
                          {("verified", "same_record"), ("lead", "text_match")})

    def test_a_malformed_or_foreign_external_id_or_a_missing_fm_row_joins_nothing(self):
        bad = ["fm-supp-", "fm-supp-x7", "fm-supp-7-1", "fm-supp--7", "fm-cust-7",
               "sage-vend-7", "fm-supp-7.5", "supp-7", "fm supp 7", "", None]
        rows = [gold(i, value, name=f"Unrelated {i}") for i, value in enumerate(bad, 1)]
        rows.append(gold(99, "fm-supp-404", name="Unrelated 99"))   # no FM row 404
        _, links = self.build(rows, [fm(7), fm(1)])
        self.assertEqual(self.verified(links), set())
        self.assertEqual([l for l in links if l["tier"] != "lead"], [])
        for value in bad[:-2]:
            with self.subTest(value=value):
                self.assertIsNone(identifiers.normalize("fm_supplier_id", value))

    def test_the_kind_maps_both_stored_forms_to_one_key_and_nothing_else(self):
        norm = lambda v: identifiers.normalize("fm_supplier_id", v)   # noqa: E731
        self.assertEqual({norm("fm-supp-7"), norm("FM-SUPP-007"), norm(7), norm("7"),
                          norm("7.0")}, {"7"})
        for value in (True, -7, "-7", 7.5, "fm-cust-7", "fm-supp-7a", "PAY5000"):
            with self.subTest(value=value):
                self.assertIsNone(norm(value))

    def test_a_reader_without_the_fm_relation_sees_no_twin(self):
        path, links = self.build([gold(1, "fm-supp-7")], [fm(7)])
        self.assertEqual(len(self.verified(links)), 1)
        everyone = frozenset(store_ddl.MARKINGS)
        policy = ReadPolicy(
            audiences={"api": lambda actor, rel: f"api:{rel}" in actor.principals},
            marking_principals={m: frozenset({"api:public.app_supplier"}) for m in everyone})
        store = ProjectionStore(path, policy)
        self.addCleanup(getattr(store, "close", lambda: None))
        gold_only = Actor("plx", frozenset({"api:public.app_supplier"}), "restricted")
        both = Actor("plx", frozenset({"api:public.app_supplier", "api:public.supplier"}),
                     "restricted")
        self.assertEqual(store.links(gold_only, gold_id(1), link_type=LINK), [])
        seen = store.links(both, gold_id(1), link_type=LINK)
        self.assertEqual({(l["to_object"], l["tier"]) for l in seen}, {(fm_id(7), "verified")})

    def test_cardinality_no_row_lost_or_duplicated(self):
        for size in (1, 10, 60):
            with self.subTest(size=size):
                started = time.monotonic()
                _, links = self.build([gold(i, f"fm-supp-{i}") for i in range(1, size + 1)],
                                      [fm(i) for i in range(1, size + 1)])
                pairs = [(l["from_object"], l["to_object"]) for l in links
                         if l["tier"] == "verified"]
                self.assertEqual(sorted(pairs),
                                 sorted((gold_id(i), fm_id(i)) for i in range(1, size + 1)))
                self.assertLess(time.monotonic() - started, 30)


if __name__ == "__main__":
    unittest.main()
