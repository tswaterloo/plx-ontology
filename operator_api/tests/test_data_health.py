"""Admin, Data health: every check the registry files under data health is listed (on ones
with their findings, off ones named), a check with findings on records the reader may not
open says how many are hidden rather than "none", and a read that failed is said, never a
list of zeros."""
from __future__ import annotations

import unittest

from operator_api import data_health as dh
from operator_api.tests.test_app_api import signed_in


class StubView:
    def __init__(self, found, counts=None):
        self.found, self.asked, self.counts, self.counted = found, None, counts, None

    def data_health_gaps(self, kinds, *, limit=0):
        self.asked = list(kinds)
        return self.found

    def data_health_counts(self, kinds):
        self.counted = list(kinds)
        return self.counts


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
        first = dh.window(got)
        po = next(c for c in first.checks if c.type == "order_without_customer_po")
        self.assertEqual((po.count, len(po.items), po.more, po.hidden), (30, dh.LISTED, 5, 0))
        self.assertEqual(po.items[0].open_url, "/object/Customer/Customer%3Ax%3A0")
        dup = next(c for c in got.checks if c.type == "duplicate_party_candidate")
        self.assertEqual((dup.count, dup.hidden, dup.items), (0, 1411, []))
        self.assertNotIn("finding 99", got.model_dump_json())

    def test_the_first_read_is_counts_only_and_reads_no_finding(self):
        view = StubView({"status": "error"}, {"status": "read", "counts": {
            "order_without_customer_po": 166, "duplicate_party_candidate": 1411}})
        got = dh.summary(view)
        self.assertIsNone(view.asked)   # no finding was read, no decision made
        on = [c for c in got.checks if c.on]
        self.assertTrue(on and all(not c.read and c.items == [] for c in on))
        self.assertEqual({c.type: c.open for c in on}["order_without_customer_po"], 166)
        self.assertEqual({c.type: c.open for c in on}["duplicate_party_candidate"], 1411)
        # an off check is named, never counted
        self.assertTrue(all(c.open is None for c in got.checks if not c.on))
        # counts that could not be read are unread, never zeros
        self.assertEqual(dh.summary(StubView({}, {"status": "unavailable", "counts": {}})).status, "unread")

    def test_one_check_is_read_alone_with_its_open_total(self):
        gaps = [gap("order_without_customer_po", i) for i in range(4)] + [gap("duplicate_party_candidate", 9)]
        view = StubView({"status": "read", "gaps": gaps, "hidden": {"order_without_customer_po": 3}})
        got = dh.page(view, "order_without_customer_po")
        self.assertEqual(view.asked, ["order_without_customer_po"])
        (c,) = got.checks
        self.assertEqual((c.count, c.hidden, c.open, c.read), (4, 3, 7, True))
        self.assertEqual(dh.page(StubView({"status": "read", "gaps": [], "hidden": {}}), "no_such").checks, [])

    def test_a_check_pages_without_loss_or_repeat_at_1_10_60_and_the_export_is_the_same_rows(self):
        for n in (1, 10, 60):
            gaps = [gap("order_without_customer_po", i) for i in range(n)]
            full = dh.page(StubView({"status": "read", "gaps": gaps, "hidden": {}}))
            seen, offset = [], 0
            while True:
                page = dh.window(full, "order_without_customer_po", offset, 7)
                (c,) = page.checks
                self.assertEqual(c.count, n)
                seen += [i.id for i in c.items]
                offset += len(c.items)
                if not c.more:
                    break
            self.assertEqual(seen, [f"g{i}" for i in range(n)], n)   # none lost, none twice
            rows = dh.export_rows(full, "order_without_customer_po")
            self.assertEqual(len(rows), n + 1)
        # a check the reader does not have is an empty page, never another's rows
        self.assertEqual(dh.window(full, "no_such_check").checks, [])
        # a page is at most PAGE_MAX rows however much is asked
        big = dh.page(StubView({"status": "read", "gaps": [
            gap("order_without_customer_po", i) for i in range(250)], "hidden": {}}))
        (c,) = dh.window(big, "order_without_customer_po", 0, 10_000).checks
        self.assertEqual(len(c.items), dh.PAGE_MAX)
        # an unread page stays unread
        self.assertEqual(dh.window(dh.page(StubView({"status": "error"})), "x").status, "unread")

    def test_the_export_defuses_formulas_and_lists_only_checks_that_are_on(self):
        g = gap("order_without_customer_po", 1)
        g["summary"] = '=HYPERLINK("http://x","y")'
        full = dh.page(StubView({"status": "read", "gaps": [g], "hidden": {}}))
        rows = dh.export_rows(full)
        self.assertEqual(rows[0], ["Check", "Finding", "Record type", "Record page"])
        self.assertEqual(rows[1][1][0], "'")
        self.assertEqual(len(rows), 2)

    def test_a_failed_read_is_unread_never_zeros(self):
        for status in ("unavailable", "error", None):
            got = dh.page(StubView({"status": status, "gaps": []}))
            self.assertEqual((got.status, got.checks), ("unread", []))
        self.assertEqual(dh.page(StubView({"status": "not_configured"})).status,
                         "not_configured")

    def test_the_route_lists_every_registered_check_to_admin_readers_only(self):
        """An Admin page (decided 11-21): the owner and the Team readers read it, like its
        menu entry; every other reader is refused (403), not served the checks."""
        from unittest import mock
        from fastapi.testclient import TestClient
        from operator_api.app import create_app
        from operator_api.auth import DevAuth, fixture_identity
        from operator_api.mock import MockBackend

        def client(owner: bool) -> TestClient:
            backend = MockBackend()
            backend._is_owner = lambda who: owner
            c = TestClient(create_app(backend, DevAuth(fixture_identity("plx", ["staff:demo"]),
                                                       mode="mock"), secure_cookie=False),
                           base_url="http://testserver")
            self.assertEqual(c.post("/api/auth/dev-login", json={}).status_code, 200)
            return c
        with mock.patch.dict("os.environ", {"PLX_TEAM_VIEW_READERS": ""}):
            body = client(owner=True).get("/api/data-health")
            self.assertEqual(body.status_code, 200, body.text)
            listed = [c["type"] for c in body.json()["checks"]]
            self.assertEqual(listed, [t["type"] for t in dh.registered()])
            exported = client(owner=True).get("/api/data-health/export")
            self.assertEqual(exported.status_code, 200, exported.text)
            self.assertTrue(exported.headers["content-type"].startswith("text/csv"))
            self.assertIn("attachment", exported.headers["content-disposition"])
            self.assertEqual(client(owner=False).get("/api/data-health/export").status_code, 403)
            self.assertEqual(client(owner=False).get("/api/data-health?check=x&offset=0").status_code, 403)
            refused = client(owner=False).get("/api/data-health")
            self.assertEqual(refused.status_code, 403, refused.text)
            self.assertNotIn("checks", refused.json())


