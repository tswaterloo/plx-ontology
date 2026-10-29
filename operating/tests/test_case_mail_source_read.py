"""case.mail_evidence: mail must reach a case, counts per reason (injected transport).

Evidence label: ``injected-transport`` (the ``CaseMailTestCase`` world of
test_case_mail: real GET-only readers over ``FakeSender``, the real mirror client
over ``FakeWorkbench``; no socket is opened).

Live run live-2026-10-02T02:02Z (main 250eae4) passed ``case.mail_evidence`` with
``evidence_label: no-source-read``: one message read, PO relation read, zero
cases. Mail is a product source only when it reaches a case, so such a run must
fail, and the survey must count the stored mail-to-PO links by tier, value-free.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import unittest

from devloop import live_verify as lv
from operating import case_mail
from operating.case_mail_scope import MailScope
from operating.tests.test_case_mail import BASE, ENV, KEY, CaseMailTestCase, _msg

ROOT = Path(lv.ROOT)
SPEC = "devloop/live-checks/case.mail_evidence.json"


def _spec(name: str = SPEC) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


class SourceReadTests(CaseMailTestCase):
    def run_spec(self) -> tuple[int, dict, str]:
        spec = _spec()
        argv = [part.replace("{tmp}", str(self.root / "spec-tmp")) for part in spec["command"]]
        argv = [str(ROOT / part) if part.startswith("examples/") else part for part in argv]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = case_mail.main(argv[3:], environ={**ENV, "PLX_WORKBENCH_API_URL": BASE,
                                                     "PLX_WORKBENCH_API_KEY": KEY},
                                  graph_sender=self.sender, workbench_fetch=self.fake)
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_captured_mail_that_reaches_no_case_is_no_source_read_and_fails(self):
        for mid, message in list(self.messages.items()):
            self.messages[mid] = _msg(mid, "Weekly hello", "Nothing about orders here.",
                                      "sales@acme-bottles.com",
                                      conversation=message["conversationId"])
        code, document, _text = self.run_spec()
        self.assertEqual(code, 0)
        self.assertEqual(document["evidence_label"], "no-source-read")
        self.assertEqual(document["totals"]["cases"], 0)
        # Mail and the PO relation were read; no case got mail.
        self.assertEqual(document["source_read"],
                         {"mail_messages": 3, "po_relation": "read", "cases_with_mail": 0,
                          "inputs_read": 2, "unblocked_unread": []})
        failures = lv.check_expectations(_spec(), code, document)
        self.assertIn("evidence_label != expected", failures)
        self.assertIn("totals.cases < 1", failures)
        self.assertIn("source_read.cases_with_mail < 1", failures)

    def test_a_po_relation_read_alone_is_not_mail_evidence(self):
        self.assertEqual(case_mail.evidence_label(
            {"counts": {"messages_visible": 0}, "mail_source": {"po_scan_status": "read"}},
            False), "no-source-read")
        self.assertEqual(case_mail.evidence_label(
            {"counts": {"messages_visible": 1}}, False), "live-read")
        self.assertEqual(case_mail.evidence_label(
            {"counts": {"messages_visible": 1}}, True), "injected-transport")

    def test_survey_counts_the_stored_links_by_tier_without_values(self):
        code, document, text = self.run_spec()
        survey = document["survey"]
        # m-confirm names PO100123 in its subject and body, m-other PO100200: four stored
        # links whose message the reader may read (the outside mailbox's is not counted).
        # This spec world corroborates none of them (as the per-scan survey's run did).
        self.assertEqual(survey["links_by_tier"], {"lead": 4})
        self.assertEqual((survey["po_links"], survey["messages_with_po_links"],
                          survey["pos_linked"], survey["po_links_withheld"]), (4, 2, 2, 0))
        self.assertEqual((survey["po_scan_status"], survey["po_rows_scanned"]), ("read", 2))
        self.assertEqual(survey["relations"]["supplier_source"]["status"], "read")
        self.assertEqual(document["totals"]["cases"], 2)
        self.assertEqual(document["source_read"]["cases_with_mail"], 2)
        self.assertGreaterEqual(document["projection"]["seconds"], 0)
        self.assertEqual(lv.check_expectations(
            _spec(), code, {**document, "evidence_label": "live-read"}), [])
        recorded = json.dumps(lv._recorded(_spec(), document))
        self.assertIn('"lead": 4', recorded)
        for secret in ("100200", "PO100123", "acme-bottles", "graph-mail://", "@"):
            self.assertNotIn(secret, recorded)
            self.assertNotIn(secret, json.dumps(survey))

    def test_a_number_naming_two_orders_reaches_cases_as_leads_only(self):
        # A second gold row PO#100123 reads as the same number as PO100123: the
        # supplier's "PO100123" names both, a lead to each, never corroborated.
        self.fake.tables["public.app_purchase_order"].append(
            {**self.fake.tables["public.app_purchase_order"][0], "id": "po-twin",
             "po_number": "PO#100123"})
        code, document, _text = self.run_spec()
        self.assertNotIn("corroborated", document["survey"]["links_by_tier"])
        relations = [name for case in document["cases"] for name in case["relations"]]
        self.assertIn("mention_link:lead", relations)
        self.assertTrue(all(name.endswith(":lead") or name.startswith("cited_in_case:")
                            for name in relations), relations)
        self.assertGreaterEqual(document["source_read"]["cases_with_mail"], 1)
        self.assertEqual(lv.check_expectations(
            _spec(), code, {**document, "evidence_label": "live-read"}), [])


class LiveScopeTests(unittest.TestCase):
    """The live scope is the purchasing/operations mail, inside the allowlist."""

    def test_scope_is_allowlisted_granted_and_counted(self):
        for name in (SPEC,):
            spec = _spec(name)
            lv.validate_spec(spec)
            command = spec["command"]
            scope_file = command[command.index("--mail-capture-scope") + 1]
            self.assertEqual(scope_file,
                             "examples/case_mail_purchasing_capture_scope.live-check.json")
            self.assertEqual(command[command.index("--capture-page-budget") + 1], "1")
            audience_file = command[command.index("--live-audience") + 1]
            scope = json.loads((ROOT / scope_file).read_text(encoding="utf-8"))
            audience = json.loads((ROOT / audience_file).read_text(encoding="utf-8"))
            addresses = [m["address"] for m in scope["mailboxes"]]
            self.assertEqual(sorted(addresses), sorted(
                f"{box}@petrasoap.com" for box in ("order-confirmation", "ship", "torontowh",
                                                   "ap", "receiving", "qclabresults")))
            builtin = MailScope.builtin()
            for address in addresses:
                with self.subTest(spec=name, address=address):
                    self.assertTrue(builtin.allows([address]))
                    grant = audience["audiences"][f"mail:{address}"]
                    self.assertIn("staff:purchasing", grant)
                    self.assertIn("svc:mail-capture", grant)
                    self.assertNotIn("staff:operations", grant)
            if name == SPEC:
                self.assertEqual(spec["expect"]["json_equals"]["capture.mailboxes_in_scope"],
                                 len(addresses))
                self.assertEqual(spec["expect"]["json_equals"]["evidence_label"], "live-read")


if __name__ == "__main__":
    unittest.main()
