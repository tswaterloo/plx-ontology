"""Evidence trail (``operating.evidence_trail``): a late PO's chain of custody (due date and
who set it, PO issued, confirmation, revision, follow-ups, receipts, verdict with its rule),
the ETA check, the next-action rules, a customer order walked in reverse to its materials'
POs with typed gaps, and what a restricted reader sees (hidden steps counted, never named).

Synthetic records over the real compiled schema and the shipped process graphs (the
``tests.test_ontology_tools_process`` fixture), read through the real generic tools as the
reader. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import json
import time
import unittest

from ontology.types import Actor
from operating import evidence_trail as et
from operating import ontology_tools as ot
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt
from ontology.tests import test_po_lateness as pl
from tests import test_ontology_tools_process as tp

STAFF, SALES = tp.STAFF, tp.SALES
# A clerk reads everything staff reads except what is marked "finance".
CLERK = Actor("plx", frozenset({"clerk"}))
POLICY = ot.ReadPolicy(
    "plx", {key: frozenset(who | {"clerk"}) if "staff" in who else who
            for key, who in tp.POLICY.audiences.items()},
    {mark: frozenset(who | ({"clerk"} if mark != "finance" else set()))
     for mark, who in tp.POLICY.markings.items()})
MAILBOX = "purchasing@plx.test"


def setUpModule():
    tp.setUpModule()


def reader(store, actor=STAFF, calls=None):
    """The reader's tool table as one ``call``: every tool reads as ``actor``."""
    base = ot.OntologyTools(ot.StoreReader(store.conn), POLICY, schema=tp.SCHEMA,
                            contracts=tp.CONTRACTS, clock=lambda: tp.NOW)
    process, traverse = op.ProcessTools(base, tp.GRAPHS), tt.TraverseTools(base)

    def call(name, arguments):
        if calls is not None:
            calls.append(name)
        table = process if name == "plx.process" else traverse \
            if name in tt.TOOL_NAMES else base
        return table.call(name, dict(arguments), actor)
    return call


def email(s, po, key, at, subject, sender, *, markings="[]"):
    oid = s.obj(f'Email:graph_mail.messages:{key}', "mail", markings=markings)
    s.val(oid, "sent_at", "timestamp", at, markings=markings)
    s.val(oid, "subject", "text", subject, markings=markings)
    s.val(oid, "sender", "text", sender, markings=markings)
    s.val(oid, "mailbox", "text", MAILBOX, markings=markings)
    s.link("Email.mentions_purchase_order", oid, po, tier="lead", kind="mention")
    return oid


def late_po(s, *, confirmations=(("a", "2099-01-20", "2099-01-05"),
                                 ("b", "2099-01-28", "2099-01-12"))):
    """An open Portal PO due 2099-01-15 (its FM twin's date), issued 2099-01-02, with
    supplier confirmations ``(key, promised, document date)`` in mail."""
    po = pl.portal_po(s, number="PO300001")
    fm = pl.fm_twin(s, po, "2099-01-15")
    s.event("PurchaseOrder.issued", fm, "2099-01-02")
    for key, promise, sent in confirmations:
        pl.confirm(s, po, key, promise, sent)
    return po


