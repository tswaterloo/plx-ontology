"""Pilot-readiness regression tests across all Petra employee roles (CEO, Purchasing,
Finance/AP-AR, Quality/QA, Operations/Logistics, Sales).

Verifies concrete defects discovered in live Assist data:
1. Automated IT/system/quarantine/password-expiry/CI/Stage-Rail/headcount noise filtered
   without suppressing genuine customer, supplier, or staff mail.
2. Zero em dashes, leaked prompt labels (`(P2)`, `from P2`), robotic boilerplate openers,
   or trailing empty MC question labels in rendered items and job summaries.
3. Wait matters infer concrete person names from details instead of showing 'No answer yet
   from them', and expose counterpart as sender rather than the owner's own address.
4. Planned matters surface `plan.acts` into `facts`, `todos`, and `to`, and `assist_routes`
   forwards `facts`, `todos`, `options`, and `redo` while deduplicating identical items.
5. `assist_plan.parse` recovers valid JSON surrounded by prose/stray braces.
6. `assist_worker` passes context-aware follow-up prompts for `wait` matters and respects
   `person_line` directions from `reply-from-line`.
7. Role-relevant Petra workflow templates (Purchasing, Finance/AP-AR, Quality/QA, Ops, Sales)
   exist and pass `operating.copy_gate`.
"""
from __future__ import annotations

import datetime as dt
import json
import unittest
from unittest.mock import patch

from operating import assist_job as aj
from operating import assist_matters as am
from operating import assist_plan as ap_plan
from operating import assist_readings as ar
from operating import assist_worker as aw
from operating import assist_workflows as wf
from operating import copy_gate
from operator_api import assist_jobs
from operator_api.tests import test_assist_routes as routes_base


class NoiseFilteringTests(unittest.TestCase):
    def test_automated_system_and_it_alerts_are_filtered(self) -> None:
        noisy = [
            ("quarantine@messaging.microsoft.com", "Microsoft 365 security: You have messages in quarantine", ""),
            ("Microsoft 365 <quarantine@messaging.microsoft.com>", "You have messages in quarantine", ""),
            ("stage-rail@plxcustomer.io", "[ACTION] Stage Rail loop - BLOCKED: x-dawn-2026-08-11", ""),
            ("password-expiry@emvue.ca", "Action needed: your work password expires in 8 days", ""),
            ("password-expiry@nfd.ca", "Your password expires soon", ""),
            ("cos@petrasoap.com", "UAT BC scheduled sync has not refreshed", ""),
            ("cos@petrasoap.com", "PLX required CI red: lint-typecheck-build", ""),
            ("cos@petrasoap.com", "Workbench API health 2026-11-15: ALERT", ""),
            ("support@github.com", "[petralabx/PLX_MC] Possible valid secrets detected", ""),
            ("cwadmin@emvue.ca", "Your open ticket #1691497 RDS login", ""),
            ("luis@petrasoap.com", "QC Morning Headcount Logged \u2014 2026-11-25", ""),
            ("", "", "Automated Stage Rail alert: one trading-idea item is blocked"),
            ("", "", "Microsoft quarantine notice: one held message from vendor"),
        ]
        for sender, subj, summ in noisy:
            self.assertTrue(
                ar.is_system_alert_mail(sender, subj, summ),
                f"Expected noise to be filtered: {(sender, subj, summ)!r}",
            )

    def test_real_petra_business_mail_is_preserved(self) -> None:
        legit = [
            ("quickbooks@notification.intuit.com", "Statement Of Account from CRS Packaging Inc.", ""),
            ("collections@roadrunners.ca", "Past Due Invoice 305911 Download Link", ""),
            ("customersupport@apexltl.com", "RE: [EXTMSG] Need POD for PRO 01259031869 Movati", ""),
            ("buyer@croda.com", "PO 106001 revised ETA and COA attached", ""),
            ("maria@petrasoap.com", "Steamworks Baths substitute BR300PC with BR385SC", ""),
            ("luis@petrasoap.com", "Lot BC-26313C COA review needed before shipment", ""),
        ]
        for sender, subj, summ in legit:
            self.assertFalse(
                ar.is_system_alert_mail(sender, subj, summ),
                f"Expected real business mail to stay visible: {(sender, subj, summ)!r}",
            )


