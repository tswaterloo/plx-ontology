"""The order-margin eval (``scripts/eval_order_margin.py``) and its oracle family F10.

The live path runs its real code over an injected BC transport (the in-process fake of
``integrations/tests/test_bc_capture.py`` serving the synthetic world of
``evals/oracle/order_margin.py``): GET-only capture, BC-only projection, ``plx.investigate``,
the oracle over the raw captured records, scoring and partitions. The fixture mode scores the
same world from in-memory captures and must agree. The oracle's import independence is the
gate ``oracle-import-ban`` (``tests/test_oracle_import_ban.py``). Evidence labels: injected-transport and
isolated-fixture (synthetic records, never live PLX data).
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import unittest.mock

from evals.oracle import order_margin as oracle
from integrations.tests import test_bc_capture as tb
from scripts import eval_order_margin as margin_eval
from tests.test_investigate_order_margin import fake_bc


def run(argv, **kwargs) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = margin_eval.main(argv, **kwargs)
    return code, json.loads(out.getvalue())


class OrderMarginEvalTests(unittest.TestCase):
    def test_live_path_scores_the_product_against_the_oracle_like_the_fixture(self):
        with tempfile.TemporaryDirectory() as scratch:
            code, report = run(["live", "--db", f"{scratch}/capture.sqlite", "--work-dir",
                                f"{scratch}/product", "--run-id", "t", "--count", "20"],
                               environ=tb.ENV, sender=fake_bc(oracle.fixture_world(tb.CO[:2])),
                               books=tb.BOOKS)
        block = report["eval"]
        self.assertEqual(code, 0, block["fail_reasons"])
        # SO-4 and SO-21 (BC no longer serves it) are only partly invoiced: the oracle decides
        # nothing, so they are excluded, never scored; every other order agrees figure by
        # figure (both companies' SO-20, which BC no longer serves either, by item; SO-5 net
        # of its applied credit
        # memo and return, SO-6 with its Charge credit kept out of the goods, SO-7, SO-10,
        # SO-11 and SO-13 (60 of them) beside their customer's unattributable credit memo,
        # SO-8 with revenue only as one memo's return is not on the ledger, SO-9 beside its
        # customer's many other memos, SO-12 net of the memo BC names by invoiceNumber
        # alone), abstentions counted apart. Orders are drawn by the invoices' orderNumber
        # and each has exactly one SalesOrder object: none duplicated, none missing.
        self.assertEqual((block["sampled"], block["scored"], block["correct"], block["wrong"],
                          block["abstain_ok"], block["excluded"], block["coverage"],
                          block["accuracy"]), (17, 15, 15, 0, 0, 2, "15/17", "15/15"))
        self.assertEqual((block["unserved"], block["duplicated"], block["not_held"]), (3, 0, 0))
        self.assertEqual((report["evidence_label"], report["adapter_status"],
                          report["row_data_check"]["leaked"]),
                         ("injected-transport", "live-unverified", 0))
        self.assertEqual(block["partition_violations"], 0)
        # Every BC set captured whole, credit memos with their lines among them: no answer
        # carries credit_memos_not_captured; the four orders whose customer holds a memo
        # applied to no invoice carry its gap, the netted ones (SO-5, SO-12) do not.
        self.assertTrue(report["capture"]["all_complete"])
        self.assertEqual(report["capture"]["units_reached"], report["capture"]["units_total"])
        self.assertEqual(block["product_caveats"], {"credit_memos_not_captured": 0,
                                                    "credit_memo_unattributed": 4})
        code, fixture = run(["fixture", "--count", "20"])
        self.assertEqual(code, 0)
        self.assertEqual({k: fixture["eval"][k] for k in ("scored", "correct", "excluded")},
                         {k: block[k] for k in ("scored", "correct", "excluded")})

    def test_completeness_is_per_company_unit_not_the_whole_capture(self):
        """Live 2026-10-07: one incomplete unit anywhere blocked every order. The salesOrders
        unit of the second company is not exposed: its orders (SO-1, which then looks
        unserved, and SO-20) stay blocked, while every order of the first company, whose
        units are all whole, scores as on a complete capture: the product holds what stands
        in for its SO-20 and SO-21 BC no longer serves (``ontology/project.py``, per company).
        With the second company's salesCreditMemos not exposed instead, its SO-20 is
        undecided (an unseen memo may credit it), excluded, never wrong against the
        product's caveated figure; its served orders still score."""
        expected = {"salesOrders": (17, 13, 13, 0, 4, 0, 4),
                    "salesCreditMemos": (17, 14, 14, 0, 3, 0, 3)}
        for entity_set, counts in expected.items():
            fake = fake_bc(oracle.fixture_world(tb.CO[:2]))
            fake.fail(rf"{tb.CO[1]}\)/{entity_set}(?:\?|$)",
                      (404, {}, b'{"error":{"code":"BadRequest_NotFound"}}'))
            with tempfile.TemporaryDirectory() as scratch:
                code, report = run(["live", "--db", f"{scratch}/capture.sqlite", "--work-dir",
                                    f"{scratch}/product", "--run-id", "t", "--count", "20"],
                                   environ=tb.ENV, sender=fake, books=tb.BOOKS)
            block = report["eval"]
            with self.subTest(entity_set):
                self.assertFalse(report["capture"]["all_complete"])
                self.assertEqual(code, 0, block["fail_reasons"])
                self.assertEqual((block["sampled"], block["scored"], block["correct"],
                                  block["wrong"], block["excluded"], block["not_held"],
                                  block["unserved"]), counts)
        # The report names companies by book: a company it does not hold exactly once, a
        # set it never reached and a set the port is not told about are all incomplete.
        books = {tb.CO[0]: "manufacturing", tb.CO[1]: "distribution", tb.CO[2]: "consol"}
        capture = {"companies": [
            {"book": "manufacturing", "units": [{"entity_set": "salesOrders", "complete": True}]},
            {"book": "consol", "units": [{"entity_set": "salesOrders", "complete": True}]},
            {"book": "consol", "units": [{"entity_set": "salesOrders", "complete": True}]}]}
        whole = margin_eval.unit_completeness(capture, books)
        self.assertEqual((whole[tb.CO[0]]["salesOrders"], whole[tb.CO[0]]["salesInvoices"],
                          whole[tb.CO[1]], whole[tb.CO[2]]), (True, False, {}, {}))
        port = oracle.RecordPort([], tb.CO[0], complete={"salesOrders": True})
        self.assertEqual((port.find("salesOrders", "number", "X")["complete"],
                          port.find("vendors", "number", "X")["complete"],
                          port.lines({"entity_set": "salesOrders", "id": "x"})["complete"]),
                         (True, False, False))

    def test_a_session_producers_capture_is_topped_up_and_never_read_again(self):
        """``{session_state}``: bc.capture_full's store (same audience and tenant) is this
        eval's --db, so in the verifier session (its start in ``FRESH_SINCE_ENV``) the eval
        reads no unit the producer completed; a later invocation reads the store again."""
        from datetime import datetime, timezone

        from integrations import bc_read
        fake = fake_bc(oracle.fixture_world(tb.CO[:2]))
        session = {**tb.ENV, bc_read.FRESH_SINCE_ENV:
                   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        with tempfile.TemporaryDirectory() as scratch:
            db = f"{scratch}/bc-capture.sqlite"
            argv = ["live", "--db", db, "--work-dir", f"{scratch}/product", "--run-id", "t",
                    "--count", "20"]
            produced = bc_read.capture_probe(tb.ENV, sender=fake, db=db, mode="full",
                                             resolve_sample=0, books=tb.BOOKS)
            before = len(fake.calls)
            code, report = run(argv, environ=session, sender=fake, books=tb.BOOKS)
            reads = [url.split("?")[0].rsplit("/", 1)[-1]
                     for method, url, _ in fake.calls[before:] if method == "GET"]
            before = len(fake.calls)
            code_later, later = run(argv, environ=tb.ENV, sender=fake, books=tb.BOOKS)
            reads_later = {url.split("?")[0].rsplit("/", 1)[-1].split("(")[0]
                           for method, url, _ in fake.calls[before:] if method == "GET"}
        self.assertTrue(produced["all_complete"])
        self.assertEqual(code, 0, report["eval"]["fail_reasons"])
        capture = report["capture"]
        self.assertEqual((capture["topped_up"], capture["all_complete"]), (True, True))
        self.assertGreater(capture["units_reused"], 0)
        self.assertGreaterEqual(capture["capture_age_seconds"], 0.0)
        self.assertEqual(set(reads), {"companies"})
        self.assertEqual((report["eval"]["scored"], report["eval"]["correct"]), (15, 15))
        # Outside the session the completed store is only a starting point: read again.
        self.assertEqual(code_later, 0, later["eval"]["fail_reasons"])
        self.assertEqual((later["capture"]["topped_up"], later["capture"]["units_reused"],
                          later["capture"]["all_complete"]), (False, 0, True))
        self.assertIn("salesInvoices", reads_later)
        self.assertIsInstance(later["capture"]["capture_age_seconds"], float)

    def test_the_memo_capture_claims_of_the_store_decide_the_credit_gap(self):
        """The eval runs the host's own tool table, so a complete ``company=`` claim on both
        credit-memo units proves them captured; without a claim the family-level proof
        stands and every answer keeps the gap."""
        code, claimed = run(["fixture", "--count", "20"])
        self.assertEqual(code, 0, claimed["eval"]["fail_reasons"])
        self.assertEqual(claimed["eval"]["product_caveats"]["credit_memos_not_captured"], 0)

        def unclaimed(self_, *a, **kw):
            described = describe(self_, *a, **kw)
            for facts in described["relations"].values():
                facts["scopes"] = []
            return described

        describe = margin_eval.MemoryCaptures.describe
        with unittest.mock.patch.object(margin_eval.MemoryCaptures, "describe", unclaimed):
            code, bare = run(["fixture", "--count", "20"])
        self.assertEqual(code, 0, bare["eval"]["fail_reasons"])
        self.assertEqual(bare["eval"]["product_caveats"]["credit_memos_not_captured"], 17)
        # The claim clears a gap; it never changes a figure of an order BC serves. The two
        # SO-20s BC no longer serves have no ordered quantity: only complete invoice,
        # shipment and ledger units prove their lines whole (the fixture's BC family is
        # partial), so without the claims they abstain, never state a figure.
        self.assertEqual({k: claimed["eval"][k] for k in ("scored", "correct", "abstain_ok",
                                                          "wrong")},
                         {"scored": 15, "correct": 15, "abstain_ok": 0, "wrong": 0})
        self.assertEqual({k: bare["eval"][k] for k in ("scored", "correct", "abstain_ok",
                                                       "wrong")},
                         {"scored": 15, "correct": 13, "abstain_ok": 2, "wrong": 0})

    def test_a_figure_over_missing_cost_is_wrong_and_a_withheld_one_is_an_abstention(self):
        caveats = [oracle.CREDIT_CAVEAT, oracle.UNATTRIBUTED_CAVEAT]
        reading = {"lines": {"l1": {"revenue": "40", "booked_cost": None, "margin": None}},
                   "total": {"revenue": None, "booked_cost": None, "margin": None},
                   "caveats": caveats}
        zero_cost = {"lines": {"l1": {"revenue": "40", "booked_cost": "0", "margin": "40"}},
                     "total": {}, "caveats": caveats}
        self.assertEqual(margin_eval.score(reading, zero_cost)["outcome"], "wrong")
        withheld = {"lines": {"l1": {"revenue": None}}, "total": {}, "caveats": caveats}
        self.assertEqual(margin_eval.score(reading, withheld)["outcome"], "abstain_ok")
        exact = {"lines": {"l1": {"revenue": "40.00"}}, "total": {}, "caveats": caveats}
        self.assertEqual(margin_eval.score(reading, exact)["outcome"], "correct")
        # The same right figure without either credit-memo gap overstates what is known.
        for kept in caveats:
            self.assertEqual(margin_eval.score(reading, dict(exact, caveats=[kept]))
                             ["outcome"], "wrong")

    def test_a_product_that_loses_the_unattributed_gap_fails_the_run(self):
        """The oracle reaches the unattributable-memo rule from the raw BC fields alone, so a
        product that stated the same figures without the gap is scored wrong: the mutation is
        the gap reason the eval looks for, as a product that stopped raising it would be."""
        with unittest.mock.patch.dict(margin_eval.CAVEAT_GAPS,
                                      {oracle.UNATTRIBUTED_CAVEAT: "no_such_gap"}):
            code, mutated = run(["fixture", "--count", "20"])
        self.assertEqual(code, 3)
        self.assertEqual((mutated["eval"]["wrong"], mutated["eval"]["fail_reasons"]),
                         (4, ["wrong_above_bound"]))

    def test_a_served_order_record_replaces_the_stand_in_incrementally_as_cold(self):
        """The stand-in for SO-20 depends on the salesOrders capture: when a salesOrders
        record of that number arrives, the incremental generation drops the stand-in for the
        record, exactly as a cold build of the same captures (one identity, same digest)."""
        from ontology import identifiers
        from ontology.project import build_store
        from ontology.project_incremental import build_generation
        from ontology.schema import load_schema

        schema, records = load_schema(), margin_eval.fixture_records()
        company, rid = margin_eval.FIXTURE_COMPANIES[0], "00000001-0001-4000-8000-0000000fffff"
        served = {"company_id": company, "entity_set": "salesOrders", "id": rid,
                  "fields": {"id": rid, "number": "SO-20", "customerNumber": "C-1"},
                  "citation": f"bc://fixture/{company}/salesOrders/{rid}@0000000000000000-1"}

        def orders(path) -> list[str]:
            with sqlite3.connect(path) as conn:
                return [row[0] for row in conn.execute(
                    "SELECT o.object_id FROM objects o JOIN object_values v ON v.object_id = "
                    "o.object_id AND v.property = 'order_number' AND v.value = 'SO-20' "
                    "WHERE o.object_type = 'SalesOrder' AND o.audience_key LIKE ?",
                    (f"bc:{company}/%",))]

        with tempfile.TemporaryDirectory() as scratch:
            state = Path(scratch, "state")
            sources = [{"bc": margin_eval.MemoryCaptures(world, margin_eval.FIXTURE_BOOKS)}
                       for world in (records, records + [served], records + [served])]
            before = build_generation(state, schema, sources[0], normalizer=identifiers.normalize)
            after = build_generation(state, schema, sources[1], normalizer=identifiers.normalize)
            cold = build_store(Path(scratch, "cold.sqlite"), schema, sources[2],
                               normalizer=identifiers.normalize)
            self.assertEqual(after.mode, "incremental")
            self.assertEqual(after.content_digest, cold["content_digest"])
            self.assertEqual([o.split(":")[1] for o in orders(before.store)],
                             ["bc.sales_invoices.order"])
            self.assertEqual(orders(after.store), [f'SalesOrder:bc.sales_orders:{{"id":"{rid}"}}'])

    def test_stand_ins_follow_each_companys_order_unit_incrementally_as_cold(self):
        """What stands in for an order BC no longer serves is held per company: only where
        that company's salesOrders unit is settled (an absent record of an unsettled unit
        is not known to be gone). From one failed unit to another's, with no record
        changed, the incremental generation follows the companies as a cold build does."""
        from integrations import bc_read
        from ontology import identifiers
        from ontology.project import build_store
        from ontology.project_bc import BcCaptures
        from ontology.project_incremental import build_generation
        from ontology.schema import load_schema

        def stood_in(path) -> dict[str, int]:
            with sqlite3.connect(path) as conn:
                return {company: conn.execute(
                    "SELECT COUNT(*) FROM objects WHERE object_type = 'SalesOrder' AND "
                    "object_id NOT LIKE 'SalesOrder:bc.sales_orders:%' AND audience_key "
                    "LIKE ?", (f"bc:{company}/%",)).fetchone()[0] for company in tb.CO[:2]}

        fake, schema = fake_bc(oracle.fixture_world(tb.CO[:2])), load_schema()
        with tempfile.TemporaryDirectory() as scratch:
            db, state, held = f"{scratch}/bc.sqlite", Path(scratch, "state"), []
            for failing in (None, tb.CO[1], tb.CO[0]):
                if failing:
                    fake.fail(rf"{failing}\)/salesOrders(?:\?|$)",
                              (404, {}, b'{"error":{"code":"BadRequest_NotFound"}}'))
                bc_read.capture_probe(tb.ENV, sender=fake, db=db, mode="full",
                                      resolve_sample=0, books=tb.BOOKS)
                if failing:
                    with BcCaptures(db) as source:
                        generation = build_generation(state, schema, {"bc": source},
                                                      normalizer=identifiers.normalize)
                    held.append((generation.mode, stood_in(generation.store)))
            with BcCaptures(db) as source:
                cold = build_store(Path(scratch, "cold.sqlite"), schema, {"bc": source},
                                   normalizer=identifiers.normalize)
            self.assertEqual(held, [("cold", {tb.CO[0]: 2, tb.CO[1]: 0}),
                                    ("incremental", {tb.CO[0]: 0, tb.CO[1]: 1})])
            self.assertEqual(generation.content_digest, cold["content_digest"])

    def test_related_documents_and_near_duplicates_share_a_partition(self):
        items = [{"number": "SO-1", "sources": ["bc:salesInvoices:a"]},
                 {"number": "so 1", "sources": ["bc:salesInvoices:b"]},   # near-duplicate
                 {"number": "SO-7", "sources": ["bc:salesInvoices:b"]},   # shares a document
                 *({"number": f"SO-{n}", "sources": [f"bc:salesShipments:{n}"]}
                   for n in range(100, 140))]
        parts = oracle.partition(items)
        self.assertEqual(len({parts[0], parts[1], parts[2]}), 1)
        self.assertEqual(set(parts), set(oracle.PARTITIONS))
        self.assertEqual(oracle.violations(items, parts), 0)


if __name__ == "__main__":
    unittest.main()
