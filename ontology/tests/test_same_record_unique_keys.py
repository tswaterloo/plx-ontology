"""Non-unique codes yield candidates, never a merge (``unique_keys`` on the
``same_record`` bindings of ``Product``/``Customer``/``Supplier.same_record_as``).

Shapes from the served store (11-12): two FileMaker finished products (a GOLD and a BLACK
pump variant) both hold code ``CG-HR-4OZ``, and the Portal product of that code was
``verified`` to both; several Portal customer cards (brands billed on one account) hold
one Sage ``erp_code`` and were each ``verified`` to that Sage customer, merging different
parties. A code more than one record of a binding holds now caps every link it makes at
``lead`` (``why_not`` ``non_unique_key``); the Portal product's own stored key of its FM
record (``external_id`` ``fm-fp-{id}`` / ``fm-bulk-{id}``) still joins the right one
``verified``, so the correct merge is kept. A unique code is untouched.

Evidence label: ``isolated-fixture`` (synthetic captures through the real projector and
the compiled repository schema; no source is read).
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

from ontology import identifiers, store_ddl
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from ontology.store import ProjectionStore, ReadPolicy
from ontology.types import Actor
from tools.timing_policy import budget

T = "2026-10-01T00:00:00+00:00"
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
SCHEMA = load_schema()
PRODUCT, CUSTOMER = "Product.same_record_as", "Customer.same_record_as"
FIRM = ("verified", "corroborated", "reviewed", "stored_by_text_join")


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
        key = {k: row[k] for k in (("idcust",) if rel == "sage.ar_customer" else ("id",))}
        digest = hashlib.sha256(json.dumps([rel, row], sort_keys=True).encode()).hexdigest()
        return CapturedRecord("portal_api", rel, json.dumps(key), row,
                              f"api://{rel}/{list(key.values())[0]}@X", "row", digest, T,
                              None, T, f"api:{rel}")


def gold(cid, code, external_id, name):
    return {"id": cid, "code": code, "external_id": external_id, "name": name}


def fp(n, code, name):
    return {"id": n, "codefp_c": code, "name": name}


def oid(binding, key):
    return f"Product:{binding}:{json.dumps(key, separators=(',', ':'))}"


GOLD = lambda cid: oid("portal.app_product", {"id": cid})            # noqa: E731
FP = lambda n: oid("portal.fm_finished_product", {"id": n})          # noqa: E731
BULK = lambda n: oid("portal.fm_bulk_product", {"id": n})            # noqa: E731


class UniqueKeyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir, self.n = Path(tmp.name), 0

    def build(self, rows, link_type=PRODUCT):
        self.n += 1
        path = self.dir / f"s{self.n}.sqlite"
        build_store(path, SCHEMA, {"portal_api": Source(rows)},
                    normalizer=identifiers.normalize, clock=lambda: NOW)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        links = [dict(r) for r in conn.execute(
            "SELECT * FROM links WHERE link_type = ? ORDER BY link_id", (link_type,))]
        return path, links

    @staticmethod
    def firm(links):
        return {(l["from_object"], l["to_object"]) for l in links if l["tier"] in FIRM}

    def test_a_code_two_fm_products_hold_merges_neither_and_the_stored_key_picks_one(self):
        path, links = self.build({
            "public.app_product": [gold("g1", "CG-HR-4OZ", "fm-fp-833", "Hair Oil 4oz GOLD"),
                                   gold("g2", "NBY-008-30", "fm-fp-900", "Glow 30ml")],
            "public.finished_product": [fp(833, "CG-HR-4OZ", "Hair Oil 4oz GOLD pumps"),
                                        fp(834, "CG-HR-4OZ", "Hair Oil 4oz BLACK pumps"),
                                        fp(900, "NBY-008-30", "Glow 30ml")]})
        # the GOLD variant is the Portal product's own FM record (its external id); the
        # BLACK one is no merge; the unique code is untouched (verified twice: code + id)
        self.assertEqual(self.firm(links), {(GOLD("g1"), FP(833)), (GOLD("g2"), FP(900))})
        code_links = {l["to_object"]: l for l in links if l["from_object"] == GOLD("g1")
                      and "codefp_c" in l["reason"]}
        self.assertEqual({k: v["tier"] for k, v in code_links.items()},
                         {FP(833): "lead", FP(834): "lead"})
        for link in code_links.values():
            [held] = [f for f in json.loads(link["why_not"])
                      if f["feature"] == "non_unique_key"]
            self.assertEqual((held["from_records"], held["to_records"], held["cap"]),
                             (1, 2, "lead"))
        by_id = [l for l in links if l["from_object"] == GOLD("g1") and l["tier"] == "verified"]
        self.assertEqual([json.loads(l["reason"])[0]["from_path"] for l in by_id], ["external_id"])
        self.assertEqual(json.loads(by_id[0]["evidence_handles"]),
                         ["api://public.app_product/g1@X", "api://public.finished_product/833@X"])
        unique = [l for l in links if l["from_object"] == GOLD("g2")]
        self.assertEqual({l["tier"] for l in unique}, {"verified"})
        self.assertNotIn("non_unique_key", json.dumps([l["why_not"] for l in unique]))

    def test_one_erp_code_on_several_customer_cards_merges_no_party(self):
        cards = [{"id": f"fm-cust-{n}", "code": c, "erp_code": "BIOP03", "company_name": name,
                  "external_id": f"fm-cust-{n}"}
                 for n, c, name in ((5, "BIO", "Biophora"), (117, "FUR", "Furgenics"),
                                    (19, "SAL", "Salima"))]
        cards.append({"id": "c-9", "code": "TK5", "erp_code": "TAKE5", "company_name": "Take Five",
                      "external_id": None})
        _, links = self.build({"public.app_customer": cards, "sage.ar_customer": [
            {"idcust": "BIOP03", "namecust": "BIOPHORA (NA)"},
            {"idcust": "TAKE5", "namecust": "Take Five"}]}, CUSTOMER)
        sage = {l["from_object"]: l["tier"] for l in links
                if l["to_object"].startswith("Customer:portal.sage_ar_customer:")}
        self.assertEqual(sage, {
            'Customer:portal.app_customer:{"id":"fm-cust-5"}': "lead",
            'Customer:portal.app_customer:{"id":"fm-cust-117"}': "lead",
            'Customer:portal.app_customer:{"id":"fm-cust-19"}': "lead",
            'Customer:portal.app_customer:{"id":"c-9"}': "verified"})

    def test_an_external_id_that_is_no_fm_product_key_joins_nothing(self):
        # bare numbers (4,886 served app_product external ids) and other prefixes name no
        # FM record; a family's id never reaches the other family's table
        rows = [gold(f"g{i}", f"UNIQ-{i}", ext, f"Item {i}") for i, ext in enumerate(
            ["833", "fm-raw-833", "fm-fp-x833", "fm-fp-833.5", "fm-bulk-834", " ", None], 1)]
        _, links = self.build({"public.app_product": rows,
                               "public.finished_product": [fp(833, "OTHER-1", "A"),
                                                           fp(834, "OTHER-2", "B")]})
        self.assertEqual(self.firm(links), set())
        for value in ("833", "fm-raw-833", "fm-fp-x833", "fm-fp-833.5", "fm-bulk-834"):
            with self.subTest(value=value):
                self.assertIsNone(identifiers.normalize("fm_finished_product_id", value))
        self.assertEqual({identifiers.normalize("fm_bulk_product_id", v)
                          for v in (" FM-BULK-0554 ", 554, "fm-bulk-554")}, {"fm-bulk-554"})

    def test_a_reader_without_the_fm_relation_sees_no_link_or_count(self):
        path, links = self.build({
            "public.app_product": [gold("g1", "CG-", "fm-bulk-554", "Daily face Lotion")],
            "public.bulk_product": [{"id": 554, "bulkcode_u": "CG-", "name": "Daily face Lotion"},
                                    {"id": 521, "bulkcode_u": "CG-", "name": "Creed Type"}]})
        self.assertEqual(self.firm(links), {(GOLD("g1"), BULK(554))})
        everyone = frozenset(store_ddl.MARKINGS)
        policy = ReadPolicy(
            audiences={"api": lambda actor, rel: f"api:{rel}" in actor.principals},
            marking_principals={m: frozenset({"api:public.app_product"}) for m in everyone})
        store = ProjectionStore(path, policy)
        self.addCleanup(getattr(store, "close", lambda: None))
        gold_only = Actor("plx", frozenset({"api:public.app_product"}), "restricted")
        self.assertEqual(store.links(gold_only, GOLD("g1"), link_type=PRODUCT), [])

    def test_cardinality_no_row_lost_or_duplicated(self):
        for size in (1, 10, 60):
            with self.subTest(size=size):
                started = time.monotonic()
                gold_rows = [gold(f"g{i}", f"CODE-{i}", f"fm-fp-{i}", f"P{i}")
                             for i in range(1, size + 1)]
                fm_rows = [fp(i, f"CODE-{i}", f"P{i}") for i in range(1, size + 1)]
                fm_rows.append(fp(10_000, "CODE-1", "P1 copy"))   # one code reused
                _, links = self.build({"public.app_product": gold_rows,
                                       "public.finished_product": fm_rows})
                verified = sorted((l["from_object"], l["to_object"]) for l in links
                                  if l["tier"] == "verified")
                expected = [(GOLD(f"g{i}"), FP(i)) for i in range(1, size + 1)]   # by id
                expected += [(GOLD(f"g{i}"), FP(i)) for i in range(2, size + 1)]  # by code
                self.assertEqual(verified, sorted(expected))
                self.assertEqual(sum(l["tier"] == "lead" for l in links), 2)
                self.assertLess(time.monotonic() - started, budget(30))


if __name__ == "__main__":
    unittest.main()
