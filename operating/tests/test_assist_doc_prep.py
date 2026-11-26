"""Tests for canonical system-write normalization and proactive document/PO/deck preparation.

Covers:
1. Canonical system-write normalization across noisy live Planner, Business Central, Portal,
   SharePoint, Calendar, Mission Control, FileMaker, and Sage strings.
2. Planner prompt & strict parser support for ``prepare_document`` and canonical ``system_write``.
3. Actual ``.xlsx``, ``.pptx``, ``.docx``, ``.pdf``, ``.csv``, and ``fill_form`` artifact
   generation via ``operating.instruct_files`` (valid non-empty zip/XML output, zero em dashes,
   zero fabricated numbers, explicit ``[TBD - confirm from BC/inventory]`` placeholders).
4. End-to-end integration with ``assist_plan.run_plans`` and ``assist_prepared.serve``.

Evidence label: ``isolated-fixture``."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

from operating import assist_doc_prep as doc_prep
from operating import assist_plan as plan
from operating import assist_prepared as prep
from operating import assist_writes as asw
from operating import autopilot_write_readiness as readiness
from operating import instruct_files as inf
from operating import prepared_writes as pw
from operating.tests import test_assist_matters as base
from operating.tests.test_autopilot_write import OWNER as OWNER_EMAIL
from operating.tests.test_autopilot_write_guards import people_guard


def _zip_text(path: Path) -> str:
    with zipfile.ZipFile(path, "r") as zf:
        assert zf.testzip() is None
        return "\n".join(
            zf.read(name).decode("utf-8", errors="ignore")
            for name in zf.namelist()
            if name.endswith(".xml")
        )


class CanonicalSystemWriteTests(unittest.TestCase):
    def test_noisy_planner_strings_normalize_to_canonical_with_plan_name(self):
        cases = [
            (
                "Microsoft Planner (QA/QC Task Schedule plan) :: create task",
                "",
                "planner",
                "create_task",
                "task_create",
                "QA/QC Task Schedule",
            ),
            (
                {"system": "Microsoft Planner (QA/QC Task Schedule plan)", "operation": "create task"},
                "",
                "planner",
                "create_task",
                "task_create",
                "QA/QC Task Schedule",
            ),
            (
                {"system": "Planner (QA/QC Task Schedule)", "operation": "add new task"},
                "",
                "planner",
                "create_task",
                "task_create",
                "QA/QC Task Schedule",
            ),
            (
                {"system": "QA/QC Task Schedule (Planner)", "operation": "create task"},
                "",
                "planner",
                "create_task",
                "task_create",
                "QA/QC Task Schedule",
            ),
            (
                {"system": "QA/QC Task Schedule (Microsoft Planner)", "operation": "mark task complete"},
                "",
                "planner",
                "update_task",
                "task_update",
                "QA/QC Task Schedule",
            ),
            (
                {"system": "MS Planner - Production Schedule", "operation": "assign task"},
                "",
                "planner",
                "create_task",
                "task_create",
                "Production Schedule",
            ),
        ]
        for raw, about, exp_sys, exp_op, exp_adapter, exp_plan in cases:
            with self.subTest(raw=raw):
                got = asw.canonicalize_system_write(raw, about=about)
                self.assertEqual(got["system"], exp_sys)
                self.assertEqual(got["operation"], exp_op)
                self.assertEqual(got["canonical_key"], f"{exp_sys}::{exp_op}")
                self.assertEqual(got["adapter_operation"], exp_adapter)
                self.assertEqual(got["plan_name"], exp_plan)

    def test_bc_portal_sharepoint_calendar_mc_filemaker_sage_normalize_cleanly(self):
        cases = [
            (
                {"system": "Microsoft Dynamics 365 Business Central", "operation": "Create Purchase Order", "record": "PO-9012"},
                "",
                "bc",
                "create_purchase_order",
                "purchase_order_create",
                {},
            ),
            (
                {"system": "Business Central (Petra Soapworks)", "operation": "new sales order"},
                "",
                "bc",
                "create_sales_order",
                "sales_order_create",
                {"company_name": "Petra Soapworks"},
            ),
            (
                {"system": "D365 BC", "operation": "prepare sales quote"},
                "",
                "bc",
                "create_quote",
                "",
                {},
            ),
            (
                "Business Central :: receive PO",
                "Post warehouse receipt for PO-8812",
                "bc",
                "record_receipt",
                "",
                {},
            ),
            (
                {"system": "BC", "operation": "QC lot release"},
                "",
                "bc",
                "record_qc",
                "",
                {},
            ),
            (
                {"system": "Petra Portal", "operation": "update order", "record": "SO-10421"},
                "",
                "portal",
                "update_order",
                "sales-order.update",
                {},
            ),
            (
                {"system": "Operations Portal", "operation": "create ship release"},
                "",
                "portal",
                "create_ship_release",
                "ship-release.create",
                {},
            ),
            (
                {"system": "SharePoint Online (Quality Docs)", "operation": "upload document"},
                "",
                "sharepoint",
                "upload_document",
                "file_upload",
                {"folder_name": "Quality Docs"},
            ),
            (
                {"system": "Outlook Calendar", "operation": "schedule meeting"},
                "",
                "calendar",
                "schedule_meeting",
                "event_create",
                {},
            ),
            (
                {"system": "Mission Control", "operation": "add progress note"},
                "",
                "mc",
                "add_note",
                "task_note",
                {},
            ),
            (
                {"system": "FileMaker Pro", "operation": "edit record"},
                "",
                "filemaker",
                "update_record",
                "record_edit",
                {},
            ),
            (
                {"system": "Sage 100", "operation": "post payment"},
                "",
                "sage",
                "post_payment",
                "",
                {},
            ),
        ]
        for raw, about, exp_sys, exp_op, exp_adapter, exp_meta in cases:
            with self.subTest(raw=raw):
                got = asw.canonicalize_system_write(raw, about=about)
                self.assertEqual(got["system"], exp_sys)
                self.assertEqual(got["operation"], exp_op)
                self.assertEqual(got["canonical_key"], f"{exp_sys}::{exp_op}")
                self.assertEqual(got.get("adapter_operation", ""), exp_adapter)
                for k, v in exp_meta.items():
                    self.assertEqual(got.get(k), v)

    def test_build_write_intent_accepts_noisy_planner_system_and_operation(self):
        owner = pw.Person(ids=(OWNER_EMAIL,), full=False)
        now = dt.datetime(2026, 11, 25, 14, 0, tzinfo=dt.timezone.utc)
        readiness_map = {
            "schema": readiness.SCHEMA,
            "checked_at": now.isoformat(timespec="seconds"),
            "systems": {
                "planner": {"status": "ready-live", "live_flag": True, "missing": []},
            },
        }
        intent = asw.build_write_intent(
            {
                "system": "Microsoft Planner (QA/QC Task Schedule plan)",
                "operation": "create task",
                "target": {},
                "change": {"planId": "PlanQAQC01", "title": "Inspect Dandylion lot"},
                "reason": "Track QA inspection.",
            },
            owner,
            may=lambda _p, _a: None,
            guard=people_guard(),
            readiness_map=readiness_map,
            known_domains=("plx.example",),
            now=now,
        )
        self.assertEqual(intent["system"], "planner")
        self.assertEqual(intent["action"], "task_create")
        self.assertEqual(intent["mode"], "dry_run")


class PlannerPromptAndParseTests(unittest.TestCase):
    def test_prompt_and_head_teach_prepare_document_and_canonical_systems(self):
        for template in (plan.PROMPT, plan.HEAD):
            self.assertIn("prepare_document", template)
            self.assertIn("xlsx", template)
            self.assertIn("pptx", template)
            self.assertIn("docx", template)
            self.assertIn("fill_form", template)
            self.assertIn("create_purchase_order", template)
            self.assertIn("create_sales_order", template)
            self.assertIn("create_quote", template)
            self.assertIn("plan_name", template)
            self.assertNotIn("\u2014", template)

    def test_parse_normalizes_prepare_document_and_noisy_system_write(self):
        payload = {
            "acts": [
                {
                    "act": "prepare_document",
                    "about": "Vince asks Ricardo for a count sheet of Dandylion packaging and finished goods",
                    "document": {
                        "kind": "xlsx",
                        "title": "Dandylion Packaging and Finished Goods Count Sheet",
                        "about": "Count sheet of Dandylion packaging and finished goods",
                    },
                },
                {
                    "act": "system_write",
                    "about": "Add QA task to schedule",
                    "system": {
                        "system": "Microsoft Planner (QA/QC Task Schedule plan) :: create task",
                        "operation": "",
                        "record": "Dandylion count",
                    },
                },
                {
                    "act": "system_write",
                    "about": "Create PO for 10,000 8oz bottles",
                    "system": {
                        "system": "Dynamics 365 Business Central",
                        "operation": "Create Purchase Order",
                        "record": "10,000 8oz bottles",
                    },
                },
            ],
            "why": "Prepare the requested count sheet, schedule the Planner task, and stage the PO in BC.",
        }
        parsed = plan.parse(json.dumps(payload))
        self.assertEqual(parsed["status"], "planned")
        acts = parsed["plan"]["acts"]
        self.assertEqual(len(acts), 3)

        self.assertEqual(acts[0]["act"], "prepare_document")
        self.assertEqual(acts[0]["document"]["kind"], "xlsx")
        self.assertEqual(acts[0]["document"]["doc_type"], "count_sheet")

        self.assertEqual(acts[1]["system"]["system"], "planner")
        self.assertEqual(acts[1]["system"]["operation"], "create_task")
        self.assertEqual(acts[1]["system"]["canonical_key"], "planner::create_task")
        self.assertEqual(acts[1]["system"]["plan_name"], "QA/QC Task Schedule")

        self.assertEqual(acts[2]["system"]["system"], "bc")
        self.assertEqual(acts[2]["system"]["operation"], "create_purchase_order")
        self.assertEqual(acts[2]["system"]["canonical_key"], "bc::create_purchase_order")
        self.assertEqual(acts[2]["system"]["adapter_operation"], "purchase_order_create")


class ProactiveDocumentGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test-assist-doc-prep-"))

    def test_count_sheet_xlsx_generates_valid_workbook_with_tbd_placeholders_and_no_fake_numbers(self):
        act = {
            "act": "prepare_document",
            "about": "Vince asks Ricardo for a count sheet of Dandylion packaging and finished goods",
            "document": {"kind": "xlsx"},
        }
        art = doc_prep.prepare_document_artifact(act, output_dir=self.tmp)
        self.assertTrue(art["download_ready"])
        self.assertEqual(art["kind"], "xlsx")
        self.assertEqual(art["doc_type"], "count_sheet")
        self.assertTrue(art["filename"].endswith(".xlsx"))
        self.assertGreater(art["size_bytes"], 500)
        self.assertTrue(art["sheets"])
        self.assertEqual(art["sheets"][0]["row_count"], 2)
        self.assertTrue(art["preview_lines"])

        path = Path(art["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        xml = _zip_text(path)
        self.assertIn("Dandylion - Packaging", xml)
        self.assertIn("Dandylion - Finished Goods", xml)
        self.assertIn(doc_prep.TBD_PLACEHOLDER, xml)
        self.assertNotIn("\u2014", xml)
        self.assertNotIn("\u2014", json.dumps(art, ensure_ascii=False))
        # No fabricated dollar amounts or percentages anywhere in the workbook.
        self.assertIsNone(re.search(r"\$\d", xml))

    def test_margin_presentation_pptx_generates_valid_deck_without_fabricated_figures(self):
        act = {
            "act": "prepare_document",
            "about": "Prepare Q4 Soapbox margin presentation",
            "document": {"kind": "pptx", "title": "Q4 Soapbox Margin Presentation"},
        }
        art = doc_prep.prepare_document_artifact(
            act,
            matter={"title": "Q4 Soapbox margin presentation", "counterpart": "Soapbox"},
            output_dir=self.tmp,
        )
        self.assertTrue(art["download_ready"])
        self.assertEqual(art["kind"], "pptx")
        self.assertEqual(art["doc_type"], "presentation")
        self.assertTrue(art["filename"].endswith(".pptx"))
        self.assertGreaterEqual(len(art["slides"]), 4)

        path = Path(art["path"])
        with zipfile.ZipFile(path, "r") as zf:
            self.assertIsNone(zf.testzip())
            slide_files = [n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
            self.assertGreaterEqual(len(slide_files), 4)
        xml = _zip_text(path)
        self.assertIn("Soapbox", xml)
        self.assertIn("Q4", xml)
        self.assertIn(doc_prep.TBD_PLACEHOLDER, xml)
        self.assertNotIn("\u2014", xml)
        self.assertIsNone(re.search(r"\$\d", xml))

    def test_draft_po_docx_keeps_grounded_quantity_and_marks_ungrounded_cost_tbd(self):
        act = {
            "act": "prepare_document",
            "about": "Draft PO for 10,000 8oz bottles",
        }
        art = doc_prep.prepare_document_artifact(
            act,
            matter={"title": "Draft PO for 10,000 8oz bottles", "counterpart": "Berlin Packaging"},
            output_dir=self.tmp,
        )
        self.assertTrue(art["download_ready"])
        self.assertEqual(art["kind"], "docx")
        self.assertEqual(art["doc_type"], "purchase_order")
        self.assertTrue(art["filename"].endswith(".docx"))
        self.assertTrue(art["sections"])

        xml = _zip_text(Path(art["path"]))
        self.assertIn("10,000", xml)
        self.assertIn("8oz bottles", xml)
        self.assertIn("Berlin Packaging", xml)
        self.assertIn(doc_prep.TBD_PLACEHOLDER, xml)
        self.assertNotIn("\u2014", xml)
        self.assertIsNone(re.search(r"\$\d", xml))

    def test_quote_pdf_and_csv_and_grounded_records_work_cleanly(self):
        quote_art = doc_prep.prepare_document_artifact(
            {"act": "prepare_document", "about": "Prepare quote for 5,000 pump caps"},
            matter={"counterpart": "Soapbox"},
            output_dir=self.tmp,
        )
        self.assertEqual(quote_art["kind"], "docx")
        self.assertEqual(quote_art["doc_type"], "quote")
        self.assertIn("5,000", _zip_text(Path(quote_art["path"])))

        pdf_art = doc_prep.prepare_document_artifact(
            {"act": "prepare_document", "about": "Prepare PDF summary of open Dandylion lots", "document": {"kind": "pdf"}},
            output_dir=self.tmp,
        )
        self.assertTrue(pdf_art["download_ready"])
        self.assertIn(pdf_art["kind"], ("pdf", "docx"))
        self.assertGreater(pdf_art["size_bytes"], 200)

        grounded_art = doc_prep.prepare_document_artifact(
            {"act": "prepare_document", "about": "Count sheet of Dandylion packaging", "document": {"kind": "xlsx"}},
            grounded_records=[
                {
                    "item": "Dandylion 8oz Bottle",
                    "sku": "PKG-801",
                    "category": "Packaging",
                    "system_qty": 4200,
                    "location": "BIN-A4",
                }
            ],
            output_dir=self.tmp,
        )
        xml = _zip_text(Path(grounded_art["path"]))
        self.assertIn("PKG-801", xml)
        self.assertIn("4200", xml)
        self.assertIn("BIN-A4", xml)

    def test_fill_form_handles_existing_template_and_missing_template_safely(self):
        # Missing template: falls back cleanly to structured docx checklist.
        no_tpl = doc_prep.prepare_document_artifact(
            {"act": "prepare_document", "about": "Fill out supplier onboarding questionnaire", "document": {"kind": "fill_form"}},
            output_dir=self.tmp,
        )
        self.assertTrue(no_tpl["download_ready"])
        self.assertEqual(no_tpl["kind"], "docx")
        self.assertTrue(no_tpl.get("form_template_needed"))

        # Existing docx template with blank field: runs through operating.doc_fill.
        tpl_bytes = inf.make("docx", "# Supplier Form\n\nPO Number: _____\nVendor: _____\n")
        tpl_path = self.tmp / "supplier_form.docx"
        tpl_path.write_bytes(tpl_bytes)
        filled = doc_prep.prepare_document_artifact(
            {
                "act": "prepare_document",
                "about": "Fill out supplier form",
                "document": {"kind": "fill_form", "source_path": str(tpl_path)},
            },
            output_dir=self.tmp,
        )
        self.assertTrue(filled["download_ready"])
        self.assertEqual(filled["kind"], "docx")
        self.assertFalse(filled.get("form_template_needed"))

    def test_screen_refusal_blocks_file_creation_fail_closed(self):
        art = doc_prep.prepare_document_artifact(
            {"act": "prepare_document", "about": "Prepare payroll memo for confidential bonus"},
            output_dir=self.tmp,
            screen=lambda text: "pay_or_hr" if "payroll" in text.lower() else None,
        )
        self.assertFalse(art["download_ready"])
        self.assertTrue(art.get("refused"))
        self.assertEqual(art.get("reason"), "pay_or_hr")
        self.assertEqual(list(self.tmp.rglob("*")), [])

    def test_proactive_detector_triggers_on_real_asks_and_skips_routine_emails(self):
        positives = [
            ("Vince asks Ricardo for a count sheet of Dandylion packaging and finished goods", "xlsx", "count_sheet"),
            ("Prepare Q4 Soapbox margin presentation", "pptx", "presentation"),
            ("Draft PO for 10,000 8oz bottles", "docx", "purchase_order"),
            ("Can you prepare a quote for 5,000 bottles?", "docx", "quote"),
            ("Please fill out the supplier questionnaire for Croda", "fill_form", "form_fill"),
        ]
        for text, exp_kind, exp_type in positives:
            with self.subTest(positive=text):
                found = doc_prep.detect_document_requests({"title": text, "detail": text}, text)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["kind"], exp_kind)
                self.assertEqual(found[0]["doc_type"], exp_type)

        negatives = [
            "Please move SO-10421 delivery date to Dec 8",
            "Where is PO-8812? Did it ship Monday?",
            "Please send the COA for lot 7",
            "Standard customer question about shipping dock hours",
        ]
        for text in negatives:
            with self.subTest(negative=text):
                found = doc_prep.detect_document_requests({"title": text, "detail": text}, text)
                self.assertEqual(found, [])

    def test_sharepoint_folder_and_excel_tab_prompts_parse_cleanly(self):
        sp_reqs = doc_prep.detect_document_requests(
            {
                "title": "Q4 Soapbox margin deck",
                "detail": "Prepare Q4 Soapbox margin presentation and save to SharePoint under Customers/Soapbox/Q4",
            },
        )
        self.assertEqual(len(sp_reqs), 1)
        self.assertEqual(sp_reqs[0]["kind"], "pptx")
        self.assertEqual(sp_reqs[0]["destination_folder"], "Customers/Soapbox/Q4")
        self.assertTrue(sp_reqs[0]["create_folder"])

        tab_reqs = doc_prep.detect_document_requests(
            {
                "title": "Add tab to supply tracker",
                "detail": "Add a tab called 'Nov Receipts' to Weekly Supply Tracker.xlsx",
            },
        )
        self.assertEqual(len(tab_reqs), 1)
        self.assertEqual(tab_reqs[0]["kind"], "xlsx")
        self.assertEqual(tab_reqs[0]["workbook_operation"], "add_sheet")
        self.assertEqual(tab_reqs[0]["target_workbook"], "Weekly Supply Tracker.xlsx")
        self.assertEqual(tab_reqs[0]["sheet_name"], "Nov Receipts")


class EndToEndAssistIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail
    read = base.Matters.read
    cycle = base.Matters.cycle

    def test_run_plans_persists_prepared_document_and_canonical_system_write(self):
        t0 = base.NOW - dt.timedelta(hours=2)
        h = self.mail("t-doc-plan", base.OUT, ["ann@ours.example"], at=t0, box=base.ANN)
        self.read(h, thread="t-doc-plan", actor=base.ANN, at=t0)
        self.cycle()

        answer = json.dumps({
            "acts": [
                {
                    "act": "prepare_document",
                    "about": "Vince asks Ricardo for a count sheet of Dandylion packaging and finished goods",
                    "document": {"kind": "xlsx", "title": "Dandylion Count Sheet"},
                },
                {
                    "act": "system_write",
                    "about": "Log QA count task",
                    "system": {
                        "system": "Microsoft Planner (QA/QC Task Schedule plan)",
                        "operation": "create task",
                        "record": "Dandylion count",
                    },
                },
            ],
            "why": "Prepare the count sheet and track the count in Planner.",
        })
        state_dir = Path(tempfile.mkdtemp(prefix="test-plan-state-"))
        out = plan.run_plans(
            self.pg,
            lambda _p: answer,
            lambda m: "Vince asks Ricardo for a count sheet of Dandylion packaging and finished goods",
            lambda ms: {m["matter_id"]: ([], 0) for m in ms},
            model="stand-in",
            state_dir=state_dir,
        )
        self.assertEqual(out["planned"], 1)
        stored = self.pg.execute(
            "SELECT next_act, plan FROM assist.matters WHERE subject_key = 't-doc-plan'"
        ).fetchone()
        self.assertEqual(stored[0], "attach")
        stored_plan = stored[1]
        self.assertEqual(stored_plan["acts"][0]["act"], "prepare_document")
        self.assertEqual(stored_plan["acts"][1]["system"]["canonical_key"], "planner::create_task")
        self.assertEqual(stored_plan["acts"][1]["system"]["plan_name"], "QA/QC Task Schedule")
        docs = stored_plan["prepared_documents"]
        self.assertEqual(len(docs), 1)
        self.assertTrue(docs[0]["download_ready"])
        self.assertEqual(docs[0]["kind"], "xlsx")
        self.assertTrue(Path(docs[0]["path"]).is_file())

    def test_assist_prepared_serve_attaches_proactive_documents_without_false_write_refusals(self):
        t0 = base.NOW - dt.timedelta(hours=1)
        h = self.mail("t-deck", base.OUT, ["ann@ours.example"], at=t0, box=base.ANN)
        self.read(h, thread="t-deck", actor=base.ANN, at=t0)
        self.cycle()
        state_dir = Path(tempfile.mkdtemp(prefix="test-prep-state-"))

        def session(_owner, _oid, extra="", as_of="", prepared=None):
            return [{
                "draft": "Here is the Q4 Soapbox margin presentation outline with TBD slots for live BC numbers.",
                "proposals": [
                    {
                        "tool": "plx.prepare_document",
                        "arguments": {
                            "kind": "pptx",
                            "title": "Q4 Soapbox Margin Presentation",
                            "about": "Prepare Q4 Soapbox margin presentation",
                        },
                    }
                ],
            }]

        out = prep.serve(
            self.pg,
            [base.ANN],
            session,
            lambda ms: {m["matter_id"]: {"text": "Prepare Q4 Soapbox margin presentation", "situation": "s"} for m in ms},
            lambda ms, _gs: {m["matter_id"]: ([], 0) for m in ms},
            screen=lambda _t: None,
            state=state_dir,
            model="stand-in",
        )
        self.assertEqual((out["served"], out.get("drafted")), (1, 1))
        row = self.pg.execute(
            "SELECT plan->'prepared'->'prepared_documents', plan->'prepared'->'write_refusals' "
            "FROM assist.matters WHERE subject_key = 't-deck'"
        ).fetchone()
        docs, refusals = row[0], row[1]
        self.assertIsNone(refusals)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["kind"], "pptx")
        self.assertTrue(docs[0]["download_ready"])
        self.assertTrue(Path(docs[0]["path"]).is_file())


class StaticHygieneTests(unittest.TestCase):
    def test_zero_em_dashes_in_all_touched_modules(self):
        for mod in (doc_prep, plan, prep, asw):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            self.assertNotIn("\u2014", src, mod.__name__)


if __name__ == "__main__":
    unittest.main()
