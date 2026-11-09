"""Every alert label, playbook text and draft composer passes the slop gate over fixture
facts: a template that fails here is fixed at its source, never allowed."""

import re
import unittest
from datetime import date

from operating import alert_kinds, alert_playbooks as ap, briefing, copy_gate as g, drafts
from operating import proactive_chasers, proactive_suggestions as ps, proactive_threads as pt

WORDS = {"n": "PO-106001", "amount": "1,200.50 CAD", "change_pct": "38.5",
         "change_points": "12", "date_now": "2026-11-20", "date_was": "2026-11-12",
         "days": "9", "first_short": "2026-11-12", "item": "ITEM-7", "late_share": "40.0",
         "late_share_before": "10.0", "orders": "3", "orders_said": "3 customer orders",
         "pos": "PO-106002", "recent_pct": "12", "required": "2026-11-12", "short_by": "2026-11-12",
         "shortfall": "40", "trailing_pct": "20", "unit": "kg"}
FACTS = list(WORDS.items())


def texts(spec):
    yield "ask", spec.get("ask")
    yield "owner", spec.get("owner")
    yield "change", (spec.get("change") or {}).get("text")
    for sub in (spec.get("by_type") or {}).values():
        yield from texts(sub)


class Sweep(unittest.TestCase):

    def assertClean(self, text, surface, facts=(), **kw):
        found = g.check(text, surface=surface, facts=facts, **kw)
        self.assertTrue(found["ok"], f"{surface}: {text!r}: {found['findings']}")

    def test_every_alert_label_and_next_step(self):
        for name, spec in briefing.EVENT_TYPES.items():
            with self.subTest(name):
                self.assertClean(spec["label"], "ui")
                self.assertClean(briefing.NEXT_ACTIONS[name][0], "ui")
        for t in alert_kinds.registry()["types"]:
            self.assertClean(t["label"], "ui")
        for k in alert_kinds.registry()["kinds"]:
            self.assertClean(k["label"], "ui")

    def test_every_playbook_text(self):
        for name, spec in ap.PLAYBOOKS.items():
            for part, template in texts(spec):
                if not template:
                    continue
                with self.subTest(f"{name}.{part}"):
                    filled = ap._fill(template, WORDS)
                    self.assertIsNotNone(filled)
                    if part == "ask":
                        draft = drafts.compose("playbook_ask", g.sentences(filled),
                                               subject="PO-106001: question", facts=FACTS,
                                               specific="{n}" in template)
                        self.assertTrue(draft["copy_check"]["ok"], (filled, draft))
                        self.assertEqual(draft["body"], " ".join(g.sentences(filled)))
                    else:
                        self.assertClean(filled, "teams" if part == "owner" else "ui")

    def test_every_composer(self):
        cited = [{"fact": "order_number", "value": "PO-106001", "citation": "h:1"}]
        vendor = ps.vendor_draft(po_number="PO-106001", supplier="DSM Nutrition",
                                 need_date="2026-11-12", eta={"answer": "no"}, contact=None,
                                 last_message={"at": "2026-11-02"},
                                 lines=[{"text": "ITEM-7: 18 kg open"}] * 4, cited=cited)
        customer = ps.customer_draft(order_label="SO-5001", required="2026-11-12",
                                     eta={"answer": "yes", "when": "2026-11-20"}, cited=cited)
        nudge = pt.nudge_draft({"kind": pt.APPROVAL_KIND,
                                "event": {"asked_on": "2026-11-02", "family": "purchase"}})
        status = proactive_chasers._status_draft(
            {"number": "SO-5001", "status": "Released", "lines": [
                {"item": "ITEM-7", "ordered": "18", "unit": "kg", "shipped": "0", "open": "18"}],
             "shipment": {}, "due": date(2026, 11, 12), "holds": [], "handles": []},
            date(2026, 11, 8))
        from operating import inbox_triage
        holding = inbox_triage.reply_draft(
            {"event": {"messages": [{"sent": "2026-11-05", "subject": "PO-106001"}],
                       "asked_by": "sam@customer.example"}}, date(2026, 11, 8))
        for name, draft in (("vendor", vendor), ("customer", customer), ("nudge", nudge),
                            ("status", status), ("holding", holding)):
            with self.subTest(name):
                self.assertTrue(draft["copy_check"]["ok"], draft)
                self.assertTrue(draft["body"])
                self.assertFalse(re.search(r"^(Hello|Dear)\b", draft["body"]))
                self.assertNotIn("Thank you", draft["body"])
        for purpose in drafts.PURPOSES:
            with self.subTest(purpose):
                made = drafts.compose(purpose, ["Can you confirm PO-106001 by Thu 12 Nov?"],
                                      subject="PO-106001: delivery date", facts=FACTS,
                                      today=date(2026, 11, 8))
                self.assertTrue(made["copy_check"]["ok"], made)

    def test_the_digest(self):
        text = briefing.digest_text({"status": "read", "count": 0, "items": [],
                                     "groups": []}, first_name="Sam")
        self.assertTrue(text.startswith("Hi Sam,"))
        self.assertClean(text, "ui")


if __name__ == "__main__":
    unittest.main()