class PoTrailTests(unittest.TestCase):
    def test_a_late_po_reads_due_issue_promises_follow_ups_and_the_verdict_in_order(self):
        s = tp.Store()
        po = late_po(s)
        email(s, po, "m1", "2099-01-18T10:00:00Z", "Chasing PO300001", f"Ann <ann@plx.test>")
        email(s, po, "m2", "2099-01-25T09:00:00Z", "RE: Chasing PO300001",
              "Bob <bob@vendor.example>")
        trail = et.po_trail(reader(s), po)
        kinds = [x["kind"] for x in trail["steps"]]
        self.assertEqual(kinds, ["due_date", "po_issued", "confirmation", "revision",
                                 "follow_up", "follow_up", "verdict"])
        due, issued, conf, rev, ours, theirs, verdict = trail["steps"]
        self.assertEqual((due["title"], due["governs"]), ("Requested by 2099-01-15", True))
        self.assertTrue(due["handle"].startswith("api://public.generated_purchase_order/"))
        self.assertEqual((issued["at"], issued["title"]), ("2099-01-02", "PO issued"))
        self.assertEqual((conf["at"], conf["title"]),
                         ("2099-01-05", "Vendor confirmed delivery by 2099-01-20"))
        self.assertEqual((rev["at"], rev["title"]),
                         ("2099-01-12", "Promise revised from 2099-01-20 to 2099-01-28"))
        # follow-ups newest last, each cited by its own record, a mention link: a lead
        self.assertEqual([(x["at"][:10], x["direction"], x["check"]) for x in (ours, theirs)],
                         [("2099-01-18", "internal", "lead"), ("2099-01-25", "external", "lead")])
        self.assertTrue(theirs["title"].startswith("Email from the vendor side: RE: Chasing"))
        self.assertEqual(verdict["title"],
                         "Late: open on 2099-02-01, past the requested-by date 2099-01-15 "
                         "(17 days).")
        self.assertEqual(verdict["check"], "computed_from_cited_records")
        self.assertEqual(verdict["handle"], due["handle"])
        self.assertTrue(all(x["handle"] for x in trail["steps"]))   # every step cited
        self.assertEqual((trail["state"], trail["days_late"], trail["hidden"],
                          trail["may_hide"]), ("late", 17, 0, False))
        # the ETA is backed by the vendor's confirmation (the latest promise's document)
        self.assertEqual((trail["eta"]["state"], trail["eta"]["date"], trail["eta"]["handle"]),
                         ("confirmed", "2099-01-28", rev["handle"]))
        # the promise moved once: confirm it, with the vendor contact who wrote last
        action = trail["next_action"]
        self.assertEqual((action["rule"], action["contact"], action["suggestion"]),
                         ("promise_passed", "Bob <bob@vendor.example>", True))
        self.assertIn("The vendor's confirmed date (2099-01-28) has passed", action["text"])
        self.assertIn("The last message about it was 7 days ago.", action["text"])

    def test_no_figure_in_a_step_is_one_the_tools_did_not_return(self):
        s = tp.Store()
        po = late_po(s)
        process = reader(s)("plx.process", {"from": po, "process": et.PROCESS})
        dates = set()

        def walk(node):
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            elif isinstance(node, str):
                dates.update(__import__("re").findall(r"\d{4}-\d{2}-\d{2}", node))
        walk(process)
        timeline = reader(s)("plx.timeline", {"from": po.replace(
            "portal.app_purchase_order:po-1", "portal.app_purchase_order:po-1")})
        walk(timeline)
        for cid in ("a", "b"):
            walk(reader(s)("plx.object", {"object_id":
                                          f"SupplierConfirmation:{pl.MAIL}:{cid}"}))
        walk(reader(s)("plx.timeline", {"from": "PurchaseOrder:portal."
                                        "fm_generated_purchase_order:fm-1"}))
        trail = et.po_trail(reader(s), po)
        for x in trail["steps"]:
            for text in (x["title"], x.get("at") or "", x.get("date") or ""):
                for day in __import__("re").findall(r"\d{4}-\d{2}-\d{2}", text):
                    self.assertIn(day, dates, x)
        self.assertEqual(trail["days_late"], process["lateness"][0]["days_late"])

    def test_no_confirmation_and_a_stale_po_suggest_asking_the_vendor(self):
        s = tp.Store()
        po = late_po(s, confirmations=())
        trail = et.po_trail(reader(s), po)
        self.assertEqual(trail["eta"]["state"], "no_confirmation_found")
        self.assertNotIn("confirmation", [x["kind"] for x in trail["steps"]])
        action = trail["next_action"]
        self.assertEqual(action["rule"], "no_confirmation")
        self.assertTrue(action["text"].startswith(
            "No vendor confirmation in 30 days since the PO was issued: ask the vendor"))
        self.assertIsNone(action["contact"])   # no vendor-side message: nobody named

    def test_a_lead_only_confirmation_is_shown_as_a_lead_and_never_counts(self):
        s = tp.Store()
        po = late_po(s, confirmations=())
        pl.confirm(s, po, "c", "2099-01-09", "2099-01-03", tier="lead")
        trail = et.po_trail(reader(s), po)
        self.assertEqual(trail["eta"]["state"], "lead_only")
        lead = [x for x in trail["steps"] if x["kind"] == "lead"]
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead[0]["check"], "lead")
        self.assertEqual(trail["eta"]["handle"], lead[0]["handle"])
        self.assertNotIn("confirmation", [x["kind"] for x in trail["steps"]])
        self.assertEqual(trail["next_action"]["rule"], "confirmation_lead_only")

    def test_a_promise_moved_twice_asks_to_confirm_the_new_date(self):
        s = tp.Store()
        po = late_po(s, confirmations=(("a", "2099-01-20", "2099-01-05"),
                                       ("b", "2099-01-28", "2099-01-12"),
                                       ("c", "2099-02-20", "2099-01-26")))
        trail = et.po_trail(reader(s), po)
        self.assertEqual([x["kind"] for x in trail["steps"]].count("revision"), 2)
        self.assertEqual(trail["next_action"]["rule"], "promise_moved")
        self.assertIn("The promise moved 2 times: confirm the new date (2099-02-20)",
                      trail["next_action"]["text"])
        self.assertEqual(trail["eta"]["text"],
                         "Yes: the vendor confirmed 2099-02-20 (revised 2 times).")

    def test_a_received_po_states_its_receipt_and_suggests_nothing(self):
        s = tp.Store()
        po = pl.portal_po(s, status="Complete", ordered=("10", "10"))
        pl.fm_twin(s, po, "2099-01-15")
        pl.receipt(s, po, "r1", "2099-01-20")
        trail = et.po_trail(reader(s), po)
        self.assertIn(("receipt", "2099-01-20"), [(x["kind"], x["at"]) for x in trail["steps"]])
        self.assertEqual(trail["steps"][-1]["title"],
                         "Late: received on 2099-01-20, 5 days after the requested-by date "
                         "2099-01-15.")
        self.assertIsNone(trail["next_action"])   # received: nothing to chase

    def test_a_restricted_reader_gets_a_count_of_hidden_mail_never_its_content(self):
        s = tp.Store()
        po = late_po(s)
        email(s, po, "m1", "2099-01-18T10:00:00Z", "Chasing PO300001", "Ann <ann@plx.test>")
        email(s, po, "m2", "2099-01-25T09:00:00Z", "SECRET-SUBJECT",
              "Secret Person <secret@vendor.example>", markings='["finance"]')
        staff = et.po_trail(reader(s), po)
        self.assertEqual(len([x for x in staff["steps"] if x["kind"] == "follow_up"]), 2)
        sales = et.po_trail(reader(s, CLERK), po)
        follow = [x for x in sales["steps"] if x["kind"] == "follow_up"]
        self.assertEqual(len(follow), 1)
        text = json.dumps(sales)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("Secret Person", text)
        self.assertNotIn("m2", text)
        # the tools trim before counting: the trail says some may be hidden, names none
        self.assertTrue(sales["may_hide"])
        self.assertFalse(staff["may_hide"])
        self.assertIsNone(sales["next_action"]["contact"])

    def test_an_unknown_process_answer_fails_closed_with_a_gap(self):
        trail = et.po_trail(lambda name, args: {"error": {"code": "object_not_found"}},
                            "PurchaseOrder:portal.app_purchase_order:nope")
        self.assertEqual((trail["state"], trail["steps"], trail["next_action"]),
                         (None, [], None))
        self.assertEqual(trail["gaps"][0]["reason"], "lateness_unavailable")

    def test_one_ten_and_sixty_follow_ups_keep_the_newest_with_no_duplicate(self):
        for n in (1, 10, 60):
            s = tp.Store()
            po = late_po(s)
            for i in range(n):
                email(s, po, f"m{i}", f"2099-01-{1 + i % 28:02d}T{i % 24:02d}:00:00Z",
                      f"Note {i}", "Ann <ann@plx.test>")
            started = time.monotonic()
            trail = et.po_trail(reader(s), po)
            self.assertLess(time.monotonic() - started, 20)
            follow = [x for x in trail["steps"] if x["kind"] == "follow_up"]
            self.assertEqual(len(follow), min(n, et.MAX_MESSAGES))
            self.assertEqual(len({x["handle"] for x in follow}), len(follow))
            self.assertEqual([x["at"] for x in follow], sorted(x["at"] for x in follow))


