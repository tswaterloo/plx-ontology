"""The answer's evidence trail at the app boundary (``operator_api.trail``): every step is
decided again for the reader, a step the reader can't open is counted and never named, a
sender or vendor contact is shown only when the people directory clears the address and the
reader can open the message it comes from, and the payload validates against the contract.
A fake view and backend stand in for the console (isolated-fixture)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from operator_api import backend as be
from operator_api import models as m
from operator_api import trail as tr

OPEN, SHUT, MAIL = "api://public.app_purchase_order/po-1@1", "bc://co/x/1@1", \
    "graph-mail://box/msg-secret@ck"
VENDOR = "Bob <bob@vendor.example>"


class Access:
    def __init__(self, refused):
        self.refused = set(refused)

    def decide(self, handles):
        return {h: ({"reason": "not_granted"} if h in self.refused else None) for h in handles}


class View:
    def __init__(self, refused=()):
        self.access = Access(refused)
        self.sources = object()

    def _can_open(self, handle):
        return True


class Backend:
    _source = staticmethod(be.WorkspaceBackend._source)


def raw_trail(contact_handle=MAIL):
    return {"subject": "PurchaseOrder:portal.app_purchase_order:po-1", "label": "PO 1",
            "state": "late", "days_late": 17, "verdict": "Late: open on 2099-02-01, past the "
            "requested-by date 2099-01-15 (17 days).", "hidden": 0, "may_hide": False,
            "eta": {"state": "confirmed", "text": "Yes: the vendor confirmed 2099-01-28.",
                    "handle": SHUT, "date": "2099-01-28"},
            "steps": [
                {"kind": "due_date", "title": "Requested by 2099-01-15", "handle": OPEN,
                 "check": "read_from_record", "at": None},
                {"kind": "confirmation", "title": "Vendor confirmed delivery by 2099-01-28",
                 "handle": SHUT, "check": "read_from_record", "at": "2099-01-05"},
                {"kind": "follow_up", "title": "Email from the vendor side: SECRET-SUBJECT",
                 "handle": MAIL, "check": "lead", "at": "2099-01-25", "sender": VENDOR},
                {"kind": "verdict", "title": "Late: open on 2099-02-01", "handle": OPEN,
                 "check": "computed_from_cited_records", "at": "2099-02-01"}],
            "gaps": [],
            "next_action": {"rule": "promise_passed", "text": "The vendor's confirmed date "
                            "(2099-01-28) has passed with nothing received: ask the vendor "
                            "for a new date.", "contact": VENDOR,
                            "contact_handle": contact_handle, "suggestion": True}}


def mapper(view, shown=True):
    out = tr.Mapper(Backend(), view, object_url=be.object_url, open_url=be.open_url,
                    host_schemes=be.HOST_READ_SCHEMES)
    out.people.shown = lambda text: shown and bool(text)
    return out


class TrailBoundaryTests(unittest.TestCase):
    def test_every_step_opens_for_a_reader_who_may_open_it(self):
        got = mapper(View()).po(raw_trail())
        m.PoTrail.model_validate(got.model_dump())
        self.assertEqual([s.kind for s in got.steps],
                         ["due_date", "confirmation", "follow_up", "verdict"])
        self.assertTrue(all(s.source.open_url.startswith("/source?handle=") for s in got.steps))
        self.assertEqual(got.steps[2].sender, VENDOR)
        self.assertEqual((got.next_action.contact, got.hidden_steps), (VENDOR, 0))
        self.assertIn(f"ask the vendor ({VENDOR}) for a new date", got.next_action.text)
        self.assertTrue(got.next_action.suggestion_only)

    def test_steps_the_reader_cant_open_are_counted_never_named(self):
        got = mapper(View(refused={SHUT, MAIL})).po(raw_trail())
        self.assertEqual([s.kind for s in got.steps], ["due_date", "verdict"])
        self.assertEqual(got.hidden_steps, 2)
        self.assertIsNone(got.eta.source)            # the confirmation is not opened
        self.assertIsNone(got.next_action.contact)   # its sender is never shown
        self.assertNotIn("bob", got.next_action.text)
        body = json.dumps(got.model_dump())
        for secret in ("SECRET", "msg-secret", "vendor.example", SHUT):
            self.assertNotIn(secret, body)
        trail = m.EvidenceTrail(kind="purchase_order", title="t", trails=[got],
                                hidden_steps=2, hidden_message=tr.hidden_words(2, False))
        self.assertEqual(trail.hidden_message, "2 more steps in places you can't open.")
        self.assertEqual(tr.hidden_words(0, True), "Some steps may be in places you can't open.")
        self.assertIsNone(tr.hidden_words(0, False))

    def test_an_openable_revision_never_names_a_refused_earlier_date(self):
        raw = raw_trail()
        raw["steps"].insert(2, {"kind": "revision", "title": "Promise revised from "
                                "2099-01-28 to 2099-02-05", "handle": OPEN, "date": "2099-02-05",
                                "check": "read_from_record", "at": "2099-01-20"})
        raw["steps"][1]["date"] = "2099-01-28"
        got = mapper(View(refused={SHUT})).po(raw)
        (rev,) = [x for x in got.steps if x.kind == "revision"]
        self.assertEqual(rev.title, "Promise revised to 2099-02-05")
        self.assertNotIn("2099-01-28", got.model_dump_json())
        shown = mapper(View()).po(raw)
        self.assertIn("Promise revised from 2099-01-28 to 2099-02-05",
                      [x.title for x in shown.steps])

    def test_a_refused_promise_drops_its_date_from_the_eta_too(self):
        got = mapper(View(refused={SHUT})).po(raw_trail())
        self.assertEqual(got.eta.state, "withheld")
        self.assertIsNone(got.eta.source)
        self.assertNotIn("2099-01-28", got.eta.text)
        self.assertNotIn("2099-01-28", got.next_action.text)
        self.assertIn("The vendor's confirmed date has passed", got.next_action.text)

    def test_the_materials_heading_counts_every_row_and_says_how_many_are_shown(self):
        def walk(n):
            return {"labels": {}, "rows": [
                {"order": None, "assembly": "a", "item": f"i{k}", "po": f"p{k}",
                 "state": "late" if k % 2 else None, "days_late": None, "verdict": None}
                for k in range(n)], "gaps": [], "trails": [], "hidden": 0, "may_hide": False,
                "late_pos": [], "checked_pos": []}
        for n, title in (
                (40, "From the recorded links: 40 purchase orders for this order's "
                     "materials, 20 late"),
                (41, "From the recorded links: 41 purchase orders for this order's "
                     "materials, 20 late (40 rows shown, 1 more not shown)"),
                (46, "From the recorded links: 46 purchase orders for this order's "
                     "materials, 23 late (40 rows shown, 6 more not shown)"),
                (60, "From the recorded links: 60 purchase orders for this order's "
                     "materials, 30 late (40 rows shown, 20 more not shown)")):
            with mock.patch.object(tr.et, "materials", return_value=walk(n)):
                got = tr.materials_trail(mapper(View()), View(),
                                         [("a", "AssemblyOrder", "AO")])
            self.assertEqual((len(got.materials), got.title), (min(n, 40), title), n)
            self.assertNotIn("missed", got.model_dump_json())
        self.assertEqual(tr.materials_title([], 0),
                         "From the recorded links: no open purchase order found")

    def test_a_person_the_directory_does_not_clear_is_never_named(self):
        got = mapper(View(), shown=False).po(raw_trail())
        self.assertIsNone(got.steps[2].sender)
        self.assertIsNone(got.next_action.contact)
        self.assertNotIn("bob@", json.dumps(got.model_dump()))

    def test_without_a_people_directory_no_name_is_shown(self):
        view = View()
        view.sources = None
        people = tr.People(view)
        self.assertFalse(people.shown(VENDOR))
        with mock.patch("operating.people_graph.PeopleDirectory.from_runtime",
                        side_effect=RuntimeError("no capture")):
            self.assertFalse(tr.People(View()).shown(VENDOR))
        self.assertFalse(tr.People(View()).shown("no address here"))

    def test_an_unknown_step_kind_or_eta_state_fails_validation(self):
        bad = raw_trail()
        bad["steps"][0]["kind"] = "guess"
        with self.assertRaises(Exception):
            mapper(View()).po(bad)
        bad = raw_trail()
        bad["eta"]["state"] = "probably"
        with self.assertRaises(Exception):
            mapper(View()).po(bad)

    def test_the_late_list_verdict_stands_beside_a_live_verdict_that_differs(self):
        raw = raw_trail()
        raw["state"] = "undetermined"
        listed = m.LatePo(object_id=raw["subject"], label="PO 1", days_late=127,
                          reason="An open purchase order needed by 2099-01-15 has no "
                                 "supplier confirmation", open_url="/o",
                          sources=[m.Source(system="Portal", handle=OPEN, label="x")])

        class Calls(View):
            def _call(self, name, args):
                return {"error": {"code": "x"}}
        view = Calls()
        with mock.patch.object(tr.et, "po_trail", return_value=raw):
            got = tr.po_trails(mapper(view), view, [raw["subject"]],
                               late={raw["subject"]: (listed, "2099-02-01T05:00:00Z")})
        steps = got.trails[0].steps
        self.assertEqual([s.kind for s in steps][-2:], ["verdict", "verdict"])
        self.assertEqual(steps[-2].title, "On your late list as of 2099-02-01: 127 days late. "
                                          "An open purchase order needed by 2099-01-15 has no "
                                          "supplier confirmation.")
        self.assertEqual(steps[-2].source.handle, OPEN)
        # a reader who can't open the list's evidence gets no such step
        view = Calls(refused={OPEN})
        with mock.patch.object(tr.et, "po_trail", return_value=raw):
            got = tr.po_trails(mapper(view), view, [raw["subject"]],
                               late={raw["subject"]: (listed, None)})
        self.assertFalse(any(s.title.startswith("On your late list")
                             for s in got.trails[0].steps))

    def test_an_order_in_two_records_lists_each_material_po_and_gap_once(self):
        po = "PurchaseOrder:portal.app_purchase_order:po-1"
        walk = {"labels": {}, "rows": [{"order": None, "assembly": "AssemblyOrder:a:1",
                                        "item": "Product:p:1", "po": po, "state": "late",
                                        "days_late": 3, "verdict": "Late"}],
                "gaps": [{"reason": "line_item_not_recorded", "state": "unknown",
                          "what": "A purchase order line for X", "where": "PO lines"}],
                "trails": [], "hidden": 0, "may_hide": False, "late_pos": [po],
                "checked_pos": [po, "PurchaseOrder:x:2"]}
        with mock.patch.object(tr.et, "materials", return_value=walk) as walked:
            got = tr.materials_trail(mapper(View()), View(), [
                ("AssemblyOrder:a:1", "AssemblyOrder", "AO-1"),
                ("AssemblyOrder:b:1", "AssemblyOrder", "AO-1")])
        self.assertEqual(walked.call_count, 2)
        self.assertEqual((len(got.materials), len(got.gaps)), (1, 1))
        self.assertEqual(got.title, "From the recorded links: 1 purchase order for this "
                                    "order's materials, 1 late")
        self.assertEqual(got.materials[0].assembly.label, "AO-1")

    def test_a_trail_that_fails_leaves_the_answer_without_one(self):
        class Broken(View):
            def _call(self, name, args):
                raise RuntimeError("store gone")
        got = tr.for_answer(Backend(), Broken(), "is PO 1 late?",
                            [{"object_id": "PurchaseOrder:portal.app_purchase_order:po-1",
                              "object_type": "PurchaseOrder"}], [], [],
                            object_url=be.object_url, open_url=be.open_url,
                            host_schemes=be.HOST_READ_SCHEMES)
        self.assertIsNone(got)

    def test_which_trail_an_answer_gets(self):
        calls = []

        class Recorder(View):
            def _call(self, name, args):
                calls.append((name, args.get("from") or args.get("object_id")))
                return {"error": {"code": "object_not_found"}}
        so = {"object_id": "SalesOrder:portal.fm_order_entry:1", "object_type": "SalesOrder"}
        tr.for_answer(Backend(), Recorder(), "which materials are late on this order?", [so],
                      [], [], object_url=be.object_url, open_url=be.open_url,
                      host_schemes=be.HOST_READ_SCHEMES)
        self.assertEqual(calls[0], ("plx.around", so["object_id"]))
        calls.clear()
        tr.for_answer(Backend(), Recorder(), "who placed this order?", [so], [], [],
                      object_url=be.object_url, open_url=be.open_url,
                      host_schemes=be.HOST_READ_SCHEMES)
        self.assertEqual(calls, [])   # no materials question, no PO: the claims trail only
        calls.clear()
        twins = [{"mention": "OR1", "candidates": [
            {"object_id": "SalesOrder:portal.fm_order_entry:1", "object_type": "SalesOrder"},
            {"object_id": "SalesOrder:portal.sage_oe_order_header:1",
             "object_type": "SalesOrder"},
            {"object_id": "Invoice:x:1", "object_type": "Invoice"}]}]
        tr.for_answer(Backend(), Recorder(), "which materials on OR1 are late?", [], [], [],
                      ambiguous=twins, object_url=be.object_url, open_url=be.open_url,
                      host_schemes=be.HOST_READ_SCHEMES)
        # each order record the identifier names is walked; another type never
        self.assertEqual(sorted({oid for name, oid in calls if name == "plx.around"}),
                         ["SalesOrder:portal.fm_order_entry:1",
                          "SalesOrder:portal.sage_oe_order_header:1"])


if __name__ == "__main__":
    unittest.main()
