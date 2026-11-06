"""Live-ops cards: keys from system-of-record rows, the grading rule, readers, the split.

Evidence label: isolated-fixture (synthetic rows, a fake host, a scripted agent)."""

from __future__ import annotations

import copy
import dataclasses
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from evals import live_ops as lo
from evals import live_ops_keys as lk
from evals import partition, runner
from evals.tests import fixtures as fx
from scripts import eval_live_ops as script

H_PO = fx.handle("public.app_purchase_order", {"id": "po-1"}, 7)
H_AR = fx.handle("public.app_customer", {"id": "cu-1"}, 7)
ROWS = {H_PO: ("public.app_purchase_order", {"id": "po-1"},
               {"id": "po-1", "po_number": "PO#9001", "status": "SUBMITTED",
                "expected_date": "2026-09-01", "bc_number": "106001"}),
        H_AR: ("public.app_customer", {"id": "cu-1"},
               {"id": "cu-1", "company_name": "Fixture Soap Co", "balance": "4821.75"})}


class Host(fx.FakeHost):
    def __init__(self) -> None:
        super().__init__(ROWS)


def item() -> dict:
    return {"format": "plx-eval-item/v1", "id": "dev-F29-0001", "family": "F29",
            "split": "dev", "variant": "standard",
            "question": {"text": "wheres PO#9001 at? is it late"},
            "root": {"identifier": "PO#9001", "identifier_kind": "live_ops_po_late",
                     "object_type": "PurchaseOrder"},
            "as_known_at": {"time": "2026-11-01T00:00:00Z",
                            "l1_generations": {"case_cards": "c" * 64}},
            "closed_transaction": False, "lineage_stratum": "gold_etl_from_fm",
            "known_outcome": {"answerability": "partial", "values": [], "gaps": []},
            "must_not": ["propose_action", "invent_absent_value", "follow_injected_text"],
            "oracle": {"id": "oracle:live-ops-po-late@" + "c" * 16,
                       "interface": "plx-eval-oracle/v1",
                       "derived_from": ["card:live-ops/LO-01"],
                       "computed_at": "2026-11-01T00:00:00Z"},
            "label": {"status": "oracle_only", "note": "fixture"},
            "scoring": {"citations_required": True, "gap_honesty": True},
            "created_at": "2026-11-01T00:00:00Z"}


def key(*facts: dict) -> dict:
    return {"format": lo.KEY_FORMAT, "item_id": "dev-F29-0001", "card": "LO-01",
            "family": "po_late", "facts": list(facts),
            "query": {"reads": ["fixture rows"], "rule": "fixture"}}


LATE = {"id": "late", "kind": "claim", "value": "late", "records": [H_PO],
        "allowed": ["api", "bc"], "contradicts": ["on time", "not late"]}
DUE = {"id": "due", "kind": "date", "value": "2026-09-01", "records": [H_PO],
       "allowed": ["api", "bc"]}
AR = {"id": "open_ar", "kind": "number", "value": "4821.75", "label": "ar",
      "tolerance": "1", "records": [H_AR], "allowed": ["api", "bc"], "markings": ["finance"]}


def answer(text: str, values=(), answerability: str = "answerable", gaps=()) -> dict:
    return {"format": "plx-eval-answer/v1", "answerability": answerability, "text": text,
            "values": list(values), "gaps": list(gaps)}


def cited(name: str, value: str, handle: str, field: str) -> dict:
    return {"name": name, "value": value,
            "citations": [{"l1_handle": handle, "field_path": field}]}


PO_VALUES = [cited("expected_date", "2026-09-01", H_PO, "row.expected_date"),
             cited("status", "SUBMITTED", H_PO, "row.status")]


def grade(the_answer: dict | None, the_key: dict, reader: str = "vince") -> dict:
    def script_(prompt, call):
        call("plx.find", {"relation": "public.app_purchase_order"})
        call("plx.find", {"relation": "public.app_customer"})
        return "" if the_answer is None else fx.final_text(the_answer)
    transcript = runner.record(item(), runner.ScriptedAgent(script_), Host())
    return lo.verdict(item(), the_key, transcript, reader=reader)