class ManyLinesTests(unittest.TestCase):
    def test_9_12_and_30_lines_keep_every_promise_and_never_decide_on_a_cut_list(self):
        for n in (9, 12, 30):
            s = tp.Store()
            po = pl.bc_po(s, expected=None)   # due dates from the lines only
            for i in range(n):
                pl.line(s, po, f"l{i}", "10", "0", binding="bc.purchase_order_lines",
                        due=f"2099-01-{1 + i % 28:02d}")
            pl.confirm(s, po, "a", "2099-01-20", "2099-01-05")
            pl.confirm(s, po, "b", "2099-01-28", "2099-01-12")
            entry = reader(s)("plx.process", {"from": po, "process": et.PROCESS})[
                "lateness"][0]
            self.assertEqual((entry["promised_date"], entry["revised_date"]),
                             ("2099-01-20", "2099-01-28"), n)
            trail = et.po_trail(reader(s), po)
            promised = [(x["kind"], x["date"], x["at"]) for x in trail["steps"]
                        if x["kind"] in ("confirmation", "revision")]
            self.assertEqual(promised, [("confirmation", "2099-01-20", "2099-01-05"),
                                        ("revision", "2099-01-28", "2099-01-12")], n)
            self.assertEqual((trail["eta"]["state"], trail["eta"]["date"]),
                             ("confirmed", "2099-01-28"), n)
            self.assertNotIn("dates_cut", [g["reason"] for g in trail["gaps"]])
            self.assertNotEqual(trail["next_action"]["rule"], "no_confirmation")

    def test_a_promise_history_not_read_whole_is_not_decided_and_says_so(self):
        s = tp.Store()
        po = pl.bc_po(s, expected=None)   # due dates from the lines only
        for i in range(12):
            pl.line(s, po, f"l{i}", "10", "0", binding="bc.purchase_order_lines",
                    due=f"2099-01-{1 + i:02d}")
        pl.confirm(s, po, "a", "2099-01-20", "2099-01-05")
        pl.confirm(s, po, "b", "2099-01-28", "2099-01-12")
        call = reader(s)

        def blind(name, args):   # the confirmations' own records can't be read
            if str(args.get("object_id") or args.get("from")).startswith("SupplierConf"):
                return {"error": {"code": "object_not_found"}}
            return call(name, args)
        trail = et.po_trail(blind, po)
        self.assertEqual(trail["eta"]["state"], "not_determined")
        self.assertIn("more dates than were read", trail["eta"]["text"])
        self.assertIn("dates_cut", [g["reason"] for g in trail["gaps"]])
        self.assertIsNone(trail["next_action"])


