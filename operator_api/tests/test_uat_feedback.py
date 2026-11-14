"""GET /api/uat-feedback: the mock backend through the app, and the real backend's reading
of ``plx.aggregate`` mode ``business_documents`` view ``uat_feedback`` through the reader's own
view (a stub that answers as the aggregate does: its gate's withheld items are only counted).
SYNTHETIC fixtures only; evidence label: isolated-fixture (live-unverified)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operator_api import backend as be
from operator_api import briefs
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend

TODAY = date(2026, 11, 14)


def row(n, status="open", priority="High", modified="2026-11-01", **kw):
    return {"record": f"SharePointListItem:{n}", "list": "MRP UAT", "title": f"Item {n}",
            "status": status, "status_word": status.title(), "resolution_word": None,
            "priority": priority, "modified": modified, "cites": [], **kw}


class FakeView:
    """Answers ``plx.aggregate`` like the view: ``withheld`` items never appear and are
    counted; rows cut at ``cap``."""

    def __init__(self, rows, withheld=0, cap=200, error=False):
        self.rows, self.withheld, self.cap, self.error = rows, withheld, cap, error
        self.calls = []

    def _request(self):
        return mock.MagicMock()

    def _call(self, name, args):
        self.calls.append((name, dict(args)))
        if self.error:
            return {"error": {"code": "invalid_input"}}
        totals = {}
        for r in self.rows:
            totals[r["status"]] = totals.get(r["status"], 0) + 1
        off = args.get("offset", 0)
        want = self.rows[off:]
        return {"rows": want[:self.cap], "totals": totals, "complete": len(want) <= self.cap,
                "withheld_counts": {"withheld_not_readable": self.withheld} if self.withheld
                else {}}


class Reading(unittest.TestCase):
    def read(self, view, status=None):
        return briefs.uat_feedback_of(view, status, TODAY)

    def test_open_first_by_priority_then_newest_with_age_and_priority_counts(self):
        v = FakeView([row(1, "fixed", "Critical"), row(2, priority="Low"),
                      row(3, priority="Critical", modified="2026-10-01"),
                      row(4, priority="Critical", modified="2026-11-05"),
                      row(5, priority=None, modified="0001-01-01"),
                      row(6, "weird", "High"),
                      row(7, priority="Critical \u2014 Blocks work, no workaround", modified="2026-11-12")])
        got = self.read(v)
        self.assertEqual([i["record"].split(":")[1] for i in got["items"]],
                         ["7", "4", "3", "2", "5", "1", "6"])
        self.assertEqual([got["items"][0]["age_days"], got["items"][1]["age_days"]], [2, 9])
        self.assertIsNone(got["items"][4]["modified"])      # the minimum date is "not dated"
        self.assertIsNone(got["items"][4]["age_days"])
        self.assertIsNone(got["items"][5]["age_days"])      # only open items age
        self.assertEqual([(p["word"], p["count"]) for p in got["open_priorities"]],
                         [("Critical", 3), ("Low", 1), ("No priority", 1)])
        self.assertEqual(got["totals"], {"fixed": 1, "open": 5, "weird": 1})
        self.assertEqual(got["matched"], sum(got["totals"].values()))

    def test_status_filter_and_unknown_status_calls_the_view_as_the_reader(self):
        v = FakeView([row(1), row(2, "fixed")])
        got = self.read(v, "fixed")
        self.assertEqual([i["record"] for i in got["items"]], ["SharePointListItem:2"])
        self.assertTrue(all(c[0] == "plx.aggregate" and c[1]["mode"] == "business_documents"
                            and c[1]["view"] == "uat_feedback" for c in v.calls))
        self.assertEqual(got["open_priorities"], [{"word": "High", "count": 1}])
        self.assertTrue(all("status" not in c[1] for c in v.calls))   # not an input of the tool

    def test_withheld_items_are_counted_not_shown(self):
        got = self.read(FakeView([row(1)], withheld=7))
        self.assertEqual(got["withheld"], 7)
        self.assertEqual(len(got["items"]), 1)

    def test_more_items_than_the_cap_are_all_reached_by_paging(self):
        rows = [row(i, "fixed" if i % 3 else "open") for i in range(1, 460)]
        v = FakeView(rows, cap=200)
        got = self.read(v)
        self.assertEqual((len(got["items"]), got["matched"], got["cut"]), (459, 459, False))
        self.assertEqual(len({i["record"] for i in got["items"]}), 459)
        self.assertEqual([c[1]["offset"] for c in v.calls], [0, 200, 400])
        fixed = self.read(FakeView(rows, cap=200), "fixed")
        self.assertEqual((len(fixed["items"]), fixed["matched"], fixed["cut"]), (306, 306, False))

    def test_a_real_cut_past_the_page_limit_is_said(self):
        with mock.patch.object(briefs, "UAT_MAX_PAGES", 2):
            got = self.read(FakeView([row(i) for i in range(1, 460)], cap=200))
        self.assertTrue(got["cut"])
        self.assertEqual((len(got["items"]), got["matched"]), (400, 459))

    def test_cardinality_one_ten_sixty(self):
        for n in (1, 10, 60):
            got = self.read(FakeView([row(i) for i in range(n)]))
            ids = [i["record"] for i in got["items"]]
            self.assertEqual((len(ids), len(set(ids)), got["cut"]), (n, n, False))

    def test_an_unavailable_people_exclusion_is_not_served_never_an_empty_list(self):
        class Unavailable(FakeView):
            def _call(self, name, args):
                return {"rows": [], "counts": {}, "withheld_counts": {}, "complete": False,
                        "withheld": "people_exclusion_unavailable"}
        self.assertEqual(self.read(Unavailable([])), {"served": False})

    def test_an_unreadable_view_is_not_served_never_an_empty_list(self):
        self.assertEqual(self.read(FakeView([], error=True)), {"served": False})


class Routes(unittest.TestCase):
    def client(self, backend):
        session = fixture_identity("plx", ["staff:demo"])()
        client = TestClient(create_app(backend, DevAuth(lambda: session), secure_cookie=False),
                            base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def backend(self, view):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)

        class Backend(be.WorkspaceBackend):
            def _brief_run(self, reader, job):
                return job(view)
        return Backend(lambda s: None, data_label="t", store_path=d / "store.sqlite",
                       data_dir=d)

    def test_mock_backend_route_filters_and_refuses_an_unknown_status(self):
        c = self.client(MockBackend())
        all_ = c.get("/api/uat-feedback").json()
        self.assertEqual((all_["total"], all_["writes"], all_["sent"]), (5, 0, 0))
        self.assertEqual([i["status"] for i in c.get("/api/uat-feedback?status=fixed").json()["items"]],
                         ["fixed"])
        self.assertEqual(c.get("/api/uat-feedback?status=bogus").status_code, 422)

    def test_real_backend_route_reads_through_the_readers_view_and_withholds(self):
        view = FakeView([row(1), row(2, "fixed")], withheld=4)
        backend = self.backend(view)
        body = self.client(backend).get("/api/uat-feedback").json()
        self.assertEqual((body["total"], body["withheld"], len(body["items"])), (2, 4, 2))
        self.assertTrue(body["served"])
        self.assertEqual(body["items"][0]["age_days"], (date.today() - date(2026, 11, 1)).days)

    def test_real_backend_route_says_not_served(self):
        backend = self.backend(FakeView([], error=True))
        body = self.client(backend).get("/api/uat-feedback").json()
        self.assertFalse(body["served"])
        self.assertEqual(body["items"], [])


if __name__ == "__main__":
    unittest.main()
