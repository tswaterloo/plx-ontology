"""Status cards (``operating.status_card``) over the console fixture and the live check.

The fixture's purchase order is a Portal staging record with a FileMaker twin: no supplier
confirmation, no receipt and no expected date on either copy. Its card must name those
blockers with citations and a next step in department words, and say its FileMaker copy
in one line. The question plans F14 (why late) and F15 (supplier position) answer through
plx.investigate. Evidence label: fixture; LiveCheckTest is injected-transport.
"""

from __future__ import annotations

import contextlib
from datetime import date
import io
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import quote

from devloop import live_verify as lv
from integrations.http import SourceHttpError
from operating import session_identity
from operating import status_card as sc
from operating.case_runtime import CaseRuntime
from operator_ui import case_console
from operator_ui import object_workspace as objects
from operator_ui.tests import case_fixtures

ROOT = Path(__file__).resolve().parents[2]
AS_OF = date(2026, 10, 20)
DEPARTMENTS = {"Procurement", "Warehouse", "Finance", "Shipping", "Production", "Operations",
               "Quality", "Sales & Business Development", "the owner of the record"}
# The card's own words never carry these (independent of the module's own check).
CODE = re.compile(r"\b[a-z]+_[a-z_]+\b|\b(?:PROC|WH|FIN|SALES|PROD|SHIP|OPS)\b|plx\.|"
                  r"[A-Z][a-z]+[A-Z]\w*:")   # an object id, not "FileMaker."


DUE = {"value": "2026-09-01", "property": "expected_date",
       "citation": {"l1_handle": "api://x/1", "field_path": "row.expected"}}


def late_entry(**extra):
    """A po_delivery_window lateness verdict: late against its governing date."""
    return {"stage": "receipt", "root": "PO", "state": "late", "late": True,
            "days_late": 49, "governing": "expected", "governing_date": "2026-09-01",
            "measured_against": "today", "values": [DUE], **extra}


def own_words(card):
    texts = [card.get("headline") or ""]
    for blocker in card.get("blockers") or ():
        texts += [blocker["title"], blocker["why"], blocker["next_step"]["what"],
                  *blocker["next_step"]["who"]]
    texts += [f["text"] for f in card.get("facts") or ()]
    texts += [u["text"] for u in card.get("unknowns") or ()]
    texts += [card["copies"]["text"]] if card.get("copies") else []
    return texts


def handles(card):
    items = [*card.get("blockers", ()), *card.get("facts", ()), *card.get("unknowns", ()),
             *([card["copies"]] if card.get("copies") else [])]
    return {c["handle"] for item in items for c in item["citations"]}


class World:
    """The seeded console fixture and one reader's tools."""

    def __init__(self, state: Path, principals) -> None:
        store = state / case_console.ONTOLOGY_FILE
        self.runtime = CaseRuntime(state, client=None,
                                   audience=state / case_fixtures.MIRROR_AUDIENCE_FILE,
                                   tenant="plx")
        session = session_identity.fixture_session("plx", frozenset(principals),
                                                   channel=session_identity.CHANNEL_CONSOLE)
        self.view = case_console.object_workspace_for(self.runtime, store, session.actor,
                                                      state_dir=state)

    def call(self, name, args):
        return self.view._call(name, args)

    def close(self):
        self.view.close()
        self.runtime.close()


class FixtureCardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.temp.name)
        case_fixtures.seed(cls.state, "plx")
        objects.project_mirror(cls.state, cls.state / case_console.ONTOLOGY_FILE)
        cls.world = World(cls.state, {case_fixtures.OPERATIONS, case_fixtures.QUALITY,
                                      case_fixtures.PURCHASING})
        with cls.world.view._request():
            found = cls.world.call("plx.find", {"query": "PO5447"})
            cls.po = found["candidates"][0]["object"]["object_id"]
            cls.card = sc.card(cls.world.call, cls.po, as_of=AS_OF)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.world.close()
        cls.temp.cleanup()

    def card_for(self, world, object_id, **kwargs):
        with world.view._request():
            return sc.card(world.call, object_id, as_of=AS_OF, **kwargs)

    def test_the_po_card_names_its_cited_blockers_and_next_steps_in_plain_words(self):
        card = self.card
        self.assertEqual(card["status"], "read")
        kinds = [(b["kind"], b["title"]) for b in card["blockers"]]
        self.assertEqual(kinds, [("not_recorded", "No supplier confirmation found"),
                                 ("not_recorded", "No receipt found"),
                                 ("no_need_date", "No expected date recorded")])
        for blocker in card["blockers"]:
            self.assertTrue(blocker["citations"], blocker)
            self.assertTrue(all(c["handle"].startswith("api://") for c in blocker["citations"]))
            self.assertTrue(blocker["next_step"]["what"])
            self.assertLessEqual(set(blocker["next_step"]["who"]), DEPARTMENTS)
        self.assertIn("not proof there is none", card["blockers"][1]["why"])
        self.assertEqual(card["next_step"], card["blockers"][0]["next_step"])
        self.assertLessEqual(len(card["blockers"]), sc.MAX_BLOCKERS)
        # one line for the FileMaker copy, cited; every unknown once
        self.assertEqual((card["copies"]["text"], card["copies"]["count"]),
                         ("Also recorded in FileMaker.", 1))
        self.assertTrue(card["copies"]["citations"])
        texts = [u["text"] for u in card["unknowns"]]
        self.assertEqual(len(texts), len(set(texts)))
        # the expected date is missing, so lateness is not stated a second time
        self.assertFalse([t for t in texts if t.startswith("Whether the receipt is late")])
        # PO5447's status (REVISED on the Portal copy) is undecided on both copies by the
        # shared PO status definition: the purchase order is not established (cited), never a blocker, and
        # the steps after it still read from the FileMaker copy
        (issued,) = [u for u in card["unknowns"]
                     if u["text"].startswith("Whether the purchase order step is done")]
        self.assertTrue(issued["citations"])
        self.assertFalse([b for b in card["blockers"] if "purchase order" in b["title"].lower()])
        self.assertTrue(card["facts"] and all(f["citations"] for f in card["facts"]))
        self.assertEqual([w for t in own_words(card) for w in CODE.findall(t)], [])
        self.assertEqual(sc.internal_codes(card), [])
        self.assertIn({"family": "F14", "title": "Which purchase orders are late, by how much and why"},
                      card["questions"])
        # the card's one lateness verdict: no expected date on any copy, so undecided and
        # naming that field, cited by the blocker that says so
        verdict = card["verdict"]
        self.assertEqual((verdict["state"], verdict["missing"], verdict["days_late"]),
                         ("undecided", ["expected_date"], None))
        self.assertEqual(verdict["line"], card["blockers"][2]["why"])
        self.assertEqual(verdict["citations"], card["blockers"][2]["citations"])
        with self.world.view._request():
            alone = sc.verdict(self.world.call, self.po, as_of=AS_OF)
        self.assertEqual({k: alone[k] for k in verdict}, verdict)
        nothing = sc.verdict(self.world.call, "not an id")
        self.assertNotEqual(nothing["status"], "read")
        self.assertNotIn("state", nothing)

    def test_a_late_stage_ranks_first_and_a_card_is_never_more_than_five_blockers(self):
        real = self.world.call

        def late(name, args):
            answer = real(name, args)
            if name == "plx.process" and args.get("process") == "po_delivery_window":
                answer = json.loads(json.dumps(answer))
                answer["lateness"] = [late_entry()]
                answer["quantity_checks"] = [{"roles": ["ordered", "received"], "agree": False,
                    "values": [{"quantity_role": "ordered", "value": "100", "unit": "EA",
                                "citation": {"l1_handle": "api://x/2", "field_path": "q"}},
                               {"quantity_role": "received", "value": "90.5", "unit": "EA",
                                "citation": {"l1_handle": "api://x/3", "field_path": "q"}}]}]
            return answer

        with self.world.view._request():
            card = sc.card(late, self.po, as_of=AS_OF)
        self.assertEqual([b["kind"] for b in card["blockers"]][:2],
                         ["late", "quantity_mismatch"])
        self.assertEqual(card["blockers"][0]["why"],
                         "Receipt was due 2026-09-01, 49 days ago, and is not recorded yet.")
        self.assertEqual(card["blockers"][0]["next_step"]["who"], ["Warehouse"])
        self.assertEqual((card["verdict"]["state"], card["verdict"]["days_late"],
                          card["verdict"]["line"]), ("late", 49, card["blockers"][0]["why"]))
        self.assertIn("ordered 100 EA; received 90.5 EA", card["blockers"][1]["why"])
        self.assertLessEqual(len(card["blockers"]), sc.MAX_BLOCKERS)
        self.assertEqual(len(card["blockers"]) + card["more_blockers"], 5)

    def test_the_late_blocker_is_the_lateness_verdict_not_the_bare_due_date(self):
        real = self.world.call

        def judged(verdict):
            def call(name, args):
                answer = real(name, args)
                if name == "plx.process" and args.get("process") == "po_delivery_window":
                    answer = json.loads(json.dumps(answer))
                    # the bare due date says overdue; the lateness verdict decides
                    answer["due"] = [{"stage": "receipt", "overdue": True, "days_overdue": 49,
                                      "due_by": [DUE]}]
                    answer["lateness"] = [verdict]
                return answer
            return call

        def card_with(verdict, **kwargs):
            with self.world.view._request():
                return sc.card(judged(verdict), self.po, as_of=AS_OF, **kwargs)

        on_time = {"stage": "receipt", "root": self.po, "state": "on_time", "late": False,
                   "governing": "promise", "governing_date": "2026-11-01", "values": [DUE]}
        self.assertNotIn("late", [b["kind"] for b in card_with(on_time)["blockers"]])
        received = late_entry(measured_against="received", received_date="2026-09-13",
                              days_late=12)
        self.assertEqual(card_with(received)["blockers"][0]["why"],
                         "Receipt was due 2026-09-01 and came 12 days late.")
        partly = late_entry(reasons=["partial_receipt"])
        self.assertEqual(card_with(partly)["blockers"][0]["why"],
                         "Receipt was due 2026-09-01, 49 days ago, and is only partly "
                         "recorded.")
        undecided = {"stage": "receipt", "root": self.po, "state": "undetermined",
                     "late": None, "gap": {"reason": "due_date_conflict"}}
        self.assertNotIn("late", [b["kind"] for b in card_with(undecided)["blockers"]])
        # the date in the words goes with its citation: a reader who may open the receipt
        # date but not the governing date reads that it is late, never the withheld date
        other = {"value": "2026-09-13", "property": "received_date",
                 "citation": {"l1_handle": "api://x/4", "field_path": "row.received"}}
        both = late_entry(values=[DUE, other])
        shown = card_with(both, readable=lambda hs: {h for h in hs if h != "api://x/1"})
        late = [b for b in shown["blockers"] if b["kind"] == "late"]
        self.assertEqual([b["why"] for b in late],
                         ["Receipt was due earlier and is not recorded yet."])
        self.assertEqual(late[0]["citations"],
                         [{"handle": "api://x/4", "field": "row.received"}])
        self.assertNotIn("2026-09-01", json.dumps(shown))
        self.assertNotIn("49", json.dumps(late))
        # the verdict follows the words: a days count only with the due date's citation
        self.assertEqual((shown["verdict"]["state"], shown["verdict"]["days_late"]),
                         ("late", None))
        self.assertEqual(card_with(received)["verdict"]["days_late"], 12)
        # decided not late, yet the expected date is empty on every copy: still undecided
        self.assertEqual(card_with(on_time)["verdict"]["state"], "undecided")

    def test_a_closed_or_draft_po_needs_no_need_date_but_an_open_one_still_does(self):
        """Lateness is not applicable to a cancelled, completed-short or draft PO (nothing is
        due), so its empty expected date is no blocker; an open PO's undecided verdict
        keeps the ``no_need_date`` blocker (the 2026-10-20 live capture: Cancel 4221 of
        5880 cards carried it)."""
        real = self.world.call

        def judged(entry):
            def call(name, args):
                answer = real(name, args)
                if name == "plx.process" and args.get("process") == "po_delivery_window":
                    answer = json.loads(json.dumps(answer))
                    answer["lateness"] = [entry]
                return answer
            return call

        def kinds(entry):
            with self.world.view._request():
                card = sc.card(judged(entry), self.po, as_of=AS_OF)
            return [b["kind"] for b in card["blockers"]], card

        closed = {"stage": "receipt", "root": self.po, "state": "not_applicable",
                  "late": False, "at_risk": False, "reasons": ["not_issued_or_closed"]}
        open_ = {"stage": "receipt", "root": self.po, "state": "undetermined", "late": None,
                 "gap": {"reason": "no_requested_date"}}
        closed_kinds, _ = kinds(closed)
        self.assertNotIn("no_need_date", closed_kinds)
        self.assertIn("no_need_date", kinds(open_)[0])

    def test_a_draft_po_without_a_label_cites_its_status_only_to_a_reader_who_may(self):
        """A draft PO with no number (no display) and nothing due has a verdict nothing
        else decided: it cites the record's own status, so the late-PO portfolio never
        withholds a record the reader may read as having nothing to cite; a reader who
        may not open that status record gets no citation (and the row is withheld)."""
        real = self.world.call
        closed = {"stage": "receipt", "root": self.po, "state": "not_applicable",
                  "late": False, "at_risk": False, "reasons": ["not_issued_or_closed"]}

        def call(name, args):
            answer = real(name, args)
            if name == "plx.process" and args.get("process") == "po_delivery_window":
                answer = json.loads(json.dumps(answer))
                answer["lateness"] = [closed]
            if name == "plx.object" and args.get("object_id") == self.po:
                answer = json.loads(json.dumps(answer))
                answer["object"]["display"] = None
            return answer

        def verdict(readable):
            with self.world.view._request():
                return sc.verdict(call, self.po, as_of=AS_OF, readable=readable)

        with self.world.view._request():
            root = real("plx.object", {"object_id": self.po, "response_format": "detailed"})
        status = {(v["citation"]["l1_handle"], v["citation"]["field_path"])
                  for v in root["values"] if v["property"] == "status" and v.get("value")}
        self.assertTrue(status)
        found = verdict(lambda handles: list(handles))
        self.assertEqual((found["status"], found["label"]), ("read", None))
        self.assertTrue(found["citations"], found)
        self.assertLessEqual({(c["handle"], c["field"]) for c in found["citations"]}, status)
        self.assertEqual(verdict(lambda handles: [])["citations"], [])

    def handles_of(self, answer):
        """Every source handle a plx.object answer cites outside its identity links."""
        own = {k: v for k, v in answer.items() if k != "identity"}
        return set(re.findall(r'"l1_handle": "([^"]+)"', json.dumps(own)))

    def test_only_a_verified_or_reviewed_copy_is_merged_never_a_lead_or_contradicted_one(self):
        real = self.world.call
        with self.world.view._request():
            root = real("plx.object", {"object_id": self.po, "response_format": "detailed"})
            twin = next(e["to"] for e in root["identity"] if e["from"] == self.po
                        and e["link_type"].endswith(".same_record_as"))
            own = self.handles_of(root)
            fm = self.handles_of(real("plx.object", {"object_id": twin,
                                                      "response_format": "detailed"})) - own
        self.assertTrue(fm)

        def claimed(card):
            return {c["handle"] for item in (*card["blockers"], *card["facts"])
                    for c in item["citations"]}

        # the verified twin is merged: its evidence is in the blockers and facts
        self.assertTrue(claimed(self.card) & fm)
        for tier in ("reviewed", "contradicted", "lead", "corroborated", "made_up"):
            processed = []

            def retiered(name, args, tier=tier):
                if name == "plx.process":
                    processed.append(args["from"])
                answer = real(name, args)
                if name == "plx.object" and args.get("object_id") == self.po:
                    answer = json.loads(json.dumps(answer))
                    for edge in answer.get("identity") or ():
                        edge["tier"] = tier
                return answer

            with self.world.view._request():
                card = sc.card(retiered, self.po, as_of=AS_OF)
            if tier == "reviewed":
                self.assertTrue(claimed(card) & fm)
                self.assertEqual(card["copies"]["text"], "Also recorded in FileMaker.")
                continue
            # blockers and facts come from the order's own record only
            self.assertTrue(card["blockers"], tier)
            self.assertLessEqual(claimed(card), own, tier)
            self.assertNotIn(twin, processed, tier)
            # the Portal staging record proves no stage by itself, so the stages the
            # FileMaker copy carried are not stated; its own empty need date still is
            self.assertEqual([b["title"] for b in card["blockers"]],
                             ["No expected date recorded"], tier)
            # its undecided purchase order step is stated as not established, and the walk
            # stops there: a staging record alone carries no later step
            self.assertEqual(len([u for u in card["unknowns"] if u["text"].startswith(
                "Whether the purchase order step is done")]), 1, tier)
            self.assertNotIn("FileMaker", " ".join(b["why"] for b in card["blockers"]))
            if tier == "contradicted":
                self.assertIsNone(card["copies"])
                self.assertFalse(handles(card) & fm)
            else:
                self.assertEqual(card["copies"]["text"],
                                 "FileMaker may hold the same purchase order: a lead to confirm, "
                                 "not read into this card.")
                self.assertEqual(card["copies"]["count"], 1)
                self.assertTrue(card["copies"]["citations"])

    def test_the_late_po_plan_answers_why_late_with_the_chain_or_plain_gaps(self):
        with self.world.view._request():
            answer = self.world.call("plx.investigate", {"family": "F14", "root": self.po})
        self.assertNotIn("error", answer)
        # the chain reaches both copies of the order and their lines; without a need date
        # lateness stays undecided with its gap, the empty need date cited, instead of a guess
        objects = answer["chain"]["objects"]
        self.assertLessEqual({"PurchaseOrder", "PurchaseOrderLine"},
                             {o["object_type"] for o in objects})
        self.assertEqual(sum(o["object_type"] == "PurchaseOrder" for o in objects), 2)
        self.assertEqual(answer["answerability"], {"state": "partial",
                                                   "reasons": ["missing_late"]})
        self.assertTrue(all(g.get("reason") for g in answer["gaps"]))
        # links the sources do not hold are typed gaps, each stated
        self.assertLessEqual({"SupplierConfirmation.confirms reverse", "Receipt.receives reverse",
                              "FreightBill.for_purchase_order reverse"},
                             {g.get("subject") for g in answer["gaps"]
                              if g["reason"] == "no_match"})
        text = json.dumps(answer)
        self.assertIn("row.expected_date", text)

    def test_the_supplier_card_and_plan_give_open_orders_and_a_balance_or_plain_unknown(self):
        with self.world.view._request():
            supplier = self.world.call("plx.find", {"object_type": "Supplier", "filters": [
                {"property": "name", "op": "present"}]})["candidates"][0]["object"]["object_id"]
            plan = self.world.call("plx.investigate", {"family": "F15", "root": supplier})
        card = self.card_for(self.world, supplier)
        self.assertEqual(card["status"], "read")
        opened = [f for f in card["facts"] if f["text"].startswith("Open purchase orders")]
        self.assertEqual([f["value"] for f in opened], ["1 of 1"])
        self.assertTrue(opened[0]["citations"])
        self.assertIn("No balance is recorded for this supplier in what you can read; "
                      "Finance keeps it.", [u["text"] for u in card["unknowns"]])
        self.assertEqual(sc.internal_codes(card), [])
        self.assertNotIn("error", plan)
        reasons = {g.get("reason") for g in plan.get("gaps") or ()}
        self.assertTrue(reasons & {"not_carried", "degraded_binding", "value_empty"}, reasons)

    def supplier(self):
        with self.world.view._request():
            return self.world.call("plx.find", {"object_type": "Supplier", "filters": [
                {"property": "name", "op": "present"}]})["candidates"][0]["object"]["object_id"]

    def test_the_supplier_card_names_each_open_order_blocker_with_the_order_and_its_owner(self):
        card = self.card_for(self.world, self.supplier())
        by_title = {b["title"]: b for b in card["blockers"]}
        # the PO card's blockers roll up onto its supplier, naming the order, cited
        self.assertEqual(sorted(by_title), [
            "No expected date recorded on 1 purchase order",
            "No receipt found on 1 purchase order",
            "No supplier confirmation found on 1 purchase order"])
        unconfirmed = by_title["No supplier confirmation found on 1 purchase order"]
        self.assertEqual(unconfirmed["kind"], "not_recorded")
        self.assertIn("PO5447", unconfirmed["why"])
        self.assertIn("not proof there is none", unconfirmed["why"])
        # a verified link to the supplier is stated plainly; one reached only by a lead
        # link says so
        self.assertNotIn("lead", unconfirmed["why"])
        real = self.world.call

        def weak(name, args):
            if (name == "plx.around" and "tier_floor" not in args
                    and args["path"][0]["link_type"] == "PurchaseOrder.ordered_from"):
                return {"objects": [], "gaps": [{"reason": "below_tier_floor"}]}
            return real(name, args)

        with self.world.view._request():
            led = sc.card(weak, self.supplier(), as_of=AS_OF)
        self.assertIn("The link to it is a lead to confirm.", {b["title"]: b for b in
                      led["blockers"]}["No supplier confirmation found on 1 purchase order"]["why"])
        self.assertEqual(unconfirmed["next_step"]["who"], ["Procurement"])
        self.assertEqual(by_title["No receipt found on 1 purchase order"]["next_step"]["who"],
                         ["Warehouse"])
        po_handles = handles(self.card)
        for blocker in card["blockers"]:
            self.assertTrue(blocker["citations"], blocker)
            self.assertLessEqual({c["handle"] for c in blocker["citations"]}, po_handles)
        self.assertEqual(card["headline"], "3 things need attention")
        self.assertEqual(card["next_step"], card["blockers"][0]["next_step"])
        self.assertEqual([w for t in own_words(card) for w in CODE.findall(t)], [])
        self.assertEqual(sc.internal_codes(card), [])

    def test_a_supplier_counts_late_orders_not_due_dates_and_one_blocker_needs_attention(self):
        real = self.world.call

        def late(name, args):
            answer = real(name, args)
            if name == "plx.process" and args.get("process") == "po_delivery_window":
                answer = json.loads(json.dumps(answer))
                # one order, two late verdicts: still one late order
                answer["lateness"] = [late_entry(), late_entry(days_late=40)]
            return answer

        with self.world.view._request():
            card = sc.card(late, self.supplier(), as_of=AS_OF)
        self.assertEqual(card["blockers"][0]["title"], "Receipt is late on 1 purchase order")
        self.assertIn("PO5447", card["blockers"][0]["why"])
        self.assertEqual(card["blockers"][0]["next_step"]["who"], ["Warehouse"])
        # a supplier's roll-up is late with no one order's days count
        self.assertEqual((card["verdict"]["state"], card["verdict"]["days_late"],
                          card["verdict"]["line"]), ("late", None, card["blockers"][0]["why"]))
        # only the late one is left for a reader who may read nothing but its due date
        with self.world.view._request():
            one = sc.card(late, self.supplier(), as_of=AS_OF,
                          readable=lambda hs: {h for h in hs if h == "api://x/1"})
        self.assertEqual([b["title"] for b in one["blockers"]],
                         ["Receipt is late on 1 purchase order"])
        self.assertNotIn("PO5447", json.dumps(one))   # the order's own citation is withheld
        self.assertEqual(one["headline"], "1 thing needs attention")

    def test_a_supplier_rolls_up_1_10_60_orders_once_each_within_the_read_cap(self):
        link = next(lid for lid, l in sorted(sc._schema().link_types.items())
                    if l["from"] == "PurchaseOrder" and l["to"] == "Supplier")
        graphs = {g["id"]: g for g in sc._graphs()}

        def world(n):
            def call(name, args):
                oid = args.get("object_id") or args.get("from")
                if name == "plx.object":
                    label = "Vendor" if oid == "S" else f"PO-{oid}"
                    return {"object": {"object_id": oid, "object_type":
                                       "Supplier" if oid == "S" else "PurchaseOrder",
                                       "display": {"untrusted_text": label, "origin": {
                                           "citation": f"api://r/{oid}", "field_path": "n"}}},
                            "values": []}
                if name == "plx.around" and oid == "S" and args["path"][0]["link_type"] == link:
                    return {"objects": [{"object_id": f"O{i}"} for i in range(n)] * 2}
                if name == "plx.process":
                    return {"stages": [{"stage": s["id"], "state": "missing",
                                        "missing": {"reason": "absence_unproven"}}
                                       for s in graphs[args["process"]]["stages"]]}
                return {}
            return call

        for n in (1, 10, 60):
            started = time.monotonic()
            card = sc.card(world(n), "S", as_of=AS_OF)
            elapsed = time.monotonic() - started
            read = min(n, sc.MAX_ORDERS)
            orders = "purchase order" + ("s" if read != 1 else "")
            line = next(b for b in card["blockers"] if b["kind"] == "not_recorded")
            self.assertTrue(line["title"].endswith(f" on {read} {orders}"), line["title"])
            self.assertEqual({c["handle"] for c in line["citations"]},
                             {f"api://r/O{i}" for i in range(read)})
            named = min(read, sc.MAX_NAMED)
            self.assertEqual(sum(f"PO-O{i}" in line["why"] for i in range(n)), named)
            if read > named:
                self.assertIn(f"and {read - named} other purchase orders", line["why"])
            opened = [f["value"] for f in card["facts"] if f["text"].startswith("Open")]
            self.assertEqual(opened, [f"{read} of {read}"])
            self.assertEqual("More orders are linked than one card reads." in
                             [u["text"] for u in card["unknowns"]], n > sc.MAX_ORDERS)
            self.assertLess(elapsed, 2.0, (n, elapsed))

    def test_an_unpaid_invoice_is_stated_on_a_proven_link_and_a_lead_on_a_weak_one(self):
        link = next(lid for lid, l in sorted(sc._schema().link_types.items())
                    if l["from"] == "PurchaseInvoice" and l["to"] == "Supplier")
        cite = {"l1_handle": "bc://p/c/purchaseInvoices/1"}

        def world(weak):
            def call(name, args):
                oid = args.get("object_id") or args.get("from")
                if name == "plx.object" and oid == "PurchaseInvoice:I":
                    return {"object": {"object_id": oid, "object_type": "PurchaseInvoice"},
                            "values": [
                                {"property": sc.DUE_PROPERTY, "value": "2026-09-30",
                                 "citation": {**cite, "field_path": "dueDate"}},
                                {"property": sc.OPEN_AMOUNT, "value": "100.50",
                                 "currency": "CAD",
                                 "citation": {**cite, "field_path": "remainingAmount"}}]}
                if name == "plx.object":
                    return {"object": {"object_id": oid, "object_type": "Supplier",
                                       "display": {"untrusted_text": "Vendor", "origin": {
                                           "citation": "api://r/S", "field_path": "n"}}},
                            "values": []}
                if name == "plx.around" and args["path"][0]["link_type"] == link:
                    if weak and "tier_floor" not in args:
                        return {"objects": [], "gaps": [{"reason": "below_tier_floor"}]}
                    return {"objects": [{"object_id": "PurchaseInvoice:I"}]}
                return {}
            return call

        proven, lead = (sc.card(world(w), "S", as_of=AS_OF) for w in (False, True))
        for card in (proven, lead):
            unpaid = [b for b in card["blockers"] if b["kind"] == "unpaid"]
            self.assertEqual([b["title"] for b in unpaid],
                             ["Unpaid purchase invoice past its due date"])
            self.assertEqual(unpaid[0]["citations"],
                             [{"handle": cite["l1_handle"], "field": "dueDate"},
                              {"handle": cite["l1_handle"], "field": "remainingAmount"}])
        said = "A purchase invoice was due 2026-09-30 and 100.50 CAD is still open."
        self.assertEqual((proven["blockers"][0]["why"], proven["blockers"][0]["basis"]),
                         (said, "proven"))
        self.assertEqual((lead["blockers"][0]["why"], lead["blockers"][0]["basis"]),
                         (said + " The link to it is a lead to confirm.", "lead"))
        self.assertIn(sc.reason_words("lead_tier_links_followed"),
                      [u["text"] for u in lead["unknowns"]])
        self.assertNotIn(sc.reason_words("lead_tier_links_followed"),
                         [u["text"] for u in proven["unknowns"]])

    def test_a_claim_without_a_citation_leaves_one_plain_not_known_line(self):
        def call(name, args):
            if name == "plx.object":
                return {"object": {"object_id": args["object_id"], "object_type": "Supplier",
                                   "display": {"untrusted_text": "Vendor"}},
                        "values": [{"property": "status", "value": "active"},
                                   {"property": "vendor_status", "value": "on hold"}]}
            return {}

        for readable in (None, lambda hs: hs):
            card = sc.card(call, "S", as_of=AS_OF, readable=readable)
            self.assertEqual(card["status"], "read")
            self.assertEqual(card["facts"], [])
            self.assertEqual([u["text"] for u in card["unknowns"]].count(sc.UNCITED_TEXT), 1)

    def test_a_balance_is_decimal_with_its_currency(self):
        work = sc._Card(lambda *_a: {}, AS_OF)
        copy = sc._Copy("S", {"object": {"lineage": "bc_manufacturing"}, "values": [
            {"property": "bc_balance", "value_type": "money", "value": "1234.50",
             "currency": "CAD", "citation": {"l1_handle": "bc://p/c/vendors/1",
                                             "field_path": "balance"}}]})
        work.balance("Supplier", [copy])
        self.assertEqual([(f["amount"], f["currency"], f["text"]) for f in work.facts],
                         [("1234.50", "CAD", "BC balance in Business Central")])
        self.assertEqual(work.unknowns, {})

    def test_a_restricted_reader_sees_nothing_the_card_would_not_cite_for_them(self):
        fm = [c["handle"] for c in self.card["copies"]["citations"]]

        def readable(hs):
            return {h for h in hs if h not in fm}

        restricted = self.card_for(self.world, self.po, readable=readable)
        self.assertFalse(handles(restricted) & set(fm))
        self.assertIsNone(restricted["copies"])
        self.assertTrue(all(b["citations"] for b in restricted["blockers"]))
        self.assertIn(sc.WITHHELD_TEXT, [u["text"] for u in restricted["unknowns"]])
        # a reader without the purchasing grant: never more than the full reader's evidence
        other = World(self.state, {case_fixtures.QUALITY})
        try:
            card = self.card_for(other, self.po)
        finally:
            other.close()
        self.assertIn(card["status"], ("read", "not_found"))
        self.assertLessEqual(handles(card), handles(self.card))

    def test_unknown_unsupported_and_failing_inputs_fail_closed(self):
        self.assertEqual(self.card_for(self.world, 'PurchaseOrder:portal.nope:{"id":1}')["status"],
                         "not_found")
        self.assertEqual(sc.card(self.world.call, None)["status"], "invalid_input")
        line = {"object": {"object_id": "L", "object_type": "PurchaseOrderLine"}, "values": []}
        self.assertEqual(sc.card(lambda *_a: line, "L"),
                         {**sc.card(lambda *_a: line, "L"), "status": "unsupported",
                          "object_type": "PurchaseOrderLine"})
        failed = sc.card(lambda *_a: {"error": {"code": "internal", "message": "secret"}}, "X")
        self.assertEqual((failed["status"], failed["error"]), ("error", {"code": "internal"}))
        self.assertNotIn("secret", json.dumps(failed))

    def test_the_sample_reads_every_found_record_once_at_n_1_10_60(self):
        real = self.world.call

        def aliased(n):
            def call(name, args):
                if name == "plx.find":
                    return {"candidates": [{"object": {"object_id": f"alias:{i}"}}
                                           for i in range(n)] * 2}   # duplicates listed
                args = {k: (self.po if isinstance(v, str) and v.startswith("alias:") else v)
                        for k, v in args.items()}
                return real(name, args)
            return call

        for n in (1, 10, 60):
            started = time.monotonic()
            with self.world.view._request():
                found = sc.sample(aliased(n), "PurchaseOrder", n, AS_OF)
            elapsed = time.monotonic() - started
            self.assertEqual((found["candidates"], found["cards"],
                              found["cards_with_cited_fact"]), (n, n, n))
            self.assertEqual(found["cards_with_internal_codes"], 0)
            self.assertEqual(found["blockers_by_kind"]["not_recorded"], 2 * n)
            self.assertLess(elapsed / n, 2.0, (n, elapsed))


