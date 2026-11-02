"""Live-ops cards: keys from system-of-record rows, the grading rule, readers, the split.

Evidence label: isolated-fixture (synthetic rows, a fake host, a scripted agent)."""

from __future__ import annotations

import copy
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
                "expected_date": "2026-09-01"}),
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


def mirror(relation: str, row: dict, n: int = 3) -> dict:
    return {"row": row, "citation": fx.handle(relation, {"id": row["id"]}, n)}


def world(changed: int = 0, **extra) -> lk.World:
    """A small invented world: one late Portal order, a batch on hold, a released lot, one
    customer with open AR, ``changed`` BC orders modified since yesterday."""
    portal = {
        lk.PO: [mirror(lk.PO, {"id": "p1", "po_number": "PO#7001", "status": "SUBMITTED",
                               "expected_date": "2026-09-15", "supplier_id": "s1"})],
        lk.PO_LINE: [mirror(lk.PO_LINE, {"id": "l1", "purchase_order_id": "p1",
                                         "product_id": "pr1", "quantity": "10",
                                         "quantity_received": "0"})],
        lk.BATCH: [mirror(lk.BATCH, {"id": "b1", "batch_number": "BX-1", "status": "Allocated",
                                     "status_qc": "On-Hold"})],
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

    def test_the_late_list_rules_decided_for_draft_twins(self):
        """A BC Draft twin with nothing
        received neither closes nor dates the order; Pending with FileMaker disputing it, or
        quantities FileMaker cannot vouch for, is never late; a released BC order outranks
        a Portal DRAFT."""
        def late(status="Pending", expected=None, received="0", fm_status="Pending",
                 fm_qty="10", qc=None, bc_status="Draft", fully=True):
            w = world()
            w.portal = {**w.portal, lk.PO: [mirror(lk.PO, {
                "id": "p1", "po_number": "PO#7001", "status": status,
                "expected_date": expected,
                "custom_fields": {"fmGeneratedPoId": 70, "renumberedTo": "PO100700"}})],
                lk.FM_PO: [mirror(lk.FM_PO, {"id": 70, "ponumber": "PO#7001",
                                             "status": fm_status})],
                lk.FM_PO_ITEM: [mirror(lk.FM_PO_ITEM, {"id": 701, "idpogen": 70,
                                                       "qty": fm_qty})],
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
        self.assertEqual(late(expected="2026-09-15", qc="10"), 0)   # FileMaker received all
        # a Draft received against is real activity:
        # its open line keeps the order open and its date dates it
        self.assertEqual(late(received="4"), 1)
        self.assertEqual(late(expected="2026-09-15", received="4"), 1)
        self.assertEqual(late(expected="2026-09-15", received="10"), 0)   # received in full
        self.assertEqual(late(status="DRAFT", bc_status="Open", fully=False), 1)  # rule 3

    def test_changed_since_yesterday_at_one_ten_and_sixty_records(self):
        self.assertEqual(lk.po_changed(world(0))[0].facts[0]["kind"], "absent")
        listed = lk.po_changed(world(1))[0].facts[0]
        self.assertEqual((listed["kind"], len(listed["members"])), ("set", 1))
        many = lk.po_changed(world(10))[0].facts[0]   # past LIST_MAX: a count, none lost
        self.assertEqual((many["kind"], many["value"], len(many["records"])),
                         ("number", "10", 10))
        sixty = lk.po_changed(world(60))[0].facts[0]
        self.assertEqual((sixty["value"], len(sixty["records"])), ("60", 60))

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
        outcomes = {row["card"]: row["outcome"] for row in doc["cards"]}
        self.assertEqual(outcomes, {"LO-15": "incomplete", "LO-20": "correct"})
        self.assertEqual(doc["totals"]["graded"], 2)
        self.assertEqual(sum(doc["by_split"][s]["runs"] for s in ("dev", "held_out")), 2)
        for value in ("4821.75", "2026-12-03", "LOT-55", "Fixture Soap"):
            self.assertNotIn(value, json.dumps(doc))

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
        rows = [mirror(lk.RAW_LOT, {"id": i, "lotnoqc": lot, "status_c": "Released",
                                    "expirydate": day})
                for i, (lot, day) in enumerate((("L1", "2026-11-20"), ("L1", "2026-11-20"),
                                                ("L2", "2026-12-31"), ("L3", "2027-01-01"),
                                                ("L4", "2026-11-10")), start=10)]
        rows.append(mirror(lk.RAW_LOT, {"id": 20, "lotnoqc": "L4", "status_c": "Released",
                                        "expirydate": "2026-11-11"}))   # rows disagree
        w.portal = {**w.portal, lk.RAW_LOT: rows}
        [count] = lk.lots_expiring_count(w)
        self.assertEqual(count.slots, {"until": "January 1"})
        self.assertEqual(count.facts[0]["value"], "2")        # L1 once, L2; not L3, not L4
        self.assertIn("exclusive", count.reads[0])

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
                        "amtbalduet": "120.50"})],
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