class DisagreeTests(unittest.TestCase):
    def test_disagreeing_vendor_dates_show_each_promise_and_say_they_disagree(self):
        s = tp.Store()
        po = late_po(s, confirmations=(("a", "2099-01-20", "2099-01-05"),
                                       ("b", "2099-01-28", "2099-01-05")))
        entry = reader(s)("plx.process", {"from": po, "process": et.PROCESS})["lateness"][0]
        self.assertIn("promises_disagree", entry["reasons"])
        trail = et.po_trail(reader(s), po)
        promised = sorted(x["date"] for x in trail["steps"] if x["kind"] == "confirmation")
        self.assertEqual(promised, ["2099-01-20", "2099-01-28"])
        self.assertNotIn("revision", [x["kind"] for x in trail["steps"]])
        reasons = [g["reason"] for g in trail["gaps"]]
        self.assertIn("dates_disagree", reasons)
        self.assertNotIn("dates_cut", reasons)
        self.assertEqual(trail["eta"]["state"], "promises_disagree")
        self.assertEqual(trail["next_action"]["rule"], "promises_disagree")


class NextActionTests(unittest.TestCase):
    def trail(self, *steps, state="late", eta=None, at_risk=False):
        return {"state": state, "at_risk": at_risk, "today": "2099-02-01",
                "eta": eta or {"state": "no_confirmation_found"}, "steps": list(steps)}

    def test_rules_in_order_and_only_for_a_late_or_at_risk_po(self):
        self.assertIsNone(et.next_action(self.trail(state="on_time")))
        self.assertIsNone(et.next_action(self.trail(state="not_due")))
        self.assertIsNone(et.next_action(self.trail(state="undetermined")))
        self.assertEqual(et.next_action(self.trail(state="not_due", at_risk=True))["rule"],
                         "no_confirmation")
        self.assertEqual(et.next_action(self.trail(), issued="2099-01-30")["text"],
                         "No vendor confirmation on record: ask the vendor to confirm the "
                         "delivery date.")
        rev = {"kind": "revision", "date": "2099-03-01", "handle": "h"}
        self.assertEqual(et.next_action(self.trail(rev, eta={"state": "confirmed",
                                                             "date": "2099-03-01"}))["rule"],
                         "promise_moved")
        self.assertEqual(et.next_action(self.trail(eta={"state": "promises_disagree"}))["rule"],
                         "promises_disagree")
        partial = {"kind": "receipt", "title": "Partial receipt: 4 of 10 each", "handle": "h"}
        self.assertEqual(et.next_action(self.trail(partial, eta={
            "state": "confirmed", "date": "2099-03-01"}))["rule"], "partial_receipt")

    def test_an_internal_sender_is_never_the_vendor_contact(self):
        ours = {"kind": "follow_up", "at": "2099-01-30", "direction": "internal",
                "sender": "Ann <ann@plx.test>", "handle": "h1"}
        action = et.next_action(self.trail(ours))
        self.assertIsNone(action["contact"])
        self.assertIn("2 days ago", action["text"])

    def test_direction_needs_both_addresses(self):
        self.assertEqual(et.direction("a@x.com", "p@x.com"), "internal")
        self.assertEqual(et.direction("Bob <b@y.com>", "p@x.com"), "external")
        self.assertIsNone(et.direction("Bob", "p@x.com"))
        self.assertIsNone(et.direction("b@y.com", None))


