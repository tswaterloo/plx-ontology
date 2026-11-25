"""Excel (.xlsx) export of any chart, table or answer (Genie item 15)."""
from __future__ import annotations

import io
import re
import unittest
import zipfile

from fastapi.testclient import TestClient

from operating import excel_export as ex
from operating import instruct_files as files
from operator_api import charts as ch
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend


def _parts(data: bytes) -> dict[str, str]:
    z = zipfile.ZipFile(io.BytesIO(data))
    return {n: z.read(n).decode("utf-8", "replace") for n in z.namelist()}


def _sample_chart():
    return ch.build(
        {"type": "bar", "x": "supplier", "y": ["spend"]},
        ["supplier", "spend"],
        [["Acme", "12500"], ["Beta", "8400"]],
        title="Spend by supplier",
        as_of="2026-11-24T12:00:00Z",
        sql="SELECT supplier, sum(total) AS spend FROM purchase_orders GROUP BY supplier",
    )


class ExcelExportUnit(unittest.TestCase):
    def test_table_export_creates_typed_sheet_and_sources_sheet(self):
        sql = "SELECT po_number, supplier, total FROM purchase_orders WHERE status = 'open'"
        data = ex.workbook_xlsx(
            title="Open POs",
            tables=[{"title": "Open POs", "columns": ["PO", "Supplier", "Total"],
                     "rows": [["PO1001", "Acme", "12500.50"], ["PO1002", "Beta", "8400.00"]],
                     "sql": sql, "as_of": "2026-11-24T12:00:00Z"}],
        )
        p = _parts(data)
        self.assertEqual(re.findall(r'name="([^"]+)"', p["xl/workbook.xml"]), ["Open POs", "Sources"])
        self.assertIn("<v>12500.50</v>", p["xl/worksheets/sheet1.xml"])
        self.assertIn(sql, p["xl/worksheets/sheet2.xml"])

    def test_answer_export_combines_summary_tables_charts_and_sources(self):
        chart = _sample_chart()
        data = ex.workbook_xlsx(
            title="Who are our top suppliers?",
            summary="Acme leads open PO spend at 12,500 CAD (unverified).",
            tables=[{"title": "Top suppliers", "columns": ["Supplier", "Spend"],
                     "rows": [["Acme", "12500.00"]], "sql": "SELECT supplier, sum(total) FROM purchase_orders GROUP BY 1"}],
            charts=[chart],
        )
        p = _parts(data)
        names = re.findall(r'name="([^"]+)"', p["xl/workbook.xml"])
        self.assertEqual(names, ["Answer", "Top suppliers", "Spend by supplier", "Sources"])
        self.assertNotIn("unverified", p["xl/worksheets/sheet1.xml"])
        self.assertIn("xl/charts/chart1.xml", p)
        self.assertIn("SELECT supplier, sum(total) FROM purchase_orders GROUP BY 1",
                      p["xl/worksheets/sheet4.xml"])

    def test_single_chart_export_produces_live_native_chart(self):
        data = ex.workbook_xlsx(title="Spend", charts=[_sample_chart()])
        p = _parts(data)
        self.assertIn("xl/charts/chart1.xml", p)
        self.assertIn("Source: 2 records, as of 2026-11-24.", p["xl/worksheets/sheet1.xml"])

    def test_empty_export_refused(self):
        with self.assertRaises(ValueError):
            ex.workbook_xlsx(title="Empty")

    def test_over_forty_columns_exported_uncut(self):
        cols = [f"col_{i}" for i in range(1, 46)]
        row = [f"v{i}" for i in range(1, 46)]
        data = ex.workbook_xlsx(
            title="Wide table",
            tables=[{"title": "Wide table", "columns": cols, "rows": [row]}],
        )
        p = _parts(data)
        sheet = p["xl/worksheets/sheet1.xml"]
        self.assertIn("col_41", sheet)
        self.assertIn("col_45", sheet)
        self.assertIn("v45", sheet)
        self.assertIn('r="AS2"', sheet)

    def test_truncated_preview_noted_honestly_on_sources_sheet(self):
        data = ex.workbook_xlsx(
            title="Orders",
            tables=[
                {
                    "title": "Open POs",
                    "columns": ["PO", "Total"],
                    "rows": [["PO1001", "100.00"], ["PO1002", "200.00"]],
                    "total_rows": 500,
                    "truncated": True,
                    "sql": "SELECT po_number, total FROM purchase_orders",
                },
                {
                    "title": "Mail hits",
                    "columns": ["Item", "Snippet"],
                    "rows": [["m1", "Hello"]],
                    "records": 42,
                },
                {
                    "title": "Unknown total",
                    "columns": ["A", "B"],
                    "rows": [["x", "1"]],
                    "truncated": True,
                },
            ],
        )
        p = _parts(data)
        self.assertIn("Note: 2 of 500 rows shown (preview).", p["xl/worksheets/sheet1.xml"])
        sources = p["xl/worksheets/sheet4.xml"]
        self.assertIn("2 of 500 rows shown (preview)", sources)
        self.assertIn("1 of 42 rows shown (preview)", sources)
        self.assertIn("1 rows shown (preview; more not shown)", sources)

    def test_literal_sql_preserved_on_sources_sheet(self):
        sql = "SELECT count(*) AS n, sum(qty * unit_price) AS gross, ~~a~~, {--keep--} FROM lines"
        data = ex.workbook_xlsx(
            title="SQL check",
            tables=[{
                "title": "Lines",
                "columns": ["n", "gross"],
                "rows": [["3", "45.00"]],
                "sql": sql,
            }],
        )
        p = _parts(data)
        sources = p["xl/worksheets/sheet2.xml"]
        self.assertIn("SELECT count(*) AS n, sum(qty * unit_price) AS gross, ~~a~~, {--keep--} FROM lines", sources)

    def test_chart_panels_series_and_measure_fields_are_masked(self):
        card = "4111 1111 1111 1111"
        chart = {
            "kind": "bar",
            "title": f"Spend for {card}",
            "x": "month",
            "labels": [],
            "series": [],
            "panels": [
                {
                    "title": f"Panel {card}",
                    "labels": ["Jan", "Feb"],
                    "series": [{"name": f"Series {card} (unverified)", "values": [10.0, 20.0]}],
                }
            ],
            "measure": {
                "id": "m1",
                "name": f"Measure {card}",
                "definition": f"Sum of {card} (unverified)",
                "owner": f"Owner {card}",
                "period": f"Period {card}",
            },
        }
        masked = files._masked(chart)
        joined_masked = str(masked)
        self.assertNotIn(card, joined_masked)
        self.assertNotIn("unverified", joined_masked)
        self.assertIn("1111", masked["panels"][0]["series"][0]["name"])
        self.assertIn("1111", masked["measure"]["definition"])
        self.assertIn("1111", masked["measure"]["owner"])
        self.assertIn("1111", masked["measure"]["period"])

        data = ex.workbook_xlsx(title="Masked panels", summary="Summary.", charts=[chart])
        joined_parts = "\n".join(_parts(data).values())
        self.assertNotIn(card, joined_parts)
        self.assertNotIn("unverified", joined_parts)

    def test_malformed_chart_raises_value_error_not_key_or_index_error(self):
        with self.assertRaises(ValueError):
            ex.workbook_xlsx(title="Bad", charts=[{
                "kind": "waterfall", "title": "WF", "x": "step",
                "labels": ["Start"], "series": [],
            }])
        with self.assertRaises(ValueError):
            ex.workbook_xlsx(title="Bad", charts=[{
                "kind": "bar", "title": "Mismatch", "x": "x",
                "labels": ["A", "B"], "series": [{"name": "v", "values": [1.0]}],
            }])