class AssignFinding(unittest.TestCase):
    """A finding is assigned through the action desk by its own card (``dh:<check>:<id>``):
    only a finding this reader's read shows is a card, only assign is previewed from it, and
    nobody is messaged."""

    def client(self):
        from fastapi.testclient import TestClient
        from operator_api.app import create_app
        from operator_api.auth import DevAuth, fixture_identity
        from operator_api.mock import MockBackend
        backend = MockBackend()
        backend._is_owner = lambda who: True
        c = TestClient(create_app(backend, DevAuth(fixture_identity("plx", ["staff:demo"]),
                                                   mode="mock"), secure_cookie=False),
                       base_url="http://testserver")
        self.assertEqual(c.post("/api/auth/dev-login", json={}).status_code, 200)
        return c

    def test_a_finding_not_on_the_readers_list_is_not_found_never_assignable(self):
        c = self.client()
        for ref in ("dh:duplicate_party_candidate:someone-elses", "dh:no_such_check:dh-1", "dh:x"):
            r = c.post("/api/actions/preview", json={"card_id": ref, "kind": "assign",
                                                     "payload": {"assignee": "ana@plx.example"}})
            self.assertEqual(r.status_code, 404, (ref, r.text))

    def test_a_shown_finding_previews_an_assign_that_messages_nobody(self):
        c = self.client()
        ask = {"card_id": "dh:duplicate_party_candidate:dh-1", "kind": "assign",
               "payload": {"assignee": "ana@plx.example"}}
        r = c.post("/api/actions/preview", json=ask)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["outbound"])
        self.assertEqual(body["payload"]["assignee"], "ana@plx.example")
        # a sending kind is refused on a card that has no draft or thread
        send = c.post("/api/actions/preview", json={**ask, "kind": "send_email"})
        self.assertEqual(send.status_code, 422, send.text)
        self.assertEqual(send.json()["error"]["code"], "action_refused")
        # an assignee outside the company is a problem the preview states, never assigned
        out = c.post("/api/actions/preview", json={**ask, "payload": {"assignee": "x@elsewhere.example"}})
        self.assertTrue(out.json()["problems"], out.text)


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


from operating.tests import test_proactive_engine as _pe  # noqa: E402


class RealStore(unittest.TestCase):
    """The workspace read over a real projection store and gap inbox: the reader who may
    read the order sees the "no customer PO" finding; a reader without that audience sees
    no item, no id and no summary of it, only that one is hidden."""

    # the engine fixture's store, inbox and run, without collecting its own tests here
    setUp = _pe.Engine.setUp
    run_engine = _pe.Engine.run_engine

    def workspace(self, audiences):
        from operator_api.workspace import object_workspace as objects
        tools = {name: {"handler": lambda arguments, actor: {}}
                 for name in ("plx.find", "plx.object", "plx.around", "plx.timeline")}
        return objects.ObjectWorkspace(tools, _pe.OPS, access=_pe.FakeAccess(audiences,
                                                                              _pe.ALL_MARKINGS),
                                       store_status=_pe.FakeStatus(self.store))

    def test_a_restricted_reader_is_told_how_many_are_hidden_never_which(self):
        from unittest import mock
        world, ids = _pe.seeded()
        self.run_engine(world, ids["coverage"])
        order = ids["no_customer_po"]
        kind = "order_without_customer_po"
        with mock.patch.object(dh, "registered", return_value=[
                {"type": kind, "label": "Order without a customer PO", "default": "on"}]):
            full = dh.page(self.workspace({"api:a", "api:b"}))
            cut = dh.page(self.workspace({"api:b"}))
        [seen] = full.checks
        self.assertEqual((full.status, seen.count, seen.hidden), ("read", 1, 0))
        self.assertEqual(seen.items[0].object_id, order)
        [hidden] = cut.checks
        self.assertEqual((cut.status, hidden.count, hidden.items, hidden.hidden),
                         ("read", 0, [], 1))
        text = cut.model_dump_json()
        self.assertNotIn(order, text)
        self.assertNotIn(seen.items[0].summary, text)
        self.assertNotIn(seen.items[0].id, text)


if __name__ == "__main__":
    unittest.main()