class ClaimsTrailTests(unittest.TestCase):
    def test_only_cited_claims_with_a_dated_value_become_steps_by_date(self):
        claims = [
            {"text": "Shipped", "citations": ["bc://x/1"], "check": {"status": "verified"},
             "value_claims": [{"value": "2099-01-09", "citation": "bc://x/1"}]},
            {"text": "Ordered", "citations": ["bc://x/2"],
             "value_claims": [{"value": "2099-01-02"}]},
            {"text": "No date", "citations": ["bc://x/3"], "value_claims": [{"value": "12"}]},
            {"text": "Uncited", "citations": [], "value_claims": [{"value": "2099-01-01"}]},
        ]
        steps = et.claims_trail(claims)
        self.assertEqual([(x["title"], x["at"], x["check"]) for x in steps],
                         [("Ordered", "2099-01-02", "gate_passed"),
                          ("Shipped", "2099-01-09", "verified")])


# --------------------------------------------------------------------------- materials

def chain(s, *, with_po=True, ao_key="ao-1", item_key="it-1", po_key="po-1", number="PO300001",
          order=None):
    """customer order <- line <- allocation -> FM assembly order <- component -> item
    <- PO line -> PO (each link verified)."""
    order = order or s.obj("SalesOrder:portal.fm_order_entry:so-1", "fm_bronze")
    line = s.obj(f"SalesOrderLine:portal.fm_order_entry_item:{ao_key}-l", "fm_bronze")
    s.link("SalesOrderLine.line_of", line, order)
    ao = s.obj(f"AssemblyOrder:portal.fm_assembly_order:{ao_key}", "fm_bronze")
    alloc = s.obj(f"AssemblyOrderAllocation:portal.fm_order_entry_item_assembly:{ao_key}-a",
                  "fm_bronze")
    s.link("AssemblyOrderAllocation.for_order_line", alloc, line)
    s.link("AssemblyOrderAllocation.allocates", alloc, ao)
    comp = s.obj(f"AssemblyOrderComponent:portal.fm_assembly_order_item:{ao_key}-c",
                 "fm_bronze")
    s.link("AssemblyOrderComponent.component_of", comp, ao)
    item = s.obj(f"Product:portal.fm_packaging:{item_key}", "fm_bronze")
    s.link("AssemblyOrderComponent.uses_item", comp, item)
    po = None
    if with_po:
        po = pl.portal_po(s, key=po_key, number=number)
        pl.fm_twin(s, po, "2099-01-15", key=f"fm-{po_key}")
        pol = f"PurchaseOrderLine:portal.app_purchase_order_line:{po_key}-line"
        s.link("PurchaseOrderLine.for_item", pol, item)   # the PO's own open line
    return order, ao, item, po