class ExcelExportRoute(unittest.TestCase):
    def test_route_requires_sign_in_and_returns_xlsx_attachment(self):
        app = create_app(MockBackend(), DevAuth(fixture_identity("plx", ["staff:ana"]), mode="mock"),
                         secure_cookie=False)
        client = TestClient(app, base_url="http://testserver")
        self.assertEqual(client.post("/api/export/xlsx", json={"title": "X", "summary": "Hi"}).status_code, 401)
        assert client.post("/api/auth/dev-login", json={}).status_code == 200
        res = client.post("/api/export/xlsx", json={
            "title": "Revenue breakdown",
            "summary": "Total revenue is 42,000 CAD.",
            "tables": [{"title": "By customer", "columns": ["Customer", "Revenue"],
                        "rows": [["Acme", "42000.00"]], "sql": "SELECT customer, sum(revenue) FROM sales_invoice_lines GROUP BY 1"}],
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn("spreadsheetml.sheet", res.headers["content-type"])
        self.assertIn('filename="Revenue breakdown.xlsx"', res.headers["content-disposition"])
        p = _parts(res.content)
        self.assertIn("xl/workbook.xml", p)
        self.assertIn("By customer", p["xl/workbook.xml"])

    def test_malformed_chart_returns_422_instead_of_500(self):
        app = create_app(MockBackend(), DevAuth(fixture_identity("plx", ["staff:ana"]), mode="mock"),
                         secure_cookie=False)
        client = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
        assert client.post("/api/auth/dev-login", json={}).status_code == 200
        # Missing required AskChart fields
        r1 = client.post("/api/export/xlsx", json={
            "title": "Bad chart 1",
            "charts": [{"kind": "bar"}],
        })
        self.assertEqual(r1.status_code, 422)
        # Series/labels length mismatch
        r2 = client.post("/api/export/xlsx", json={
            "title": "Bad chart 2",
            "charts": [{
                "kind": "bar", "title": "Mismatch", "x": "supplier",
                "labels": ["Acme", "Beta"],
                "series": [{"name": "spend", "values": [100.0]}],
            }],
        })
        self.assertEqual(r2.status_code, 422)
        # Waterfall with labels and empty series (previously raised IndexError 500)
        r3 = client.post("/api/export/xlsx", json={
            "title": "Bad chart 3",
            "charts": [{
                "kind": "waterfall", "title": "WF", "x": "step",
                "labels": ["Start"], "series": [],
            }],
        })
        self.assertEqual(r3.status_code, 422)


if __name__ == "__main__":
    unittest.main()
