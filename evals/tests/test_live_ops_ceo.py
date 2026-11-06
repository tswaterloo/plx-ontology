"""The CEO suite of live-ops cards: its families' keys, the harder grading (an action or a
made-up citation is false, a wrong answer costs more than an abstention) and its split.

Evidence label: isolated-fixture (synthetic rows, a fake host, a scripted agent)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evals import live_ops as lo
from evals import live_ops_ceo as ceo
from evals import live_ops_keys as lk
from evals import runner
from evals.tests import fixtures as fx
from evals.tests import test_live_ops as base
from scripts import eval_live_ops as script


def run_with(the_answer: dict, *, act: bool = False) -> dict:
    def script_(prompt, call):
        call("plx.find", {"relation": "public.app_purchase_order"})
        if act:
            call("plx.case_propose", {"case_id": "C1", "action": "follow_up"})
        return fx.final_text(the_answer)
    transcript = runner.record(base.item(), runner.ScriptedAgent(script_), base.Host())
    return lo.verdict(base.item(), base.key(base.LATE, base.DUE), transcript, reader="vince")


class HarderGradingTests(unittest.TestCase):
    def test_an_action_or_a_citation_no_tool_served_makes_a_right_answer_false(self):
        text = "PO#9001 is late: still SUBMITTED, due 2026-09-01."
        self.assertEqual(run_with(base.answer(text, base.PO_VALUES))["outcome"], "correct")
        acted = run_with(base.answer(text, base.PO_VALUES), act=True)
        self.assertEqual((acted["outcome"], acted["codes"]), ("false", ["action_taken"]))
        made_up = fx.handle("public.app_purchase_order", {"id": "po-404"}, 7)
        invented = run_with(base.answer(text, [*base.PO_VALUES, base.cited(
            "source", "PO#9001", made_up, "row.po_number")]))
        self.assertEqual((invented["outcome"], invented["codes"]),
                         ("false", ["citation_unresolved"]))

    def test_none_against_a_count_is_false_and_against_nothing_is_the_honest_answer(self):
        w = base.world()
        w.bc = [*w.bc, base.bc("purchaseReceipts", {"id": "r1", "postingDate": "2026-09-01"},
                               40)]
        found = {c.slots["days"]: c.facts[0] for c in ceo.bc_receipts_window(w)}
        none = "I found no BC posted purchase receipts dated in that window."
        wrong = lo.contradicted(found["90"], none, [], "how many receipts in 90 days")
        self.assertEqual(wrong, ["none_against_a_figure"])
        self.assertEqual(lo.contradicted(found["90"], "1 receipt was posted.", [], ""), [])
        self.assertTrue(lo.stated(found["30"], none, []))

    def test_a_wrong_answer_costs_nine_right_ones_and_skipped_cards_are_counted(self):
        rows = ([{"item_id": f"c{n}", "split": "dev", "outcome": "correct", "codes": [],
                  "card": "CEO-01", "family": "po_first_mail"} for n in range(18)]
                + [{"item_id": "f", "split": "dev", "outcome": "false", "codes": [],
                    "card": "CEO-03", "family": "po_vendor_confirmed"},
                   {"item_id": "a", "split": "held_out", "outcome": "abstained", "codes": [],
                    "card": "CEO-03", "family": "po_vendor_confirmed"}])
        skipped = [{"card": "CEO-01", "wanted": 18, "made": 18},
                   {"card": "CEO-15", "wanted": 3, "made": 0}]
        doc = script.report(rows, reader="vince", evidence_label="fixture", skipped=skipped)
        self.assertEqual(doc["scoreboard"]["all"]["score"], round((18 - 9) / 20, 4))
        self.assertFalse(doc["scoreboard"]["all"]["false_bar_met"])   # 1/20 > 1%
        self.assertEqual((doc["skipped"], [c["card"] for c in doc["skipped_cards"]]),
                         (3, ["CEO-15"]))
        self.assertEqual(doc["per_family"]["po_vendor_confirmed"]["false"], 1)
        self.assertEqual(doc["per_card"]["CEO-01"]["correct"], 18)
        # no skipped file: nothing is claimed
        self.assertIsNone(script.report(rows, reader="vince", evidence_label="x")["skipped"])

    def test_an_unknown_suite_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cards.json"
            path.write_text(json.dumps({"format": script.CARDS_FORMAT, "cards": [
                {"id": "X", "family": "po_late", "instances": 1, "question": "q {po}",
                 "suite": "vip"}]}))
            with self.assertRaisesRegex(ValueError, "card_suite_unknown"):
                script.load_cards(path)


def mail(n: int, subject: str, sender: str, at: str, preview: str = "") -> dict:
    return {"handle": f"graph-mail://m/{n}@g-1", "message_id": f"m{n}", "from": sender,
            "subject": subject, "preview": preview, "received_at": at, "sent_at": at,
            "internet_message_id": f"<{n}@x>", "external": not sender.endswith("@us.test")}


def po_world(messages=(), **extra) -> lk.World:
    w = base.world(**extra)
    w.portal[lk.PO][0]["row"].update({"created_at": "2026-08-01T12:00:00Z",
                                      "custom_fields": {"fmGeneratedPoId": 77}})
    w.portal[lk.FM_PO] = [base.mirror(lk.FM_PO, {"id": 77, "ponumber": "PO#7001",
                                                 "emailrep": "rep@vendor.test",
                                                 "datearrive": "2026-09-15"})]
    w.mail = list(messages)
    return w


class FamilyTests(unittest.TestCase):
    def test_a_vendor_order_confirmation_is_yes_with_its_first_day_and_a_request_is_not(self):
        messages = [mail(1, "Please confirm PO#7001 ship-to", "rep@vendor.test",
                         "2026-08-02T15:00:00Z"),
                    mail(2, "Order Confirmation for PO#7001", "rep@vendor.test",
                         "2026-08-03T15:00:00Z"),
                    mail(3, "RE: Order Confirmation for PO#7001", "rep@vendor.test",
                         "2026-08-05T15:00:00Z"),
                    mail(4, "Order Confirmation for PO#7001", "sales@other.test",
                         "2026-08-01T15:00:00Z")]
        found = ceo.po_vendor_confirmed(po_world(messages))
        self.assertEqual(len(found), 1)
        facts = {f["id"]: f for f in found[0].facts}
        self.assertEqual((facts["confirmation"]["message_id"], facts["confirmed_on"]["value"]),
                         ("m2", "2026-08-03"))
        self.assertEqual(ceo.po_vendor_confirmed(po_world(messages[:1])), [])

    def test_a_confirmation_naming_another_copys_number_counts_and_a_reused_one_does_not(self):
        """Truth rules 2026-11-06: the order's BC copy (joined by renumberedTo, a stored
        key) is the same order, so the vendor confirming its BC number confirms it; a number
        another order also holds confirms nothing."""
        def found(bc_number: str, other: bool) -> list:
            w = po_world([mail(1, f"Order Confirmation for {bc_number}", "rep@vendor.test",
                               "2026-08-03T15:00:00Z")])
            w.portal[lk.PO][0]["row"]["custom_fields"]["renumberedTo"] = bc_number
            if other:   # a different Portal order using the same number
                w.portal[lk.PO].append(base.mirror(lk.PO, {"id": "p9", "po_number": bc_number,
                                                           "status": "SUBMITTED"}))
            w.bc = [*w.bc, base.bc("purchaseOrders", {"id": "o1", "number": bc_number,
                                                      "status": "Open",
                                                      "fullyReceived": False}, 60)]
            return ceo.po_vendor_confirmed(w)
        self.assertEqual(len(found("PO100701", other=False)), 1)
        self.assertEqual(found("PO100701", other=True), [])

    def test_the_original_email_is_the_first_in_the_vendor_exchange_after_the_po(self):
        messages = [mail(1, "PO#7001 old thread", "rep@vendor.test", "2026-07-01T15:00:00Z"),
                    mail(2, "New PO#7001", "buyer@us.test", "2026-08-01T16:00:00Z"),
                    mail(3, "RE: New PO#7001", "rep@vendor.test", "2026-08-02T16:00:00Z"),
                    mail(4, "PO#7001 spam", "x@third.test", "2026-08-01T13:00:00Z")]
        found = ceo.po_first_mail(po_world(messages))
        self.assertEqual([f["message_id"] for f in found[0].facts], ["m2"])
        # two different messages at the first second: no one original, no key
        tie = [*messages, mail(5, "PO#7001 too", "rep@vendor.test", "2026-08-01T16:00:00Z")]
        self.assertEqual(ceo.po_first_mail(po_world(tie)), [])

    def test_a_vendor_is_cleared_only_when_no_late_order_is_theirs(self):
        late = base.bc("purchaseOrders", {"id": "o9", "number": "PO7001", "status": "Open",
                                          "fullyReceived": False, "vendorName": "Late Co",
                                          "vendorNumber": "V9", "orderDate": "2026-08-01"}, 30)
        fine = base.bc("purchaseOrders", {"id": "o8", "number": "PO7002", "status": "Open",
                                          "fullyReceived": False, "vendorName": "Fine Co",
                                          "vendorNumber": "V8", "orderDate": "2026-08-01"}, 31)
        w = base.world()
        w.bc = [*w.bc, late, fine]
        w._cache["late"] = {"late": [{"numbers": ["PO7001"], "headers": [
            {"system": "bc", "citation": late["citation"]}]}], "groups": [], "counts": {}}
        self.assertEqual([c.identifier for c in ceo.vendor_nothing_late(w)], ["Fine Co"])
        # a late group no BC order carries: no vendor can be cleared
        w._cache["late"]["late"].append({"numbers": ["PO#1"], "headers": [
            {"system": "portal", "citation": "api://x"}]})
        self.assertEqual(ceo.vendor_nothing_late(w), [])

    def test_a_window_with_nothing_in_it_is_an_honest_none_never_a_zero_figure(self):
        w = base.world()
        w.bc = [*w.bc, base.bc("purchaseReceipts", {"id": "r1", "postingDate": "2026-09-01"}, 40)]
        found = {c.slots["days"]: c.facts[0] for c in ceo.bc_receipts_window(w)}
        self.assertEqual((found["30"]["kind"], found["90"]["kind"], found["90"]["value"]),
                         ("absent", "number", "1"))

    def test_a_name_several_sage_cards_hold_keys_every_account(self):
        from evals import live_ops_more as more
        w = base.world()
        w.portal[more.SAGE_CUSTOMER] = [
            base.mirror(more.SAGE_CUSTOMER, {"id": n, "idcust": c, "namecust": "Acme Ltd"})
            for n, c in ((1, "ACME01"), (2, "ACME02"))] + [
            base.mirror(more.SAGE_CUSTOMER, {"id": 3, "idcust": "SOLO01",
                                             "namecust": "Solo Inc"})]
        found = ceo.sage_same_name(w)
        self.assertEqual([(c.identifier, c.facts[0]["members"]) for c in found],
                         [("Acme Ltd", [["ACME01"], ["ACME02"]])])


class SplitTests(unittest.TestCase):
    def test_a_suite_never_moves_a_core_item_and_follows_a_held_out_record(self):
        core = [{"id": "LO-01", "family": "po_late", "instances": 1, "question": "{po}?"},
                {"id": "LO-20", "family": "lot_expiry", "instances": 1, "question": "{lot}?"}]
        ceo_cards = [{"id": "CEO-16", "family": "po_late", "instances": 1,
                      "question": "is {po} runnin late", "suite": "ceo"},
                     {"id": "CEO-18", "family": "lot_expiry", "instances": 1,
                      "question": "lot {lot} go bad when", "suite": "ceo"}]
        items, keys_, _ = script.build(core, "c" * 64, base.world(), {})
        script.split(items, keys_, [])
        before = {k["card"]: (i["id"], i["split"]) for i, k in zip(items, keys_)}
        items, keys_, _ = script.build(core + ceo_cards, "c" * 64, base.world(), {})
        script.split(items, keys_, [])
        after = {k["card"]: (i["id"], i["split"]) for i, k in zip(items, keys_)}
        self.assertEqual({c: after[c] for c in before}, before)
        # each CEO card asks about the record of a core card: it lands with it
        self.assertEqual(after["CEO-16"][1], before["LO-01"][1])
        self.assertEqual(after["CEO-18"][1], before["LO-20"][1])

    def test_the_committed_ceo_suite_covers_each_asked_use_case(self):
        cards, _sha = script.load_cards()
        mine = [c for c in cards if c.get("suite") == "ceo"]
        families = {c["family"] for c in mine}
        self.assertLessEqual(set(ceo.FAMILIES), families)
        self.assertGreaterEqual(sum(c["instances"] for c in mine), 75)
        # harder phrasings of the core families ride in the suite too
        core = {c["family"] for c in cards if c.get("suite") is None}
        self.assertGreaterEqual(len(families & core), 5)


if __name__ == "__main__":
    unittest.main()