class MaterialsTests(unittest.TestCase):
    def test_a_customer_order_walks_to_its_late_material_po_with_its_trail(self):
        s = tp.Store()
        order, ao, item, po = chain(s)
        got = et.materials(reader(s), order, "SalesOrder")
        (row,) = got["rows"]
        self.assertEqual((row["order"], row["assembly"], row["item"], row["po"]),
                         (order, ao, item, po))
        self.assertEqual((row["state"], row["days_late"]), ("late", 17))
        self.assertEqual(got["late"], 1)
        (trail,) = got["trails"]
        self.assertEqual((trail["subject"], trail["state"]), (po, "late"))
        self.assertEqual(got["gaps"], [])

    def test_a_received_po_of_the_material_is_counted_not_listed(self):
        s = tp.Store()
        order, ao, item, po = chain(s)
        done = pl.portal_po(s, key="old", status="Complete", ordered=("10", "10"),
                            number="PO300099")
        pl.fm_twin(s, done, "2099-01-15", key="fm-old")
        pl.receipt(s, done, "r-old", "2099-01-10")
        s.link("PurchaseOrderLine.for_item", "PurchaseOrderLine:portal.app_purchase_order_line:"
               "old-line", item)
        got = et.materials(reader(s), order, "SalesOrder")
        self.assertEqual([r["po"] for r in got["rows"]], [po])
        (note,) = [g for g in got["gaps"] if g["reason"] == "received_or_closed"]
        self.assertEqual((note["state"], note["what"]),
                         ("note", "1 other purchase order for these materials"))
        self.assertEqual(got["checked"], 2)

    def test_every_open_material_po_is_returned_for_the_heading_to_count(self):
        s = tp.Store()
        order, ao, item, po = chain(s)
        for k in range(45):
            extra = pl.portal_po(s, key=f"x{k}", number=f"PO31{k:04d}")
            pl.fm_twin(s, extra, "2099-01-15", key=f"fm-x{k}")
            s.link("PurchaseOrderLine.for_item",
                   f"PurchaseOrderLine:portal.app_purchase_order_line:x{k}-line", item)
        got = et.materials(reader(s), order, "SalesOrder", seconds=None, trails=0)
        self.assertEqual(len(got["rows"]), 46)   # never capped here: the app caps and counts
        self.assertEqual(len({r["po"] for r in got["rows"]}), 46)
        self.assertEqual(got["late"], 46)

    def test_a_link_not_recorded_is_its_typed_gap_never_a_guess(self):
        s = tp.Store()
        order, ao, item, _ = chain(s, with_po=False)
        got = et.materials(reader(s), order, "SalesOrder")
        self.assertEqual(got["rows"], [])
        (gap,) = got["gaps"]
        self.assertEqual(gap["reason"], "line_item_not_recorded")
        self.assertIn(gap["state"], ("absent_in_source", "unknown"))
        self.assertIn("purchase order line", gap["where"])
        # an order with no assembly order linked
        s = tp.Store()
        lone = s.obj("SalesOrder:portal.fm_order_entry:so-9", "fm_bronze")
        got = et.materials(reader(s), lone, "SalesOrder")
        self.assertEqual([g["reason"] for g in got["gaps"]],
                         ["assembly_order_not_linked_to_order_line"])
        # a lead-only link is link_lead_only, never walked
        s = tp.Store()
        order = s.obj("SalesOrder:portal.fm_order_entry:so-1", "fm_bronze")
        line = s.obj("SalesOrderLine:portal.fm_order_entry_item:l", "fm_bronze")
        s.link("SalesOrderLine.line_of", line, order, tier="lead")
        got = et.materials(reader(s), order, "SalesOrder")
        self.assertEqual([g["reason"] for g in got["gaps"]], ["link_lead_only"])

    def test_one_ten_and_sixty_assemblies_lose_and_duplicate_no_row(self):
        for n in (1, 10, 60):
            s = tp.Store()
            order = s.obj("SalesOrder:portal.fm_order_entry:so-1", "fm_bronze")
            pos = []
            for i in range(n):
                _, _, _, po = chain(s, ao_key=f"ao-{i}", item_key=f"it-{i}", po_key=f"p{i}",
                                    number=f"PO3{i:05d}", order=order)
                pos.append(po)
            calls = []
            started = time.monotonic()
            got = et.materials(reader(s, calls=calls), order, "SalesOrder", seconds=None,
                               trails=1)
            self.assertLess(time.monotonic() - started, 60)
            walked = min(n, et.MAX_ASSEMBLIES)
            self.assertEqual(len(got["rows"]), walked)
            self.assertEqual(len({r["po"] for r in got["rows"]}), walked)
            self.assertEqual({r["state"] for r in got["rows"]}, {"late"})
            truncated = [g for g in got["gaps"] if g["reason"] == "assemblies_truncated"]
            self.assertEqual(len(truncated), 1 if n > et.MAX_ASSEMBLIES else 0)
            self.assertLessEqual(calls.count("plx.process"), walked + 1)

    def test_a_reader_without_the_po_unit_sees_no_po_and_no_name(self):
        s = tp.Store()
        order, ao, item, _ = chain(s, with_po=False)
        twin = pl.bc_po(s, key="bc-9", expected="2099-01-05")
        pol = s.obj("PurchaseOrderLine:bc.purchase_order_lines:bc-9-l")
        s.link("PurchaseOrderLine.line_of", pol, twin)
        s.link("PurchaseOrderLine.for_item", pol, item)
        staff = et.materials(reader(s), order, "SalesOrder")
        self.assertEqual([r["po"] for r in staff["rows"]], [twin])
        sales = et.materials(reader(s, SALES), order, "SalesOrder")
        self.assertEqual(sales["rows"], [])
        self.assertNotIn("bc-9", json.dumps(sales))
        self.assertEqual([g["state"] for g in sales["gaps"]], ["unknown"])  # never "absent"

    def test_an_unknown_root_type_reads_nothing(self):
        calls = []
        got = et.materials(lambda n, a: calls.append(n) or {}, "Shipment:x:1", "Shipment")
        self.assertEqual((got["rows"], calls), ([], []))


if __name__ == "__main__":
    unittest.main()