class ReferenceAndRouteTest(unittest.TestCase):
    """A case root's card by its reference, and /api/status-card for the request's reader."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.temp.name)
        case_fixtures.seed(cls.state, "plx")
        objects.project_mirror(cls.state, cls.state / case_console.ONTOLOGY_FILE)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def serve(self, principals):
        from http.client import HTTPConnection
        from queue import Queue
        import threading
        from operator_ui.case_workspace import CaseWorkspace
        from operator_ui.server import make_server

        ready: Queue = Queue(maxsize=1)

        def run():
            try:
                world = World(self.state, principals)
                session = session_identity.fixture_session(
                    "plx", frozenset(principals), channel=session_identity.CHANNEL_CONSOLE)
                server = make_server(session=session, object_workspace=world.view,
                                     case_workspace=CaseWorkspace(world.runtime, session.actor))
                ready.put(server)
                server.serve_forever(poll_interval=0.05)
                server.server_close()
                world.close()
            except BaseException as error:  # surfaced to the test thread
                ready.put(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        server = ready.get(timeout=60)
        if isinstance(server, BaseException):
            raise server
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10)))
        port, token = server.server_address[1], server.operator_access_token

        def get(path, *, auth=True):
            conn = HTTPConnection("127.0.0.1", port, timeout=60)
            conn.request("GET", path, headers={"Authorization": "Bearer " + token}
                         if auth and token else {})
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            return response.status, body
        return get

    def test_a_reference_reads_one_record_or_no_card(self):
        world = World(self.state, {case_fixtures.OPERATIONS, case_fixtures.QUALITY,
                                   case_fixtures.PURCHASING})
        try:
            with world.view._request():
                found = sc.card_for_reference(world.call, "PurchaseOrder", "po5447 ", as_of=AS_OF)
                missing = sc.card_for_reference(world.call, "PurchaseOrder", "PO0000001",
                                                as_of=AS_OF)
                marked = sc.card_for_reference(world.call, "PurchaseOrder", "PO#5447", as_of=AS_OF)
                copies = sc.card_for_reference(world.call, "AssemblyOrder", "AO-26005B",
                                               as_of=AS_OF)
        finally:
            world.close()
        self.assertEqual((found["status"], found["label"]), ("read", "PO5447"))
        self.assertEqual([b["kind"] for b in found["blockers"]],
                         ["not_recorded", "not_recorded", "no_need_date"])
        self.assertEqual(missing["status"], "not_found")
        # the case's own spelling of the number is an identifier match, not display text
        self.assertEqual(marked["status"], "read")
        self.assertEqual(marked["blockers"], found["blockers"])
        # two verified copies of one assembly order read as one card with its copy line
        self.assertEqual((copies["status"], copies["copies"]["count"]), ("read", 1), copies)
        twin = {"candidates": [
            {"reason": "exact_identifier",
             "object": {"object_id": f"PurchaseOrder:{i}", "object_type": "PurchaseOrder",
                        "display": {"untrusted_text": "PO7"}}} for i in range(2)]}
        self.assertEqual(sc.card_for_reference(lambda *_a: twin, "PurchaseOrder", "PO7"),
                         {**sc.card_for_reference(lambda *_a: twin, "PurchaseOrder", "PO7"),
                          "status": "ambiguous", "matches": 2})
        lead = {"candidates": [{**c, "reason": "display_text"} for c in twin["candidates"][:1]]}
        self.assertEqual(sc.card_for_reference(lambda *_a: lead, "PurchaseOrder", "PO7")
                         ["status"], "not_found")
        for bad in ((None, "PO7"), ("PurchaseOrder", ""), ("PurchaseOrder", 7)):
            self.assertEqual(sc.card_for_reference(lambda *_a: twin, *bad)["status"],
                             "invalid_input")
        self.assertEqual(sc.card_for_reference(lambda *_a: twin, "PurchaseOrderLine", "x")
                         ["status"], "unsupported")

    def test_the_route_serves_the_request_readers_card_and_refuses_bad_queries(self):
        get = self.serve({case_fixtures.OPERATIONS, case_fixtures.QUALITY,
                          case_fixtures.PURCHASING})
        status, by_ref = get("/api/status-card?type=PurchaseOrder&ref=PO5447")
        self.assertEqual((status, by_ref["status"]), (200, "read"), by_ref)
        status, by_id = get("/api/status-card?id=" + quote(by_ref["object_id"], safe=""))
        self.assertEqual(status, 200)
        self.assertEqual(by_id["blockers"], by_ref["blockers"])
        self.assertEqual(sc.internal_codes(by_id), [])
        for query in ("", "?id=a&type=PurchaseOrder", "?type=PurchaseOrder", "?id=a&id=b"):
            self.assertEqual(get("/api/status-card" + query)[0], 400, query)
        self.assertEqual(get("/api/status-card?id=x", auth=False)[0], 401)
        # a reader without the purchase-order grant gets no more than the full reader
        narrow = self.serve({case_fixtures.QUALITY})
        status, card = narrow("/api/status-card?id=" + quote(by_ref["object_id"], safe=""))
        self.assertEqual(status, 200)
        self.assertLessEqual(handles(card) if card["status"] == "read" else set(),
                             handles(by_id))


class LiveCheckTest(unittest.TestCase):
    """live check ``case.po_status`` over injected sources: its own command,
    the order fixture's FakeWorkbench with ten purchase orders, the network refused.
    Evidence label: injected-transport."""

    SPEC_ID = "case.po_status"

    def setUp(self) -> None:
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("network access attempted in an offline test")

        for target in ("connect", "connect_ex"):
            patcher = mock.patch.object(socket.socket, target, refuse)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.spec = json.loads((ROOT / "devloop" / "live-checks" / f"{self.SPEC_ID}.json")
                               .read_text("utf-8"))

    def run_spec(self, *, fetch=None):
        from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
        from operating.tests import order_fixture as fixture

        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        fake.tables = fixture.tables()
        for index in range(4):
            fake.tables["public.app_purchase_order"].append(
                fixture._po(f"po-x{index}", f"PO10200{index}", "sup-1", "SUBMITTED", "27"))
        config = json.loads((ROOT / "examples" / "po_status_host.live-check.json")
                            .read_text("utf-8"))
        for key in ("capture_audience", "live_audience", "mirror_audience"):
            config[key] = str(ROOT / "examples" / config[key])
        (self.temp / "host.json").write_text(json.dumps(config), "utf-8")
        argv = [str(self.temp / "host.json") if arg.endswith("_host.live-check.json")
                else str(ROOT / arg) if arg.startswith("examples/")
                else arg.replace("{tmp}", str(self.temp)) for arg in self.spec["command"][3:]]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sc.main(argv, environ={"PLX_WORKBENCH_API_URL": BASE,
                                          "PLX_WORKBENCH_API_KEY": KEY},
                           host_options={"workbench_fetch": fetch or fake,
                                         "sleep": lambda _s: None})
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_the_spec_passes_on_ten_cited_cards_and_reads_blocked_on_a_portal_outage(self):
        lv.validate_spec(self.spec, stem=self.SPEC_ID)
        code, summary, text = self.run_spec()
        self.assertEqual(lv.check_expectations(self.spec, code, summary), [], summary)
        self.assertEqual(summary["cards"], 10)
        self.assertNotIn("PO10200", text)   # counts and enums only
        other = summary["readers"][1]      # no purchase-order grant
        self.assertEqual((other["reader"], other["cards"]), (["staff:sales"], 0))

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        code, summary, _text = self.run_spec(fetch=portal_down)
        self.assertTrue(lv.blocked_parts(summary))
        self.assertEqual(lv.check_expectations(
            self.spec, code, summary, self.spec["expect_blocked"]), [])


if __name__ == "__main__":
    unittest.main()