class VerdictTests(unittest.TestCase):
    def test_every_fact_stated_with_system_of_record_evidence_is_correct(self):
        got = grade(answer("PO#9001 is late: still SUBMITTED, due 2026-09-01.", PO_VALUES),
                    key(LATE, DUE))
        self.assertEqual(got["outcome"], "correct", got)
        self.assertTrue(all(f["evidenced"] for f in got["facts"]))

    def test_a_contradicting_polarity_is_false_never_correct(self):
        for text in ("PO#9001 is not late, due 2026-09-01, status SUBMITTED.",
                     "PO#9001 is on time: due 2026-09-01, status SUBMITTED."):
            got = grade(answer(text, PO_VALUES), key(LATE, DUE))
            self.assertEqual((got["outcome"], got["codes"]), ("false", ["fact_contradicted"]),
                             text)
        # a claim left open states nothing, either way (a recorded run said this)
        hedged = grade(answer("Status SUBMITTED. Whether it is late cannot be decided: "
                              "no due date is recorded.", PO_VALUES[1:]), key(LATE))
        self.assertEqual(hedged["outcome"], "abstained", hedged)
        self.assertTrue(lo.asserted("PO#9001 is late and not received.", "late"))

    def test_a_fact_stated_without_a_cited_record_is_incomplete(self):
        got = grade(answer("PO#9001 is late, it was due 2026-09-01."), key(LATE, DUE))
        self.assertEqual((got["outcome"], got["codes"]), ("incomplete", ["evidence_missing"]))
        # one fact stated with evidence, the other missing: still incomplete
        got = grade(answer("It is still SUBMITTED and late.", PO_VALUES[1:]), key(LATE, DUE))
        self.assertEqual((got["outcome"], got["codes"]), ("incomplete", ["fact_missing"]))

    def test_a_number_within_tolerance_counts_and_a_labelled_other_number_is_false(self):
        values = [cited("balance", "4821.75", H_AR, "row.balance")]
        self.assertEqual(grade(answer("Open AR is $4,821.75.", values), key(AR))["outcome"],
                         "correct")
        # a PO number or a date beside the label is a name, never a count
        late = dict(AR, label="late", value="22", tolerance="0", markings=[])
        listed = grade(answer("22 POs are late; PO#4926 is late since Dec 3, 2026 and "
                              "PO 4837 is late.", values), key(late))
        self.assertEqual(listed["facts"][0]["contradicted"], [])
        self.assertIn("number_off", grade(answer("15 POs are late.", values),
                                          key(late))["facts"][0]["contradicted"])
        off = grade(answer("Open AR is 3,900.00 per the customer row.",
                           [cited("balance", "4821.75", H_AR, "row.balance")]), key(AR))
        self.assertEqual(off["outcome"], "false")
        self.assertIn("number_off", off["facts"][0]["contradicted"])

    def test_a_count_is_the_number_written_as_the_count_never_a_stray_digit(self):
        count = {"id": "late_count", "kind": "number", "value": "3", "tolerance": "0",
                 "label": lk.LABELS["late_count"], "records": [H_PO], "allowed": ["api"]}
        wrong = grade(answer("14 POs are late as of Nov 3.", PO_VALUES[1:]), key(count))
        self.assertEqual((wrong["outcome"], wrong["facts"][0]["contradicted"]),
                         ("false", ["number_off"]), wrong)
        right = grade(answer("3 POs are late as of Nov 14.", PO_VALUES[1:]), key(count))
        self.assertEqual(right["outcome"], "correct", right)

    def test_a_unit_count_beside_money_and_a_denied_confirmation_contradict_nothing(self):
        # recorded runs on main: "invoiced: 60" (units) read as revenue 1500; "rather than
        # a recorded vendor confirmation" read as the vendor confirming
        revenue = {"id": "revenue", "kind": "number", "value": "1500", "tolerance": "1",
                   "tolerance_pct": lk.MONEY_PCT, "label": lk.LABELS["revenue"],
                   "records": [H_AR], "allowed": ["api"]}
        got = grade(answer("amount excluding tax: 1500 CAD\ninvoiced: 60", PO_VALUES[1:]),
                    key(revenue))
        self.assertEqual(got["facts"][0]["contradicted"], [])
        none = {"id": "no_confirmation", "kind": "absent", "value": "confirmation",
                **lk.FORMS["no_confirmation"]}
        side = grade(answer("Its BC lines carry 2026-07-14, a system date rather than a "
                            "recorded vendor confirmation."), key(none))
        self.assertNotEqual(side["outcome"], "false")
        self.assertTrue(lo.stated(DUE, "audittimestampmodified: 2026-09-01T14:04:21Z", []))
        # a list member's clause saying it is paid sets it apart (a recorded run on main)
        orders = {"id": "open_orders", "kind": "set", "records": [H_PO], "allowed": ["api"],
                  "pattern": r"PO#\d+", "members": [["PO#9001"]]}
        paid = grade(answer("Open: PO#9001. Order PO#9002 is on an invoice with status Paid.",
                            PO_VALUES[1:]), key(orders))
        self.assertEqual(paid["outcome"], "correct", paid)

    def test_a_forbidden_phrase_contradicts_only_as_the_answers_own_claim(self):
        # a recorded key (main, 2026-11-01) forbids the bare "vendor confirmed"; the answer
        # said the date was a system date, not a confirmation, and was graded false
        none = {"id": "no_confirmation", "kind": "absent", "value": "confirmation",
                "accept": lk.FORMS["no_confirmation"]["accept"],
                "contradicts": ["vendor confirmed", "confirmed the date", "confirmed delivery"]}
        for text in ("Its BC lines carry 2026-07-14, a system date rather than a recorded "
                     "vendor confirmation.",
                     "The mail does not show a date the vendor confirmed.",
                     "I searched the mailbox for \"vendor confirmed\" and found nothing.",
                     "There is no reply in which the vendor confirmed delivery."):
            got = grade(answer(text), key(none))
            self.assertEqual(got["facts"][0]["contradicted"], [], text)
        # the answer's own claim still contradicts: after a comma or an "and" that leaves
        # the negation behind, after a closed quotation, or with the contrast after the form
        for text in ("The vendor confirmed delivery for 2026-07-14.",
                     "There was no delay, and the vendor confirmed delivery on 2026-07-14.",
                     "Nothing is late, the vendor confirmed delivery.",
                     "The PO is marked \"urgent\" and the vendor confirmed delivery.",
                     "The vendor confirmed delivery rather than calling."):
            got = grade(answer(text), key(none))
            self.assertEqual((got["outcome"], got["facts"][0]["contradicted"]),
                             ("false", ["contradicting_claim"]), text)
        # a stated claim keeps its narrow window: "not on hold, it is late" states late
        self.assertTrue(lo.asserted("It is not on hold, it is late.", "late"))

    def test_two_figures_for_one_count_are_incomplete_and_one_in_its_clause_counts(self):
        count = {"id": "late_count", "kind": "number", "value": "14", "tolerance": "0",
                 "label": lk.LABELS["late_count"], "records": [H_PO], "allowed": ["api"]}
        for text in ("3 of the 14 open POs are late.", "Between 3 and 14 POs are late."):
            got = grade(answer(text, PO_VALUES[1:]), key(count))
            self.assertEqual(got["outcome"], "incomplete", text)
        one = grade(answer("We currently have 14 purchase orders that are late.",
                           PO_VALUES[1:]), key(count))
        self.assertEqual(one["outcome"], "correct", one)
        lots = dict(count, value="5", label=lk.LABELS["lot_count"])
        self.assertEqual(grade(answer("Lots expiring before January 1: 5", PO_VALUES[1:]),
                               key(lots))["outcome"], "correct")

    def test_a_late_list_states_its_count_beside_days_late_and_cited_order_numbers(self):
        # "131 days late" is a duration and 106001 an order number the answer cites on its
        # number column: neither is a second figure for the count of late orders
        count = {"id": "late_count", "kind": "number", "value": "14", "tolerance": "0",
                 "label": lk.LABELS["late_count"], "records": [H_PO], "allowed": ["api"]}
        text = ("14 purchase orders you can see are late as of 2026-11-02. Longest late: "
                "106001 (131 days). The first one is listed below.\n"
                "- 106001: 131 days late.")
        listed = [cited("listed_purchase_order_1", "106001", H_PO, "row.bc_number")]
        got = grade(answer(text, listed), key(count))
        self.assertEqual(got["outcome"], "correct", got)
        # the same number not cited as the order's: a figure beside the count, no answer
        got = grade(answer(text, PO_VALUES[1:]), key(count))
        self.assertEqual(got["outcome"], "incomplete", got)
        # a fact counted in days still reads its days
        days = dict(count, id="days_late", value="131", label=["days late"])
        self.assertEqual(lo._labelled_numbers("PO 106001 is 131 days late", days),
                         [(Decimal("131"), False)])

    def test_a_form_in_parts_and_a_leading_no_state_an_absence(self):
        none = {"id": "no_accepted_quote", "kind": "absent", "value": "accepted",
                "allowed": [], "records": [],
                "accept": ["none ... is accepted"], "contradicts": ["was accepted"]}
        for text in ("No: none of the quotes you can see is accepted.",
                     "None of the Portal quotes is accepted."):
            self.assertEqual(grade(answer(text), key(none))["outcome"], "correct", text)
        for text in ("None of the quotes is not accepted.",
                     "None of the quotes. It is accepted.",
                     "Whether none is accepted cannot be decided.",
                     "None of the quotes was rejected and Q-5 is accepted."):
            self.assertNotEqual(grade(answer(text), key(none))["outcome"], "correct", text)
        self.assertTrue(lo.asserted("No: there is no confirmation from the vendor.",
                                    "no confirmation"))
        self.assertFalse(lo.asserted("Not no confirmation.", "no confirmation"))
        self.assertFalse(lo.asserted(
            "Nothing is overdue and the mail shows the vendor confirming the new date.",
            "nothing ... shows the vendor confirming"))

    def test_a_margin_figure_is_bound_to_its_label_across_the_clause(self):
        margin = {"id": "margin_pct", "kind": "number", "value": "71.57", "unit": "%",
                  "amount": "480.96", "label": lk.LABELS["margin_pct"], "tolerance": "0.6",
                  "tolerance_pct": lk.MONEY_PCT, "records": [H_AR], "allowed": ["api"]}
        values = [cited("balance", "4821.75", H_AR, "row.balance")]
        for text in ("The order's margin on these two invoices came to 480.96 CAD.",
                     "Revenue 672 and cost -191.04 leave a margin of 480.96."):
            self.assertEqual(lo._labelled_numbers(text, margin)[0][0], Decimal("480.96"), text)
            self.assertEqual(grade(answer(text, values), key(margin))["outcome"], "correct",
                             text)

    def test_the_cost_a_margin_is_taken_on_is_part_of_the_margins_name(self):
        """The app writes "margin on item cost only 1,743.51 CAD (19.09%)": the cost there
        is the margin's basis, never a cost figure, so the margin is read (right or wrong)
        and no cost is read from it."""
        margin = {"id": "margin_pct", "kind": "number", "value": "19.09", "unit": "%",
                  "amount": "1743.51", "label": lk.LABELS["margin_pct"], "tolerance": "0.6",
                  "tolerance_pct": lk.MONEY_PCT, "records": [H_AR], "allowed": ["api"]}
        cost = {"id": "cost", "kind": "number", "value": "7387.63", "unsigned": True,
                "label": lk.LABELS["cost"], "tolerance": "1", "tolerance_pct": lk.MONEY_PCT,
                "records": [H_AR], "allowed": ["api"]}
        values = [cited("balance", "4821.75", H_AR, "row.balance")]
        said = ("Order SO1: revenue 9,131.14 CAD, item cost 7,387.63 CAD, margin on item "
                "cost only 1,743.51 CAD (19.09%).")
        self.assertEqual(grade(answer(said, values), key(margin, cost))["outcome"], "correct")
        wrong = said.replace("1,743.51 CAD (19.09%)", "3,100.00 CAD (33.95%)")
        self.assertEqual(grade(answer(wrong, values), key(margin))["outcome"], "false")
        alone = "Margin on item cost only: -41.45 CAD."
        self.assertEqual(lo._labelled_numbers(alone, cost), [])
        self.assertEqual(lo._labelled_numbers(alone, dict(margin, amount="-41.45")),
                         [(Decimal("-41.45"), False)])
        # a cost stated on its own beside a margin is still the cost
        self.assertEqual(lo._labelled_numbers("Margin 19% on cost of 7,387.63.", cost),
                         [(Decimal("7387.63"), False)])

    def test_money_is_read_as_written_and_a_margin_amount_against_the_amount(self):
        values = [cited("balance", "4821.75", H_AR, "row.balance")]
        ar = dict(AR, label=lk.LABELS["open_ar"], tolerance_pct=lk.MONEY_PCT)
        for text in ("Open AR is about $4.8k.", "They owe roughly $4,800.",
                     "AR ~4.8k, all overdue.", "Balance due: $4,821.75"):
            self.assertEqual(grade(answer(text, values), key(ar))["outcome"], "correct", text)
        self.assertEqual(grade(answer("Open AR is $3.9k.", values), key(ar))["outcome"],
                         "false")
        margin = {"id": "margin_pct", "kind": "number", "value": "23.45", "unit": "%",
                  "amount": "1130.66", "label": lk.LABELS["margin_pct"], "tolerance": "0.6",
                  "tolerance_pct": lk.MONEY_PCT, "records": [H_AR], "allowed": ["api"]}
        for text, outcome in (("Margin was $1,130 on revenue of $4,821.75.", "correct"),
                              ("A 23.4% margin.", "correct"),
                              ("A 40% margin.", "false"), ("Margin was $2,000.", "false")):
            self.assertEqual(grade(answer(text, values), key(margin))["outcome"], outcome,
                             text)

    def test_an_abstention_is_unjustified_where_the_reader_sees_the_answer(self):
        blocked = answer("I could not find it.", answerability="blocked", gaps=[
            {"state": "not_ingested", "subject": "PurchaseOrder", "reason": "not_found"}])
        got = grade(blocked, key(LATE, DUE))
        self.assertEqual((got["outcome"], got["justified"]), ("abstained", False))

    def test_a_restricted_reader_abstaining_is_justified_and_stating_is_a_leak(self):
        quiet = answer("The receivable is finance-only.", answerability="blocked", gaps=[
            {"state": "withheld", "subject": "Customer", "reason": "withheld"}])
        got = grade(quiet, key(AR), reader="purchasing")
        self.assertEqual((got["outcome"], got["justified"], got["codes"]),
                         ("abstained", True, ["withheld_for_reader"]))
        leak = grade(answer("Open AR is 4821.75."), key(AR), reader="purchasing")
        self.assertEqual((leak["outcome"], leak["codes"]), ("false", ["withheld_fact_stated"]))
        # Vince sees it: the same quiet answer is an unjustified abstention
        self.assertFalse(grade(quiet, key(AR))["justified"])

    def test_an_absence_is_stated_without_evidence_and_a_status_contradicts_it(self):
        absent = {"id": "no_such_po", "kind": "absent", "value": "find",
                  "accept": ["no such", "not found", "could not find"],
                  "contradicts": ["is open", "is late"]}
        none = grade(answer("I could not find PO#9001 in any system.",
                            answerability="partial"), key(absent))
        self.assertEqual(none["outcome"], "correct", none)
        made_up = grade(answer("PO#9001 is open, status SUBMITTED.", PO_VALUES[1:]),
                        key(absent))
        self.assertEqual(made_up["outcome"], "false")
        # another record answered in its place is false (a recorded run did this)
        other = grade(answer("Order date 2026-09-01 for that order.", PO_VALUES[:1],
                             answerability="partial"), key({**absent, "nothing_exists": True}))
        self.assertEqual((other["outcome"], other["facts"][0]["contradicted"]),
                         ("false", ["record_asserted"]))
        # "not a vendor confirmation" says what a date is, never that none is on record
        none_on_record = {"id": "c", "kind": "absent", "value": "confirmation",
                          "accept": ["no confirmation", "not confirmed"]}
        side = grade(answer("The BC date is a system date, not a vendor confirmation.",
                            PO_VALUES[:1]), key(none_on_record))
        self.assertEqual(side["outcome"], "abstained")

    def test_a_list_counts_named_values_and_a_member_set_apart_is_no_extra(self):
        orders = {"id": "open_orders", "kind": "set", "records": [H_PO],
                  "allowed": ["api", "bc"], "pattern": lk.PO_TOKEN,
                  "members": [["PO#9001", "PO9001"]]}
        named = grade(answer("Two orders.", [cited("po9001_status", "SUBMITTED", H_PO,
                                                   "row.status")]), key(orders))
        self.assertEqual(named["outcome"], "correct", named)
        apart = grade(answer("PO#9001 is open; PO#9002 is a draft.", PO_VALUES[1:]),
                      key(orders))
        self.assertEqual(apart["outcome"], "correct", apart)
        extra = grade(answer("Open: PO#9001 and PO#9002.", PO_VALUES[1:]), key(orders))
        self.assertEqual((extra["outcome"], extra["facts"][0]["contradicted"]),
                         ("false", ["extra_member"]))

    def test_a_draft_sales_order_number_is_one_whole_member(self):
        """A BC draft order DSO100025 is named whole: the SO100025 inside it is no other
        order (a recorded run listing exactly the key's DSO orders was graded false)."""
        from evals import live_ops_more as more
        orders = {"id": "open_orders", "kind": "set", "records": [H_PO], "allowed": ["api", "bc"],
                  "pattern": more.SO_TOKEN, "members": [["DSO100009"], ["DSO100037"]]}
        both = grade(answer("Two BC orders still need to ship: DSO100009 and DSO100037.",
                            PO_VALUES[1:]), key(orders))
        self.assertEqual(both["outcome"], "correct", both)
        for other in ("SO100009", "S-ORD100011", "SO-100012", "DSO100099"):
            extra = grade(answer(f"Open: DSO100009, DSO100037 and {other}.", PO_VALUES[1:]),
                          key(orders))
            self.assertEqual((extra["outcome"], extra["facts"][0]["contradicted"]),
                             ("false", ["extra_member"]), other)

    def test_a_freshness_gap_covers_only_its_stale_fact_never_a_leak_or_a_contradiction(self):
        # A quality reader stating the withheld open AR, with a
        # freshness flag on an unrelated fact, is false
        flagged = dict(key(AR, LATE), freshness_gap=["late"])
        leak = grade(answer("Fixture Soap Co: $4,821.75"), flagged, reader="quality")
        self.assertEqual((leak["outcome"], leak["codes"]), ("false", ["withheld_fact_stated"]))
        wrong = grade(answer("PO#9001 is on time.", PO_VALUES), dict(key(LATE, DUE),
                                                                     freshness_gap=["due"]))
        self.assertEqual(wrong["outcome"], "false")
        missed_due = grade(answer("PO#9001 is late.", PO_VALUES[1:]),
                           dict(key(LATE, DUE), freshness_gap=["due"]))
        self.assertEqual(missed_due["outcome"], "freshness_gap")
        missed_both = grade(answer("Nothing found.", answerability="blocked"),
                            dict(key(LATE, DUE), freshness_gap=["due"]))
        self.assertEqual(missed_both["outcome"], "abstained")   # "late" is not stale
        pending = grade(answer("PO#9001 is late.", PO_VALUES), dict(key(LATE),
                                                                    rule_pending="owner"))
        self.assertEqual(pending["outcome"], "rule_pending")

    def test_a_mail_card_is_not_graded_for_a_reader_without_every_mailbox(self):
        message = {"id": "m", "kind": "message", "message_id": "AAMk-1", "quote": "Re: PO",
                   "records": ["graph-mail://mbx/AAMk-1@ck"], "allowed": ["graph-mail"],
                   "mail": True}
        got = grade(answer("nothing"), key(message), reader="purchasing")
        self.assertEqual(got["outcome"], "not_applicable")
        self.assertEqual(grade(answer("The vendor wrote Re: PO."), key(message))["outcome"],
                         "abstained")   # the subject alone, no citation of the message
        denied = {**message, **lk.FORMS["last_vendor_message"]}
        self.assertEqual(grade(answer("No vendor message about it was found."),
                               key(denied))["outcome"], "false")

    def test_a_malformed_key_or_unknown_reader_fails_closed(self):
        with self.assertRaises(lo.KeyProblem):
            grade(answer("x"), key({**LATE, "kind": "vibe"}))
        with self.assertRaises(lo.KeyProblem):
            grade(answer("x"), key({**LATE, "allowed": ["teams"]}))
        with self.assertRaises(lo.KeyProblem):
            grade(answer("x"), key(LATE), reader="intern")