class CleanFormattingAndPlanSurfaceTests(unittest.TestCase):
    def test_clean_user_text_removes_em_dash_prompt_labels_and_robotic_openers(self) -> None:
        self.assertEqual(
            am.clean_user_text("PO 106001 \u2014 delayed from P2 by Venessa Bandiera (P2)"),
            "PO 106001 - delayed from them by Venessa Bandiera",
        )
        self.assertEqual(
            am.clean_user_text("Mission Control question on "),
            "Mission Control question",
        )
        self.assertEqual(
            am.clean_user_text("I have reviewed the records. Shipment leaves Friday.", strip_opener=True),
            "Shipment leaves Friday.",
        )
        self.assertEqual(
            am.clean_user_text("Based on the data, order 418-S-16701 is ready.", strip_opener=True),
            "Order 418-S-16701 is ready.",
        )
        self.assertNotIn("\u2014", assist_jobs.strip_internal_uris("Summary \u2014 3 late POs"))

    def test_wait_matter_infers_person_and_uses_counterpart_as_sender(self) -> None:
        now = dt.datetime(2026, 11, 25, 18, 0, tzinfo=dt.timezone.utc)
        row = {
            "matter_id": "a" * 24,
            "owner_person": "maria-id",
            "kind": "wait",
            "subject_key": "t-1",
            "sources": {"readings": [["emails", "m1"]]},
            "basis_at": now - dt.timedelta(days=1),
            "title": "No answer yet from them",
            "detail": "which months to bill $40 + HST, from Venessa Bandiera (P2) (nothing in your mail says how long this usually takes: when should it be looked for?)",
            "next_act": "ask_person",
            "due_at": None,
            "state": "open",
            "verified_at": now,
            "verify_note": "",
            "plan": None,
            "plan_status": "none",
            "plan_reason": "",
            "audiences": [],
            "markings": [],
            "counterpart": "",
            "legacy_item_id": None,
            "opened_at": now,
            "handles": [],
            "source": "emails",
            "quote": "",
            "sender": "maria@petrasoap.com",
            "subject": "Billing question",
            "tsender": None,
            "plan_at": None,
            "requested_at": None,
            "touched_at": None,
        }
        item = am.item_of(row, now=now)
        self.assertEqual(item["summary"], "No answer yet from Venessa Bandiera")
        self.assertEqual(item["sender"], "Venessa Bandiera")
        self.assertNotIn("(P2)", item["note"])
        self.assertNotIn("nothing in your mail says", item["note"])
        self.assertIn("no typical reply time on record yet", item["note"])

    def test_planned_matter_surfaces_acts_into_facts_todos_and_recipients(self) -> None:
        now = dt.datetime(2026, 11, 25, 18, 0, tzinfo=dt.timezone.utc)
        row = {
            "matter_id": "b" * 24,
            "owner_person": "vince-id",
            "kind": "conversation",
            "subject_key": "t-2",
            "sources": {"readings": [["emails", "m2"]]},
            "basis_at": now - dt.timedelta(hours=2),
            "title": "Customer asks about 90ml tube fill \u2014 SIDIA",
            "detail": "Check with R&D and update PO",
            "next_act": "forward",
            "due_at": now + dt.timedelta(days=1),
            "state": "open",
            "verified_at": now,
            "verify_note": "",
            "plan": {
                "ver": 1,
                "why": "Forward technical fill question to R&D.",
                "acts": [
                    {"act": "forward", "to": ["giulia@petrasoap.com"], "about": "Check 90ml tube fill specs"},
                    {"act": "system_write", "to": [], "about": "Update expected ship date to Nov 28",
                     "system": {"system": "bc", "operation": "sales_order_update", "record": "SO-1042"}},
                ],
            },
            "plan_status": "planned",
            "plan_reason": "",
            "audiences": [],
            "markings": [],
            "counterpart": "SIDIA",
            "legacy_item_id": None,
            "opened_at": now,
            "handles": [],
            "source": "emails",
            "quote": "Can we move to a 90ml tube?",
            "sender": "alex@sidia.example",
            "subject": "90ml tube question",
            "tsender": None,
            "plan_at": now,
            "requested_at": None,
            "touched_at": None,
        }
        item = am.item_of(row, now=now)
        self.assertNotIn("\u2014", item["summary"])
        self.assertEqual(item["to"], ["giulia@petrasoap.com"])
        self.assertEqual(item["facts"], ["Suggested next step: Pass it on to giulia@petrasoap.com: Check 90ml tube fill specs"])
        self.assertEqual(item["todos"], [{"system": "bc", "record": "SO-1042", "what": "Update expected ship date to Nov 28"}])

    def test_plan_parse_recovers_json_with_surrounding_braces(self) -> None:
        raw = (
            "Note about {previous order}: here is the structured plan.\n"
            "```json\n"
            + json.dumps({"acts": [{"act": "reply", "to": ["buyer@example.com"]}], "why": "Direct answer"})
            + "\n```\nEnd {ref}."
        )
        got = ap_plan.parse(raw)
        self.assertEqual(got["status"], "planned")
        self.assertEqual(got["plan"]["acts"][0]["act"], "reply")


