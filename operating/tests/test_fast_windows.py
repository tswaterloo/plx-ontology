"""Time-windowed fast answers (receipt counts, invoiced totals) and same-name account
answers, at their output boundary over stubbed tool results: the window read into exact
days or declined, every figure cited (the gate's unbacked screen passes the reply), none
in the window said with the latest earlier record, anything cut, withheld or unexpected
declined to the agent."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import unittest

from operating import answer_gate, fast_answer, time_window
from operating.tests.test_fast_answer import _cell, _run

NOW = datetime(2026, 11, 4, 0, 30, tzinfo=timezone.utc)     # Nov 3 in the business day
TODAY = date(2026, 11, 3)


def _window(question):
    return time_window.window(fast_answer.fold(question), NOW)


def _screened(draft, question):
    """What the final reply's screen would drop: the reply's figures no value backs."""
    values = [v["value"] for v in draft.values]
    return answer_gate.unbacked(" ".join(draft.sentences), values, question=question)


class WindowGrammarTests(unittest.TestCase):
    def test_each_window_is_exact_days_through_today(self):
        cases = {
            "how many purchase receipts did we post in BC in the last 90 days?":
                (date(2026, 8, 6), TODAY, "from Aug 6 through Nov 3"),
            # BIZ-009: N days ending today, today counted (30 days, never 31)
            "# BC receipts last 30 days?": (date(2026, 10, 5), TODAY, "from Oct 5 through Nov 3"),
            "receipts last 2 weeks": (date(2026, 10, 21), TODAY, None),
            "receipts last 3 months": (date(2026, 8, 4), TODAY, None),
            "receipts the last month": (date(2026, 10, 4), TODAY, None),   # rolling
            "receipts in the past week": (date(2026, 10, 28), TODAY, None),
            "how much have we invoiced Acme this quarter in BC?":
                (date(2026, 10, 1), TODAY, "this quarter (Oct 1 through Nov 3)"),
            "Acme BC invoices this qtr?": (date(2026, 10, 1), TODAY, None),
            "receipts last month": (date(2026, 10, 1), date(2026, 10, 31), None),
            "receipts last quarter": (date(2026, 7, 1), date(2026, 9, 30), None),
            "invoiced since 2026-09-01": (date(2026, 9, 1), TODAY, None),
            "invoiced since March 3": (date(2026, 3, 3), TODAY, None),
            "invoiced since december": (date(2025, 12, 1), TODAY, None),
            "receipts ytd": (date(2026, 1, 1), TODAY, None),
            "receipts yesterday": (date(2026, 11, 2), date(2026, 11, 2), None),
            "receipts in august": (date(2026, 8, 1), date(2026, 8, 31), None),
        }
        for question, (start, end, said) in cases.items():
            with self.subTest(question=question):
                found = _window(question)
                self.assertEqual((found.start, found.end), (start, end))
                if said:
                    self.assertEqual(found.said, said)

    def test_a_frame_it_does_not_read_is_no_window(self):
        for question in ("receipts between jan and feb", "receipts last 30 days and this month",
                         "receipts before march", "invoices next month", "receipts 2 weeks ago",
                         "since 2026-02-30", "receipts", "invoices last 0 days",
                         # BIZ-008: days no calendar holds are no window, never a crash
                         "receipts in january 0000", "receipts in december 9999",
                         "since 0000-01-01", "receipts last 9999 months"):
            with self.subTest(question=question):
                self.assertIsNone(_window(question))


class WindowRoutingTests(unittest.TestCase):
    def test_windowed_questions_name_their_intent_party_and_system(self):
        cases = {
            "how many purchase receipts did we post in BC in the last 90 days?":
                ("receipts_window", "", "bc"),
            "# BC receipts last 30 days?": ("receipts_window", "", "bc"),
            "any receipts in sage since march?": ("receipts_window", "", "sage"),
            "how much have we invoiced Acme Foods this quarter in BC?":
                ("invoiced_window", "Acme Foods", "bc"),
            "Acme Foods BC invoices this qtr?": ("invoiced_window", "Acme Foods", "bc"),
            "what did we bill Acme Foods last month": ("invoiced_window", "Acme Foods", None),
            "which sage account is Acme Foods - East?":
                ("customer_account", "Acme Foods - East", "sage"),
            "Acme Foods - East in sage - account #?":
                ("customer_account", "Acme Foods - East", "sage"),
        }
        for question, want in cases.items():
            with self.subTest(question=question):
                ask = fast_answer.understand(question, NOW)
                self.assertEqual((ask.intent, ask.mention, ask.system), want)
                self.assertTrue(fast_answer.serves(question))

    def test_a_window_it_cannot_read_a_negation_or_a_record_goes_to_the_agent(self):
        for question in ("how many receipts between jan and feb", "receipts this month",
                         "how many receipts not posted this month",
                         "how many receipts on PO100055 this month",
                         "how much have we invoiced Acme", "biggest receipts this month"):
            with self.subTest(question=question):
                ask = fast_answer.understand(question, NOW)
                self.assertFalse(ask is not None and ask.intent in fast_answer.WINDOW_INTENTS)


BC = "bc://production/c1/purchaseReceipts/{}@g"


class ReceiptsWindowTests(unittest.TestCase):
    """BC receipts dated by their own posting date, counted by one aggregate of their
    numbers (N=1/10/60), listed when few; none in the window said with the latest."""

    def world(self, dates, *, page=25, bindings=None, withheld=False, cut=False,
              aggregate=None, portal=None):
        handles = [BC.format(i) for i in range(len(dates))]
        rows = {h: {"row.postingDate": d, "row.number": str(107000 + i)}
                for i, (h, d) in enumerate(zip(handles, dates))}

        def answers(name, args):
            if name == "plx.find":
                start = int(args.get("cursor") or 0)
                chunk = handles[start:start + page]
                last = start + page >= len(handles)
                out = {"candidates": [{"object": {
                    "object_id": f"Receipt:{(bindings or {}).get(h, 'bc.purchase_receipts')}:"
                                 f"{h}", "object_type": "Receipt"},
                    "matched": {"raw": _cell(h[-12:], h, "native_key")}} for h in chunk],
                       "completeness": {"complete": last and not cut,
                                        "next_cursor": None if last or cut
                                        else str(start + page),
                                        "total": len(handles) if last else None}}
                if withheld:
                    out["withheld"] = {"objects": 1}
                return out
            if name == "plx.read":
                h, path = args["citation"], args["field_path"]
                return {"status": "resolved", "citation": h,
                        "fields": {path: _cell(rows[h][path], h, path)}}
            if name == "plx.aggregate":
                if args["filters"][0]["property"] != "bc_number":
                    return portal
                if aggregate is not None:
                    return aggregate
                numbers = set(args["filters"][0]["value"])
                cited = [h for h in handles if rows[h]["row.number"] in numbers]
                return {"groups": [{"key": {}, "value": str(len(cited)), "inputs": len(cited),
                                    "inputs_citations": [{"l1_handle": h,
                                                          "field_path": "row.number"}
                                                         for h in cited[:50]]}],
                        "partial": False, "completeness": {"complete": True},
                        "gaps": [{"reason": "citations_capped"}] if len(cited) > 50 else []}
            return None
        return answers, handles

    def ask(self, question):
        return fast_answer.understand(question, NOW)

    def test_the_window_is_counted_exactly_and_cited(self):
        question = "how many purchase receipts did we post in BC in the last 90 days?"
        for n in (1, 10, fast_answer.MAX_WINDOW_COUNTED):
            with self.subTest(n=n):
                inside = [(TODAY - timedelta(days=i % 80)).isoformat() for i in range(n)]
                older = ["2026-01-05", "2026-08-05"]       # the day before the window
                answers, handles = self.world(older + inside)
                draft, reads = _run(fast_answer.receipts_window, answers, self.ask(question))
                values = {v["name"]: v for v in draft.values}
                self.assertEqual(values["bc_receipt_count"]["value"], str(n))
                cited = [i["l1_handle"] for i in values["bc_receipt_count"]["calculation"][
                    "inputs"]]
                self.assertTrue(set(cited) <= set(handles[2:]))   # never the older ones
                said = " ".join(draft.sentences)
                self.assertIn(f"has {n} posted purchase receipt", said)
                self.assertIn("from Aug 6 through Nov 3", said)
                self.assertEqual("listed_j" in values, n == 10)
                self.assertNotIn(f"listed_{fast_answer.MAX_WINDOW_LISTED + 1}", values)
                self.assertEqual(_screened(draft, question), [])
                reads_of_dates = [a for t, a in reads.calls if t == "plx.read"
                                  and a["field_path"] == "row.postingDate"]
                self.assertEqual(len(reads_of_dates), n + 2)   # every receipt dated once

    def test_more_in_the_window_than_one_count_binds_declines_never_cut(self):
        question = "how many purchase receipts did we post in BC in the last 90 days?"
        answers, _ = self.world(["2026-10-01"] * (fast_answer.MAX_WINDOW_COUNTED + 10))
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.receipts_window, answers, self.ask(question))

    def test_none_in_the_window_is_said_with_the_latest_earlier_receipt(self):
        question = "how many purchase receipts did we post in BC in the last 30 days?"
        answers, handles = self.world(["2026-09-18", "2026-09-26", "2026-07-01"])
        draft, _ = _run(fast_answer.receipts_window, answers, self.ask(question))
        said = " ".join(draft.sentences)
        self.assertIn("no purchase receipts posted from Oct 5 through Nov 3 that you can see",
                      said)
        values = {v["name"]: v for v in draft.values}
        self.assertEqual(values["latest_receipt_posted"]["value"], "2026-09-26")
        self.assertEqual(values["latest_receipt"]["citations"][0]["l1_handle"], handles[1])
        self.assertEqual(_screened(draft, question), [])

    def test_a_cut_withheld_unexpected_or_miscounted_read_declines(self):
        question = "how many purchase receipts did we post in BC in the last 90 days?"
        dated = ["2026-10-01"] * 30
        cases = {
            "withheld": {"withheld": True},
            "cut": {"cut": True},
            "not bc": {"bindings": {BC.format(3): "portal.app_receipt"}},
            "count differs": {"aggregate": {"groups": [{"key": {}, "value": "29",
                                                        "inputs": 29, "inputs_citations": [
                {"l1_handle": BC.format(0)}]}], "partial": False,
                "completeness": {"complete": True}, "gaps": []}},
            "count partial": {"aggregate": {"groups": [], "partial": True}},
        }
        for case, extra in cases.items():
            with self.subTest(case=case):
                answers, _ = self.world(dated, **extra)
                with self.assertRaises(fast_answer.Unanswerable):
                    _run(fast_answer.receipts_window, answers, self.ask(question))
        answers, _ = self.world(["2026-10-01", "not a date"])
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.receipts_window, answers, self.ask(question))

    def test_a_sage_zero_is_said_only_over_a_whole_read(self):
        question = "how many receipts in sage this month?"
        zero = {"groups": [{"key": {}, "value": "0", "inputs": 0}], "partial": False,
                "completeness": {"complete": True},
                "gaps": [{"reason": "stale_source", "subject": "graph_mail"}]}
        answers, _ = self.world([], portal=zero)
        # a zero alone cites nothing: the agent answers it
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.receipts_window, answers, self.ask(question))
        self.assertTrue(fast_answer._zero_whole(zero, "portal_api"))
        own_stale = dict(zero, gaps=[{"reason": "stale_source", "subject": "portal_api"}])
        self.assertFalse(fast_answer._zero_whole(own_stale, "portal_api"))
        self.assertFalse(fast_answer._zero_whole(dict(zero, withheld=["date"]), "portal_api"))


INV = "bc://production/c1/salesInvoices/{}@g"


class InvoicedWindowTests(unittest.TestCase):
    """A customer's posted BC invoices in the window, every same-named card read, summed
    per currency in Decimal; a draft or a withheld amount never added."""

    def world(self, cards, *, withheld_amount=False):
        """``cards``: card number -> [(invoice date, status, amount, currency)]."""
        objects = {}
        invoices = {}
        for c, (number, rows) in enumerate(cards.items()):
            card = f"Customer:bc.customers:{c}"
            handle = f"bc://production/c1/customers/{c}@g"
            objects[card] = {"object_id": card, "object_type": "Customer",
                             "display": _cell("Acme Foods", handle, "row.displayName"),
                             "number": number, "handle": handle}
            for i, row in enumerate(rows):
                invoices[f"SalesInvoice:bc.sales_invoices:{c}-{i}"] = (card, INV.format(
                    f"{c}-{i}"), row)

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [{"object": {k: o[k] for k in
                                                   ("object_id", "object_type", "display")},
                                        "matched": {"binding": "bc.customers"}}
                                       for o in objects.values()],
                        "completeness": {"complete": True}}
            if name == "plx.read":
                o = next(o for o in objects.values() if o["handle"] == args["citation"])
                return {"status": "resolved", "citation": o["handle"],
                        "fields": {"row.number": _cell(o["number"], o["handle"])}}
            if name == "plx.around":
                (lo, hi) = (None, None)
                for f in args["filters"]:
                    if f["op"] == "gte":
                        lo = f["value"]
                    else:
                        hi = f["value"]
                held = [{"object_id": oid, "object_type": "SalesInvoice"}
                        for oid, (card, _h, row) in invoices.items()
                        if card == args["from"] and (lo is None or row[0] >= lo)
                        and (hi is None or row[0] < hi)]
                return {"objects": held, "completeness": {"complete": True}}
            if name == "plx.object":
                _card, h, (day, status, amount, currency) = invoices[args["object_id"]]
                values = [
                    {"property": "invoice_number", "value": _cell(h[-6:], h),
                     "citation": {"l1_handle": h, "field_path": "row.number"}},
                    {"property": "invoice_date", "value": day,
                     "citation": {"l1_handle": h, "field_path": "row.invoiceDate"}},
                    {"property": "status", "value": _cell(status, h, "row.status"),
                     "citation": {"l1_handle": h, "field_path": "row.status"}}]
                if not withheld_amount:
                    values.append({"property": "subtotal", "value": amount,
                                   "currency": currency,
                                   "citation": {"l1_handle": h,
                                                "field_path": "row.totalAmountExcludingTax"}})
                return {"values": values}
            return None
        return answers

    def test_every_same_named_card_is_read_and_summed_per_currency(self):
        question = "how much have we invoiced Acme Foods this quarter in BC?"
        ask = fast_answer.understand(question, NOW)
        draft, _ = _run(fast_answer.invoiced_window, self.world({
            "ACME01": [("2026-10-02", "Open", "100.10", "CAD"),
                       ("2026-10-20", "Paid", "0.20", "CAD"),
                       ("2026-10-21", "Draft", "999", "CAD"),
                       ("2026-09-30", "Paid", "5000", "CAD")],
            "C00042": [("2026-11-01", "Open", "40", "USD")],
            "ACME02": [("2026-07-30", "Paid", "58500", "CAD")]}), ask)
        values = {v["name"]: v for v in draft.values}
        self.assertEqual(values["invoiced_a_cad"]["value"], "100.3")     # Decimal, no draft
        self.assertEqual(values["invoiced_a_cad"]["currency"], "CAD")
        self.assertEqual(values["invoiced_b_usd"]["value"], "40")
        self.assertNotIn("invoiced_all_cad", values)        # two currencies: never added
        said = " ".join(draft.sentences)
        self.assertIn("more than one customer card named Acme Foods", said)
        self.assertIn("customer ACME02: no invoices posted in it (the latest before the window", said)
        self.assertIn("Draft or cancelled invoices in the window are not counted", said)
        self.assertEqual(values["last_invoice_c_date"]["value"], "2026-07-30")
        self.assertEqual(_screened(draft, question), [])

    def test_none_in_the_window_is_an_honest_none_per_card(self):
        question = "how much have we invoiced Acme Foods this quarter in BC?"
        ask = fast_answer.understand(question, NOW)
        draft, _ = _run(fast_answer.invoiced_window, self.world({
            "ACME01": [("2026-07-30", "Paid", "1", "CAD")]}), ask)
        said = " ".join(draft.sentences)
        self.assertIn("this quarter (Oct 1 through Nov 3)", said)
        self.assertIn("no invoices posted in it", said)
        self.assertEqual(_screened(draft, question), [])

    def test_a_withheld_amount_or_another_system_declines(self):
        question = "how much have we invoiced Acme Foods this quarter in BC?"
        world = self.world({"ACME01": [("2026-10-02", "Open", "1", "CAD")]},
                           withheld_amount=True)
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.invoiced_window, world, fast_answer.understand(question, NOW))
        sage = fast_answer.understand("how much have we invoiced Acme Foods this quarter in "
                                      "sage?", NOW)
        with self.assertRaises(fast_answer.Unanswerable):
            _run(fast_answer.invoiced_window, world, sage)


class CustomerAccountTests(unittest.TestCase):
    def test_every_card_of_the_name_is_named_never_one_picked(self):
        question = "which sage account is Acme Foods - East?"
        for n in (1, 3):
            with self.subTest(n=n):
                handles = [f"api://sage.ar_customer/{i}@g" for i in range(n)]

                def answers(name, args):
                    if name == "plx.find":
                        return {"candidates": [{"object": {
                            "object_id": f"Customer:portal.sage_ar_customer:{i}",
                            "object_type": "Customer",
                            "display": _cell("ACME FOODS - EAST", h, "row.namecust")},
                            "matched": {"binding": "portal.sage_ar_customer"}}
                            for i, h in enumerate(handles)],
                            "completeness": {"complete": True}}
                    if name == "plx.read":
                        h = args["citation"]
                        return {"status": "resolved", "citation": h, "fields": {
                            "row.idcust": _cell(f"ACME0{handles.index(h) + 1}", h,
                                                "row.idcust")}}
                    return None
                draft, _ = _run(fast_answer.customer_account, answers,
                                fast_answer.understand(question, NOW))
                said = " ".join(draft.sentences)
                for i in range(n):
                    self.assertIn(f"ACME0{i + 1}", said)
                if n > 1:
                    self.assertIn("the name alone does not say which account", said)
                self.assertEqual(_screened(draft, question), [])


if __name__ == "__main__":
    unittest.main()
