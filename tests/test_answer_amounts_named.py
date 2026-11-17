"""Ask answers name customers and vendors and give every amount its currency (business rule,
2026-11-16: an overdue-customers answer showed codes and a bare "549477.34").

The source carries the facts, the gate only verifies:

* ``plx.aggregate`` groups keyed by a party's code carry ``party_name`` (only a name the
  reader may read; none when any record holding the code, readable or not, names another
  party) and each money group its ``currency_basis``: the document's currency, else the
  counterparty's Business Central record's, never guessed.
* The final reply's sentences are never rewritten for it: an amount a tool returned that
  the reply writes with no currency beside it adds one footnote line at the end.
"""
from __future__ import annotations

import json
import time
import unittest
from decimal import Decimal

from operating import agent_session, answer_gate
from operating.ontology_tools_policy import load_contracts
from ontology.json_contract import validate
from tests import test_ontology_tools_aggregate as agg

SALES_INVOICE = "SalesInvoice:portal.bc_invoice_snapshot:{}"
CUSTOMER = "Customer:bc.customers:{}"
FOOT = answer_gate.CURRENCY_FOOTNOTE


def setUpModule():
    agg.setUpModule()


def amounts(*values):
    return frozenset(Decimal(v) for v in values)


class GateOnlyVerifies(unittest.TestCase):
    """No number replaced, no label written."""

    def test_no_sentence_is_edited_and_a_bare_amount_gets_one_footnote(self):
        held = amounts("1500", "617.61", "549477.34", "10000", "1100.00", "3", "12.3")
        for text in ("There are 3 open POs.", "Acme has 1500 units and owes 1500 CAD.",
                     "Acme owes 617.61 $ (currency not stated).", "Acme owes $549,477.34 CAD.",
                     "Acme owes 549,477.34 (CAD) past due.", "Customer 10000 owes 10000 USD.",
                     "The rate is 12.3 per unit.", "Acme owes USD 1,100.00 in total."):
            with self.subTest(text=text):
                self.assertEqual(answer_gate.currency_footnote(text, held), text)
        for text in ("Northern owes 1100.00.", "Acme owes $617.61 in total.",
                     "Acme owes 1,500.00 (from customer record)."):
            with self.subTest(text=text):
                out = answer_gate.currency_footnote(text, held)
                self.assertEqual(out, text + "\n\n" + FOOT)
                # idempotent: once, never twice
                self.assertEqual(answer_gate.currency_footnote(out, held), out)
        # a figure no tool returned as an amount is no amount; nothing to verify
        self.assertEqual(answer_gate.currency_footnote("Acme owes 99.", held), "Acme owes 99.")
        self.assertEqual(answer_gate.currency_footnote("Acme owes 99.", frozenset()),
                         "Acme owes 99.")

    def events(self, groups):
        return [{"type": "tool_call", "name": "plx.aggregate",
                 "result": json.dumps({"groups": groups})}]

    def test_returned_amounts_reads_money_only(self):
        found = answer_gate.returned_amounts(self.events([
            {"key": {}, "value": "150", "currency": "CAD", "currency_basis": "document"},
            {"key": {}, "value": "7", "inputs": 7}]) + [
            {"type": "tool_call", "name": "plx.object", "is_error": True,
             "result": json.dumps({"value": "9", "currency": "USD"})},
            {"type": "tool_call", "name": "plx.object", "result": "not json"}])
        self.assertEqual(found, amounts("150"))

    def test_the_gate_appends_the_line_without_touching_the_reply(self):
        from evals.tests import test_final_reply_gate as frg
        doc = frg.DOC
        events = self.events([{"key": {}, "value": "617.61", "currency": "CAD",
                               "currency_basis": "document"}]) + [
            frg._submit("answered", passed=[("617.61", doc, "row.content_text")])]
        answer = frg._answer("Acme owes 617.61.", [frg._value("owed", "617.61", doc)])
        text, parsed, error, report = frg._gated(answer, events, "What does Acme owe?",
                                                 prose="Acme owes 617.61.")
        self.assertIsNone(error)
        self.assertEqual(parsed["text"], "Acme owes 617.61.\n\n" + FOOT)
        prose = frg.sc._JSON_FENCE.sub("", text).strip()
        self.assertEqual(prose, "Acme owes 617.61.\n\n" + FOOT)
        self.assertTrue(report["currency_footnote"])
        # written with the currency the gate checked: returned exactly as written
        events[-1]["result"]["claims"][0]["value_claims"][0]["currency"] = "CAD"
        value = dict(frg._value("owed", "617.61", doc), currency="CAD")
        answer = frg._answer("Acme owes 617.61 CAD.", [value])
        final = "Acme owes 617.61 CAD.\n```json\n" + json.dumps(answer) + "\n```\n"
        text, report = agent_session.gate_final_reply(final, events, "What does Acme owe?")
        self.assertEqual(text, final)
        self.assertNotIn("currency_footnote", report)


