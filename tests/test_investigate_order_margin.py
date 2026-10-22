"""plx.investigate family ``order_margin`` (F10) at the tool boundary, BC only.

Synthetic Business Central sales documents (BC API v2.0 field names) are captured through the
real GET-only ``BcCapture`` from the in-process fake of ``integrations/tests/test_bc_capture.py``
and projected alone (no Portal mirror: the plan answers while the Portal API is down); the
generic tools, ``plx.investigate`` and a per-reader citation resolver over the capture store run
their real code. Two companies reuse every number. Every output is validated against the tool
contract. The oracle (``evals/oracle/order_margin.py``) scores the same world in
``tests/test_eval_order_margin.py``. Evidence label: isolated-fixture (synthetic records over an
injected transport, never live PLX data).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unittest

from evals.oracle import order_margin as oracle_world
from evals.oracle.source import BcReads
from integrations.bc_read import CAPTURE_ENTITY_SETS
from integrations.tests import test_bc_capture as tb
from ontology.json_contract import validate
from ontology import store_ddl
from ontology.tests.test_project_bc import FULL, READER, BcWorld
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt
from operating import order_margin as om

CONTRACTS = ot.load_contracts()


class SignedBc(tb.FakeBc):
    """The BC fake, also sending negative JSON numbers (ledger quantities and costs)."""

    @staticmethod
    def body(value) -> bytes:
        return re.sub(r'"#(-?[0-9.]+)"', r"\1", json.dumps(value)).encode()


def fake_bc(records) -> SignedBc:
    """The BC fake serving raw records of ``evals.oracle.order_margin.fixture_world``."""
    fake = SignedBc({name: 0 for name in CAPTURE_ENTITY_SETS}, companies=tb.CO)
    for record in records:
        fields = {k: (f"#{v}" if isinstance(v, (int, float)) and not isinstance(v, bool)
                      else v) for k, v in record["fields"].items()}
        row = {"@odata.etag": f'W/"{record["id"][-6:]}"',
               "lastModifiedDateTime": "2026-09-30T10:00:00Z", **fields}
        if record.get("document_set"):
            fake.lines.setdefault((record["company_id"], record["document_set"],
                                   record["document_id"]), []).append(row)
        else:
            fake.data.setdefault((record["company_id"], record["entity_set"]), []).append(row)
    return fake


def resolver(world: BcWorld):
    def rows(handle: str, actor) -> dict | None:
        result = world.h.store.resolve(handle, tb.SERVICE)
        return result if result.get("ok") else None
    return rows


class OrderMarginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = oracle_world.fixture_world(companies=tb.CO[:2])
        self.open_world(BcWorld(self, fake_bc(self.records)))

    def open_world(self, world: BcWorld, *, claims: bool = True) -> None:
        """The tools over ``world``; ``claims``: the host passes the per-unit capture claims
        (``capture_claims``, as ``operating/ontology_host.py`` does), else family only."""
        self.world = world
        table: dict = {}
        ot.register(table, world.base)
        tt.register(table, world.traverse)
        oi.register(table, oi.InvestigateTools(
            table, rows=resolver(world), scope=oi.read_scope(world.base),
            captured=oi.capture_claims(world.base) if claims else None))
        self.table = table

    def failing_world(self, *units: tuple[str, str]) -> BcWorld:
        """The fixture world whose capture of each (company, path pattern) unit fails."""
        fake = fake_bc(self.records)
        for company, pattern in units:
            fake.fail(rf"{company}\)/{pattern}", (403, {}, b'{"error":{"code":"Forbidden"}}'))
        return BcWorld(self, fake)

    def order(self, company: str, number: str) -> str:
        """The one BC SalesOrder object of the company with that number, served or not."""
        with sqlite3.connect(self.world.store_path) as conn:
            found = conn.execute(
                "SELECT o.object_id FROM objects o JOIN object_values v ON v.object_id = "
                "o.object_id AND v.property = 'order_number' WHERE o.object_type = "
                "'SalesOrder' AND o.binding_id LIKE 'bc.%' AND o.audience_key LIKE ? "
                "AND v.value = ?", (f"bc:{company}/%", number)).fetchall()
        self.assertEqual(len(found), 1, found)
        return found[0][0]

    def ask(self, company: str, number: str) -> dict:
        """The answer over all its pages (each validated); gaps and state of the first."""
        args = {"family": "F10", "root": self.order(company, number)}
        result = None
        while True:
            page = self.table["plx.investigate"]["handler"](args, FULL)
            self.assertEqual(validate(page, CONTRACTS["tools"]["plx.investigate"]["output"],
                                      root=CONTRACTS, limit=3), [], page.get("error"))
            if result is None:
                result = page
            else:
                result["answer"] += [e for e in page["answer"] if e not in result["answer"]]
            cursor = (page.get("completeness") or {}).get("next_cursor")
            if not cursor:
                return result
            args = dict(args, cursor=cursor)

    @staticmethod
    def computed(result: dict) -> dict:
        return {e["name"]: e for e in result["answer"] if e["kind"] == "computed"}

    def test_a_wholly_invoiced_order_gets_line_and_total_margin_with_bc_citations(self):
        result = self.ask(tb.CO[0], "SO-3")
        got = self.computed(result)
        self.assertEqual((got["order_margin_line_1_revenue"]["value"],
                          got["order_margin_line_1_booked_cost"]["value"],
                          got["order_margin_line_1_margin"]["value"]), ("20", "8", "12"))
        self.assertEqual((got["order_margin_revenue"]["value"],
                          got["order_margin_booked_cost"]["value"], got["order_margin"]["value"],
                          got["order_margin_margin_pct"]["value"]), ("20", "8", "12", "60"))
        self.assertEqual(got["order_margin"]["currency"], "CAD")  # blank code, manufacturing book
        # Every input is a cited BC value of the same answer, re-read and claim-checked.
        cited = {e["name"]: e for e in result["answer"] if e["kind"] == "property"}
        for name in (got["order_margin_line_1_revenue"]["inputs"]
                     + got["order_margin_line_1_booked_cost"]["inputs"]):
            self.assertTrue(cited[name]["citation"]["l1_handle"].startswith("bc://"), name)
        self.assertTrue(result["claim_checks"]["passed"], result["claim_checks"])
        # Every BC set captured whole and the customer reached: no credit memo can be unseen,
        # so no credit gap stands and the answer is final.
        self.assertEqual(result["answerability"], {"state": "answerable", "reasons": []})
        # The landed-cost estimate is shown apart, never inside booked cost.
        landed = got["order_margin_landed_estimate"]
        self.assertIsNone(landed["value"])
        self.assertIn("never added", landed["steps"][0])
        self.assertNotIn("order_margin_landed_estimate",
                         got["order_margin_booked_cost"]["inputs"])
        # Reproducible: the same question gives the same records.
        again = self.computed(self.ask(tb.CO[0], "SO-3"))
        self.assertEqual(got["order_margin"]["digest"], again["order_margin"]["digest"])

    def test_missing_cost_lines_are_a_typed_gap_never_a_zero(self):
        result = self.ask(tb.CO[0], "SO-1")
        got = self.computed(result)
        self.assertEqual(got["order_margin_line_1_margin"]["value"], "28.6")
        self.assertEqual(got["order_margin_line_2_revenue"]["value"], "40")
        self.assertIsNone(got["order_margin_line_2_booked_cost"]["value"])
        self.assertIsNone(got["order_margin_line_2_margin"]["value"])
        self.assertIsNone(got["order_margin"]["value"])  # incomplete line set: no total
        self.assertEqual(result["answerability"]["state"], "partial")
        self.assertIn("cost_lines_missing", result["answerability"]["reasons"])
        gap = next(g for g in result["gaps"] if g["reason"] == "cost_lines_missing")
        self.assertIn("order_margin_line_2", gap["subject"])
        # The other company's SO-1 (same number, other amounts) never enters the figures.
        self.assertTrue(all(tb.CO[1] not in json.dumps(e.get("citation") or {})
                            for e in result["answer"]))

    def test_partial_invoices_and_two_currencies_give_no_margin(self):
        partial = self.ask(tb.CO[0], "SO-4")
        self.assertIsNone(self.computed(partial)["order_margin_line_1_margin"]["value"])
        self.assertIn("line_partially_invoiced", partial["answerability"]["reasons"])
        mixed = self.ask(tb.CO[0], "SO-2")
        got = self.computed(mixed)
        self.assertEqual(got["order_margin_line_1_revenue"]["currency"], "USD")
        self.assertIsNone(got["order_margin_line_1_margin"]["value"])
        gap = next(g for g in mixed["gaps"] if g["reason"] == "mixed_currency_no_cited_rate")
        self.assertEqual(gap["state"], "not_ingested")

    def test_a_lead_tier_invoice_never_enters_a_sum(self):
        """The finance-sum rule (decisions 2026-10-02): the same order with
        its invoice lines resting on a lead (as a Portal-rooted text match would) gets no
        revenue, margin or total."""
        def leads(args, evidence, *rest, **kw):
            evidence = dict(evidence, revenue=[dict(e, lead_basis=True)
                                               for e in evidence["revenue"]])
            return om.plan_run(args, evidence, *rest, **kw)
        self.table.pop("plx.investigate")
        oi.register(self.table, oi.InvestigateTools(
            self.table, rows=resolver(self.world), scope=oi.read_scope(self.world.base),
            engines={**oi.default_engines(), "order_margin": {"check": om.plan_check,
                                                              "run": leads}}))
        result = self.ask(tb.CO[0], "SO-3")
        got = self.computed(result)
        self.assertIsNone(got["order_margin_line_1_revenue"]["value"])
        self.assertIsNone(got["order_margin"]["value"])
        self.assertIn("lead_tier_invoice_excluded", result["answerability"]["reasons"])

    def test_an_applied_credit_memo_is_netted_by_document_type_and_cited(self):
        """SO-5: CM-5 (applied to SI-5) credits 2 of 10 FG-1 for 10 and the item ledger shows
        the return at cost 4.28; SO-6: CM-6's only line is a Charge naming FG-2's number."""
        result = self.ask(tb.CO[0], "SO-5")
        got = self.computed(result)
        self.assertEqual([got[f"order_margin_line_1_{k}"]["value"] for k in
                          ("revenue", "booked_cost", "margin")], ["40", "17.12", "22.88"])
        self.assertEqual([got[k]["value"] for k in ("order_margin_revenue",
                          "order_margin_booked_cost", "order_margin", "order_margin_margin_pct")],
                         ["40", "17.12", "22.88", "57.2"])
        self.assertEqual(result["answerability"], {"state": "answerable", "reasons": []})
        cited = {e["name"]: e for e in result["answer"] if e["kind"] == "property"}
        handles = {cited[n]["citation"]["l1_handle"] for n in
                   got["order_margin_line_1_revenue"]["inputs"]
                   + got["order_margin_line_1_booked_cost"]["inputs"]}
        memo = next(r for r in self.records if r["entity_set"] == "salesCreditMemos"
                    and r["fields"]["number"] == "CM-5")
        line = next(r for r in self.records if r.get("document_id") == memo["id"]
                    and r["fields"]["lineType"] == "Item")
        back = next(r for r in self.records if r["entity_set"] == "itemLedgerEntries"
                    and r["fields"]["documentNumber"] == "CM-5")
        for record in (line, back):   # the credit memo line and its return are cited
            self.assertTrue(any(h.startswith("bc://") and record["id"] in h for h in handles))
        self.assertTrue(result["claim_checks"]["passed"], result["claim_checks"])
        # A Charge credit is never goods, even when its number is an item's: the line stands
        # as invoiced and the total, which would leave the credit out, is not stated.
        charge = self.ask(tb.CO[0], "SO-6")
        got = self.computed(charge)
        self.assertEqual([got[f"order_margin_line_1_{k}"]["value"] for k in
                          ("revenue", "booked_cost", "margin")], ["40", "8", "32"])
        self.assertIsNone(got["order_margin"]["value"])
        self.assertIn("credit_memo_line_unattributed", charge["answerability"]["reasons"])

    def test_an_unattributable_or_uncaptured_credit_memo_is_a_typed_gap(self):
        # SO-7's customer has a posted credit memo naming no invoice: never netted, never
        # ignored; the figures stand net of the attributable memos (none) beside the gap.
        result = self.ask(tb.CO[0], "SO-7")
        got = self.computed(result)
        self.assertEqual((got["order_margin_line_1_revenue"]["value"],
                          got["order_margin"]["value"]), ("15", "8.58"))
        self.assertEqual(result["answerability"],
                         {"state": "partial", "reasons": ["credit_memo_unattributed"]})
        # Credit memos not captured for the company: SO-5's credit is unseen, so its revenue
        # reads as invoiced and the standing gap says so.
        self.open_world(BcWorld(self, fake_bc(self.records), entity_sets=tuple(
            s for s in CAPTURE_ENTITY_SETS if s != "salesCreditMemos")))
        result = self.ask(tb.CO[0], "SO-5")
        self.assertEqual(self.computed(result)["order_margin_line_1_revenue"]["value"], "50")
        gap = next(g for g in result["gaps"] if g["reason"] == "credit_memos_not_captured")
        self.assertEqual((gap["state"], result["answerability"]["state"]),
                         ("not_ingested", "partial"))

    def test_a_credit_memo_applied_to_no_invoice_is_counted_and_cited(self):
        """A posted memo of the order's customer BC states no invoiceId for whose
        invoiceNumber names no invoice (SO-10 / CM-10) or two of them (SO-11 / CM-11) is
        applied to nothing: counted and cited beside the gap and netted nowhere, so the
        figures stand gross and the answer is never a complete net margin. SO-13's customer
        holds LOOSE_MEMOS such memos and NUMBERED_MEMOS applied by number to invoices of no
        order: the answer counts exactly the former, cites a bounded few and stays one
        response. SO-12's memo names its own invoice: netted as an invoiceId would be."""
        cases = (("SO-10", 1, "CM-10"), ("SO-11", 1, "CM-11"),
                 ("SO-13", oracle_world.LOOSE_MEMOS, "CM-13S"))
        for number, count, memo_number in cases:
            result = self.ask(tb.CO[0], number)
            got = self.computed(result)
            self.assertEqual((got["order_margin_line_1_revenue"]["value"],
                              got["order_margin"]["value"]), ("15", "8.58"))
            self.assertEqual(result["answerability"],
                             {"state": "partial", "reasons": ["credit_memo_unattributed"]})
            self.assertNotIn("answer_page", result)
            counted = got["order_margin_unattributed_credits"]
            self.assertEqual(counted["value"], str(count))
            gap = next(g for g in result["gaps"] if g["reason"] == "credit_memo_unattributed")
            self.assertIn(counted["name"], gap["detail"])
            ids = {r["id"] for r in self.records if r["entity_set"] == "salesCreditMemos"
                   and r["fields"]["number"].startswith(memo_number)}
            self.assertTrue(0 < len(counted["basis"]) <= min(count, om.CITED_MEMOS))
            self.assertTrue(all(json.loads(b.split(":", 2)[2])["id"] in ids
                                for b in counted["basis"]), counted["basis"])
        netted = self.ask(tb.CO[0], "SO-12")
        got = self.computed(netted)
        self.assertEqual([got[f"order_margin_line_1_{k}"]["value"] for k in
                          ("revenue", "booked_cost", "margin")], ["40", "17.12", "22.88"])
        self.assertEqual(netted["answerability"], {"state": "answerable", "reasons": []})
        self.assertNotIn("order_margin_unattributed_credits", got)

    def test_a_reader_without_the_credit_memo_grant_keeps_the_capture_gap(self):
        audiences = {"bc:*": {READER}, "api:*": {READER}}
        for company in tb.CO[:2]:
            for unit in ("salesCreditMemos", "salesCreditMemoLines"):
                audiences[f"bc:{company}/{unit}"] = set()
        self.world.tools(ot.ReadPolicy(tenant="plx", audiences=audiences,
                                       markings={m: {READER} for m in store_ddl.MARKINGS}))
        self.open_world(self.world)
        # SO-5's credit is unseen by this reader: its figures are gross, never presented as
        # net of every credit, and the same gap stands where no memo exists (SO-3), so the
        # gap never tells this reader whether one does.
        # Both credit memo units are claimed complete for every company: a claim is a capture
        # fact, never a grant, so the hidden memos never become a confident absence.
        for number in ("SO-5", "SO-3"):
            result = self.ask(tb.CO[0], number)
            self.assertIn("credit_memos_not_captured", result["answerability"]["reasons"])
            self.assertIsNone(self.computed(result)["order_margin"]["value"])
        # SO-10's customer holds a memo applied to no invoice, withheld from this reader: the
        # capture gap stands for it, and no unattributed gap or citation reveals it.
        result = self.ask(tb.CO[0], "SO-10")
        self.assertIn("credit_memos_not_captured", result["answerability"]["reasons"])
        self.assertNotIn("credit_memo_unattributed", result["answerability"]["reasons"])
        memo = next(r for r in self.records if r["entity_set"] == "salesCreditMemos"
                    and r["fields"]["number"] == "CM-10")
        self.assertNotIn(memo["id"], json.dumps([result["answer"], result["gaps"]]))
        # Only the credit memo lines are withheld, the BC family is partial and both units
        # are claimed complete: the claims never stand in for lines this reader cannot see.
        world = self.failing_world((tb.CO[1], "generalLedgerEntries"))
        world.tools(ot.ReadPolicy(tenant="plx", audiences={
            "bc:*": {READER}, "api:*": {READER},
            f"bc:{tb.CO[0]}/salesCreditMemoLines": set()},
            markings={m: {READER} for m in store_ddl.MARKINGS}))
        self.open_world(world)
        result = self.ask(tb.CO[0], "SO-5")
        self.assertIn("credit_memos_not_captured", result["answerability"]["reasons"])

    def test_complete_credit_memo_units_of_the_company_clear_the_capture_gap(self):
        """Another company's G/L capture failed, so the BC family is partial; both credit
        memo units of the order's company are claimed complete: the family fallback keeps the
        gap, the per-unit claims clear it, and the figures are the same either way."""
        world = self.failing_world((tb.CO[1], "generalLedgerEntries"))
        self.open_world(world, claims=False)
        family = self.ask(tb.CO[0], "SO-3")
        self.assertIn("credit_memos_not_captured", family["answerability"]["reasons"])
        self.open_world(world)
        units = self.ask(tb.CO[0], "SO-3")
        self.assertNotIn("credit_memos_not_captured", units["answerability"]["reasons"])
        self.assertEqual([g for g in units["gaps"] if g["reason"] != "credit_memos_not_captured"],
                         [g for g in family["gaps"] if g["reason"] != "credit_memos_not_captured"])
        got = self.computed(units)
        self.assertEqual(got, self.computed(family))
        self.assertEqual([got[k]["value"] for k in ("order_margin_revenue",
                          "order_margin_booked_cost", "order_margin")], ["20", "8", "12"])
        # A stale BC capture: its claims never clear the gap (as in plx.process).
        with sqlite3.connect(world.store_path) as conn:
            [(raw,)] = conn.execute(
                "SELECT value FROM store_meta WHERE key = 'freshness'").fetchall()
            conn.execute("UPDATE store_meta SET value = ? WHERE key = 'freshness'",
                         (json.dumps([dict(f, stale=f["source"] == "bc")
                                      for f in json.loads(raw)]),))
        self.assertIn("credit_memos_not_captured",
                      self.ask(tb.CO[0], "SO-3")["answerability"]["reasons"])
        with sqlite3.connect(world.store_path) as conn:
            conn.execute("UPDATE store_meta SET value = ? WHERE key = 'freshness'", (raw,))
        # No claims at all (an older store): the answer is exactly the family-level one.
        with sqlite3.connect(world.store_path) as conn:
            [(raw,)] = conn.execute(
                "SELECT value FROM store_meta WHERE key = 'source_freshness'").fetchall()
            fresh = json.loads(raw)
            for facts in fresh["bc"].values():
                facts.pop("scopes", None)
            conn.execute("UPDATE store_meta SET value = ? WHERE key = 'source_freshness'",
                         (json.dumps(fresh),))
        bare = self.ask(tb.CO[0], "SO-3")
        self.assertEqual({k: bare[k] for k in ("answer", "gaps", "answerability")},
                         {k: family[k] for k in ("answer", "gaps", "answerability")})

    def test_a_partial_credit_memo_unit_keeps_the_gap_for_its_company_only(self):
        """The credit memo lines of the first company did not finish; the second company's
        units are complete. Its claims never vouch for the first company's order."""
        self.open_world(self.failing_world(
            (tb.CO[0], r"salesCreditMemos\([^)]*\)/salesCreditMemoLines")))
        kept = self.ask(tb.CO[0], "SO-3")
        self.assertIn("credit_memos_not_captured", kept["answerability"]["reasons"])
        cleared = self.ask(tb.CO[1], "SO-1")
        self.assertNotIn("credit_memos_not_captured", cleared["answerability"]["reasons"])

    def test_each_credit_memo_needs_its_own_return_on_the_item_ledger(self):
        """SO-8 (sold in BOX, base unit PCS): CM-8A's box is back on its own Sales Credit Memo
        ledger entry, CM-8B's went through a return receipt. CM-8A's entry never stands in
        for CM-8B's: the returned cost is unknown, not 6 (cost 54, margin 26)."""
        result = self.ask(tb.CO[0], "SO-8")
        got = self.computed(result)
        self.assertEqual(got["order_margin_line_1_revenue"]["value"], "80")
        self.assertIsNone(got["order_margin_line_1_booked_cost"]["value"])
        self.assertIsNone(got["order_margin"]["value"])
        self.assertIn("return_not_in_ledger", result["answerability"]["reasons"])

    def test_an_order_bc_no_longer_serves_gets_the_oracle_margin_under_one_identity(self):
        """SO-20 has no salesOrders record (BC deleted it once shipped and invoiced): rooted
        at the order its posted invoices name, one line per item, it gets the oracle's
        figures. A still-served order stays its own record and is never a second object:
        one SalesOrder per company and number, the other company's SO-20 apart."""
        with sqlite3.connect(self.world.store_path) as conn:
            twins = conn.execute(
                "SELECT substr(o.audience_key, 1, instr(o.audience_key, '/')), v.value "
                "FROM objects o JOIN object_values v ON v.object_id = o.object_id AND "
                "v.property = 'order_number' WHERE o.object_type = 'SalesOrder' "
                "GROUP BY 1, 2 HAVING COUNT(*) > 1").fetchall()
        self.assertEqual(twins, [])
        self.assertIn(":bc.sales_orders:", self.order(tb.CO[0], "SO-3"))
        root = self.order(tb.CO[0], "SO-20")
        self.assertIn(":bc.sales_invoices.order:", root)
        result = self.ask(tb.CO[0], "SO-20")
        got = self.computed(result)
        self.assertEqual(result["answerability"], {"state": "answerable", "reasons": []})
        self.assertTrue(result["claim_checks"]["passed"], result["claim_checks"])
        reading = oracle_world.reading(BcReads(oracle_world.RecordPort(self.records, tb.CO[0])),
                                       "SO-20", book="manufacturing")
        lines = {json.loads(got[f"order_margin_line_{n}_margin"]["basis"][0].split(":", 2)[2])
                 ["id"]: [got[f"order_margin_line_{n}_{k}"]["value"] for k in oracle_world.FIGURES]
                 for n in (1, 2)}
        self.assertEqual(lines, {key: [line[k] for k in oracle_world.FIGURES]
                                 for key, line in reading["lines"].items()})
        self.assertEqual(sorted(lines.values()), [["12", "5", "7"], ["20", "8", "12"]])
        self.assertEqual([got[k]["value"] for k in ("order_margin_revenue",
                          "order_margin_booked_cost", "order_margin", "order_margin_margin_pct")],
                         [reading["total"][k] for k in (*oracle_world.FIGURES, "margin_pct")])
        self.assertEqual(reading["total"]["margin_pct"], "59.38")
        self.assertEqual(self.computed(self.ask(tb.CO[1], "SO-20"))["order_margin"]["value"], "4")

    def test_an_order_bc_no_longer_serves_without_a_proven_line_set_has_no_figure(self):
        """SO-21 shipped 10 and invoiced 4: no ordered quantity, so the shipped ledger
        quantity is what proves a line whole; partly invoiced, it is incomplete. A reader
        who may not read the shipments cannot prove SO-20 whole. A salesOrders capture that
        did not finish never lets an order it may hold be taken for a deleted one: nothing
        stands in for an order of that company, while the other company, its salesOrders
        whole, keeps its SO-20 and SO-21."""
        partial = self.ask(tb.CO[0], "SO-21")
        got = self.computed(partial)
        self.assertEqual([got[f"order_margin_line_1_{k}"]["value"] for k in oracle_world.FIGURES]
                         + [got["order_margin"]["value"]], [None] * 4)
        self.assertIn("line_partially_invoiced", partial["answerability"]["reasons"])
        reading = oracle_world.reading(BcReads(oracle_world.RecordPort(self.records, tb.CO[0])),
                                       "SO-21", book="manufacturing")
        self.assertEqual([line["reason"] for line in reading["lines"].values()],
                         ["not_wholly_invoiced"])
        self.world.tools(ot.ReadPolicy(tenant="plx", audiences={
            "bc:*": {READER}, "api:*": {READER}, f"bc:{tb.CO[0]}/salesShipments": set()},
            markings={m: {READER} for m in store_ddl.MARKINGS}))
        self.open_world(self.world)
        hidden = self.ask(tb.CO[0], "SO-20")
        self.assertIsNone(self.computed(hidden)["order_margin_line_1_revenue"]["value"])
        self.assertIn("order_lines_incomplete", hidden["answerability"]["reasons"])
        world = self.failing_world((tb.CO[1], "salesOrders"))
        with sqlite3.connect(world.store_path) as conn:
            stood = {company: conn.execute(
                "SELECT COUNT(*) FROM objects WHERE binding_id IN ('bc.sales_invoices.order', "
                "'bc.sales_shipments.order') AND audience_key LIKE ?", (f"bc:{company}/%",)
                ).fetchone()[0] for company in tb.CO[:2]}
            [(report,)] = conn.execute(
                "SELECT value FROM store_meta WHERE key = 'build_report'").fetchall()
        self.assertEqual(stood, {tb.CO[0]: 2, tb.CO[1]: 0})
        self.assertIn(("source_capture_incomplete", "bc.sales_invoices.order"),
                      {(g["code"], g["where"]) for g in json.loads(report)["gaps"]})

    def test_a_customer_with_many_credit_memos_elsewhere_still_gets_an_answer(self):
        """SO-9's customer has MANY_MEMOS posted credit memos, each applied to another
        invoice: none is read or cited for this order, so the answer stays small."""
        result = self.ask(tb.CO[0], "SO-9")
        got = self.computed(result)
        self.assertEqual([got[k]["value"] for k in ("order_margin_revenue",
                          "order_margin_booked_cost", "order_margin")], ["20", "8", "12"])
        self.assertEqual(result["answerability"], {"state": "answerable", "reasons": []})
        self.assertFalse(any(e.get("object_type") == "SalesCreditMemo"
                             for e in result["answer"]))
        self.assertGreaterEqual(oracle_world.MANY_MEMOS, 10)


if __name__ == "__main__":
    unittest.main()