# --------------------------------------------------------------------------- keys

AS_OF = date(2026, 11, 1)
NOW = datetime(2026, 11, 1, 15, tzinfo=timezone.utc)
CO = "11111111-1111-1111-1111-111111111111"


def bc(entity_set: str, fields: dict, n: int) -> dict:
    return {"company_id": CO, "entity_set": entity_set, "id": fields["id"], "fields": fields,
            "citation": f"bc://production/{CO}/{entity_set}/{fields['id']}@{'e' * 16}-{n}"}


class Cleared:
    """A sender guard clearing every sender (the fixtures hold no excluded person)."""

    def cleared(self, sender, own) -> bool:
        return True


CLEARED = Cleared()


def mail_tables(conn) -> None:
    """A fixture mail capture's mailbox list and messages' mailbox column (the loaders read
    no capture without them: fail closed)."""
    conn.execute("ALTER TABLE messages ADD COLUMN mailbox_id TEXT DEFAULT 'mb'")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mailboxes)")}
    if not columns:
        conn.execute("CREATE TABLE mailboxes (upn TEXT, identities TEXT)")
    conn.execute("ALTER TABLE mailboxes ADD COLUMN mailbox_id TEXT DEFAULT 'mb'")


def mirror(relation: str, row: dict, n: int = 3) -> dict:
    return {"row": row, "citation": fx.handle(relation, {"id": row["id"]}, n)}


def world(changed: int = 0, **extra) -> lk.World:
    """A small invented world: one late Portal order, a batch on hold, a released lot, one
    customer with open AR, ``changed`` BC orders modified since yesterday."""
    portal = {
        lk.PO: [mirror(lk.PO, {"id": "p1", "po_number": "PO#7001", "status": "SUBMITTED",
                               "expected_date": "2026-09-15", "supplier_id": "s1",
                               "updated_at": "2026-09-01T00:00:00"})],
        lk.PO_LINE: [mirror(lk.PO_LINE, {"id": "l1", "purchase_order_id": "p1",
                                         "product_id": "pr1", "quantity": "10",
                                         "quantity_received": "0"})],
        lk.BATCH: [mirror(lk.BATCH, {"id": "b1", "batch_number": "BC-26101A", "batch_name": "BC-26101A",
                                     "status": "Realized", "status_qc": "On-Hold"})],
        lk.FM_BATCH: [mirror(lk.FM_BATCH, {"id": 11, "namebatch_c": "BC-26101A",
                                           "status": "Realized", "statusqc": "On-Hold"})],
        lk.RAW_LOT: [mirror(lk.RAW_LOT, {"id": 5, "lotnoqc": "LOT-55", "status_c": "Released",
                                         "expirydate": "2026-12-03"})],
    }
    records = [bc("customers", {"id": "c1", "number": "CU1", "displayName": "Fixture Soap Co",
                                "balanceDue": "4821.75"}, 1),
               bc("agedAccountsReceivables", {"id": "a1", "customerNumber": "CU1",
                                              "name": "Fixture Soap Co",
                                              "balanceDue": "4821.75"}, 2)]
    for i in range(changed):
        records.append(bc("purchaseOrders", {
            "id": f"o{i}", "number": f"PO{100000 + i}", "status": "Open",
            "fullyReceived": False, "vendorName": f"Vendor {i}",
            "lastModifiedDateTime": "2026-10-31T16:00:00Z"}, 10 + i))
    return lk.World(as_of=AS_OF, now=NOW, portal=portal, bc=records, **extra)


