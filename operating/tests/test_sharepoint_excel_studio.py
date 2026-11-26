"""Tests for the SharePoint folder/upload studio, non-destructive Excel workbook ops,
and executive document/deck/sheet visual polish.

Evidence label: ``isolated-fixture`` / ``injected-transport``.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

from operating import assist_doc_prep as doc_prep
from operating import autopilot_write_adapters as wa
from operating import chart_ooxml as ox
from operating import doc_excel_ops as deo
from operating import instruct_files as inf


def _zip_parts(data: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        assert zf.testzip() is None
        return {name: zf.read(name).decode("utf-8", errors="replace") for name in zf.namelist()}


class _FakeTransport:
    def __init__(self, answers=None):
        self.calls: list[tuple[dict, str]] = []
        self.answers = list(answers or [])

    def missing(self) -> list[str]:
        return []

    def __call__(self, request, key):
        self.calls.append((dict(request), key))
        if self.answers:
            return self.answers.pop(0)
        return 201, {"id": f"item_{len(self.calls)}", "eTag": f'"{{ETAG{len(self.calls)}}},1"'}


class SharePointFolderAndUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = _FakeTransport()
        self.adapter = wa.SharePointAdapter(self.transport, environ={})

    def test_create_folder_builds_nested_steps_preview_and_undo(self) -> None:
        action = {
            "system": "sharepoint",
            "operation": "create_folder",
            "target": {"drive_id": "b!drive1"},
            "payload": {"folder_path": "Customers/Soapbox/2026/Q4 Reviews"},
        }
        req = self.adapter.request(action)
        self.assertEqual(req["operation"], "create_folder")
        self.assertEqual(req["folder_path"], "Customers/Soapbox/2026/Q4 Reviews")
        self.assertEqual(req["segments"], ["Customers", "Soapbox", "2026", "Q4 Reviews"])
        self.assertEqual(len(req["steps"]), 4)
        self.assertEqual(
            req["steps"][0]["url"],
            f"{wa.GRAPH}/drives/b!drive1/root/children",
        )
        self.assertEqual(
            req["steps"][1]["url"],
            f"{wa.GRAPH}/drives/b!drive1/root:/Customers:/children",
        )
        self.assertEqual(
            req["steps"][3]["url"],
            f"{wa.GRAPH}/drives/b!drive1/root:/Customers/Soapbox/2026:/children",
        )
        self.assertEqual(req["steps"][3]["body"]["name"], "Q4 Reviews")
        self.assertEqual(
            req["destination_url"],
            f"{wa.GRAPH}/drives/b!drive1/root:/Customers/Soapbox/2026/Q4%20Reviews",
        )

        prev = self.adapter.preview(action)
        self.assertEqual(prev["folder_path"], "Customers/Soapbox/2026/Q4 Reviews")
        self.assertEqual(prev["step_count"], 4)
        self.assertEqual(prev["destination_url"], req["destination_url"])
        self.assertEqual(self.transport.calls, [])

        undo = self.adapter.undo_request(action, req)
        self.assertEqual(
            undo,
            {
                "method": "DELETE",
                "url": f"{wa.GRAPH}/drives/b!drive1/items/{{response:id}}",
                "body": None,
            },
        )

    def test_create_folder_is_idempotent_when_segments_already_exist(self) -> None:
        self.transport.answers = [
            (409, {"error": {"code": "nameAlreadyExists"}}),
            (409, {"error": {"code": "nameAlreadyExists"}}),
            (201, {"id": "fld_2026"}),
            (201, {"id": "fld_q4"}),
        ]
        action = {
            "system": "sharepoint",
            "operation": "create_folder",
            "target": {"drive_id": "b!drive1"},
            "payload": {"folder_path": "Customers/Soapbox/2026/Q4 Reviews"},
        }
        req = self.adapter.request(action)
        res = self.adapter.perform(req, "k-folder-1")
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["folders_existing"], ["Customers", "Customers/Soapbox"])
        self.assertEqual(
            res["folders_created"],
            ["Customers/Soapbox/2026", "Customers/Soapbox/2026/Q4 Reviews"],
        )
        self.assertFalse(res["already_existed"])
        self.assertEqual(len(self.transport.calls), 4)

        # All folders already exist: still idempotent done with already_existed=True.
        self.transport.answers = [(409, {"error": {"code": "nameAlreadyExists"}})] * 2
        req2 = self.adapter.request({
            "system": "sharepoint",
            "operation": "create_folder",
            "target": {"drive_id": "b!drive1"},
            "payload": {"folder_path": "Customers/Soapbox"},
        })
        res2 = self.adapter.perform(req2, "k-folder-2")
        self.assertEqual(res2["status"], "done")
        self.assertTrue(res2["already_existed"])

    def test_create_folder_refuses_traversal_and_illegal_segments(self) -> None:
        bad_paths = [
            "",
            "/",
            "../Secrets",
            "Customers/../Admin",
            "Customers/Bob's Docs",
            "Customers/R&D",
            "Customers//Empty",
            "/".join(f"L{i}" for i in range(15)),
        ]
        for bad in bad_paths:
            with self.subTest(path=bad):
                with self.assertRaises(wa.AdapterRefused):
                    self.adapter.request({
                        "system": "sharepoint",
                        "operation": "create_folder",
                        "target": {"drive_id": "b!drive1"},
                        "payload": {"folder_path": bad},
                    })

    def test_upload_document_and_put_file_defaults_to_rename_and_auto_creates_folders(self) -> None:
        raw_xlsx = inf.make("xlsx", "Item | Qty\nBottle | 100")
        b64 = base64.b64encode(raw_xlsx).decode("ascii")
        action = {
            "system": "sharepoint",
            "operation": "upload_document",
            "target": {"drive_id": "b!drive1"},
            "payload": {
                "name": "Soapbox Tracker.xlsx",
                "content_b64": b64,
                "content_type": inf.MEDIA["xlsx"],
                "folder_path": "Customers/Soapbox/2026",
                "create_folders": True,
            },
        }
        req = self.adapter.request(action)
        self.assertEqual(req["method"], "PUT")
        self.assertEqual(req["conflict_behavior"], "rename")
        self.assertIn("@microsoft.graph.conflictBehavior=rename", req["url"])
        self.assertEqual(req["destination_path"], "Customers/Soapbox/2026/Soapbox Tracker.xlsx")
        self.assertEqual(len(req["steps"]), 3)

        self.transport.answers = [
            (409, {"error": {"code": "nameAlreadyExists"}}),
            (201, {"id": "fld_soapbox"}),
            (201, {"id": "fld_2026"}),
            (201, {"id": "file_99", "eTag": '"{F99},1"'}),
        ]
        res = self.adapter.perform(req, "k-up-1")
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["folders_existing"], ["Customers"])
        self.assertEqual(res["folders_created"], ["Customers/Soapbox", "Customers/Soapbox/2026"])
        self.assertEqual(res["destination_path"], "Customers/Soapbox/2026/Soapbox Tracker.xlsx")
        self.assertEqual(len(self.transport.calls), 4)
        self.assertEqual(self.transport.calls[-1][0]["method"], "PUT")

        undo = self.adapter.undo_request(action, req)
        self.assertEqual(undo["method"], "DELETE")
        self.assertEqual(undo["url"], f"{wa.GRAPH}/drives/b!drive1/items/{{response:id}}")
        self.assertEqual(undo["headers"], {"If-Match": "{response:eTag}"})

        # put_file alias works identically.
        put_action = {**action, "operation": "put_file"}
        put_req = self.adapter.request(put_action)
        self.assertEqual(put_req["url"], req["url"])

    def test_upload_document_replace_requires_explicit_confirmation(self) -> None:
        b64 = base64.b64encode(b"hello studio").decode("ascii")
        base_action = {
            "system": "sharepoint",
            "operation": "upload_document",
            "target": {"drive_id": "b!drive1"},
            "payload": {
                "name": "Report.docx",
                "content_b64": b64,
                "folder_path": "Customers/Soapbox",
                "conflict_behavior": "replace",
            },
        }
        with self.assertRaises(wa.AdapterRefused):
            self.adapter.request(base_action)

        confirmed = {
            **base_action,
            "payload": {**base_action["payload"], "confirmed_replace": True},
        }
        req = self.adapter.request(confirmed)
        self.assertIn("@microsoft.graph.conflictBehavior=replace", req["url"])

    def test_unavailable_adapter_never_calls_transport_for_folders_or_upload(self) -> None:
        dry_adapter = wa.SharePointAdapter(None, environ={})
        req = dry_adapter.request({
            "system": "sharepoint",
            "operation": "create_folder",
            "target": {"drive_id": "b!drive1"},
            "payload": {"folder_path": "Customers/Soapbox"},
        })
        res = dry_adapter.perform(req, "k-dry")
        self.assertEqual(res["status"], "not_performed")
        self.assertTrue(res["missing"])


class NonDestructiveExcelStudioTests(unittest.TestCase):
    def _base_workbook_with_chart_and_formula(self) -> bytes:
        chart = {
            "kind": "bar",
            "title": "Monthly Volume",
            "x": "Month",
            "labels": ["Oct", "Nov"],
            "series": [{"name": "Units", "values": [1200, 1550]}],
            "rows": 2,
        }
        base = inf.make(
            "xlsx",
            "| Item | Qty | Unit Cost | Extended |\n"
            "| --- | --- | --- | --- |\n"
            "| 8oz Bottle | 1000 | $0.45 | $450.00 |\n"
            "| Pump Cap | 1000 | $0.22 | $220.00 |\n",
            charts=[chart],
        )
        # Add a definedName and a live formula on Sheet1 to verify non-destructive preservation.
        with zipfile.ZipFile(io.BytesIO(base), "r") as zf:
            book = zf.read("xl/workbook.xml").decode("utf-8")
            sheet1 = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        book = book.replace(
            "</workbook>",
            '<definedNames><definedName name="LeadVendor">Sheet1!$A$2</definedName></definedNames></workbook>',
        )
        sheet1 = sheet1.replace(
            "</sheetData>",
            '<row r="4"><c r="A4" t="inlineStr"><is><t>Subtotal</t></is></c>'
            '<c r="D4"><f>SUM(D2:D3)</f></c></row></sheetData>',
        )
        return deo._rezip_with_updates(
            base,
            {
                "xl/workbook.xml": book.encode("utf-8"),
                "xl/worksheets/sheet1.xml": sheet1.encode("utf-8"),
            },
        )

    def test_add_sheet_to_workbook_adds_styled_tab_with_formulas_and_preserves_charts_and_existing_tabs(self) -> None:
        original = self._base_workbook_with_chart_and_formula()
        orig_parts = _zip_parts(original)

        updated = deo.add_sheet_to_workbook(
            original,
            "Q4 Margin Summary",
            ["SKU", "Units", "Revenue", "Margin %", "Ship Date"],
            [
                ["00412", 2500, "$12,500.00", "34.5%", "2026-11-25"],
                ["00413", 1800, "$9,900.00", "38.0%", "2026-12-02"],
            ],
            title="Q4 Soapbox Margin Summary",
            formulas={"Units": "SUM", "Revenue": "SUM", "Margin %": "AVERAGE"},
            number_formats={"Units": "integer", "Revenue": "currency", "Margin %": "percent", "Ship Date": "date"},
        )

        new_parts = _zip_parts(updated)
        # Existing tabs, charts, and drawings survive byte-for-byte intact.
        self.assertEqual(new_parts["xl/worksheets/sheet1.xml"], orig_parts["xl/worksheets/sheet1.xml"])
        self.assertEqual(new_parts["xl/worksheets/sheet2.xml"], orig_parts["xl/worksheets/sheet2.xml"])
        self.assertEqual(new_parts["xl/charts/chart1.xml"], orig_parts["xl/charts/chart1.xml"])
        self.assertEqual(new_parts["xl/drawings/drawing2.xml"], orig_parts["xl/drawings/drawing2.xml"])
        self.assertIn("<f>SUM(D2:D3)</f>", new_parts["xl/worksheets/sheet1.xml"])

        info = deo.inspect_workbook(updated)
        self.assertEqual(info["sheet_names"], ["Sheet1", "Chart 1", "Q4 Margin Summary"])
        self.assertEqual(info["chart_count"], 1)

        new_sheet = new_parts["xl/worksheets/sheet3.xml"]
        self.assertIn("Q4 Soapbox Margin Summary", new_sheet)
        self.assertIn('state="frozen"', new_sheet)
        self.assertIn("<autoFilter", new_sheet)
        self.assertIn("<cols>", new_sheet)
        # Leading-zero SKU kept as text (inlineStr), not stripped to 412.
        self.assertIn('t="inlineStr"><is><t xml:space="preserve">00412</t></is>', new_sheet)
        # Live Excel SUM and AVERAGE formulas on totals row (rows 3..4 because title is row 1, header row 2).
        self.assertIn("<f>SUM(B3:B4)</f>", new_sheet)
        self.assertIn("<f>SUM(C3:C4)</f>", new_sheet)
        self.assertIn("<f>AVERAGE(D3:D4)</f>", new_sheet)
        self.assertIn('fullCalcOnLoad="1"', new_parts["xl/workbook.xml"])
        self.assertNotIn("\u2014", new_sheet)

    def test_append_rows_to_sheet_matches_headers_preserves_styles_and_creates_if_missing(self) -> None:
        base = inf.make(
            "xlsx",
            "| PO Number | Vendor | Amount | Due Date |\n"
            "| --- | --- | --- | --- |\n"
            "| PO-9001 | Berlin Packaging | $4,200.00 | 2026-12-01 |\n",
        )
        updated = deo.append_rows_to_sheet(
            base,
            "Sheet1",
            [
                {"vendor": "Croda", "po_number": "PO-9002", "due_date": "2026-12-10", "amount": "$6,150.50"},
                {"PO Number": "PO-9003", "Vendor": "TricorBraun", "Amount": "$1,800.00", "Due Date": "2026-12-15"},
            ],
            match_headers=True,
        )
        parts = _zip_parts(updated)
        sheet1 = parts["xl/worksheets/sheet1.xml"]
        self.assertIn("PO-9001", sheet1)
        self.assertIn("PO-9002", sheet1)
        self.assertIn("Croda", sheet1)
        self.assertIn("<v>6150.50</v>", sheet1)
        self.assertIn("PO-9003", sheet1)
        self.assertIn('ref="A1:D4"', sheet1)

        info = deo.inspect_workbook(updated)
        self.assertEqual(info["sheets"][0]["row_count"], 3)

        # Appending to a missing sheet creates it cleanly with formatted headers.
        with_new = deo.append_rows_to_sheet(
            updated,
            "QA Hold Log",
            [{"Lot": "LOT-22", "Status": "Hold", "Units": 450}],
            match_headers=True,
        )
        info2 = deo.inspect_workbook(with_new)
        self.assertEqual(info2["sheet_names"], ["Sheet1", "QA Hold Log"])
        self.assertEqual(info2["sheets"][1]["headers"], ["Lot", "Status", "Units"])
        self.assertEqual(info2["sheets"][1]["row_count"], 1)

    def test_update_named_cells_preserves_formatting_and_protects_formulas(self) -> None:
        original = self._base_workbook_with_chart_and_formula()
        updated = deo.update_named_cells(
            original,
            {"LeadVendor": "Berlin Packaging", "Sheet1!B2": 1250},
        )
        parts = _zip_parts(updated)
        sheet1 = parts["xl/worksheets/sheet1.xml"]
        self.assertIn("Berlin Packaging", sheet1)
        self.assertIn("<v>1250</v>", sheet1)
        self.assertIn("<f>SUM(D2:D3)</f>", sheet1)

        # Formula cells are protected unless allow_formula_overwrite=True.
        with self.assertRaises(deo.ExcelOpError):
            deo.update_named_cells(updated, {"Sheet1!D4": 999})

    def test_instruct_files_excel_helpers_round_trip(self) -> None:
        wb = inf.make("xlsx", "Item | Qty\nBottle | 100")
        wb2 = inf.add_excel_sheet(wb, "Summary", ["Metric", "Value"], [["Total", "=Sheet1!B2"]])
        wb3 = inf.append_excel_rows(wb2, "Sheet1", [{"Item": "Cap", "Qty": 200}])
        summary = inf.inspect_excel(wb3)
        self.assertEqual(summary["sheet_names"], ["Sheet1", "Summary"])
        self.assertEqual(summary["sheets"][0]["row_count"], 2)
        self.assertEqual(summary["sheets"][1]["formula_count"], 1)


class DocumentStudioVisualPolishTests(unittest.TestCase):
    def test_docx_xlsx_pptx_have_studio_executive_polish_and_zero_em_dashes(self) -> None:
        # DOCX polish
        docx_bytes = inf.make(
            "docx",
            "# Executive Brief\n\nSummary \u2014 zero em dashes allowed.\n\n"
            "## Open Orders\n"
            "| PO | Vendor | Total |\n| --- | --- | --- |\n"
            "| PO-1 | Berlin | 1250.00 |\n| PO-2 | Croda | 3400.50 |\n",
        )
        wparts = _zip_parts(docx_bytes)
        self.assertIn("F1F5F9", wparts["word/document.xml"])
        self.assertIn("F8FAFC", wparts["word/document.xml"])
        self.assertIn("0F172A", wparts["word/styles.xml"])
        self.assertNotIn("\u2014", "".join(wparts.values()))

        # XLSX polish
        xlsx_bytes = inf.make(
            "xlsx",
            "| Item | Amount | Margin |\n| --- | --- | --- |\n| SKU-1 | $1,250.00 | 32.5% |\n",
        )
        xparts = _zip_parts(xlsx_bytes)
        self.assertIn("F1F5F9", xparts["xl/styles.xml"])
        self.assertIn('fitToPage="1"', xparts["xl/worksheets/sheet1.xml"])
        self.assertIn('customHeight="1"', xparts["xl/worksheets/sheet1.xml"])
        self.assertNotIn("\u2014", "".join(xparts.values()))

        # PPTX polish + pagination for > 8 bullets
        many_bullets = "\n".join(f"- Point {i} for review" for i in range(1, 13))
        pptx_bytes = inf.make(
            "pptx",
            f"# Long Agenda\n{many_bullets}\n\n# Table Slide\n- Lead 1\n- Lead 2\n- Lead 3\n"
            "| SKU | Qty |\n| --- | --- |\n| A-1 | 500 |\n",
        )
        pparts = _zip_parts(pptx_bytes)
        slide_names = sorted(n for n in pparts if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        # 12 bullets split into 2 slides (8 + 4) + 1 table slide = 3 slides
        self.assertEqual(len(slide_names), 3)
        self.assertIn("Long Agenda (continued)", pparts["ppt/slides/slide2.xml"])
        self.assertIn("HeaderRule", pparts["ppt/slides/slide1.xml"])
        self.assertIn("SlideNumber", pparts["ppt/slides/slide1.xml"])
        self.assertIn("1 / 3", pparts["ppt/slides/slide1.xml"])
        self.assertIn("Lead 3", pparts["ppt/notesSlides/notesSlide3.xml"])
        self.assertNotIn("\u2014", "".join(pparts.values()))


class AssistSharePointAndExcelPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test-sp-excel-studio-"))

    def test_save_to_sharepoint_under_nested_folder_parses_into_previewable_plan(self) -> None:
        act = {
            "act": "prepare_document",
            "about": "Prepare Q4 Soapbox margin presentation and save to SharePoint under Customers/Soapbox/Q4",
        }
        art = doc_prep.prepare_document_artifact(
            act,
            matter={"title": "Q4 Soapbox margin presentation", "counterpart": "Soapbox"},
            output_dir=self.tmp,
        )
        self.assertTrue(art["download_ready"])
        self.assertEqual(art["kind"], "pptx")
        self.assertEqual(art["destination_folder"], "Customers/Soapbox/Q4")
        self.assertTrue(art["create_folder"])
        self.assertEqual(art["conflict_policy"], "rename")
        sp_plan = art["sharepoint_plan"]
        self.assertEqual(sp_plan["destination_folder"], "Customers/Soapbox/Q4")
        self.assertEqual(
            sp_plan["folders_to_ensure"],
            ["Customers", "Customers/Soapbox", "Customers/Soapbox/Q4"],
        )
        self.assertTrue(sp_plan["destination_path"].startswith("Customers/Soapbox/Q4/"))
        self.assertTrue(any("SharePoint destination: Customers/Soapbox/Q4/" in ln for ln in art["preview_lines"]))
        self.assertNotIn("\u2014", json.dumps(art, ensure_ascii=False))

    def test_add_tab_to_existing_workbook_prompt_edits_real_xlsx_non_destructively(self) -> None:
        existing_wb = inf.make("xlsx", "Item | Qty\nBottle | 500\nCap | 500")
        wb_path = self.tmp / "Weekly Supply Tracker.xlsx"
        wb_path.write_bytes(existing_wb)

        act = {
            "act": "prepare_document",
            "about": "Add a tab called 'Q4 Soapbox' to Weekly Supply Tracker.xlsx and save to SharePoint under Customers/Soapbox/Q4",
            "document": {
                "source_path": str(wb_path),
                "headers": ["SKU", "Units", "Revenue"],
                "rows": [["SB-100", 1200, "$6,000.00"], ["SB-200", 800, "$4,400.00"]],
                "formulas": {"Units": "SUM", "Revenue": "SUM"},
            },
        }
        art = doc_prep.prepare_document_artifact(act, output_dir=self.tmp)
        self.assertTrue(art["download_ready"])
        self.assertEqual(art["kind"], "xlsx")
        self.assertEqual(art["workbook_operation"], "add_sheet")
        self.assertEqual(art["target_workbook"], "Weekly Supply Tracker.xlsx")
        self.assertEqual(art["sheet_name"], "Q4 Soapbox")
        self.assertEqual(art["destination_folder"], "Customers/Soapbox/Q4")
        self.assertFalse(art.get("workbook_template_needed", False))
        self.assertEqual(art["workbook_sheets"], ["Sheet1", "Q4 Soapbox"])

        out_bytes = Path(art["path"]).read_bytes()
        info = deo.inspect_workbook(out_bytes)
        self.assertEqual(info["sheet_names"], ["Sheet1", "Q4 Soapbox"])
        self.assertEqual(info["sheets"][0]["row_count"], 2)
        self.assertEqual(info["sheets"][1]["formula_count"], 2)

    def test_append_rows_to_existing_workbook_prompt_preserves_prior_rows(self) -> None:
        existing_wb = inf.make(
            "xlsx",
            "| Item | Reference | Quantity | Status |\n"
            "| --- | --- | --- | --- |\n"
            "| 8oz Bottle | PO-1001 | 5000 | Open |\n",
        )
        wb_path = self.tmp / "Weekly Supply Tracker.xlsx"
        wb_path.write_bytes(existing_wb)

        act = {
            "act": "prepare_document",
            "about": "Append rows to Weekly Supply Tracker.xlsx on sheet Sheet1",
            "document": {
                "source_path": str(wb_path),
                "headers": ["Item", "Reference", "Quantity", "Status"],
                "rows": [["Pump Cap", "PO-1002", 5000, "Confirmed"]],
            },
        }
        art = doc_prep.prepare_document_artifact(act, output_dir=self.tmp)
        self.assertEqual(art["workbook_operation"], "append_rows")
        self.assertEqual(art["target_workbook"], "Weekly Supply Tracker.xlsx")
        info = deo.inspect_workbook(Path(art["path"]).read_bytes())
        self.assertEqual(info["sheets"][0]["row_count"], 2)
        xml = _zip_parts(Path(art["path"]).read_bytes())["xl/worksheets/sheet1.xml"]
        self.assertIn("PO-1001", xml)
        self.assertIn("PO-1002", xml)


class StudioHygieneTests(unittest.TestCase):
    def test_zero_em_dashes_in_all_studio_modules(self) -> None:
        for mod in (wa, inf, doc_prep, deo):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            self.assertNotIn("\u2014", src, mod.__name__)


if __name__ == "__main__":
    unittest.main()
