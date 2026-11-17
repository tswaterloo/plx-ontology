"""An amount a document gives no currency for: shown as
"$ (currency not stated)", or with the one linked vendor's or customer's BC currency
labelled as coming from that record; never guessed, never converted. A value with no
currency basis field is shown exactly as before."""
from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from operating import currency_display as cd
from operator_api import backend as be
from operator_api import history
from operator_api import models as m
from operator_api.serving import plain
from operator_api.tests.test_backend_fixture import _View

DOC = "sharepoint://invoices/INV-77.pdf@g-1"
BC = "bc://vendors/V-1@g-1"


def cite(handle=DOC):
    return {"l1_handle": handle, "field_path": "amount", "digest": "0" * 64,
            "lineage": "document_text"}


def amount(**extra):
    return {"property": "amount", "value_type": "money", "value": "1234.5",
            "citation": cite(), "markings": [], **extra}


class CounterpartyView(_View):
    def __init__(self, cards):
        super().__init__(())
        self.cards, self.calls = cards, []
        self._request = contextlib.nullcontext

    def _call(self, name, args):
        self.calls.append((name, args))
        return self.cards.get(args["object_id"], {})


def vendor_card(code):
    return {"object": {"object_type": "Supplier"}, "values": (
        [{"property": "currency_code", "value_type": "text", "value": code,
          "citation": cite(BC)}] if code is not None else [])}


def page_for(value, hops=()):
    return {"object_id": "Invoice:sp:inv-77", "status": "read", "card": {
        "object": {"object_type": "Invoice", "display": "Invoice 77"}, "values": [value]},
        "around": {"hops": [{"result": {"objects": list(hops)}}]}}


def fact_text(page, view):
    backend = be.WorkspaceBackend(lambda s: None, data_label="t")
    with mock.patch.object(backend, "freshness", return_value=m.Freshness()):
        model = be._page_model(backend, view, page)
    return [f.text for f in model.facts if f.text.startswith("Amount")]


class UnstatedAmountShown(unittest.TestCase):
    def test_the_record_page_says_the_currency_is_not_stated(self):
        for basis in ({"currency_basis": "unstated"}, {"currency_source": "unstated"}):
            with self.subTest(basis=basis):
                got = fact_text(page_for(amount(currency=None, **basis)),
                                CounterpartyView({}))
                self.assertEqual(got, ["Amount: 1,234.50 $ (currency not stated)"])

    def test_one_linked_vendor_gives_its_currency_labelled_as_the_vendors(self):
        hop = {"object_id": "Supplier:bc:V-1", "object_type": "Supplier", "display": "Acme"}
        view = CounterpartyView({"Supplier:bc:V-1": vendor_card("usd")})
        got = fact_text(page_for(amount(currency=None, currency_basis="unstated"), [hop]),
                        view)
        self.assertEqual(got, ["Amount: 1,234.50 USD (from vendor record)"])
        self.assertEqual(len(view.calls), 1)

    def test_a_customer_is_labelled_a_customer(self):
        hop = {"object_id": "Customer:bc:C-1", "object_type": "Customer"}
        card = {"object": {"object_type": "Customer"}, "values": [
            {"property": "currency_code", "value": "CAD", "citation": cite(BC)}]}
        got = fact_text(page_for(amount(currency=None, currency_basis="unstated"), [hop]),
                        CounterpartyView({"Customer:bc:C-1": card}))
        self.assertEqual(got, ["Amount: 1,234.50 CAD (from customer record)"])

    def test_two_counterparties_or_no_code_or_a_bad_code_state_nothing(self):
        a = {"object_id": "Supplier:bc:V-1", "object_type": "Supplier"}
        b = {"object_id": "Customer:bc:C-1", "object_type": "Customer"}
        value = amount(currency=None, currency_basis="unstated")
        for name, hops, cards in (
                ("two", [a, b], {"Supplier:bc:V-1": vendor_card("USD"),
                                 "Customer:bc:C-1": vendor_card("CAD")}),
                ("blank code (local currency, not read here)", [a],
                 {"Supplier:bc:V-1": vendor_card(None)}),
                ("not a code", [a], {"Supplier:bc:V-1": vendor_card("dollars")}),
                ("other record kinds", [{"object_id": "Item:1", "object_type": "Item"}], {})):
            with self.subTest(name):
                self.assertEqual(fact_text(page_for(value, hops), CounterpartyView(cards)),
                                 ["Amount: 1,234.50 $ (currency not stated)"])

    def test_no_basis_field_or_a_stated_currency_is_shown_as_before(self):
        view = CounterpartyView({})
        # no basis field, no currency: the amount shows bare, as it always did
        self.assertEqual(fact_text(page_for(amount(currency=None)), view),
                         ["Amount: 1234.5"])
        self.assertEqual(fact_text(page_for(amount(currency="CAD")), view),
                         ["Amount: 1234.5"])   # no record currency fact: unchanged
        self.assertEqual(view.calls, [])
        # a currency on the value is never overridden by the unstated mark
        self.assertFalse(cd.is_unstated(amount(currency="CAD", currency_basis="unstated")))
        self.assertFalse(cd.is_unstated(amount(currency=None, currency_basis="document")))
        self.assertFalse(cd.is_unstated(amount(currency=None)))

    def test_history_and_cited_values_say_it_too_and_nothing_else_changes(self):
        shown = history.value_text(amount(currency=None, currency_basis="unstated"))
        self.assertEqual(shown, "1,234.50 $ (currency not stated)")
        self.assertEqual(history.value_text(amount(currency="CAD")), "1,234.50 CAD")
        self.assertEqual(history.value_text(amount(currency=None)), "1,234.50")
        self.assertEqual(plain({"value": "617.61", "currency": None,
                                "currency_basis": "unstated"}),
                         "617.61 $ (currency not stated)")
        self.assertEqual(plain({"value": "617.61", "currency": "USD"}), "617.61 USD")
        self.assertEqual(plain({"value": "617.61"}), "617.61")

    def test_the_wording_never_converts_or_invents_a_code(self):
        self.assertEqual(cd.currency_words(), "$ (currency not stated)")
        self.assertEqual(cd.currency_words({"currency": "US", "kind": "vendor"}),
                         "$ (currency not stated)")
        self.assertEqual(cd.currency_words({"currency": "USD", "kind": "bank"}),
                         "$ (currency not stated)")
        self.assertIsNone(cd.counterparty_currency("USD", "Item"))


if __name__ == "__main__":
    unittest.main()