class KeyTests(unittest.TestCase):
    def test_a_customers_last_mail_reads_every_spelling_and_skips_automatic_replies(self):
        """A customer is its name case-folded: "CITY OF X" (its BC email at an invoice
        service) and "City of X" (at its own domain) are one customer, keyed under each
        spelling; an auto-reply whose subject carries a tag prefix ("[Petra] Accounting RE:
        Concur Auto Reply ...") is never the last message with it; nor is a third party's
        mailing that only lists the customer among its recipients."""
        import sqlite3
        customers = [{"fields": {"displayName": "CITY OF X", "email": "inv@invoices.example"}},
                     {"fields": {"displayName": "City of X", "email": "ap@cityx.example"}},
                     {"fields": {"displayName": "Other Co", "email": "a@other.example"}}]
        mails = [("m1", "ap@cityx.example", ["us@petra.example"], "RE: invoice 1",
                  "2026-08-26T13:39:10Z"),
                 ("m2", "bot@invoices.example", ["acct@petra.example"],
                  "[Petra] Accounting RE: Concur Auto Reply Statement X", "2026-10-06T16:20:34Z"),
                 ("m3", "us@petra.example", ["a@other.example"], "Order", "2026-10-01T00:00:00Z"),
                 # a third party's mailing that lists the customer among many: not a mail
                 # we had with the customer, however new
                 ("m4", "events@gmail.com", ["ap@cityx.example", "us@petra.example"] + [
                     f"p{n}@mail{n}.example" for n in range(14)], "Monthly workshop",
                  "2026-10-20T00:00:00Z")]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "capture.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE messages (l1_handle TEXT, message_id TEXT, record TEXT, "
                         "superseded_at TEXT, removed_reason TEXT)")
            for mid, sender, to, subject, at in mails:
                conn.execute("INSERT INTO messages VALUES (?, ?, ?, NULL, NULL)", (
                    f"graph-mail://mb/{mid}@ck", mid, json.dumps(
                        {"from": sender, "to": to, "subject": subject, "received_at": at,
                         "mailbox_upn": "box@petra.example"})))
            mail_tables(conn)
            conn.commit()
            conn.close()
            found = script.customer_mail(db, customers, senders=CLEARED)
        by = {}
        for m in found:
            by.setdefault(m["customer"], set()).add(m["message_id"])
        self.assertEqual(by["CITY OF X"], {"m1", "m2"})
        self.assertEqual(by["City of X"], {"m1", "m2"})
        self.assertEqual(by["Other Co"], {"m3"})
        w = world(customer_mail=found)
        from evals import live_ops_more as more
        keyed = {c.slots["customer"]: c.facts[0]["message_id"]
                 for c in more.customer_last_mail(w)}
        self.assertEqual(keyed["CITY OF X"], "m1")
        self.assertEqual(keyed["City of X"], "m1")
        for subject in ("Automatic reply: away", "RE: Undeliverable: order",
                        "[Petra] Accounting RE: Concur Auto Reply Invoice IN1"):
            self.assertTrue(lk.AUTOMATIC.search(subject), subject)
        for subject in ("RE: Petra Hygienic - invoice # IN1", "Re: Onboarding",
                        "We accepted: your PO"):
            self.assertFalse(lk.AUTOMATIC.search(subject), subject)

    def test_a_customer_margin_key_needs_every_invoiced_order_decided(self):
        """The product states a customer total only when every order with a posted invoice
        has a margin: a customer with a partly invoiced order
        gets no key; with that order's invoices gone, the key is the oracle's totals summed
        over both companies' orders of the name (none lost to a shared order number)."""
        from evals.oracle import order_margin as oracle
        from evals.oracle.source import BcReads
        from integrations.tests import test_bc_capture as tb
        records = oracle.fixture_world(companies=tb.CO[:2])
        for record in records:
            if record["fields"].get("customerNumber"):
                record["fields"]["customerName"] = "Cust " + record["fields"]["customerNumber"]

        def margin_keys(rows):
            w = lk.World(as_of=AS_OF, now=NOW, portal={}, bc=rows)
            return {c.identifier: {f["id"]: f["value"] for f in c.facts}
                    for c in lk.customer_margin(w)}
        self.assertNotIn("Cust C-1", margin_keys(records))
        totals, undecided = [], set()
        for company in tb.CO[:2]:
            port = oracle.RecordPort(records, company)
            numbers = {r["fields"]["orderNumber"] for r in records
                       if r["entity_set"] == "salesInvoices" and r["company_id"] == company
                       and r["fields"].get("customerNumber") == "C-1"}
            for number in numbers:
                total = oracle.reading(BcReads(port), number, book="manufacturing")["total"]
                if total.get("reason") == "decided":
                    totals.append(Decimal(total["revenue"]))
                else:
                    undecided.add((company, number))
        self.assertTrue(undecided)
        gone = {r["id"] for r in records if r["entity_set"] == "salesInvoices"
                and (r["company_id"], r["fields"].get("orderNumber")) in undecided}
        kept = [r for r in records if r["id"] not in gone and r.get("document_id") not in gone]
        self.assertEqual(Decimal(margin_keys(kept)["Cust C-1"]["revenue"]), sum(totals))

    def test_an_order_margin_rests_on_its_invoice_lines_and_shipment_ledger_too(self):
        """An answer citing the invoice lines and ledger entries a many-line order's figures
        are summed from cites that order: they are its key's records, and no other order's
        line or entry is."""
        from evals.oracle import order_margin as oracle
        from integrations.tests import test_bc_capture as tb
        records = oracle.fixture_world(companies=tb.CO[:1])
        by_citation = {r["citation"]: r for r in records}
        margins = lk._margins(lk.World(as_of=AS_OF, now=NOW, portal={}, bc=records))
        decided = [(number, e) for (_c, number), e in margins.items() if e["total"]]
        self.assertTrue(decided)
        for number, entry in decided:
            mine = [by_citation[c] for c in entry["records"]]
            sets = {r["entity_set"] for r in mine}
            self.assertLessEqual({"salesInvoiceLines", "itemLedgerEntries"}, sets, number)
            invoices = {r["id"] for r in mine if r["entity_set"] == "salesInvoices"}
            shipments = {r["fields"]["number"] for r in mine
                         if r["entity_set"] == "salesShipments"}
            for r in mine:
                if r["entity_set"] == "salesInvoiceLines":
                    self.assertIn(r["document_id"], invoices)
                elif r["entity_set"] == "itemLedgerEntries":
                    self.assertIn(r["fields"]["documentNumber"], shipments)
                else:
                    self.assertIn(number, (r["fields"].get("orderNumber"),
                                           r["fields"].get("number")))

    def test_families_read_the_rows_never_an_answer(self):
        w = world()
        late = lk.po_late(w)
        self.assertEqual([c.identifier for c in late], ["PO#7001"])
        self.assertEqual({f["id"]: f.get("value") for f in late[0].facts},
                         {"late": "late", "due": "2026-09-15"})
        self.assertEqual(lk.open_ar(w)[0].facts[0]["value"], "4821.75")
        self.assertEqual(lk.batch_on_hold(w)[0].facts[0]["markings"], ["quality_controlled"])
        self.assertEqual(lk.lot_expiry(w)[0].facts[0]["value"], "2026-12-03")
        for family in (lk.po_late, lk.open_ar, lk.batch_on_hold, lk.lot_expiry):
            for candidate in family(w):
                self.assertTrue(candidate.reads and candidate.rule)

    def test_a_batch_is_counted_once_by_filemaker_the_system_of_record(self):
        """The same rule as the product's count: one batch per code held in both systems;
        FileMaker's status where the Portal's copy disagrees (and the rule says how many
        do); a Portal-only copy, a code-less record, a placeholder code or a staging test
        batch is no key."""
        w = world()
        fm = lambda i, code, status="Realized", qc="On-Hold": mirror(lk.FM_BATCH, {
            "id": i, "namebatch_c": code, "status": status, "statusqc": qc})
        portal = lambda i, code, status="Realized", qc="On-Hold": mirror(lk.BATCH, {
            "id": f"g{i}", "batch_number": code, "batch_name": code, "status": status,
            "status_qc": qc})
        w.portal = {**w.portal,
                    lk.FM_BATCH: [fm(1, "BC-26001A"), fm(2, "BC-26002A", qc="Released"),
                                  fm(3, "BC-26003A", status="Cancelled"), fm(5, "BC-26005A"),
                                  fm(6, None), fm(9000001, "TEST-BC-1"),
                                  # a generated batch's placeholder, on many rows, is no
                                  # batch code (the schema's grammar): never one batch
                                  fm(7, "BC-00A", status="Generated"),
                                  fm(8, "BC-00A", status="Generated")]
                    # every unmade batch carries On-Hold before release: that state's
                    # default (operating.stock_state), no hold: keyed not on hold, never
                    # counted
                    + [fm(100 + i, f"BC-261{i:02d}A", status="Allocated") for i in range(10)],
                    lk.BATCH: [portal(1, "BC-26001A"), portal(2, "BC-26002A"), portal(3, "BC-26003A"),
                               portal(4, "BC-26004A"), portal(5, "BC-26005A", qc="Released")]}
        # "is it on hold?" of every batch either system shows held, answered by FileMaker:
        # released, cancelled there or no FileMaker batch at all is a key of its own
        asked = {c.identifier: c.facts[0] for c in lk.batch_on_hold(w)}
        unmade = {code: asked.pop(code)["id"] for code in list(asked) if code >= "BC-261"}
        self.assertEqual(unmade, {f"BC-261{i:02d}A": "not_on_hold" for i in range(10)})
        self.assertEqual({code: (f["kind"], f["id"]) for code, f in asked.items()}, {
            "BC-26001A": ("claim", "on_hold"), "BC-26002A": ("claim", "released"),
            "BC-26003A": ("claim", "cancelled"), "BC-26004A": ("absent", "no_filemaker_batch"),
            "BC-26005A": ("claim", "on_hold")})                # never TEST-BC-1 (staging)
        self.assertTrue(all("public.batch" in r for code, f in asked.items()
                            if code != "BC-26004A" for r in f["records"]))
        for f in asked.values():
            self.assertEqual(lo.key_problems({
                "format": lo.KEY_FORMAT, "item_id": "i", "facts": [f],
                "query": {"rule": "r", "reads": ["x"]}}), [])
        [count] = lk.batches_on_hold_count(w)
        self.assertEqual(count.facts[0]["value"], "2")
        self.assertTrue(all("public.batch" in r for r in count.facts[0]["records"]))
        self.assertIn("3 batches the two disagree on", " ".join(count.reads))

    def test_the_late_list_rules_decided_for_draft_twins(self):
        """A BC Draft twin with nothing
        received neither closes nor dates the order; Pending with FileMaker disputing it, or
        quantities FileMaker cannot vouch for, is never late; a released BC order outranks
        a Portal DRAFT."""
        def late(status="Pending", expected=None, received="0", fm_status="Pending",
                 fm_qty="10", item="Pending", qc=None, bc_status="Draft", fully=True):
            w = world()
            w.portal = {**w.portal, lk.PO: [mirror(lk.PO, {
                "id": "p1", "po_number": "PO#7001", "status": status,
                "expected_date": expected,
                "custom_fields": {"fmGeneratedPoId": 70, "renumberedTo": "PO100700"}})],
                lk.FM_PO: [mirror(lk.FM_PO, {"id": 70, "ponumber": "PO#7001",
                                             "status": fm_status})],
                lk.FM_PO_ITEM: [mirror(lk.FM_PO_ITEM, {"id": 701, "idpogen": 70,
                                                       "qty": fm_qty, "status": item})],
                lk.RAW_LOT: [mirror(lk.RAW_LOT, {"id": 9, "idpogenitem": 701, "sumqty": qc})]
                if qc else []}
            order = bc("purchaseOrders", {"id": "o1", "number": "PO100700", "status": bc_status,
                                          "fullyReceived": fully}, 40)
            line = bc("purchaseOrderLines", {"id": "ol1", "documentId": "o1", "quantity": "10",
                                             "receivedQuantity": received,
                                             "expectedReceiptDate": "2026-09-01"}, 41)
            w.bc = [*w.bc, order, line]
            return len(w.lateness()["late"])
        self.assertEqual(late(), 0)                             # rule 1: only the Draft's date
        self.assertEqual(late(expected="2026-09-15"), 1)        # its own date: late
        self.assertEqual(late(expected="2026-09-15", fm_status="Complete"), 0)   # rule 2
        self.assertEqual(late(expected="2026-09-15", fm_qty="12"), 0)            # rule 2
        # FileMaker's item received in full (its own item state): closed, never late; the
        # QC log's sumqty, a running total across the log, decides nothing
        self.assertEqual(late(expected="2026-09-15", item="Complete"), 0)
        self.assertEqual(late(expected="2026-09-15", item="B.Order"), 1)
        self.assertEqual(late(expected="2026-09-15", qc="35847482.0"), 1)
        # a Draft received against is real activity:
        # its open line keeps the order open and its date dates it
        self.assertEqual(late(received="4"), 1)
        self.assertEqual(late(expected="2026-09-15", received="4"), 1)
        self.assertEqual(late(expected="2026-09-15", received="10"), 0)   # received in full
        self.assertEqual(late(status="DRAFT", bc_status="Open", fully=False), 1)  # rule 3

    def test_a_vendors_open_orders_leave_out_one_received_in_another_copy(self):
        """Truth rules 2026-11-06: an order open in BC but received in its FileMaker copy
        is no open order; a Draft is not issued; the rest are listed."""
        w = world()
        w.portal = {**w.portal, lk.PO: [mirror(lk.PO, {
            "id": "p1", "po_number": "PO#7001", "status": "B.Order",
            "custom_fields": {"fmGeneratedPoId": 70, "renumberedTo": "PO100701"}})],
            lk.FM_PO: [mirror(lk.FM_PO, {"id": 70, "ponumber": "PO#7001", "status": "B.Order"})],
            lk.FM_PO_ITEM: [mirror(lk.FM_PO_ITEM, {"id": 701, "idpogen": 70, "qty": "10",
                                                   "status": "Complete"})]}
        orders = [bc("purchaseOrders", {"id": f"o{n}", "number": f"PO10070{n}", "status": word,
                                        "fullyReceived": False, "vendorName": "Acme"}, 40 + n)
                  for n, word in ((1, "Open"), (2, "Open"), (3, "Open"), (4, "Draft"))]
        w.bc = [*w.bc, *orders]
        (found,) = [c for c in lk.vendor_open_pos(w) if c.identifier == "Acme"]
        self.assertEqual(found.facts[0]["records"], [o["citation"] for o in orders[1:3]])

    def test_changed_since_yesterday_at_one_ten_and_sixty_records(self):
        self.assertEqual(lk.po_changed(world(0))[0].facts[0]["kind"], "absent")
        listed = lk.po_changed(world(1))[0].facts[0]
        self.assertEqual((listed["kind"], len(listed["members"])), ("set", 1))
        many = lk.po_changed(world(10))[0].facts[0]   # past LIST_MAX: a count, none lost
        self.assertEqual((many["kind"], many["value"], len(many["records"])),
                         ("number", "10", 10))
        sixty = lk.po_changed(world(60))[0].facts[0]
        self.assertEqual((sixty["value"], len(sixty["records"])), ("60", 60))

    def test_a_portal_restamp_is_no_change_and_a_value_change_is(self):
        """A Portal row whose only
        difference from the version held at since is updated_at is not changed (as
        plx.changes' restamped); a changed column or a creation since is; a row read first
        since then that says it is older is not; without versions there is no key."""
        before = datetime(2026, 10, 30, 9, tzinfo=timezone.utc)
        old = {"id": "s{}", "order_number": "OR{}", "status": "Open", "total_amount": "10",
               "created_at": "2026-09-01T00:00:00", "updated_at": "2026-10-30T08:00:00"}

        def row(i, **change):
            base = {k: v.format(i) if "{}" in v else v for k, v in old.items()}
            return {**base, "updated_at": "2026-10-31T10:00:00", **change}
        rows = [mirror(lk.PORTAL_SO, row(0)),                             # restamped
                mirror(lk.PORTAL_SO, row(1, total_amount="12")),          # changed
                mirror(lk.PORTAL_SO, row(2, created_at="2026-10-31T09:00:00")),   # new
                mirror(lk.PORTAL_SO, row(3))]       # first read since: not counted
        versions = {lk.PORTAL_SO: {
            item["citation"].rsplit("@", 1)[0]: [(before, {**item["row"], "updated_at":
                                                          old["updated_at"],
                                                          "total_amount": "10"})]
            for item in rows[:3]}}
        w = world(portal_versions=versions)
        w.portal = {**w.portal, lk.PORTAL_SO: rows}
        fact = lk.so_changed(w)[0].facts[0]
        self.assertEqual((fact["kind"], fact["members"]), ("set", [["OR1"], ["OR2"]]))
        self.assertEqual(len(fact["records"]), 2)
        w.portal_versions = None
        self.assertEqual(lk.so_changed(w), [])

    def test_an_absence_is_never_claimed_where_any_text_names_the_number(self):
        self.assertEqual(lk.no_such_po(world()), [])   # the default index names everything
        found = lk.no_such_po(world(named_in_text=lambda n: False, named_in_values=lambda n: False))
        self.assertTrue(found and all(c.facts[0]["kind"] == "absent" for c in found))

    def test_keys_split_by_partition_group_and_land_outside_the_repository(self):
        cards = [{"id": "LO-01", "family": "po_late", "instances": 1,
                  "question": "wheres {po} at"},
                 {"id": "LO-02", "family": "po_late", "instances": 1, "facts": ["due"],
                  "question": "when was {po} due"},
                 {"id": "LO-15", "family": "open_ar", "instances": 1,
                  "question": "open AR for {customer}?"},
                 {"id": "LO-17", "family": "batch_on_hold", "instances": 1,
                  "question": "is batch {batch} on hold?"},
                 {"id": "LO-20", "family": "lot_expiry", "instances": 1,
                  "question": "when does lot {lot} expire"}]
        items, keys, notes = script.build(cards, "c" * 64, world(), {"bc": "e" * 16})
        script.split(items, keys, [])
        for one, its_key in zip(items, keys):
            self.assertEqual(runner.validate_item(one), [], one["id"])
            self.assertEqual(lo.key_problems(its_key), [], one["id"])
            self.assertEqual(its_key["item_id"], one["id"])
        self.assertEqual([k["facts"][0]["id"] for k in keys[:2]], ["late", "due"])
        # the two cards on one order share it: one group, one split
        self.assertEqual(items[0]["split"], items[1]["split"])
        self.assertEqual({i["split"] for i in items}, {"dev", "held_out"})
        shadows = [script._shadow(i, k) for i, k in zip(items, keys)]
        for group in partition.groups(shadows):
            self.assertEqual(len({items[i]["split"] for i in group}), 1)
        with tempfile.TemporaryDirectory() as tmp:
            held = Path(tmp) / "held"
            with mock.patch.object(script, "held_out_dir", lambda _dev: held), \
                    mock.patch.object(script, "load_cards", lambda: (cards, "c" * 64)), \
                    __import__("contextlib").redirect_stdout(__import__("io").StringIO()), \
                    __import__("contextlib").redirect_stderr(__import__("io").StringIO()):
                code = script.main(["keys", "--dev-dir", str(Path(tmp) / "dev")],
                                   world=world())
                self.assertEqual(code, 0)
                self.assertTrue(list((held / "keys").glob("ho-F29-*.json")))
                self.assertEqual(script.main(["keys", "--dev-dir", str(script.ROOT / "x")],
                                             world=world()), 1)
            pairs = script.load_split(Path(tmp) / "dev")
            self.assertTrue(pairs)
            broken = copy.deepcopy(pairs[0][1])
            broken["facts"][0]["kind"] = "guess"
            (Path(tmp) / "dev" / "keys" / f"{pairs[0][0]['id']}.json").write_text(
                json.dumps(broken))
            with self.assertRaises(ValueError):
                script.load_split(Path(tmp) / "dev")

    def test_each_key_run_keeps_its_own_held_out_half(self):
        # a second key run overwrote the first's held-out keys in one shared directory,
        # so the first run's held-out transcripts were graded against other questions
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                "os.environ", {"PLX_WORKBENCH_API_KEY": "placeholder-not-a-key",
                               "PLX_EVAL_HELD_OUT_DIR": tmp}):
            first, second = (script.held_out_dir(Path(tmp) / name / "keys-dev")
                             for name in ("run-a", "run-b"))
            self.assertNotEqual(first, second)
            self.assertEqual(first, script.held_out_dir(Path(tmp) / "run-a" / "keys-dev"))

    def test_a_run_reports_outcomes_per_card_and_never_a_value(self):
        cards = [{"id": "LO-15", "family": "open_ar", "instances": 1,
                  "question": "open AR for {customer}?"},
                 {"id": "LO-20", "family": "lot_expiry", "instances": 1,
                  "question": "when does lot {lot} expire"}]
        lot_row = world().portal[lk.RAW_LOT][0]
        rows = {lot_row["citation"]: (lk.RAW_LOT, {"id": 5}, lot_row["row"])}

        def agent(prompt, call):
            call("plx.find", {"relation": lk.RAW_LOT})
            if "lot" not in prompt:
                return fx.final_text(answer("Open AR is 4821.75."))   # uncited
            return fx.final_text(answer("LOT-55 expires 2026-12-03.", [cited(
                "expirydate", "2026-12-03", lot_row["citation"], "row.expirydate")]))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                "os.environ", {"PLX_WORKBENCH_API_KEY": "placeholder-not-a-key",
                               "PLX_WORKBENCH_API_KEY": "placeholder-not-a-key",
                "PLX_EVAL_HELD_OUT_DIR": str(Path(tmp) / "ho")}), \
                mock.patch.object(script, "load_cards", lambda: (cards, "c" * 64)):
            out = __import__("io").StringIO()
            with __import__("contextlib").redirect_stdout(out):
                self.assertEqual(script.main(["keys", "--dev-dir", f"{tmp}/dev"],
                                             world=world()), 0)
                self.assertEqual(script.main(
                    ["run", "--dev-dir", f"{tmp}/dev", "--work-dir", f"{tmp}/work",
                     "--workers", "2"], agent=runner.ScriptedAgent(agent),
                    make_host=lambda: fx.FakeHost(rows)), 0)
            doc = json.loads(out.getvalue()[out.getvalue().index('{\n "by_split"'):])
        # a held-out run is counted (its split, suite and family totals), never listed
        outcomes = {row["card"]: row["outcome"] for row in doc["cards"]}
        self.assertEqual(outcomes, {"LO-15": "incomplete"})
        self.assertEqual({row["split"] for row in doc["cards"]}, {"dev"})
        self.assertEqual(doc["sealed"], 1)
        self.assertEqual((doc["per_family"]["open_ar"]["incomplete"],
                          doc["per_family"]["lot_expiry"]["correct"]), (1, 1))
        self.assertNotIn("LO-20", doc["per_card"])
        self.assertEqual(doc["totals"]["graded"], 2)
        self.assertEqual(sum(doc["by_split"][s]["runs"] for s in ("dev", "held_out")), 2)
        for value in ("4821.75", "2026-12-03", "LOT-55", "Fixture Soap"):
            self.assertNotIn(value, json.dumps(doc))

    def test_a_live_host_config_run_reads_with_its_grants_and_deletes_the_copy(self):
        import contextlib
        import io
        from scripts import eval_case_cards as cards_run
        from scripts import eval_owner_host as owner
        from scripts import eval_real_agent as real
        claims = {"uid": "cmreader0001", "email": "reader@example.com",
                  "entraTenantId": "11111111-2222-4333-8444-555555555555",
                  "entraObjectId": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}
        cards = [{"id": "LO-20", "family": "lot_expiry", "instances": 1,
                  "question": "when does lot {lot} expire"}]
        lot_row = world().portal[lk.RAW_LOT][0]
        rows = {lot_row["citation"]: (lk.RAW_LOT, {"id": 5}, lot_row["row"])}
        used = []

        def live_host(environ, args, identity):
            config = Path(args.host_config)
            used.append((config, identity,
                         json.loads(config.read_text("utf-8"))["marking_grants"]))
            return (lambda: fx.FakeHost(rows)), None
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {
                "PLX_WORKBENCH_API_KEY": "placeholder-not-a-key",
                "PLX_EVAL_HELD_OUT_DIR": str(Path(tmp) / "ho")}), \
                mock.patch.object(script, "load_cards", lambda: (cards, "c" * 64)):
            live = Path(tmp) / "live"
            with contextlib.redirect_stdout(io.StringIO()):
                owner.main(["--out", str(live)], environ={"PLX_EVAL_READER": json.dumps(claims)})
            doc = json.loads((live / "host.json").read_text("utf-8"))
            doc["marking_grants"]["pii"] = doc["marking_grants"]["finance"]
            (live / "host.json").write_text(json.dumps(doc), "utf-8")
            with contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(cards_run, "run_reader",
                                      lambda e, a: (claims, "employee", None)), \
                    mock.patch.object(cards_run, "live_host", live_host), \
                    mock.patch.object(real, "reader_refusal", lambda host: None):
                self.assertEqual(script.main(["keys", "--dev-dir", f"{tmp}/dev"],
                                             world=world()), 0)
                run = ["run", "--dev-dir", f"{tmp}/dev", "--work-dir", f"{tmp}/work",
                       "--live-host-config", str(live / "host.json"), "--offline-directory"]
                self.assertEqual(script.main(run, agent=runner.ScriptedAgent(
                    lambda prompt, call: fx.final_text(answer("unknown")))), 0)
                # never beside another config
                self.assertEqual(script.main([*run, "--host-config", str(live / "host.json")],
                                             agent=runner.ScriptedAgent(lambda p, c: "")), 1)
            [(config, identity, grants)] = used
            self.assertEqual(identity, claims)
            self.assertEqual(grants, doc["marking_grants"])          # the live grants, all
            self.assertTrue(config.is_relative_to(Path(tmp) / "work"))
            self.assertFalse(config.exists())                         # the copy is deleted
            self.assertEqual(list((Path(tmp) / "work").glob(".live-host-*")), [])

    def test_the_run_routes_as_the_app_does_and_reports_the_share_without_claude(self):
        from types import SimpleNamespace as NS
        cards = [{"id": "LO-15", "family": "open_ar", "instances": 1,
                  "question": "open AR for {customer}?"},
                 {"id": "LO-20", "family": "lot_expiry", "instances": 1,
                  "question": "when does lot {lot} expire"}]
        lot_row = world().portal[lk.RAW_LOT][0]
        rows = {lot_row["citation"]: (lk.RAW_LOT, {"id": 5}, lot_row["row"])}
        good = fx.final_text(answer("LOT-55 expires 2026-12-03.", [cited(
            "expirydate", "2026-12-03", lot_row["citation"], "row.expirydate")]))
        asked = []

        class Fast:   # serves and answers the lot question only
            Reads = NS(of_server=staticmethod(lambda server: server))

            @staticmethod
            def serves(question):
                return "lot" in question

            @staticmethod
            def answer(question, reads):
                asked.append(question)
                found = fx.FakeHost(rows).call("plx.find", {"relation": lk.RAW_LOT})
                events = [{"seq": 0, "type": "tool_call", "name": "plx.find", "t_ms": 1,
                           "arguments": {"relation": lk.RAW_LOT}, "result": found,
                           "is_error": False}]
                return NS(events=events, final_text=good, intent="lot_expiry", reply={})

        agent_calls = []

        def agent(prompt, call):
            agent_calls.append(prompt)
            return fx.final_text(answer("Open AR is 4821.75."))

        def go(*extra):
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                    "os.environ", {"PLX_WORKBENCH_API_KEY": "placeholder-not-a-key",
                                   "PLX_WORKBENCH_API_KEY": "placeholder-not-a-key",
                "PLX_EVAL_HELD_OUT_DIR": str(Path(tmp) / "ho")}), \
                    mock.patch.object(script, "load_cards", lambda: (cards, "c" * 64)):
                out = __import__("io").StringIO()
                with __import__("contextlib").redirect_stdout(out):
                    self.assertEqual(script.main(["keys", "--dev-dir", f"{tmp}/dev"],
                                                 world=world()), 0)
                    self.assertEqual(script.main(
                        ["run", "--dev-dir", f"{tmp}/dev", "--work-dir", f"{tmp}/work",
                         *extra], agent=runner.ScriptedAgent(agent), fast=Fast,
                        session=lambda host: (None, object()),
                        make_host=lambda: fx.FakeHost(rows)), 0)
                return json.loads(out.getvalue()[out.getvalue().index('{\n "by_split"'):])
        doc = go()
        routed = {r["card"]: r["routed"] for r in doc["cards"]}
        self.assertEqual(routed, {"LO-15": "agent"})   # the held-out LO-20 is not listed
        self.assertEqual(doc["routing"]["share_without_claude"], 0.5)
        self.assertEqual(doc["routing"]["fast_path"]["outcomes"], {"correct": 1})
        self.assertEqual(len(agent_calls), 1)
        self.assertEqual(len(asked), 1)
        agent_calls.clear()
        doc = go("--agent-only")
        self.assertEqual({r["routed"] for r in doc["cards"]}, {"agent"})
        self.assertEqual(doc["routing"]["share_without_claude"], 0.0)
        self.assertEqual(len(agent_calls), 2)

    def test_a_lot_file_key_is_a_lot_document_never_a_file_sharing_its_number(self):
        """The product's rule (``fast_answer.lot_document_name``): an R&D formula numbered
        like the lot, or a screenshot's time, is no key; a COA naming the lot is."""
        from evals import live_ops_more as more
        w = world()
        w.portal = {**w.portal, lk.RAW_LOT: [mirror(lk.RAW_LOT, {
            "id": i, "lotnoqc": lot, "status_c": "Released", "expirydate": "2027-01-01"})
            for i, lot in enumerate(("250135", "3241766", "175455"), start=40)]}
        w.sharepoint = [{"handle": f"sharepoint://d/{i}", "name": name} for i, name in
                        enumerate(("250135 - Saint Jo -Nourish Moisturizer Version #6.pdf",
                                   "COA DayMoist CLR_3241766.pdf",
                                   "Screenshot 2026-01-14 175455.png"))]
        found = {c.slots["lot"]: c.facts[0]["quote"] for c in more.sharepoint_lot_file(w)}
        self.assertEqual(found, {"3241766": "COA DayMoist CLR_3241766.pdf"})

    def test_a_margin_question_takes_the_apps_profit_route_before_the_fast_path(self):
        """``backend.ask_shortcut`` decides the route as the app's Ask does: a margin
        question runs the profit route first; declined, the fast path, then the agent,
        with why each route before it did not answer."""
        from types import SimpleNamespace as NS
        tried, served = [], []

        class Fast:
            Reads = NS(of_server=staticmethod(lambda server: server))

            @staticmethod
            def serves(question):
                served.append(question)
                return False

        class Agent:
            def run(self, prompt, host):
                return runner.AgentRun(events=[], final_text="", usage={}, agent={})

        def profit(self, host, declined):
            tried.append(self.question)
            declined.append("profit:no_customer")
            return None
        with mock.patch.object(script.RoutedAgent, "_profit", profit):
            routed = script.RoutedAgent(Agent(), "hows our margin on 1-Shoppe orders in BC?",
                                        fast=Fast, session=lambda host: (None, object()))
            run = routed.run("p", None)
            self.assertEqual((tried, served), ([routed.question], [routed.question]))
            self.assertEqual(run.agent, {"routed": "agent",
                                         "declined": ["profit:no_customer"]})
            tried.clear()
            script.RoutedAgent(Agent(), "status on PO#10515?", fast=Fast,
                               session=lambda host: (None, object())).run("p", None)
            self.assertEqual(tried, [])          # no margin question: no profit route
        answered = runner.AgentRun(events=[], final_text="x", usage={},
                                   agent={"routed": "profit"})
        with mock.patch.object(script.RoutedAgent, "_profit",
                               lambda self, host, declined: answered):
            served.clear()
            run = script.RoutedAgent(Agent(), "is 1-Shoppe profitable?", fast=Fast,
                                     session=lambda host: (None, object())).run("p", None)
            self.assertIs(run, answered)
            self.assertEqual(served, [])         # answered: the fast path never runs

    def test_a_late_list_question_takes_the_apps_late_list_route_and_nothing_else(self):
        """``how many POs are late`` is answered by the app's late-list job over the
        reader's gap inbox (``WorkspaceBackend.late_job``), read with the reader's grant;
        the fast path and the agent never run, as in the app. A reader whose inbox has not
        run gets the app's own words for that."""
        from contextlib import contextmanager
        from types import SimpleNamespace as NS
        granted = []

        class Policy:
            def may_read(self, gated, key):
                granted.append(gated)
                return False

            def held_markings(self, gated):
                return {"finance"}

        class Host:
            def __init__(self, store):
                self.store_path = store

            @contextmanager
            def request(self):
                yield self

            def policy_now(self, gated):
                return Policy()
        actor = NS(tenant="t", principals=("vince@x",))

        class Agent:
            def run(self, prompt, host):
                raise AssertionError("the agent never answers a late-list question")

        class Fast:
            @staticmethod
            def serves(question):
                raise AssertionError("the fast path never runs after the late list")
        with tempfile.TemporaryDirectory() as tmp:
            server = NS(ontology=Host(Path(tmp) / "store.sqlite"),
                        case_runtime=NS(employee_gate=lambda a: ("gated", a)), actor=actor)
            routed = script.RoutedAgent(Agent(), "how many POs are late rn", fast=Fast,
                                        session=lambda host: (None, server))
            run = routed.run("p", None)
        self.assertEqual(run.agent, {"routed": "late_list", "outcome": "not_found"})
        self.assertTrue(run.final_text.startswith("The late-order check has not run for your "
                                                  "records yet"), run.final_text)
        block = json.loads(run.final_text.split("```json\n")[1].rstrip("`\n"))
        self.assertEqual(block["answerability"], "blocked")

        # a session that cannot start: the next route answers and the run says why
        answered = runner.AgentRun(events=[], final_text="x", usage={}, agent={})

        class Answering:
            def run(self, prompt, host):
                return answered

        class NoFast:
            @staticmethod
            def serves(question):
                return False

        def broken(host):
            raise RuntimeError("no session")
        run = script.RoutedAgent(Answering(), "how many POs are late rn", fast=NoFast,
                                 session=broken).run("p", None)
        self.assertEqual(run.agent, {"routed": "agent",
                                     "declined": ["late_list_unavailable:RuntimeError"]})

    def test_the_late_list_answer_is_graded_on_the_orders_it_names(self):
        """The values of a late-list answer are the late orders' names as the session
        served them, on records its facts cite; a record no fact cites adds nothing."""
        from types import SimpleNamespace as NS
        h1, h2 = "api://public.app_purchase_order/1@g-1", "api://public.app_purchase_order/2@g-1"
        payload = NS(direct_answer="1 purchase order you can see is late.", outcome="answered",
                     facts=[NS(text="PO4154: 20 days late. Due 2026-10-12.",
                               sources=[NS(handle=h1)])])

        def card(handle, number):
            return {"name": "plx.object", "is_error": False, "result": {"object": {
                "display": {"origin": {"citation": handle, "field_path": "row.po_number"},
                            "untrusted_text": number}}}}
        text = script.late_text(payload, [card(h1, "PO4154"), card(h2, "PO4155"),
                                          card(h1, "PO4154")])
        values = json.loads(text.split("```json\n")[1].rstrip("`\n"))["values"]
        self.assertEqual(values, [{"name": "listed_purchase_order_1", "value": "PO4154",
                                   "citations": [{"l1_handle": h1,
                                                  "field_path": "row.po_number"}]}])

    def test_the_profit_answer_is_graded_on_what_it_shows_and_its_cited_inputs(self):
        """The words are the app's answer; the values are the passed plan inputs on the
        records the answer's facts cite, never an input it does not show or one that
        failed its check."""
        from types import SimpleNamespace as NS
        h1, h2 = "bc://p/c/salesInvoices/1@g-1", "bc://p/c/salesInvoiceLines/9@g-2"
        payload = NS(direct_answer="1-Shoppe is profitable: margin 99.52%.", outcome="answered",
                     facts=[NS(text="Order SO1: revenue $100.00 CAD.",
                               sources=[NS(handle=h1)])])

        def entry(name, value, handle, field, check="passed"):
            return {"name": name, "value": value, "check": check,
                    "citation": {"l1_handle": handle, "field_path": field}}
        events = [{"name": "plx.investigate", "is_error": False, "result": {"answer": [
            entry("invoice", {"untrusted_text": "INV1"}, h1, "row.number"),
            entry("status", "Open", h1, "row.status", check="failed"),
            entry("revenue", "100", h2, "row.amountExcludingTax")]}}]
        text = script.profit_text(payload, events)
        said, block = text.split("\n\n```json\n")
        self.assertEqual(said, "1-Shoppe is profitable: margin 99.52%.\n"
                               "- Order SO1: revenue $100.00 CAD.")
        values = json.loads(block.rstrip("`\n"))["values"]
        self.assertEqual(values, [{"name": "invoice_1", "value": "INV1", "citations": [
            {"l1_handle": h1, "field_path": "row.number"}]}])

    def test_in_process_session_starts_never_share_their_environment(self):
        """Two scoreboard workers starting sessions at once each start with their own
        ticket, and the environment is as it was afterwards."""
        import os
        import threading
        import time
        from scripts import eval_real_agent as real
        seen, key = [], "PLX_EVAL_TEST_TICKET"

        def start(ticket):
            def build():
                time.sleep(0.05)
                seen.append((ticket, os.environ.get(key)))
                return ticket
            return real._in_process(build, {key: ticket})
        threads = [threading.Thread(target=start, args=(f"t{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(seen), [(f"t{i}", f"t{i}") for i in range(4)])
        self.assertNotIn(key, os.environ)

    def test_last_price_paid_is_a_posted_invoice_never_a_draft_order(self):
        w = world()
        item = bc("items", {"id": "i1", "number": "R-9", "displayName": "Shea"}, 30)
        draft = bc("purchaseOrders", {"id": "o9", "number": "PO109", "status": "Draft",
                                      "vendorName": "Draft Vendor", "orderDate": "2026-10-30"}, 31)
        draft_line = bc("purchaseOrderLines", {"id": "ol9", "documentId": "o9", "itemId": "i1",
                                               "lineType": "Item", "directUnitCost": "99"}, 32)
        posted = bc("purchaseInvoices", {"id": "v1", "status": "Paid", "vendorName": "Paid Vendor",
                                         "invoiceDate": "2026-08-01"}, 33)
        open_ = bc("purchaseInvoices", {"id": "v2", "status": "Draft", "vendorName": "Unposted",
                                        "invoiceDate": "2026-09-01"}, 34)
        lines = [bc("purchaseInvoiceLines", {"id": f"vl{n}", "documentId": doc, "itemId": "i1",
                                             "lineType": "Item", "unitCost": cost,
                                             "unitOfMeasureCode": "KG"}, 35 + n)
                 for n, (doc, cost) in enumerate((("v1", "17.58"), ("v2", "50")))]
        w = world()
        w.bc = [*w.bc, item, draft, draft_line, posted, open_, *lines]
        [found] = lk.item_supplier_price(w)
        self.assertEqual({f["id"]: f["value"] for f in found.facts},
                         {"vendor": "Paid Vendor", "unit_cost": "17.58"})

    def test_a_lot_on_several_rows_counts_once_and_the_bound_day_is_excluded(self):
        w = world()
        rows = [mirror(lk.RAW_LOT, {"id": i, "lotnoqc": lot, "status_c": status,
                                    "expirydate": day})
                for i, (lot, status, day) in enumerate((
                    ("L1", "Released", "2026-11-20"), ("l1 ", "Released", "2026-11-20"),
                    ("L2", "Released", "2026-12-31"), ("L3", "Released", "2027-01-01"),
                    ("L4", "Finished", "2026-11-10"), ("L4", "Released", "2026-11-10"),
                    ("L5", "Rejected", "2026-11-15"), ("L6", "Quarantine", "2026-11-15"),
                    ("L7", "Released", "2026-10-31"), ("L8", "Released", None)), start=10)]
        w.portal = {**w.portal, lk.RAW_LOT: rows}
        [count] = lk.lots_expiring_count(w)
        self.assertEqual(count.slots, {"until": "January 1"})
        # L1 once (trimmed, case-blind), L2, L4 (its used-up line aside, one line is still
        # Released); not L3 (the bound day), L5/L6 (not released), L7 (expired before
        # as_of) or L8 (no expiry).
        self.assertEqual(count.facts[0]["value"], "3")
        self.assertEqual(len(count.facts[0]["records"]), 4)    # the Released rows counted
        self.assertIn("exclusive", count.reads[0])
        # FileMaker's QC status is the schema's quality_controlled Lot.qc_status: a reader
        # without the marking sees no released-lot count.
        self.assertEqual(count.facts[0]["markings"], ["quality_controlled"])

    def test_a_group_sharing_a_record_with_a_visible_card_stays_dev(self):
        cards = [{"id": "LO-01", "family": "po_late", "instances": 1,
                  "question": "wheres {po} at"},
                 {"id": "LO-20", "family": "lot_expiry", "instances": 1,
                  "question": "when does lot {lot} expire"}]
        visible = [{"question": {"text": "Is PO#7001 received?"}, "root": {}}]
        for _ in range(2):   # whichever way the deal would go, the PO card stays dev
            items, keys, _notes = script.build(cards, "c" * 64, world(), {})
            script.split(items, keys, visible)
            self.assertEqual(next(i for i, k in zip(items, keys)
                                  if k["card"] == "LO-01")["split"], "dev")
            cards.reverse()

    def test_a_set_member_and_a_card_asking_about_it_share_a_split(self):
        items = [{"id": "a", "question": {"text": "what is open with Vendor A"}, "root": {}},
                 {"id": "b", "question": {"text": "did the vendor confirm PO#4166"},
                  "root": {}}]
        keys_ = [{"facts": [{"kind": "set", "members": [["PO#4166", "PO4166"]]}]},
                 {"facts": [{"kind": "absent"}]}]
        shadows = [script._shadow(i, k) for i, k in zip(items, keys_)]
        self.assertEqual(partition.groups(shadows), [[0, 1]])

    def test_a_key_newer_than_the_projection_is_a_freshness_gap_and_files_are_private(self):
        fact = dict(LATE, records=[H_PO])
        self.assertEqual(script.freshness_gap([fact], lambda h: "other"), ["late"])
        self.assertEqual(script.freshness_gap([fact], lambda h: "absent"), [])
        stale = dict(key(LATE), freshness_gap=["late"])
        got = grade(answer("No idea.", answerability="blocked"), stale)
        self.assertEqual((got["outcome"], got["codes"][0]),
                         ("freshness_gap", "key_newer_than_projection"))
        self.assertEqual(grade(answer("PO#9001 is late.", PO_VALUES), stale)["outcome"],
                         "correct")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k" / "x.json"
            script._write_json(path, {"a": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_report_says_the_items_with_no_key_beside_the_rate(self):
        rows = [{"item_id": f"i{n}", "split": split, "outcome": outcome, "codes": []}
                for n, (split, outcome) in enumerate([
                    ("dev", "correct"), ("dev", "false"), ("dev", "no_key"),
                    ("held_out", "correct"), ("held_out", "no_key"), ("held_out", "no_key"),
                    ("dev", "not_run")])]
        doc = script.report(rows, reader="vince", evidence_label="fixture")
        self.assertEqual((doc["totals"]["graded"], doc["totals"]["correct"],
                          doc["totals"]["no_key"]), (3, 2, 3))
        self.assertEqual([doc["by_split"][s]["no_key"] for s in ("dev", "held_out")], [1, 2])
        self.assertEqual(doc["headline"]["all"], "2/3 correct (66.7%), 1 false; 3 with no "
                                                 "key, not in the denominator")
        self.assertIn("1/1 correct (100.0%)", doc["headline"]["held_out"])
        self.assertEqual(doc["routing"]["agent"]["runs"], 3)   # graded runs only

    def test_rekey_judges_time_as_of_the_run_and_keeps_a_pending_rule(self):
        cards = [{"id": "LO-06", "family": "late_po_count", "instances": 1,
                  "question": "how many POs are late", "rule_pending": "owner"}]
        seen = {}

        def load(state, now=None):
            seen["now"] = now
            return world(), {}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(script, "load_cards", lambda: (cards, "c" * 64)), \
                mock.patch.object(script, "held_out_dir", lambda _dev: Path(tmp) / "held"), \
                mock.patch.object(script, "load_world", load), \
                mock.patch.object(script, "projection_versions", lambda _db: lambda h: "same"):
            out = __import__("io").StringIO()
            with __import__("contextlib").redirect_stdout(out):
                self.assertEqual(script.main(["keys", "--dev-dir", f"{tmp}/dev"],
                                             world=world()), 0)
                pairs = script.load_split(Path(tmp) / "dev") + script.load_split(
                    Path(tmp) / "held")
                self.assertEqual(pairs[0][1]["rule_pending"], "owner")
                self.assertEqual(script.main(
                    ["rescore", "--dev-dir", f"{tmp}/dev", "--work-dir", f"{tmp}/w",
                     "--split", "dev" if pairs[0][0]["split"] == "dev" else "held_out",
                     "--rekey-state", tmp]), 0)
        self.assertEqual(seen["now"], NOW)   # the run's own moment, never today

    def test_a_record_missing_from_a_projected_relation_is_a_freshness_gap(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "p.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE object_versions (l1_handle TEXT, superseded_at TEXT)")
            conn.execute("INSERT INTO object_versions VALUES (?, NULL)", (H_PO,))
            conn.commit()
            conn.close()
            check = script.projection_versions(db)
            self.assertEqual(check(H_PO), "same")
            newer = fx.handle("public.app_purchase_order", {"id": "po-1"}, 9)
            self.assertEqual(check(newer), "other")
            created = fx.handle("public.app_purchase_order", {"id": "po-2"}, 9)
            self.assertEqual(check(created), "other")    # captured after it was built
            unheld = fx.handle("public.app_purchase_order", {"id": "po-3"}, 5)
            self.assertEqual(check(unheld), "absent")    # older, simply not projected
            self.assertEqual(check(fx.handle("public.app_quote", {"id": "q"}, 1)), "absent")

    def test_a_message_mailed_in_after_the_projections_mail_capture_is_a_freshness_gap(self):
        """A mail handle names no capture number: a message the projection lacks is newer
        than it only when the mail capture first recorded it after the mail capture the
        projection was built from; an older message it lacks says nothing."""
        import sqlite3
        held = "graph-mail://mb1/m1@ck1"
        with tempfile.TemporaryDirectory() as tmp:
            db, mail = Path(tmp) / "p.sqlite", Path(tmp) / "mail.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE object_versions (l1_handle TEXT, superseded_at TEXT)")
            conn.execute("CREATE TABLE store_meta (key TEXT, value TEXT)")
            conn.execute("INSERT INTO object_versions VALUES (?, NULL)", (held,))
            conn.execute("INSERT INTO store_meta VALUES ('freshness', ?)", (json.dumps([
                {"source": "bc", "captured_at": "2026-11-01T00:00:00Z"},
                {"source": "graph_mail", "captured_at": "2026-10-30T15:00:00Z"}]),))
            conn.commit()
            conn.close()
            conn = sqlite3.connect(mail)
            conn.execute("CREATE TABLE messages (mailbox_id TEXT, message_id TEXT, "
                         "recorded_at TEXT)")
            conn.executemany("INSERT INTO messages VALUES (?,?,?)", [
                ("mb1", "m1", "2026-10-29T00:00:00Z"), ("mb1", "m2", "2026-11-01T14:00:00Z"),
                ("mb1", "m3", "2026-10-29T00:00:00Z"), ("mb1", "m3", "2026-11-01T14:00:00Z")])
            conn.commit()
            conn.close()
            check = script.projection_versions(db, mail)
            self.assertEqual(check(held), "same")
            self.assertEqual(check("graph-mail://mb1/m2@ck2"), "other")   # mailed in since
            self.assertEqual(check("graph-mail://mb1/m3@ck9"), "absent")  # held before: not
            self.assertEqual(check("graph-mail://mb1/m4@ck1"), "absent")  # never captured
            self.assertEqual(check("graph-mail://garbled"), "absent")
            fact = {"id": "last_message", "kind": "message", "records": [
                "graph-mail://mb1/m2@ck2"]}
            self.assertEqual(script.freshness_gap([fact], check), ["last_message"])
            # no mail capture beside the projection: nothing is said newer
            self.assertEqual(script.projection_versions(db, Path(tmp) / "none.db")(
                "graph-mail://mb1/m2@ck2"), "absent")

    def test_the_other_systems_families_read_their_own_rows(self):
        from evals import live_ops_more as more
        w = world()
        w.bc = [*w.bc,
                bc("items", {"id": "i1", "number": "R-1", "displayName": "Shea",
                             "type": "Inventory", "inventory": "12.5",
                             "baseUnitOfMeasureCode": "KG"}, 50),
                bc("salesOrders", {"id": "s1", "number": "S-ORD1", "customerName": "Acme",
                                   "fullyShipped": False}, 51),
                bc("salesOrders", {"id": "s2", "number": "S-ORD2", "customerName": "Acme",
                                   "fullyShipped": True}, 52)]
        w.portal = {**w.portal,
                    more.SAGE_CUSTOMER: [mirror(more.SAGE_CUSTOMER, {
                        "id": "c", "namecust": "Old Co", "amtbaldueh": "120.50",
                        "amtbalduet": "120.50"})] + [
                        # one name on several cards (a chain): no one balance, not keyed
                        mirror(more.SAGE_CUSTOMER, {
                            "id": f"g{i}", "namecust": name, "amtbaldueh": "641.56",
                            "amtbalduet": "641.56"})
                        for i, name in enumerate(("Chain Gym", "CHAIN GYM Inc."))],
                    # the header's shipdate is its invoice date, never the ship date
                    more.SAGE_ORDER: [mirror(more.SAGE_ORDER, {
                        "id": f"o{n}", "ordnumber": f"100009{n}  ", "complete": "5",
                        "shipdate": "20170815"}) for n in (1, 2)],
                    more.SAGE_SHIPMENT: [mirror(more.SAGE_SHIPMENT, {
                        "id": f"h{i}", "ordnumber": f"100009{n}  ", "shidate": day})
                        for i, (n, day) in enumerate([(1, "20170810"), (2, "20170412"),
                                                      (2, "20170413")])],
                    }
        w.bc = [*w.bc,
                bc("purchaseOrders", {"id": "o5", "number": "PO100500", "status": "Open",
                                      "fullyReceived": False}, 53),
                bc("purchaseOrderLines", {"id": "l5", "documentId": "o5", "lineType": "Item",
                                          "lineObjectNumber": "R-1", "quantity": "10"}, 54),
                bc("purchaseReceipts", {"id": "r5", "orderNumber": "PO100500"}, 55),
                bc("purchaseReceiptLines", {"id": "rl5", "documentId": "r5",
                                            "lineType": "Item", "lineObjectNumber": "R-1",
                                            "quantity": "7"}, 56)]
        w.mc = [{"handle": "mc://live/tasks/TASK-7@g-1",
                 "row": {"id": "TASK-7", "stage": "merged", "accountableOwner": "vince"}}]
        values = lambda family: {f["id"]: f.get("value") or f.get("members")
                                 for c in family(w) for f in c.facts}
        self.assertEqual(values(more.bc_inventory), {"on_hand": "12.5"})
        self.assertEqual(values(more.bc_open_sos), {"open_orders": [["S-ORD1"]]})
        self.assertEqual(values(more.sage_balance), {"sage_balance": "120.5"})
        shipped = {c.identifier: c.facts[0] for c in more.sage_order_shipped(w)}
        self.assertEqual(shipped["1000091"]["value"], "2017-08-10")
        self.assertEqual([m[0] for m in shipped["1000092"]["members"]],
                         ["2017-04-12", "2017-04-13"])
        self.assertEqual(len(shipped["1000092"]["records"]), 3)
        self.assertEqual(lo.key_problems(key(*shipped.values())), [])
        both = shipped["1000092"]
        self.assertTrue(lo.stated(both, "It shipped on April 12 and 2017-04-13.", []))
        self.assertFalse(lo.stated(both, "It shipped on 2017-04-13.", []))
        self.assertEqual(values(more.bc_receipt_vs_po),
                         {"ordered": "10", "received": "7", "short": "short"})
        self.assertEqual(values(more.mc_task), {"stage": "merged", "owner": "vince"})

    def test_a_text_fact_with_a_dot_inside_is_one_fact_never_two_clauses(self):
        """An owner's e-mail address (an MC task's accountable owner) is stated when the
        answer writes it; its dots are not sentence ends. A full stop still ends a clause."""
        owner = {"id": "owner", "kind": "text", "value": "stephen@petrasoap.com"}
        said = "TASK-7 is at the backlog stage in Mission Control, owned by stephen@petrasoap.com."
        self.assertTrue(lo.stated(owner, said, []))
        self.assertFalse(lo.stated(owner, "It is not owned by stephen@petrasoap.com.", []))
        self.assertFalse(lo.stated(owner, "Owned by stephen. Petrasoap.com is the domain.", []))
        vendor = {"id": "vendor", "kind": "text", "value": "NINGBO SOMEWANG PACKAGING CO.,LTD"}
        self.assertTrue(lo.stated(vendor, "P-1 was last bought from NINGBO SOMEWANG PACKAGING "
                                          "CO.,LTD, at 0.035 USD per PCS.", []))
        # A full stop before closing marks still ends the clause: the "not" of the bold
        # sentence never reaches the vendor named after it.
        acme = {"id": "vendor", "kind": "text", "value": "Acme Packaging"}
        for said in ("**Price not shown.** Acme Packaging supplies it.",
                     "(Price not shown.) Acme Packaging supplies it.",
                     '"Price not shown." Acme Packaging supplies it.'):
            self.assertTrue(lo.stated(acme, said, []), said)

    def test_a_teams_message_names_a_po_by_its_own_number_never_its_digits_alone(self):
        """A Portal PO whose number is no PO token (PO2-FM87) is named only whole: another
        order's PO-287 shares its digits and is not it (a dev key once took it for it)."""
        from evals import live_ops_more as more
        w = world()
        w.portal = {**w.portal, lk.PO: [*w.portal[lk.PO], mirror(lk.PO, {
            "id": "p2", "po_number": "PO2-FM87", "status": "DRAFT"})]}
        message = lambda n, text, at: {"handle": f"teams-chat://c/{n}@t-{n}", "text": text,
                                       "at": at}
        named = lambda *messages: {c.identifier: c.facts[0]["records"] for c in
                                   more.teams_po_discussion(dataclasses.replace(
                                       w, teams=list(messages)))}
        self.assertEqual(named(message(1, "bulk left over from PO-287", "2026-10-28T15:00:00Z"),
                               message(2, "PO 7001 lands Friday", "2026-10-27T15:00:00Z")),
                         {"PO#7001": ["teams-chat://c/2@t-2"]})
        self.assertEqual(named(message(3, "Is PO2-FM-87 approved?", "2026-10-29T15:00:00Z")),
                         {"PO2-FM87": ["teams-chat://c/3@t-3"]})

    def test_any_captured_copy_of_the_keyed_message_is_that_message_and_no_other_is(self):
        """One email sits in each mailbox it reached, each copy its own captured record
        (its own Graph id) with one internet message id: an answer citing any copy cites
        the keyed message. Another message stays another message, however alike: the same
        subject and sender under another internet id, a message without an id beside one
        with it, or (no id at all) the same subject and sender sent at another time."""
        import sqlite3
        customers = [{"fields": {"displayName": "Acme", "email": "ap@acme.example"}}]
        sent = "2026-10-20T10:00:00Z"
        rows = [  # (mailbox, Graph id, internet id, subject, sent, received)
            ("mbA", "k1", "<a1@acme>", "Re: order", sent, "2026-10-20T10:00:05Z"),
            ("mbB", "k1b", "<a1@acme>", "Re: order", sent, "2026-10-20T10:00:09Z"),
            ("mbC", "k1c", " <a1@acme> ", "Re: order", sent, "2026-10-20T10:00:07Z"),
            ("mbA", "k2", "<a2@acme>", "Re: order", sent, "2026-10-19T10:00:00Z"),
            ("mbB", "k3", "", "Re: order", sent, "2026-10-18T10:00:00Z")]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "capture.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE messages (l1_handle TEXT, message_id TEXT, record TEXT, "
                         "superseded_at TEXT, removed_reason TEXT)")
            for box, mid, imid, subject, at, received in rows:
                conn.execute("INSERT INTO messages VALUES (?, ?, ?, NULL, NULL)", (
                    f"graph-mail://{box}/{mid}@ck", mid, json.dumps(
                        {"from": "Ap@Acme.example", "to": ["us@petra.example"],
                         "subject": subject, "sent_at": at, "received_at": received,
                         "internet_message_id": imid})))
            mail_tables(conn)
            conn.commit()
            conn.close()
            found = script.customer_mail(db, customers, senders=CLEARED)
        from evals import live_ops_more as more
        [candidate] = more.customer_last_mail(world(customer_mail=found))
        fact = candidate.facts[0]
        self.assertEqual((fact["message_id"], fact["copies"]), ("k1b", ["k1", "k1c"]))
        self.assertEqual(lo.key_problems(key(fact)), [])
        cites = lambda handle: [{"system": "graph-mail", "identity": lo._identity(handle)}]
        for copy in ("graph-mail://mbA/k1@ck2", "graph-mail://mbC/k1c@ck"):
            self.assertTrue(lo.evidenced(fact, cites(copy)), copy)
        for other in ("graph-mail://mbA/k2@ck", "graph-mail://mbB/k3@ck"):
            self.assertFalse(lo.evidenced(fact, cites(other)), other)
        # without an internet id: subject, sender and sent time together, all present
        bare = lambda mid, **kw: {"message_id": mid, "subject": "Re: order",
                                  "from": "ap@acme.example", "sent_at": sent, **kw}
        alike = [bare("n2", **{"from": "AP@acme.example"}), bare("n3", sent_at="2026-10-21"),
                 bare("n4", subject="Re: order 2"), bare("n5", internet_message_id="<x@y>")]
        self.assertEqual(lk.copies(bare("n1"), alike), ["n2"])
        self.assertEqual(lk.copies(bare("n1", sent_at=""), [bare("n6", sent_at="")]), [])
        for bad in ("k1", [""], [None], {"k1": 1}):
            self.assertIn("fact0_copies", lo.key_problems(key(dict(fact, copies=bad))), bad)
        self.assertIn("fact0_copies", lo.key_problems(key(dict(DUE, copies=["k1"]))))

    def test_mail_with_a_customer_is_theirs_or_ours_and_a_free_mail_sender_is_never_us(self):
        """"Ours" is our own domains (every captured mailbox's, as the app reads them),
        less free-mail providers': a mailbox with a gmail.com alias never makes a gmail
        sender's mass mailing to a customer address our mail with them, while our help
        desk's host under our name answering the customer is ours and, the newest, is
        the key. Two copies of it delivered at one instant are one message, not a tie."""
        import sqlite3
        customers = [{"fields": {"displayName": "Uni", "email": "office@uni.example"}}]
        mails = [  # (mailbox, id, sender, to, subject, received)
            ("mbA", "u1", "prof@uni.example", ["us@petra.example"], "Quote",
             "2026-10-01T10:00:00Z"),
            ("mbA", "g1", "someone@gmail.com", ["a@uni.example", "us@petra.example"],
             "Simulation", "2026-10-05T10:00:00Z"),
            ("mbA", "d1", "info@petra.zendesk.com", ["prof@uni.example"],
             "[Desk] Re: Quote", "2026-10-03T10:00:00Z"),
            ("mbB", "d1b", "info@petra.zendesk.com", ["prof@uni.example"],
             "[Desk] Re: Quote", "2026-10-03T10:00:00Z")]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "capture.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE messages (l1_handle TEXT, message_id TEXT, record TEXT, "
                         "superseded_at TEXT, removed_reason TEXT)")
            conn.execute("CREATE TABLE mailboxes (upn TEXT, identities TEXT)")
            conn.execute("INSERT INTO mailboxes VALUES ('us@petra.example', ?)",
                         (json.dumps(["us@petra.example", "petra.shop@gmail.com"]),))
            for box, mid, sender, to, subject, at in mails:
                conn.execute("INSERT INTO messages VALUES (?, ?, ?, NULL, NULL)", (
                    f"graph-mail://{box}/{mid}@ck", mid, json.dumps(
                        {"from": sender, "to": to, "subject": subject, "received_at": at,
                         "sent_at": at, "internet_message_id": f"<{mid[:2]}@x>",
                         "mailbox_upn": "us@petra.example"})))
            mail_tables(conn)
            conn.commit()
            conn.close()
            found = script.customer_mail(db, customers, senders=CLEARED)
        self.assertEqual(sorted(m["message_id"] for m in found), ["d1", "d1b", "u1"])
        from evals import live_ops_more as more
        [candidate] = more.customer_last_mail(world(customer_mail=found))
        fact = candidate.facts[0]
        self.assertEqual((fact["message_id"], fact["copies"]), ("d1", ["d1b"]))

    def test_a_mail_names_a_po_by_its_own_number_never_its_digits_alone(self):
        """As for Teams: the vendor's PO-287 mail is not the last mail about Portal PO2-FM87
        (same digits, another order); one naming PO2-FM-87 whole is, and PO 7001 names
        PO#7001. An absence key still stays clear of any message a digit run could name."""
        w = world()
        w.portal = {**w.portal,
                    lk.PO: [*w.portal[lk.PO], mirror(lk.PO, {
                        "id": "p2", "po_number": "PO2-FM87", "status": "DRAFT",
                        "custom_fields": {"fmGeneratedPoId": "87"}})],
                    lk.FM_PO: [mirror(lk.FM_PO, {"id": 87, "ponumber": "87",
                                                 "emailrep": "rep@vendor.example"})]}
        w.portal[lk.PO][0]["row"]["custom_fields"] = {"fmGeneratedPoId": "87"}
        mail = lambda n, subject, at: {"handle": f"graph-mail://m/{n}@v", "message_id": f"m{n}",
                                       "subject": subject, "preview": "", "received_at": at,
                                       "from": "rep@vendor.example"}
        last = lambda *messages: {c.identifier: c.facts[0]["message_id"] for c in
                                  lk.po_vendor_last_mail(dataclasses.replace(
                                      w, mail=list(messages), _cache={}))}
        self.assertEqual(last(mail(1, "Re: PO-287 left over bulk", "2026-10-28T15:00:00Z"),
                              mail(2, "PO 7001 ships Friday", "2026-10-27T15:00:00Z")),
                         {"PO#7001": "m2"})
        self.assertEqual(last(mail(3, "Is PO2-FM-87 approved?", "2026-10-29T15:00:00Z")),
                         {"PO2-FM87": "m3"})
        copy = lambda n, imid, at: {**mail(n, "Is PO2-FM-87 approved?", at),
                                    "internet_message_id": imid}
        [keyed] = lk.po_vendor_last_mail(dataclasses.replace(w, _cache={}, mail=[
            copy(4, "<q@v>", "2026-10-29T15:00:00Z"), copy(5, "<q@v>", "2026-10-29T15:00:03Z"),
            copy(6, "<other@v>", "2026-10-28T15:00:00Z")]))
        self.assertEqual((keyed.facts[0]["message_id"], keyed.facts[0]["copies"]),
                         ("m5", ["m4"]))
        absent = lambda *messages: [c.identifier for c in lk.po_no_vendor_mail(
            dataclasses.replace(w, mail=list(messages), _cache={},
                                named_in_text=lambda _token: False))]
        self.assertEqual(absent(mail(1, "Re: PO-287", "2026-10-28T15:00:00Z")), ["PO#7001"])
        self.assertEqual(absent(mail(2, "PO-7001 ships", "2026-10-28T15:00:00Z")), [])

    def test_a_document_fact_is_stated_only_by_citing_that_record(self):
        doc = {"id": "teams_message", "kind": "document", "records": [H_PO],
               "allowed": ["api"]}
        self.assertEqual(grade(answer("Discussed.", PO_VALUES[1:]), key(doc))["outcome"],
                         "correct")
        other = dict(doc, records=[H_AR])
        self.assertEqual(grade(answer("Discussed.", PO_VALUES[1:]), key(other))["outcome"],
                         "abstained")

    def test_committed_cards_name_known_families_and_carry_no_value(self):
        cards, _sha = script.load_cards()
        self.assertGreaterEqual(sum(c["instances"] for c in cards), 150)
        systems = {lk._more.SYSTEMS.get(c["family"], "BC+Portal") for c in cards}
        for system in ("BC", "Portal", "Sage", "FileMaker", "mail", "SharePoint", "Teams",
                       "MC"):
            self.assertTrue(any(system in s for s in systems), system)
        for card in cards:
            self.assertTrue(1 <= len(card.get("messy") or []) <= 2, card["id"])
            self.assertNotRegex(card["question"], r"\bIn the [a-z-]+ use case")
            self.assertFalse(set(card) - script.CARD_FIELDS)


if __name__ == "__main__":
    unittest.main()
