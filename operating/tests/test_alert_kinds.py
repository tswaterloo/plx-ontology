"""The alert registry obeys its rule, covers every alert type, and decides My work."""

import unittest

from operating import alert_kinds as ak
from operating.briefing import EVENT_TYPES


class Registry(unittest.TestCase):

    def test_on_by_default_only_when_measured_at_the_bar_and_actionable(self):
        for t in ak.registry()["types"]:
            with self.subTest(t["type"]):
                self.assertIn(t["default"], ("on", "off"))
                if t["default"] == "on":
                    p = t["precision"]
                    self.assertIsNotNone(p, "an unmeasured type is off")
                    self.assertGreaterEqual(p["value"], 0.90)
                    self.assertGreaterEqual(p["checked"], 15)
                    self.assertTrue(t["actionable"])
                if t["home"]:
                    self.assertEqual(t["default"], "on", "My work needs default on")
                if t["precision"] is not None:
                    self.assertTrue({"value", "checked", "on", "source"} <= set(t["precision"]))
                    # a measurement stating its judged count agrees with its precision
                    if "correct" in t["precision"]:
                        p = t["precision"]
                        self.assertLessEqual(p["correct"], p["checked"])
                        self.assertAlmostEqual(p["value"], p["correct"] / p["checked"],
                                               places=3)
                else:
                    # every type carries a measurement or says why it has none yet
                    self.assertTrue(str(t.get("unmeasured", "")).startswith(
                        "not measurable yet: "), "an unmeasured type states why")

    def test_every_alert_type_has_one_entry_and_a_known_kind(self):
        types = [t["type"] for t in ak.registry()["types"]]
        self.assertEqual(len(types), len(set(types)))
        self.assertEqual(set(EVENT_TYPES) - set(types), set())
        kinds = {k["kind"] for k in ak.registry()["kinds"]}
        for t in ak.registry()["types"]:
            self.assertIn(t["kind"], kinds, t["type"])
        # the merge: dozens of detector types, about eighteen alert kinds
        alerts = [k for k in ak.registry()["kinds"] if k["surface"] == "alert"]
        self.assertLessEqual(len(alerts), 20)
        self.assertGreater(len(types), 3 * len(alerts))

    def test_data_health_and_trends_never_reach_my_work(self):
        for t in ak.registry()["types"]:
            if ak.surface(t["kind"]) != "alert":
                self.assertFalse(t["home"], t["type"])

    def test_unknown_types_fail_closed(self):
        self.assertFalse(ak.default_on("no_such_type"))
        self.assertFalse(ak.home("no_such_type"))
        self.assertIsNone(ak.kind_of("no_such_type"))
        # a reader may switch an off type on for Watching, never for My work
        self.assertTrue(ak.shown("chase_escalating", reader_on=["chase_escalating"]))
        self.assertFalse(ak.home("chase_escalating"))

    def test_the_design_decisions_hold(self):
        for name in ("needs_my_reply", "po_late", "lot_on_hold", "label_change"):
            self.assertTrue(ak.home(name), name)
        for name in ("duplicate_party_candidate", "key_field_missing"):
            self.assertTrue(ak.default_on(name) and not ak.home(name), name)
        for name in ("customer_order_late", "po_undated", "approval_waiting",
                     "chase_escalating", "watcher_attention", "lot_near_expiry",
                     "commitment_due", "priority_order_unfilled",
                     "labour_overtime_spike"):
            self.assertFalse(ak.default_on(name), name)
        self.assertEqual(ak.kind_of("teams_unanswered_ask"), "reply_owed")
        self.assertEqual(ak.kind_of("duplicate_supplier_payment"), "duplicate_payable")


class OffMeansNothing(unittest.TestCase):
    """An off type yields nothing on any reader surface unless the reader switched it on."""

    def inbox(self):
        def gap(n, kind, summary):
            return {"id": f"g{n}", "kind": kind, "state": "open", "object_id": f"PurchaseOrder:{n}",
                    "object_type": "PurchaseOrder", "summary": summary, "citations": [],
                    "follow_up": {}, "first_seen": "2026-11-01", "last_seen": "2026-11-08"}
        return {"status": "read", "as_of": "2026-11-08", "complete": True,
                "gaps": [gap(1, "chase_escalating", "PO-1 was chased 4 times"),
                         gap(2, "lot_on_hold", "Batch 7 is on hold")]}

    def test_briefing_items_and_digest_leave_off_types_out(self):
        from operating import briefing as bf
        routing = bf.Routing(basis="fixture", everything=True)
        built = bf.build(self.inbox(), routing=routing, hidden=None, last_visit=None)
        types = [i["event_type"] for i in built["items"]]
        self.assertNotIn("chase_escalating", types)
        self.assertIn("lot_on_hold", types)
        self.assertNotIn("PO-1 was chased", bf.digest_text(built, first_name="Sam"))
        on = bf.build(self.inbox(), routing=routing, hidden=None, last_visit=None,
                      enabled=["chase_escalating"])
        self.assertIn("chase_escalating", [i["event_type"] for i in on["items"]])

    def test_every_reader_read_of_the_inbox_filters_off_types(self):
        # agent tools (mcp_server), alerts (watch), the record workspace and the briefing all
        # read through proactive_engine.read_inbox, whose default leaves off types out; only
        # the Late POs at-risk panel asks for them, by name
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        for path in ("operating/mcp_server.py", "operator_api/watch.py",
                     "operating/briefing.py", "operator_api/backend.py",
                     "operator_api/answers.py", "operator_api/page_parts.py",
                     "operator_api/briefing_view.py", "operator_api/serving.py"):
            text = (root / path).read_text(encoding="utf-8")
            for m in re.finditer(r"include_off=True", text):
                line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end())]
                self.assertIn("AT_RISK_KINDS", line, f"{path}: {line.strip()}")
        self.assertIn("chase_escalating", ak.off_gap_kinds())
        self.assertNotIn("chase_escalating", ak.off_gap_kinds(["chase_escalating"]))
        self.assertNotIn("late_po", ak.off_gap_kinds())


if __name__ == "__main__":
    unittest.main()