def _identifier(store, oid, code):
    handle, lineage = store.handles[oid]
    store.conn.execute(
        "INSERT INTO identifiers (identifier_kind, normalized, raw, role, object_id, "
        "property, l1_handle, field_path, lineage, markings, recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("customer_erp_code", code, code, "primary", oid, "erp_code", handle,
         "row.number", lineage, "[]", agg.T0))


class AggregateNamesPartiesAndCurrencies(agg._Base):
    def setUp(self):
        super().setUp()
        s = self.store
        self.invoices = []
        for index, (code, amount, currency) in enumerate((
                ("ACME01", "100.00", "CAD"), ("ACME01", "50.00", "CAD"),
                ("ZED02", "70.00", "CAD"), ("TWIN03", "5.00", "CAD"),
                ("NONE04", "1.00", "CAD"), ("DIVX06", "9.00", "CAD"),
                ("HIDE07", "4.00", "CAD"),
                # the documents state no currency: the customer's BC record does
                ("USDC08", "20.00", None), ("USDC08", "2.50", None),
                # neither does: no value, never a guess
                ("BARE09", "3.00", None),
                # two parties hold the code: neither's currency is the documents'
                ("AMBI10", "6.00", None))):
            oid = s.obj(SALES_INVOICE.format(f"i{index}"))
            s.val(oid, "customer_number", "identifier", code)
            s.val(oid, "total_amount", "money", amount, currency=currency)
            self.invoices.append(oid)
        for native, code, name, currency in (
                ("c1", "ACME01", "Acme Foods Ltd", None), ("c2", "ZED02", "Zed Corp", None),
                ("c3", "TWIN03", "Twin One", None), ("c4", "TWIN03", "Twin Two", None),
                # one customer in three systems, each its own spelling
                ("c5", "DIVX06", "Diversified", None),
                ("portal.sage_ar_customer:c6", "DIVX06",
                 "Diversified Hospitality Solutions, Ltd.", None),
                ("portal.app_customer:c7", "DIVX06", "Diversified", None),
                # a readable Portal customer and a BC one (unreadable to sales) under
                # another party's name: the code is not unique for anyone
                ("portal.app_customer:c8", "HIDE07", "Hidden Shared Name", None),
                ("c9", "HIDE07", "Completely Other Party", None),
                ("c10", "USDC08", "US Buyer Inc", "USD"),
                ("c11", "BARE09", "Bare Co", None),
                ("c12", "AMBI10", "Alpha Ltd", "USD"), ("c13", "AMBI10", "Omega Inc", "USD")):
            oid = s.obj(f"Customer:{native}" if ":" in native else CUSTOMER.format(native))
            s.val(oid, "name", "text", name)
            if currency:
                s.val(oid, "currency", "text", currency)
            _identifier(s, oid, code)

    def run_agg(self, actor=agg.STAFF, **extra):
        return self.agg(actor, self.invoices, {"op": "sum", "property": "total_amount"},
                        group_by=["customer_number"], **extra)

    @staticmethod
    def by_code(out):
        return {g["key"]["customer_number"]["untrusted_text"]: g for g in out["groups"]}

    @staticmethod
    def name(group):
        return (group.get("party_name") or {}).get("customer_number", {}).get("untrusted_text")

    def test_each_code_group_carries_its_name_and_currency_basis(self):
        out = self.run_agg()
        groups = self.by_code(out)
        self.assertEqual({code: self.name(g) for code, g in groups.items()},
                         {"ACME01": "Acme Foods Ltd", "ZED02": "Zed Corp", "TWIN03": None,
                          "NONE04": None, "DIVX06": "Diversified", "HIDE07": None,
                          "USDC08": "US Buyer Inc", "BARE09": "Bare Co", "AMBI10": None})
        acme = groups["ACME01"]
        self.assertEqual((acme["value"], acme["currency"], acme["currency_basis"]),
                         ("150", "CAD", "document"))
        self.assertTrue(acme["party_name"]["customer_number"]["origin"]["citation"]
                        .startswith("bc://"))
        usd = groups["USDC08"]
        self.assertEqual((usd["value"], usd["currency"], usd["currency_basis"]),
                         ("22.5", "USD", "counterparty_record"))
        for code in ("BARE09", "AMBI10"):
            self.assertIsNone(groups[code]["value"])
            self.assertNotIn("currency", groups[code])
        # the tool's own contract holds the new fields
        contracts = load_contracts()
        self.assertEqual(validate(out, contracts["tools"]["plx.aggregate"]["output"],
                                  root=contracts), [])

    def test_a_reader_sees_only_names_and_currencies_they_may_read(self):
        # the sales reader reads Portal records but no Business Central one (POLICY)
        out = self.run_agg(agg.SALES)
        groups = self.by_code(out)
        self.assertEqual({code: self.name(g) for code, g in groups.items()},
                         {"ACME01": None, "ZED02": None, "TWIN03": None, "NONE04": None,
                          "DIVX06": "Diversified", "HIDE07": None, "USDC08": None,
                          "BARE09": None, "AMBI10": None})
        text = json.dumps(out)
        for hidden in ("Acme Foods", "Zed Corp", "Twin", "Completely Other", "US Buyer",
                       "Diversified Hospitality", "Hidden Shared Name"):
            self.assertNotIn(hidden, text)
        # the BC customer's currency is unreadable: no value, never a guessed currency
        self.assertIsNone(groups["USDC08"]["value"])
        self.assertNotIn('"USD"', text)

    def test_cardinality_1_10_60_no_group_lost_or_slowed(self):
        s = self.store
        for index in range(60):
            code = f"BULK{index:02d}"
            oid = s.obj(SALES_INVOICE.format(f"b{index}"))
            s.val(oid, "customer_number", "identifier", code)
            s.val(oid, "total_amount", "money", "1.00", currency="CAD")
            self.invoices.append(oid)
            cust = s.obj(CUSTOMER.format(f"bulk{index}"))
            s.val(cust, "name", "text", f"Bulk Customer {index}")
            _identifier(s, cust, code)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                ids = [i for i in self.invoices if "b" in i.rsplit(":", 1)[1]][:n]
                started = time.monotonic()
                seen, cursor = {}, None
                while True:
                    extra = {"cursor": cursor} if cursor else {}
                    out = self.agg(agg.STAFF, ids, {"op": "sum", "property": "total_amount"},
                                   group_by=["customer_number"], **extra)
                    for g in out["groups"]:
                        code = g["key"]["customer_number"]["untrusted_text"]
                        self.assertNotIn(code, seen)
                        seen[code] = self.name(g)
                    cursor = out["completeness"].get("next_cursor")
                    if not cursor:
                        break
                self.assertEqual(len(seen), n)
                self.assertTrue(all(seen[c] == f"Bulk Customer {int(c[4:])}" for c in seen))
                self.assertLess(time.monotonic() - started, 30)


if __name__ == "__main__":
    unittest.main()