class WorkerFollowUpAndLineTests(unittest.TestCase):
    def test_wait_matter_and_person_line_generate_concrete_extra_context(self) -> None:
        matter = {
            "kind": "wait",
            "counterpart": "dsm-firmenich",
            "title": "No answer yet from dsm-firmenich",
            "detail": "USMCA/CUSMA cert for PO 106001 from Jacquelyn Reisig",
            "basis_at": dt.datetime(2026, 11, 21, 15, 0, tzinfo=dt.timezone.utc),
            "person_line": "Ask if they can send it by Thursday noon",
        }
        extra = aw._request_extra(matter)
        self.assertIn("FOLLOW-UP DRAFT REQUEST", extra)
        self.assertIn("dsm-firmenich", extra)
        self.assertIn("PO 106001", extra)
        self.assertIn("2026-11-21", extra)
        self.assertIn("Ask if they can send it by Thursday noon", extra)

    def test_outcome_cleans_em_dash_and_robotic_opener_from_draft_body(self) -> None:
        matter = {"oid": "o1", "kind": "conversation", "source": "emails"}
        rows = [{"draft": "I have reviewed the records. PO 106001 \u2014 shipping Friday."}]
        got = aw.outcome(rows, matter)
        self.assertEqual(got["status"], "drafted")
        body = got["plan"]["prepared"]["draft"]["body"]
        self.assertEqual(body, "PO 106001 - shipping Friday.")
        self.assertNotIn("\u2014", body)


class RoutesSurfaceAndDedupeTests(routes_base.AssistRoutes):
    def test_routes_deduplicate_filter_noise_and_forward_facts_and_todos(self) -> None:
        m1 = routes_base.matter(
            "1" * 24,
            facts=["Suggested next step: Reply to buyer@example.com"],
            todos=[{"system": "bc", "record": "SO-1", "what": "Update date"}],
            redo=True,
        )
        m1_dup = routes_base.matter(
            "2" * 24,
            facts=["Suggested next step: Reply to buyer@example.com"],
            todos=[{"system": "bc", "record": "SO-1", "what": "Update date"}],
        )
        m_noise = routes_base.matter(
            "3" * 24,
            sender="quarantine@messaging.microsoft.com",
            subject="Microsoft 365 security: You have messages in quarantine",
            summary="Microsoft quarantine notice: held message",
        )
        self.envelope["items"] = [m1, m1_dup, m_noise]
        got = self.listed()
        matters = [i for i in got["items"] if not i["item_id"].startswith("ap_")]
        self.assertEqual([i["item_id"] for i in matters], ["1" * 24])
        self.assertEqual(matters[0]["facts"], ["Suggested next step: Reply to buyer@example.com"])
        self.assertEqual(matters[0]["todos"], [{"system": "bc", "record": "SO-1", "what": "Update date"}])
        self.assertTrue(matters[0]["redo"])


class RoleWorkflowTemplateTests(unittest.TestCase):
    def test_all_petra_role_templates_present_and_pass_copy_gate(self) -> None:
        ids = {t["template_id"] for t in wf.TEMPLATES}
        for required in (
            "late-pos-weekly",
            "uninvoiced-shipments-check",
            "qa-coa-followup",
            "customer-order-status-digest",
            "ap-ar-overdue-review",
            "qc-hold-impact",
            "reconcile-supplier-invoices",
        ):
            self.assertIn(required, ids)
        for tpl in wf.TEMPLATES:
            for field in ("name", "description", "instruction"):
                text = tpl[field]
                self.assertNotIn("\u2014", text, f"{tpl['template_id']}.{field} has em dash")
                res = copy_gate.check(text, surface="ui")
                self.assertTrue(res["ok"], f"{tpl['template_id']}.{field}: {res['findings']}")


if __name__ == "__main__":
    unittest.main()
