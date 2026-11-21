"""Admin, Data health: every check the registry files under data health is listed (on ones
with their findings, off ones named), a check with findings on records the reader may not
open says how many are hidden rather than "none", and a read that failed is said, never a
list of zeros."""
from __future__ import annotations

import unittest

from operator_api import data_health as dh
from operator_api.tests.test_app_api import signed_in


class StubView:
    def __init__(self, found):
        self.found, self.asked = found, None

    def data_health_gaps(self, kinds, *, limit=0):
        self.asked = list(kinds)
        return self.found


def gap(kind: str, n: int) -> dict:
    return {"id": f"g{n}", "kind": kind, "object_id": f"Customer:x:{n}",
            "object_type": "Customer", "summary": f"finding {n}"}


class DataHealthPage(unittest.TestCase):
    def test_every_registered_check_is_listed_and_only_the_on_ones_are_read(self):
        types = dh.registered()
        self.assertIn("order_without_customer_po", [t["type"] for t in types])
        view = StubView({"status": "read", "gaps": [], "hidden": {}})
        got = dh.page(view)
        self.assertEqual([c.type for c in got.checks], [t["type"] for t in types])
        self.assertEqual(sorted(view.asked),
                         sorted(t["type"] for t in types if t.get("default") == "on"))
        for c in got.checks:
            self.assertEqual(c.on, next(t for t in types if t["type"] == c.type)
                             .get("default") == "on")

    def test_findings_counted_listed_and_hidden_ones_counted_never_named(self):
        gaps = [gap("order_without_customer_po", i) for i in range(30)]
        gaps.append(gap("off_type_never_asked", 99))
        got = dh.page(StubView({"status": "read", "gaps": gaps,
                                "hidden": {"duplicate_party_candidate": 1411}}))
        po = next(c for c in got.checks if c.type == "order_without_customer_po")
        self.assertEqual((po.count, len(po.items), po.more, po.hidden), (30, dh.LISTED, 5, 0))
        self.assertEqual(po.items[0].open_url, "/object/Customer/Customer%3Ax%3A0")
        dup = next(c for c in got.checks if c.type == "duplicate_party_candidate")
        self.assertEqual((dup.count, dup.hidden, dup.items), (0, 1411, []))
        self.assertNotIn("finding 99", got.model_dump_json())

    def test_a_failed_read_is_unread_never_zeros(self):
        for status in ("unavailable", "error", None):
            got = dh.page(StubView({"status": status, "gaps": []}))
            self.assertEqual((got.status, got.checks), ("unread", []))
        self.assertEqual(dh.page(StubView({"status": "not_configured"})).status,
                         "not_configured")

    def test_the_route_lists_every_registered_check(self):
        body = signed_in().get("/api/data-health")
        self.assertEqual(body.status_code, 200, body.text)
        listed = [c["type"] for c in body.json()["checks"]]
        self.assertEqual(listed, [t["type"] for t in dh.registered()])


class WorkspaceRead(unittest.TestCase):
    """The workspace read counts hidden findings with this reader's own grant and item
    decision, and asks for nothing when the visible read failed."""

    def view(self, status="read"):
        import contextlib
        from types import SimpleNamespace
        decide = object()
        v = SimpleNamespace(
            gap_inbox=lambda **kw: {"status": status, "gaps": [], "kw": kw},
            _request=contextlib.nullcontext,
            access=SimpleNamespace(grant=lambda: ("MAY_READ", ["held"]), decide=decide),
            store_status=SimpleNamespace(store_path="/x/store.sqlite"))
        return v, decide

    def test_hidden_counts_use_the_readers_grant(self):
        from unittest import mock
        from operator_api.workspace.object_workspace import ObjectWorkspace
        v, decide = self.view()
        with mock.patch("operating.proactive_engine.hidden_counts",
                        return_value={"duplicate_party_candidate": 3}) as hc:
            got = ObjectWorkspace.data_health_gaps(v, ["duplicate_party_candidate"])
        self.assertEqual(got["hidden"], {"duplicate_party_candidate": 3})
        kw = hc.call_args.kwargs
        self.assertEqual((kw["may_read"], kw["held"], kw["decide"], kw["kinds"]),
                         ("MAY_READ", ["held"], decide, ["duplicate_party_candidate"]))
        v2, _ = self.view(status="not_configured")
        with mock.patch("operating.proactive_engine.hidden_counts") as hc2:
            self.assertEqual(ObjectWorkspace.data_health_gaps(v2, ["x"])["hidden"], {})
        hc2.assert_not_called()


if __name__ == "__main__":
    unittest.main()
