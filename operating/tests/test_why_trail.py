"""The generic why trail (``operating.why_trail``) over scripted reader reads
(isolated-fixture): every repository plan loads against the compiled schema and a plan the
schema does not support fails closed; each use-case question picks its plan; per plan, a
fixture world gives the ordered, typed steps, the verdict only from a gated claim (else as
recorded, else not established) and a suggestion with no figure; a step the reader's access
refuses is counted, never named; a read that fails or a tool error leaves the trail
incomplete, never silent; N = 1/10/60 events lose or duplicate nothing within the cap."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
import unittest

from operating import why_trail as wt


def handle(kind: str, n: int) -> str:
    return f"api://public.{kind}/%7B%22id%22%3A{n}%7D@fixture-{n}"


class World:
    """A reader's tool dispatch over a scripted world: events per root (the timeline),
    objects per (root, link) (plx.around, with a set handle), events per set and values
    per object (plx.object). Records every call."""

    def __init__(self, events=None, links=None, set_events=None, values=None, errors=()):
        self.events = events or {}
        self.links = links or {}
        self.set_events = set_events or {}
        self.values = values or {}
        self.errors = set(errors)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name, arguments):
        self.calls.append((name, json.loads(json.dumps(arguments))))
        if name in self.errors:
            raise RuntimeError("store busy")
        if name == "plx.timeline":
            source = arguments["from"]
            rows = self.set_events.get(source) if source.startswith("set:") else \
                self.events.get(source, [])
            rows = [e for e in rows or () if e["event_type"] in arguments.get(
                "event_types", [e["event_type"]])]
            return {"events": rows, "gaps": [], "completeness": {"complete": True}}
        if name == "plx.around":
            hop = arguments["path"][0]
            if "link" not in hop:
                return {"objects": [], "edges": [], "completeness": {"complete": True}}
            key = (arguments["from"], hop["link"])
            reached = self.links.get(key)
            if reached is None:
                return {"objects": [], "edges": [],
                        "completeness": {"complete": False, "reason": "withheld_by_permission"},
                        "gaps": [{"state": "inaccessible"}]}
            return {"set": f"set:{hop['link']}", "objects": reached,
                    "edges": self.links.get(("edges", hop["link"]), []),
                    "completeness": {"complete": True}}
        if name == "plx.object":
            oid = arguments["object_id"]
            return {"object": {"object_id": oid, "display": oid.rsplit(":", 1)[-1]},
                    "values": [{"property": p, "value": v, "citation": {"l1_handle": h}}
                               for p, (v, h) in self.values.get(oid, {}).items()]}
        return {"error": {"code": "tool_not_configured"}}


def event(event_type, time_, n, *objects):
    return {"event_type": event_type, "time": time_, "time_basis": "source_business_date",
            "citation": {"l1_handle": handle(event_type.split(".")[0].lower(), n),
                         "field_path": f"row.{event_type.split('.')[1]}"},
            "objects": [{"object_id": o, "qualifier": "x"} for o in objects]}


def allow(handles):
    return {h: None for h in handles}


def by_id():
    return {p.id: p for p in wt.plans()}


def mail_world(root, link_events=()):
    """Two mails naming ``root`` (Email.received over the around set), one sender each."""
    mails = [f"Email:graph_mail.messages:m{i}" for i in (1, 2)]
    world_links = {(root, "mentioned_in_email"): [{"object_id": m, "display": None}
                                                  for m in mails]}
    # a mention: the mails reach the root through a text match only (a lead link)
    world_links[("edges", "mentioned_in_email")] = [{"from": m, "to": root, "tier": "lead"}
                                                    for m in mails]
    set_events = {"set:mentioned_in_email": [
        {**event("Email.received", f"2026-10-0{i}T09:00:00Z", 900 + i, m),
         "citation": {"l1_handle": f"graph-mail://box/m{i}"}} for i, m in
        zip((3, 5), mails)]}
    values = {mails[0]: {"sender": ("Dana Reyes <dana@example.com>", "graph-mail://box/m1")},
              mails[1]: {"sender": ("orders@example.com", "graph-mail://box/m2")}}
    return world_links, set_events, values


# One fixture world per use-case plan: (plan, root id, question, root events, extra links,
# gated claims, expected verdict state, the next action's rule).
CASES = {
    "hold_release": dict(
        root="Batch:portal.fm_batch:{\"id\":1}", type="Batch",
        question="Why is batch B-1 on hold?",
        events=[("Batch.manufactured", "2026-09-01"), ("Batch.tested", "2026-09-03"),
                ("Batch.quality_status_changed", "2026-09-04")],
        verdict_from="Batch.tested", claim="Batch B-1 is on hold: the pH test failed.",
        state="checked"),
    "customer_order_update": dict(
        root="SalesOrder:portal.app_sales_order:{\"id\":2}", type="SalesOrder",
        question="What did we last tell the customer about order S-2?",
        events=[("SalesOrder.ordered", "2026-09-01"), ("SalesOrder.required", "2026-10-20"),
                ("Shipment.shipped", "2026-10-21")],
        verdict_from=None, claim=None, state="recorded"),
    "quantity_match": dict(
        root="PurchaseOrder:portal.app_purchase_order:{\"id\":3}", type="PurchaseOrder",
        question="Why don't the invoice and receipt quantities on PO 3 match?",
        events=[("PurchaseOrder.issued", "2026-08-01"), ("Receipt.received", "2026-09-01"),
                ("PurchaseInvoice.posted", "2026-09-10")],
        verdict_from="Receipt.received", claim="The receipt shows fewer units received "
                                               "than were invoiced.", state="checked"),
    "production_shortfall": dict(
        root="AssemblyOrder:portal.fm_assembly_order:{\"id\":4}", type="AssemblyOrder",
        question="Why did AO 4 make fewer units than planned?",
        events=[("AssemblyOrder.scheduled", "2026-09-01"),
                ("AssemblyOrderDamage.entered", "2026-09-02"),
                ("AssemblyOrder.manufactured", "2026-09-03")],
        verdict_from=None, claim=None, state="missing"),
    "shipment_shortfall": dict(
        root="SalesOrder:portal.app_sales_order:{\"id\":5}", type="SalesOrder",
        question="Why did order S-5 ship short?",
        events=[("SalesOrder.ordered", "2026-09-01"), ("Shipment.shipped", "2026-09-09")],
        verdict_from="Shipment.shipped", claim="The shipment shipped fewer units than "
                                               "were ordered.", state="checked"),
    "decision_basis": dict(
        root="MCApproval:mission_control.approvals:{\"id\":\"g6\"}", type="MCApproval",
        question="Who approved this and on what basis?",
        events=[("MCApproval.requested", "2026-09-01T10:00:00Z"),
                ("MCApproval.decided", "2026-09-02T11:00:00Z")],
        verdict_from=None, claim=None, state="recorded"),
}


class Plans(unittest.TestCase):
    def test_every_plan_loads_and_a_plan_the_schema_does_not_support_fails_closed(self):
        loaded = wt.load_plans()
        self.assertEqual({p.id for p in loaded} - {"record_history"}, set(CASES))
        good = json.loads((wt.PLAN_DIR / "hold_release.json").read_text())
        broken = {
            "unknown key": lambda d: d.update(shortcut=True),
            "undeclared event": lambda d: d["root_events"].update(
                {"Batch.teleported": {"kind": "record_change", "label": "x"}}),
            "event of another type": lambda d: d["root_events"].update(
                {"Quote.issued": {"kind": "record_change", "label": "x"}}),
            "unknown link": lambda d: d["links"].append(
                {"link": "mentioned_in_fax", "kind": "communication", "label": "Fax"}),
            "property not on the far type": lambda d: d["links"].append(
                {"link": "mentioned_in_email", "kind": "communication", "label": "Email",
                 "who": "signature"}),
            "unknown step kind": lambda d: d["root_events"]["Batch.tested"].update(
                kind="rumour"),
            "figure in a suggestion": lambda d: d["next_actions"].insert(
                0, {"when": "always", "text": "Credit 250 units on {record}."}),
            "unknown placeholder": lambda d: d["next_actions"].insert(
                0, {"when": "always", "text": "Email {customer_email}."}),
            "unknown condition": lambda d: d["next_actions"].insert(
                0, {"when": "sometimes", "text": "Wait."}),
            "bad pattern": lambda d: d["question_terms"].append("(unclosed"),
            "a string for a list": lambda d: d.update(question_terms="hold"),
            "a string for link events": lambda d: d["links"][0].update(
                events="Receipt.received"),
            "a boolean maximum": lambda d: d["links"][0].update(max=True),
            "a boolean priority": lambda d: d.update(priority=False),
            "a string for verdict terms": lambda d: d["verdict"].update(claim_terms="held"),
            "too many links": lambda d: d.update(links=d["links"] * 3),
        }
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name, change in broken.items():
            with self.subTest(name):
                doc = copy.deepcopy(good)
                change(doc)
                (tmp / "hold_release.json").write_text(json.dumps(doc))
                with self.assertRaises(wt.WhyPlanError):
                    wt.load_plans(tmp)

    def test_each_question_selects_its_plan_and_an_unrelated_one_none(self):
        for plan_id, case in CASES.items():
            with self.subTest(plan_id):
                self.assertEqual(wt.select(case["question"], case["type"]).id, plan_id)
        self.assertEqual(wt.select("Why is this customer flagged?", "Customer").id,
                         "record_history")
        self.assertIsNone(wt.select("What is the list price of this item?", "Product"))
        # a type the plan does not cover never takes it, however the question reads
        self.assertNotEqual(wt.select("Why is it on hold?", "Customer").id, "hold_release")


class PerPlan(unittest.TestCase):
    def run_case(self, plan_id, case, *, decide=allow, extra_claims=()):
        plan = by_id()[plan_id]
        root = case["root"]
        events = [event(t, d, i, root) for i, (t, d) in enumerate(case["events"], 1)]
        links, set_events, values = mail_world(root)
        world = World(events={root: events}, links=links, set_events=set_events,
                      values=values)
        claims = list(extra_claims)
        if case["claim"]:
            cited = handle(case["verdict_from"].split(".")[0].lower(),
                           [t for t, _ in case["events"]].index(case["verdict_from"]) + 1)
            claims.append({"text": case["claim"], "citations": [cited], "check": "passed"})
        built = wt.build(plan, {"object_id": root, "object_type": case["type"],
                                "label": "the record"}, call=world, decide=decide,
                         claims=claims)
        return built, world

    def test_each_plan_gives_ordered_typed_steps_a_gated_verdict_and_a_suggestion(self):
        for plan_id, case in CASES.items():
            with self.subTest(plan_id):
                built, world = self.run_case(plan_id, case)
                steps = built["steps"]
                plan = by_id()[plan_id]
                self.assertEqual(built["plan"], plan_id)
                self.assertEqual(steps[-1]["kind"], "verdict")
                self.assertEqual(built["verdict_state"], case["state"])
                body = steps[:-1]
                dates = [s["date"] for s in body if s["date"]]
                self.assertEqual(dates, sorted(dates))         # time order
                for step in body:
                    self.assertIn(step["kind"], wt.KINDS)
                    self.assertTrue(step["handle"])            # every step opens
                    self.assertIsNone(step["value"])           # no gated claim cites it
                    # a mail naming the record by text is a text match, never a read
                    self.assertEqual(step["check"], "text_match" if step["kind"] ==
                                     "communication" else "read_from_record")
                # the root's own events, typed as the plan says
                kinds = {s["label"]: s["kind"] for s in body}
                for event_type, _ in case["events"]:
                    spec = plan.root_events[event_type]
                    self.assertEqual(kinds.get(spec["label"]), spec["kind"], event_type)
                mails = [s for s in body if s["kind"] == "communication"]
                if any(link["link"] == "mentioned_in_email" and wt._far_types(
                        wt.default_schema(), case["type"], link["link"]) for link in plan.links):
                    self.assertEqual(len(mails), 2)
                    # a person by name, never an address
                    self.assertIn("Email from Dana Reyes", [s["label"] for s in mails])
                    self.assertNotIn("@", json.dumps([s["label"] for s in steps]))
                verdict = steps[-1]
                if case["state"] == "checked":
                    self.assertEqual(verdict["value"], case["claim"])
                    self.assertEqual(verdict["check"], "passed")
                else:
                    self.assertIsNone(verdict["value"])         # no figure outside the gate
                self.assertEqual(verdict["check"] == "not_established",
                                 case["state"] == "missing")
                action = built["next_action"]
                self.assertTrue(action["suggestion_only"])
                self.assertNotRegex(action["text"], r"\d")
                self.assertIn("the record", action["text"])
                if case["state"] == "missing":
                    rule = next(a for a in plan.next_actions if a["when"] == "verdict_missing")
                    self.assertEqual(action["text"], rule["text"].replace("{record}",
                                                                          "the record"))
                # reads only: nothing but the read-only primitives was called
                self.assertLessEqual({n for n, _ in world.calls},
                                     {"plx.timeline", "plx.around", "plx.object"})

    def test_a_value_comes_only_from_a_gated_claim_citing_that_step_and_field(self):
        case = CASES["production_shortfall"]
        damage, scheduled = handle("assemblyorderdamage", 2), handle("assemblyorder", 1)
        built, _ = self.run_case("production_shortfall", case, extra_claims=[
            {"text": "Damage was entered on the order.", "citations": [damage],
             "checked": [(damage, "entered")], "check": "passed"},
            # live 2026-10-30: a claim about another field of the record an event cites
            # (an order's comment beside its order date) was shown as that event's value
            {"text": "The order header comment says paid.", "citations": [scheduled],
             "checked": [(scheduled, "comment")], "check": "passed"}])
        by_label = {s["label"]: s for s in built["steps"]}
        self.assertEqual(by_label["Damage entered"]["value"],
                         "Damage was entered on the order.")
        self.assertEqual(by_label["Damage entered"]["check"], "passed")
        self.assertIsNone(by_label["Scheduled"]["value"])
        self.assertEqual(by_label[wt.CHECKED_FACT]["value"],
                         "The order header comment says paid.")
        self.assertEqual(by_label[wt.CHECKED_FACT]["handle"], scheduled)

    def test_a_fact_is_a_value_only_on_the_record_the_gate_checked_and_never_passed_unchecked(self):
        case = CASES["production_shortfall"]
        damage, scheduled = handle("assemblyorderdamage", 2), handle("assemblyorder", 1)
        built, _ = self.run_case("production_shortfall", case, extra_claims=[
            # cites both records, but the gate checked only the damage entry's field
            {"text": "Damage was entered after scheduling.", "citations": [scheduled, damage],
             "checked": [(damage, "entered")], "check": None}])
        by_label = {s["label"]: s for s in built["steps"]}
        self.assertEqual(by_label["Damage entered"]["value"],
                         "Damage was entered after scheduling.")
        self.assertIsNone(by_label["Scheduled"]["value"])
        self.assertEqual(by_label["Damage entered"]["check"], "unchecked")
        self.assertNotIn("passed", [s["check"] for s in built["steps"]])

    def test_a_refused_step_is_counted_never_named_and_withheld_links_are_said(self):
        case = CASES["hold_release"]
        secret = "graph-mail://box/m3"     # the mail Dana Reyes sent

        def decide(handles):
            return {h: ({"reason": "not_granted"} if h == secret else None) for h in handles}
        built, _ = self.run_case("hold_release", case, decide=decide)
        self.assertEqual(built["hidden"], 1)
        # one refused source behind two steps (a record and its twin's) is one hidden step
        twice = World(events={case["root"]: [
            event("Batch.tested", "2026-09-03", 1, case["root"]),
            {**event("Batch.released", "2026-09-04", 1, case["root"])}]})
        built = wt.build(by_id()["hold_release"], {"object_id": case["root"],
                                                   "object_type": "Batch", "label": "B"},
                         call=twice, decide=lambda hs: {h: {"reason": "x"} for h in hs},
                         claims=[])
        self.assertEqual(built["hidden"], 1)
        self.assertNotIn(secret, json.dumps(built))
        self.assertNotIn("Dana Reyes", json.dumps(built))
        # the decision records and chats the scripted world withholds
        self.assertTrue(built["may_be_withheld"])

    def test_a_failing_read_or_tool_error_leaves_the_trail_incomplete(self):
        plan = by_id()["hold_release"]
        root = CASES["hold_release"]["root"]
        broken = World(errors={"plx.around"})
        built = wt.build(plan, {"object_id": root, "object_type": "Batch", "label": "B"},
                         call=broken, decide=allow, claims=[])
        self.assertTrue(built["incomplete"])
        self.assertEqual(built["verdict_state"], "missing")

        def refusing(name, arguments):
            return {"error": {"code": "invalid_input", "message": "no"}}
        built = wt.build(plan, {"object_id": root, "object_type": "Batch", "label": "B"},
                         call=refusing, decide=allow, claims=[])
        self.assertTrue(built["incomplete"])

    def test_one_ten_sixty_events_none_lost_or_duplicated_within_the_cap(self):
        plan = by_id()["production_shortfall"]
        root = CASES["production_shortfall"]["root"]
        for n in (1, 10, 60):
            with self.subTest(n=n):
                events = [event("AssemblyOrderStatusEntry.entered",
                                f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", i, root)
                          for i in range(n)]
                world = World(events={root: events})
                started = time.monotonic()
                built = wt.build(plan, {"object_id": root, "object_type": "AssemblyOrder",
                                        "label": "AO"}, call=world, decide=allow, claims=[])
                self.assertLess(time.monotonic() - started, 2.0)
                body = [s for s in built["steps"] if s["kind"] != "verdict"]
                handles = [s["handle"] for s in body]
                self.assertEqual(len(handles), len(set(handles)))
                self.assertEqual(len(body), min(n, wt.MAX_STEPS))
                self.assertEqual(built["incomplete"], n > wt.MAX_STEPS)
                if n > wt.MAX_STEPS:     # the latest are kept: what led to the verdict
                    self.assertIn(events[-1]["citation"]["l1_handle"], handles)

    def test_a_run_of_same_day_entries_is_one_step_and_a_checked_one_stands_alone(self):
        # live 2026-10-30: an assembly order's thirteen production-task starts on two days
        # filled the trail
        plan = by_id()["production_shortfall"]
        root = CASES["production_shortfall"]["root"]
        events = [event("ProductionTask.started", "2026-08-17", i, root) for i in range(9)]
        events += [event("AssemblyOrderDamage.entered", "2026-08-18", 20 + i, root)
                   for i in range(3)]
        checked = events[-1]["citation"]["l1_handle"]
        built = wt.build(plan, {"object_id": root, "object_type": "AssemblyOrder",
                                "label": "AO"}, call=World(events={root: events}),
                         decide=allow, claims=[{"text": "Damage was entered.",
                                                "citations": [checked],
                                                "checked": [(checked, "entered")],
                                                "check": "passed"}])
        titles = [s["label"] for s in built["steps"][:-1]]
        self.assertEqual(titles, ["Production task started (9 records)",
                                  "Damage entered (2 records)", "Damage entered"])
        self.assertEqual(built["steps"][2]["value"], "Damage was entered.")
        self.assertFalse(built["incomplete"])

    def test_twins_in_other_systems_are_read_and_an_unestablished_one_is_not(self):
        plan = by_id()["hold_release"]
        root, twin, lead = ("Batch:portal.fm_batch:{\"id\":1}",
                            "Batch:portal.app_batch_record:{\"id\":\"b1\"}",
                            "Batch:portal.app_batch_record:{\"id\":\"b9\"}")
        world = World(events={twin: [event("Batch.released", "2026-09-09", 7, twin)],
                              lead: [event("Batch.released", "2026-09-10", 8, lead)]})
        inner = world.__call__

        def call(name, arguments):
            hop = arguments.get("path", [{}])[0]
            if name == "plx.around" and "link_type" in hop:
                return {"objects": [], "edges": [
                    {"from": root, "to": twin, "tier": "verified"},
                    {"from": root, "to": lead, "tier": "lead"}]}
            return inner(name, arguments)
        built = wt.build(plan, {"object_id": root, "object_type": "Batch", "label": "B"},
                         call=call, decide=allow, claims=[])
        handles = {s["handle"] for s in built["steps"]}
        self.assertIn(handle("batch", 7), handles)
        self.assertNotIn(handle("batch", 8), handles)     # a lead is not the same batch
        self.assertEqual(built["verdict_state"], "recorded")


class NoCaseValues(unittest.TestCase):
    def test_plans_carry_no_figure_in_their_wording(self):
        for path in sorted(wt.PLAN_DIR.glob("*.json")):
            doc = json.loads(path.read_text())
            words = [doc["title"], doc["verdict"]["label"], doc["verdict"]["missing"]] + [
                a["text"] for a in doc["next_actions"]] + [
                s["label"] for s in doc["root_events"].values()] + [
                link["label"] for link in doc["links"]]
            for text in words:
                self.assertIsNone(re.search(r"\d", text), f"{path.name}: {text}")


if __name__ == "__main__":
    unittest.main()
