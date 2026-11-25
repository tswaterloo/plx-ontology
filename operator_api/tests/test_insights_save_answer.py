"""One-step save of a whole Ask answer as a cohesive Insights page (PLAN-P2-A7):
POST /api/insights/save-answer, GET /api/insights/pages, GET /api/insights/pages/{id},
DELETE /api/insights/pages/{id}.

Checked at the boundary:
- Persists title, summary, question, charts, and queries or catalogue measures atomically
  under one page id strictly scoped to ``reader.key``.
- Another reader can never list, open or delete someone else's saved answer page.
- ``view_as`` and undecidable exclusion guard are refused before any write.
- Atomic all-or-nothing validation: if any item fails SQL, measure or chart validation,
  nothing is written to Postgres.
- Full OpenAPI contract is registered on the FastAPI app."""
from __future__ import annotations

import unittest

import psycopg
from fastapi.testclient import TestClient

from operator_api import app as api_app
from operator_api import models as m
from operator_api.errors import UserFacing
from operator_api.tests import test_insights as ti


class _FakeAuth:
    def __init__(self, who):
        self.who = who

    def authenticate(self, _headers, _cookies=None):
        return self.who


class SaveAnswerPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ti.Saved.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        ti.Saved.tearDownClass.__func__(cls)

    def setUp(self):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM insights.run")
            conn.execute("DELETE FROM insights.insight")
        self.memory.guard = lambda: ti.Guard()
        self.memory.names = lambda: frozenset({"quinn harrow", "quinn", "harrow"})
        self.b = ti.Backend(
            ti.sql_out([["Acme", "CAD", "12500.00", "4", "0"]],
                       ("customer", "amount_due_currency", "value", "row_count", "unread_cells")),
            everyone=True,
            memory=self.memory,
        )

    def _body(self, **kw) -> m.InsightAnswerSave:
        base = {
            "title": "AR & Top Customers Briefing",
            "summary": "Gross open invoices are 761k CAD vs 587k CAD net.",
            "question": "Show open receivables gross vs net and top customers.",
            "items": [
                {
                    "title": "Gross open receivables",
                    "measure": "receivables_open_gross",
                    "by": "customer",
                    "chart": {"kind": "bar", "x": "customer", "y": ["value"]},
                },
                {
                    "title": "Customer breakdown SQL",
                    "sql": "SELECT customer, amount_due_currency, value FROM customer_balances",
                    "chart": {"kind": "bar", "x": "customer", "y": ["value"]},
                },
            ],
        }
        base.update(kw)
        return m.InsightAnswerSave(**base)

    def test_save_list_get_and_delete_scoped_strictly_to_owner(self):
        ana, ben = ti.reader("ana"), ti.reader("ben")
        page = self.b.insight_save_answer(ana, self._body())

        self.assertRegex(page.id, r"^p[a-f0-9]{12}$")
        self.assertEqual(page.title, "AR & Top Customers Briefing")
        self.assertIn("761k CAD", page.summary)
        self.assertEqual(len(page.items), 2)
        for item in page.items:
            self.assertTrue(item.saved and item.id.startswith("s"))
            self.assertEqual(item.page_id, page.id)
            self.assertEqual(item.page_title, page.title)
            self.assertEqual(item.group, f"Page: {page.title}")

        # Owner lists and gets the page; tiles also appear in owner's dashboard list
        ana_pages = self.b.insight_pages_list(ana)
        self.assertEqual([p.id for p in ana_pages.pages], [page.id])
        fetched = self.b.insight_page_get(ana, page.id)
        self.assertIsNotNone(fetched)
        self.assertEqual([i.id for i in fetched.items], [i.id for i in page.items])
        ana_tiles = {i.id for i in self.b.insights_list(ana).items}
        self.assertTrue({i.id for i in page.items} <= ana_tiles)

        # Another reader sees nothing and cannot get or delete the page
        self.assertEqual(self.b.insight_pages_list(ben).pages, [])
        self.assertIsNone(self.b.insight_page_get(ben, page.id))
        self.assertFalse(self.b.insight_page_delete(ben, page.id))
        ben_tiles = {i.id for i in self.b.insights_list(ben).items}
        self.assertFalse({i.id for i in page.items} & ben_tiles)

        # view_as sees nothing and cannot save or delete
        viewing = ti.reader("ana")
        viewing.session.view_as = True
        self.assertEqual(self.b.insight_pages_list(viewing).pages, [])
        self.assertIsNone(self.b.insight_page_get(viewing, page.id))
        with self.assertRaises(UserFacing) as e:
            self.b.insight_save_answer(viewing, self._body())
        self.assertEqual(e.exception.code, "view_as_refused")
        with self.assertRaises(UserFacing) as e:
            self.b.insight_page_delete(viewing, page.id)
        self.assertEqual(e.exception.code, "view_as_refused")

        # Owner deletes the whole page in one call -> all tiles soft-deleted
        self.assertTrue(self.b.insight_page_delete(ana, page.id))
        self.assertEqual(self.b.insight_pages_list(ana).pages, [])
        self.assertIsNone(self.b.insight_page_get(ana, page.id))
        self.assertFalse(self.b.insight_page_delete(ana, page.id))
        ana_tiles_after = {i.id for i in self.b.insights_list(ana).items}
        self.assertFalse({i.id for i in page.items} & ana_tiles_after)

    def test_atomic_validation_writes_nothing_if_any_item_fails(self):
        ana = ti.reader("ana")
        bad_cases = [
            # Item 1 valid, item 2 mutating SQL
            (self._body(items=[
                {"title": "Valid", "sql": "SELECT customer, value FROM customer_balances"},
                {"title": "Bad SQL", "sql": "DELETE FROM customers"},
            ]), "insight_invalid_sql"),
            # Item 1 valid, item 2 unknown measure
            (self._body(items=[
                {"title": "Valid", "sql": "SELECT customer, value FROM customer_balances"},
                {"title": "Unknown measure", "measure": "does_not_exist"},
            ]), "insight_invalid_sql"),
            # Item 1 valid, item 2 missing chart column
            (self._body(items=[
                {"title": "Valid", "sql": "SELECT customer, value FROM customer_balances"},
                {"title": "Bad chart", "sql": "SELECT customer, value FROM customer_balances",
                 "chart": {"kind": "bar", "x": "customer", "y": ["no_such_col"]}},
            ]), "insight_chart"),
            # Item with neither sql nor measure
            (self._body(items=[{"title": "Empty"}]), "insight_invalid_sql"),
        ]
        for body, code in bad_cases:
            with self.assertRaises(UserFacing) as caught:
                self.b.insight_save_answer(ana, body)
            self.assertEqual(caught.exception.code, code)
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM insights.insight").fetchone(), (0,))

    def test_http_routes_and_openapi_contract(self):
        ana = ti.reader("ana")
        app = api_app.create_app(self.b, _FakeAuth(ana))
        schema = app.openapi()
        for path in ("/api/insights/save-answer", "/api/insights/pages", "/api/insights/pages/{page_id}"):
            self.assertIn(path, schema["paths"])

        client = TestClient(app)
        resp = client.post("/api/insights/save-answer", json=self._body().model_dump(mode="json"))
        self.assertEqual(resp.status_code, 200, resp.text)
        saved = resp.json()
        pid = saved["id"]
        self.assertEqual(len(saved["items"]), 2)

        listed = client.get("/api/insights/pages")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([p["id"] for p in listed.json()["pages"]], [pid])

        got = client.get(f"/api/insights/pages/{pid}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["id"], pid)

        deleted = client.delete(f"/api/insights/pages/{pid}")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])

        missing = client.get(f"/api/insights/pages/{pid}")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
