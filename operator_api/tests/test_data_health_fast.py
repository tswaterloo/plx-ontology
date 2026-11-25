"""Fast Data Health tests (PLAN-P2-C2(C)): pagination beyond 25 rows, server-side
filtering (q + object_type), single-pass read+hidden, O(1) single-gap lookup for fast
assign, batched data_health_all export, and fail-closed role gating (including measure
service session on /api/data-health/export)."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from operating import proactive_engine as pe
from operating.tests import test_proactive_engine as _pe
from operator_api import data_health as dh
from operator_api.app import MEASURE_ROUTES, create_app, service_route
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend


def _gap(kind: str, n: int, *, object_type: str = "Customer", summary: str | None = None) -> dict:
    return {
        "id": f"g-{n}",
        "kind": kind,
        "object_id": f"{object_type}:x:{n}",
        "object_type": object_type,
        "summary": summary if summary is not None else f"Finding {n} for {object_type} {n}",
    }


class _StubView:
    def __init__(self, gaps: list[dict], hidden: dict[str, int] | None = None):
        self.gaps = gaps
        self.hidden = hidden or {}
        self.calls: list[list[str]] = []

    def data_health_gaps(self, kinds, *, limit=0):
        self.calls.append(list(kinds))
        want = set(kinds)
        return {
            "status": "read",
            "gaps": [g for g in self.gaps if g["kind"] in want],
            "hidden": {k: v for k, v in self.hidden.items() if k in want},
        }


class FastPaginationAndFiltering(unittest.TestCase):
    def test_pages_beyond_25_rows_with_exact_counts_and_no_loss(self):
        kind = "order_without_customer_po"
        gaps = [_gap(kind, i, object_type="SalesOrder") for i in range(120)]
        full = dh.page(_StubView(gaps), kind)
        seen: list[str] = []
        offset = 0
        pages = 0
        while True:
            win = dh.window(full, kind, offset=offset, limit=50)
            (c,) = win.checks
            self.assertEqual(c.count, 120)
            seen.extend(it.id for it in c.items)
            offset += len(c.items)
            pages += 1
            if c.more == 0:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(len(seen), 120)
        self.assertEqual(seen, [f"g-{i}" for i in range(120)])

    def test_filters_by_query_and_object_type_across_all_rows(self):
        kind = "duplicate_party_candidate"
        gaps = (
            [_gap(kind, i, object_type="Customer", summary=f"Customer pair {i}") for i in range(40)]
            + [_gap(kind, 100 + i, object_type="Vendor", summary=f"Acme Supplier {i}") for i in range(15)]
        )
        full = dh.page(_StubView(gaps), kind)

        # Match rows past index 25 via q
        q_win = dh.window(full, kind, offset=0, limit=10, q="acme")
        (c_q,) = q_win.checks
        self.assertEqual(c_q.count, 15)
        self.assertEqual(len(c_q.items), 10)
        self.assertEqual(c_q.more, 5)

        # Page 2 of filtered results
        q_win2 = dh.window(full, kind, offset=10, limit=10, q="acme")
        (c_q2,) = q_win2.checks
        self.assertEqual(c_q2.count, 15)
        self.assertEqual(len(c_q2.items), 5)
        self.assertEqual(c_q2.more, 0)

        # Filter by object_type
        t_win = dh.window(full, kind, offset=0, limit=50, object_type="vendor")
        (c_t,) = t_win.checks
        self.assertEqual(c_t.count, 15)
        self.assertTrue(all(it.object_type == "Vendor" for it in c_t.items))

        # Filtered export matches exact filtered rows
        t0 = time.perf_counter()
        rows = dh.export_rows(full, kind, q="acme", object_type="Vendor")
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(rows), 16)  # header + 15 matching rows
        self.assertLess(elapsed, 0.5)

    def test_route_accepts_filters_and_enforces_role_gating_for_export(self):
        class _ManyBackend(MockBackend):
            def data_health(self, reader, check=None):
                if check is None:
                    return super().data_health(reader, None)
                gaps = [
                    _gap(check, i, object_type="Customer" if i < 35 else "Vendor",
                         summary=f"Row {i} special" if i >= 30 else f"Row {i}")
                    for i in range(45)
                ]
                return dh.page(_StubView(gaps), check)

        def make_client(owner: bool) -> TestClient:
            backend = _ManyBackend()
            backend._is_owner = lambda who: owner
            c = TestClient(
                create_app(backend, DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
                           secure_cookie=False),
                base_url="http://testserver",
            )
            self.assertEqual(c.post("/api/auth/dev-login", json={}).status_code, 200)
            return c

        with mock.patch.dict("os.environ", {"PLX_TEAM_VIEW_READERS": ""}):
            owner_c = make_client(owner=True)
            r = owner_c.get("/api/data-health?check=duplicate_party_candidate&offset=0&limit=50&q=special")
            self.assertEqual(r.status_code, 200, r.text)
            (chk,) = r.json()["checks"]
            self.assertEqual(chk["count"], 15)
            self.assertEqual(len(chk["items"]), 15)

            exp = owner_c.get("/api/data-health/export?check=duplicate_party_candidate&object_type=Vendor")
            self.assertEqual(exp.status_code, 200, exp.text)
            lines = [ln for ln in exp.text.strip().splitlines() if ln]
            self.assertEqual(len(lines), 11)  # header + 10 Vendor rows (35..44)

            # Fail-closed: non-owner gets 403 on both routes
            other_c = make_client(owner=False)
            self.assertEqual(other_c.get("/api/data-health/export").status_code, 403)
            self.assertEqual(other_c.get("/api/data-health?check=duplicate_party_candidate&q=special").status_code, 403)

    def test_measure_service_session_is_allowed_on_data_health_export_get(self):
        self.assertIn("/api/data-health/export", MEASURE_ROUTES)
        measure_session = SimpleNamespace(measure=True)
        self.assertTrue(service_route(measure_session, "GET", "/api/data-health/export"))
        self.assertFalse(service_route(measure_session, "POST", "/api/data-health/export"))


class SinglePassAndFastAssignRealStore(unittest.TestCase):
    """Over a real projection store + gap inbox:
    1) data_health_gaps computes visible + hidden in one single pass (never calling hidden_counts twice).
    2) data_health_gap_one performs an O(1) permission-checked lookup for assign, fail-closed for restricted readers."""

    setUp = _pe.Engine.setUp
    run_engine = _pe.Engine.run_engine

    def _workspace(self, audiences):
        from operator_api.workspace import object_workspace as objects
        tools = {name: {"handler": lambda arguments, actor: {}}
                 for name in ("plx.find", "plx.object", "plx.around", "plx.timeline")}
        return objects.ObjectWorkspace(
            tools, _pe.OPS,
            access=_pe.FakeAccess(audiences, _pe.ALL_MARKINGS),
            store_status=_pe.FakeStatus(self.store),
        )

    def test_single_pass_hidden_and_fast_single_gap_lookup_fail_closed(self):
        world, ids = _pe.seeded()
        self.run_engine(world, ids["coverage"])
        kind = "order_without_customer_po"

        ws_full = self._workspace({"api:a", "api:b"})
        ws_cut = self._workspace({"api:b"})

        # Single-pass data_health_gaps must NOT fall back to a second pe.hidden_counts call
        with mock.patch("operating.proactive_engine.hidden_counts",
                        side_effect=AssertionError("should not run second hidden_counts pass")):
            res_full = ws_full.data_health_gaps([kind])
            res_cut = ws_cut.data_health_gaps([kind])

        self.assertEqual(res_full["status"], "read")
        self.assertEqual(len(res_full["gaps"]), 1)
        self.assertEqual(res_full["hidden"], {})

        self.assertEqual(res_cut["status"], "read")
        self.assertEqual(res_cut["gaps"], [])
        self.assertEqual(res_cut["hidden"], {kind: 1})

        # O(1) single-gap lookup: visible to authorized reader, None for restricted reader
        gap_id = res_full["gaps"][0]["id"]
        t0 = time.perf_counter()
        one_ok = ws_full.data_health_gap_one(kind, gap_id)
        one_denied = ws_cut.data_health_gap_one(kind, gap_id)
        one_missing = ws_full.data_health_gap_one(kind, "does-not-exist")
        elapsed = time.perf_counter() - t0

        self.assertIsNotNone(one_ok)
        self.assertEqual(one_ok["id"], gap_id)
        self.assertIsNone(one_denied)
        self.assertIsNone(one_missing)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
