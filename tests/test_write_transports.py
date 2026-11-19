"""Person-approved write transports (``integrations.write_transports``) through the one
guarded client (``live_source_guard.write_action``): off unless the owner opened the
system; only the exact shapes the adapters build (and their undo requests) pass, inside the
sender too; a token without the system's permission sends nothing.

SYNTHETIC ids, addresses and tokens; injected senders only: nothing here reaches a network.
Evidence label: injected-transport.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from integrations import live_source_guard as guard
from integrations import write_transports as wt
from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating.tests.test_autopilot_write import BC_COMPANY, NOW, OWNER, Base
from operating.tests.test_autopilot_write_systems import CASES, UPLOAD, item

TENANT = "77777777-7777-4777-8777-777777777777"
DOC = "55555555-5555-4555-8555-555555555555"


def token(*roles: str) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"roles": list(roles)}).encode()).decode()
    return "h." + claims.rstrip("=") + ".s"


class Sender:
    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers), body))
        status, answer = self.answers.pop(0) if self.answers else (201, {"id": "x1"})
        return status, {}, json.dumps(answer).encode()


OPEN = {guard.WRITES_FLAG: "planner,calendar,sharepoint,bc", "BC_TENANT_ID": TENANT,
        "PLX_WRITES_LIVE": "1", "PLX_WRITES_SCOPES_GRANTED": "bc",
        "PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY}


def full(system: str, url: str) -> str:
    return f"https://{guard.BC_HOST}" + url.replace("{tenant}", TENANT) if system == "bc" \
        else url


class Shapes(unittest.TestCase):
    def test_every_adapter_request_and_undo_is_an_approved_shape(self):
        adapters = {cls.kind: cls(environ=OPEN) for cls in (
            wa.PlannerAdapter, wa.CalendarAdapter, wa.SharePointAdapter, wa.BCAdapter)}
        cases = [(n, p) for n, p, _w, _u in CASES] + [("upload", UPLOAD)]
        seen = set()
        for name, prepared in cases:
            actions = [a for a in aw.actions_of(item(name, **prepared)) if a["kind"] in adapters]
            for action in actions:
                with self.subTest(name):
                    adapter = adapters[action["kind"]]
                    request = adapter.request(action)
                    system = adapter.system
                    self.assertEqual(guard.write_shape(request["method"],
                                                       full(system, request["url"])), system)
                    answer = {"id": DOC if system == "bc" else "17", "@odata.etag": 'W/"2"',
                              "eTag": '"{A},2"', "response": {"recordId": "1"}}
                    undo = wa.fill(adapter.undo_request(action, request), answer)
                    if undo is not None:
                        self.assertEqual(guard.write_shape(undo["method"],
                                                           full(system, undo["url"])), system)
                    seen.add(system)
        self.assertEqual(seen, {"planner", "calendar", "sharepoint", "bc"})

    def test_anything_else_is_refused_before_and_inside_the_sender(self):
        g = "https://graph.microsoft.com/v1.0"
        bad = [("POST", f"{g}/users/{OWNER}/sendMail"),           # mail is its own path
               ("PATCH", f"{g}/planner/tasks/t1?x=1"),             # a query
               ("PATCH", f"{g}/planner/tasks/a%2Fb"),              # encoded slash
               ("DELETE", f"{g}/planner/tasks/../plans/p1"),
               ("PATCH", f"{g}/planner/plans/p1"),                 # another entity
               ("PUT", f"{g}/drives/d1/items/p1:/a.pdf:/content"),  # may replace a file
               ("PUT", f"{g}/drives/d1/items/p1:/a%2E.pdf:/content?" + guard.UPLOAD_QUERY),
               ("DELETE", f"{g}/users/{OWNER}/messages/m1"),
               ("POST", "https://evil.example/v1.0/planner/tasks"),
               ("POST", f"http://graph.microsoft.com/v1.0/planner/tasks"),
               ("POST", f"https://{guard.BC_HOST}/v2.0/{TENANT}/Production/api/v2.0/"
                        f"companies({BC_COMPANY})/vendors"),              # a new party
               ("GET", f"{g}/planner/tasks")]
        for method, url in bad:
            with self.subTest(url):
                self.assertIsNone(guard.write_shape(method, url))
                with self.assertRaises(guard.ReadOnlyGuardError):
                    guard.check_write_request(method, url, {}, b"{}", environ=OPEN)
        ok = ("PATCH", f"{g}/planner/tasks/t1")
        for headers, body in [({"Authorization": "Bearer x"}, b"{}"),
                              ({"If-Match": 'W/"1"\r\nX: y'}, b"{}"),
                              ({"X-Other": "1"}, b"{}"), ({}, None), ({}, b"")]:
            with self.assertRaises(guard.ReadOnlyGuardError):
                guard.check_write_request(*ok, headers, body, environ=OPEN)
        with self.assertRaises(guard.ReadOnlyGuardError):        # DELETE carries no body
            guard.check_write_request("DELETE", ok[1], {}, b"{}", environ=OPEN)
        # off unless the owner opened that system; an unknown name opens nothing
        for env in ({}, {guard.WRITES_FLAG: "calendar"}, {guard.WRITES_FLAG: "1,all,*"}):
            with self.assertRaises(guard.ReadOnlyGuardError):
                guard.check_write_request(*ok, {}, b"{}", environ=env)
        # inside the real sender: the flag and shape are re-checked from the environment
        with mock.patch.dict(os.environ, {guard.WRITES_FLAG: ""}):
            with self.assertRaises(guard.ReadOnlyGuardError):
                guard._refuse_non_read_send(*ok, b"{}")
        with mock.patch.dict(os.environ, {guard.WRITES_FLAG: "planner"}):
            guard._refuse_non_read_send(*ok, b"{}")
            with self.assertRaises(guard.ReadOnlyGuardError):
                guard._refuse_non_read_send("DELETE", ok[1], b"{}")
            with self.assertRaises(guard.ReadOnlyGuardError):
                guard._refuse_non_read_send("PATCH", f"{g}/planner/plans/p1", b"{}")


class Transports(Base):
    def test_off_by_default_nothing_is_constructed(self):
        self.assertIsNone(wt.for_system("planner", {}))
        found = wa.default_adapters({})
        self.assertIsNone(found["planner_write"].transport)
        self.assertTrue(found["sharepoint_write"].unavailable())
        opened = wa.default_adapters({guard.WRITES_FLAG: "planner"})
        self.assertIsInstance(opened["planner_write"].transport, wt.GraphWriteTransport)
        self.assertIsNone(opened["calendar_write"].transport)

    def test_graph_token_without_the_permission_sends_nothing(self):
        send = Sender()
        t = wt.GraphWriteTransport("planner", OPEN, bearer=lambda: token("Mail.Send"),
                                   send=send)
        request = {"method": "POST", "url": "https://graph.microsoft.com/v1.0/planner/tasks",
                   "headers": {"Content-Type": "application/json"},
                   "body": {"planId": "p1", "title": "Book the carrier"}}
        self.assertEqual(t(request, "k")[0], 403)
        self.assertEqual(send.calls, [])
        t = wt.GraphWriteTransport("planner", OPEN, bearer=lambda: token("Tasks.ReadWrite.All"),
                                   send=send)
        status, answer = t(request, "k")
        self.assertEqual((status, answer), (201, {"id": "x1"}))
        method, url, headers, body = send.calls[0]
        self.assertEqual((method, url, json.loads(body)["title"]),
                         ("POST", request["url"], "Book the carrier"))
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        # closed off again: refused before a token is asked for
        asked = []
        t = wt.GraphWriteTransport("planner", {}, bearer=lambda: asked.append(1) or "",
                                   send=send)
        self.assertEqual((t(request, "k")[0], asked, len(send.calls)), (403, [], 1))

    def test_bc_needs_every_flag_and_sends_exact_decimals_to_the_tenant(self):
        bc = wa.BCAdapter(environ=OPEN)
        change = {"operation": "purchase_order_line_update", "target": {"record_id": DOC},
                  "payload": {"quantity": "5.25"},
                  "precondition": {"etag": 'W/"1"', "prior": {"quantity": "4"}}}
        request = bc.request(change)
        for drop in ("PLX_WRITES_LIVE", "PLX_WRITES_SCOPES_GRANTED", "BC_TENANT_ID",
                     guard.WRITES_FLAG):
            env = {k: v for k, v in OPEN.items() if k != drop}
            send = Sender()
            t = wt.BCWriteTransport(env, bearer=lambda: "t", send=send)
            self.assertTrue(t.missing(), drop)
            self.assertEqual(t(request, "k")[0], 403, drop)
            self.assertEqual(send.calls, [], drop)
        send = Sender((200, {"@odata.etag": 'W/"2"'}))
        t = wt.BCWriteTransport(OPEN, bearer=lambda: "t", send=send)
        self.assertEqual(t.send(request), {"status": "written", "http_status": 200})
        method, url, headers, body = send.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertTrue(url.startswith(f"https://{guard.BC_HOST}/v2.0/{TENANT}/Production/"))
        self.assertEqual((headers["If-Match"], body), ('W/"1"', b'{"quantity":5.25}'))
        t = wt.BCWriteTransport(OPEN, bearer=lambda: "t", send=Sender((503, {})))
        self.assertEqual(t.send(request)["status"], "unknown")
        # INT-001: the sandbox switch alone opens the configured sandbox, never Production
        from operating import write_intents as wi
        sandbox_env = {**{k: v for k, v in OPEN.items() if k != "PLX_WRITES_LIVE"},
                       wi.SANDBOX_FLAG: "1", wi.SANDBOX_ENV: "Sandbox"}
        in_sandbox = {**request, "url": request["url"].replace("/Production/", "/Sandbox/", 1)}
        self.assertNotEqual(in_sandbox["url"], request["url"])
        send = Sender((200, {"@odata.etag": 'W/"2"'}))
        t = wt.BCWriteTransport(sandbox_env, bearer=lambda: "t", send=send)
        self.assertEqual(t(request, "k")[0], 403)
        self.assertEqual(t.send(in_sandbox), {"status": "written", "http_status": 200})
        self.assertEqual(len(send.calls), 1)
        self.assertIn(f"/{TENANT}/Sandbox/", send.calls[0][1])

    def test_approved_item_reaches_planner_once_through_the_guard(self):
        send = Sender((201, {"id": "pt_9", "@odata.etag": 'W/"e"'}))
        env = {**OPEN, aw.APPROVED_ENV: "planner_write"}
        transport = wt.GraphWriteTransport("planner", env,
                                           bearer=lambda: token("Tasks.ReadWrite.All"), send=send)
        ex = self.executor(env, planner_write=wa.PlannerAdapter(transport, environ=env))
        planner = {n: p for n, p, _w, _u in CASES}["planner_create"]
        approval = aw.Approval(by=(OWNER,), confirmed_live=True)
        row = ex.run(item("live1", **planner), NOW, approval)[0]
        self.assertEqual(row["status"], "done", row)
        self.assertEqual(row["inverse"]["url"],
                         "https://graph.microsoft.com/v1.0/planner/tasks/pt_9")
        self.assertEqual(ex.run(item("live1", **planner), NOW, approval)[0]["status"],
                         "duplicate")
        self.assertEqual(len(send.calls), 1)
        # the undo goes through the same guard
        self.assertEqual(ex.undo(row["key"], NOW)["status"], "done")
        self.assertEqual(send.calls[-1][0], "DELETE")
        # the system closed: the adapter is unavailable, nothing is attempted
        closed = {**env, guard.WRITES_FLAG: "calendar"}
        transport.environ = closed
        row = ex.run(item("live2", **planner), NOW, approval)[0]
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(len(send.calls), 2)


class FakePortal:
    """The Portal's staff routes that take a workbench_write key (TASK-5101), in memory as
    the handlers at plx-customer-portal staging a8b4b92 behave: a ``Bearer plx_`` key, the
    route's own body, no version check and no Idempotency-Key replay (both asked of the
    Portal team). Answers like ``urllib_sender``."""

    KEY = "plx_synthetic_workbench_key"

    def __init__(self):
        self.orders = {"so_9": {"poReference": None, "notes": None, "status": "DRAFT"}}
        self.notes, self.releases, self.calls = [], [], []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers), body))
        if headers.get("Authorization") != f"Bearer {self.KEY}":
            return 401, {}, b'{"error":"Unauthorized"}'
        path = url.split("https://portal-write.test", 1)[1]
        data = json.loads(body) if body else {}
        parts = path.strip("/").split("/")
        if method == "PATCH" and parts[:3] == ["api", "mrp", "sales-orders"]:
            order = self.orders.get(parts[3])
            if order is None:
                return 404, {}, b'{"error":"Not found"}'
            # the handler reads its own header fields; anything else is not applied
            order.update({k: v for k, v in data.items()
                          if k in ("poReference", "notes", "requiredDate", "status")})
            return 200, {}, json.dumps({"id": parts[3], "status": order["status"]}).encode()
        if method == "POST" and parts[:2] == ["api", "activity"] and parts[-1] == "note":
            self.notes.append((parts[2], parts[3], data["content"]))
            return 201, {}, json.dumps({"id": f"n{len(self.notes)}"}).encode()
        if method == "POST" and parts[-1] == "ship-releases":
            self.releases.append((parts[3], data))
            return 201, {}, json.dumps({"id": f"r{len(self.releases)}"}).encode()
        return 404, {}, b'{"error":"Not found"}'


PORTAL = {guard.WRITES_FLAG: "portal", guard.PORTAL_URL_ENV: "https://portal-write.test",
          "PORTAL_WRITE_KEY": FakePortal.KEY, aw.APPROVED_ENV: "portal_write"}
SEEN = "2026-11-18T14:00:00+00:00"


def portal_item(n, operation, target, payload, precondition=None):
    write = {"system": "portal", "operation": operation, "target": target,
             "payload": payload}
    if precondition:
        write["precondition"] = precondition
    return item(n, write=write)


class Portal(Base):
    """The Portal adapter on the Portal's real staff routes against a fake of them, end to
    end through the executor, the transport and the guard (injected-transport)."""

    def setUp(self) -> None:
        super().setUp()
        self.portal = FakePortal()
        self.env = dict(PORTAL)
        self.transport = wt.PortalWriteTransport(self.env, send=self.portal)
        self.ex = self.executor(self.env, portal_write=wa.PortalAdapter(
            self.transport, environ=self.env))
        self.approval = aw.Approval(by=(OWNER,), confirmed_live=True)

    def test_an_order_edit_a_note_and_a_release_go_to_the_routes_and_the_edit_undoes(self):
        edit = portal_item("p1", "sales-order.update", {"id": "so_9"},
                           {"poReference": "PO-1"},
                           {"updated_at": SEEN, "prior": {"poReference": None}})
        row = self.ex.run(edit, NOW, self.approval)[0]
        self.assertEqual((row["mode"], row["status"]), ("live", "done"), row)
        self.assertEqual(self.ex.run(edit, NOW, self.approval)[0]["status"], "duplicate")
        method, url, headers, body = self.portal.calls[0]
        self.assertEqual((method, url), ("PATCH",
                                         "https://portal-write.test/api/mrp/sales-orders/so_9"))
        self.assertEqual(headers["Idempotency-Key"], row["key"])
        self.assertNotIn("If-Match", headers)
        # the version as read goes along (the Portal is asked to check it)
        self.assertEqual(json.loads(body), {"poReference": "PO-1", "expectedUpdatedAt": SEEN})
        self.assertEqual(self.portal.orders["so_9"]["poReference"], "PO-1")
        self.assertEqual(self.ex.undo(row["key"], NOW)["status"], "done")
        self.assertIsNone(self.portal.orders["so_9"]["poReference"])
        note = portal_item("p2", "activity.note", {"record_type": "sales_order",
                                                   "record_id": "so_9"},
                           {"content": "Customer confirmed the dock time."})
        self.assertEqual(self.ex.run(note, NOW, self.approval)[0]["status"], "done")
        self.assertEqual(self.portal.notes, [("sales_order", "so_9",
                                              "Customer confirmed the dock time.")])
        # a quantity travels as an exact JSON number, never a float or a string
        release = portal_item("p3", "ship-release.create", {"order_id": "fm_1"},
                              {"customerLocationId": "loc_1", "lines": [
                                  {"orderLineRef": "ol_1", "productId": "p_1",
                                   "qtyAuthorized": "12.5", "uom": "CS"}]})
        shown = self.ex.run(release, NOW, self.approval)[0]
        if shown["status"] == "not_routine":
            shown = self.ex.run(release, NOW, aw.Approval(
                by=(OWNER,), confirmed_live=True, acknowledged=tuple(shown["reasons"])))[0]
        self.assertEqual(shown["status"], "done", shown)
        self.assertIn(b'"qtyAuthorized":12.5', self.portal.calls[-1][3])
        self.assertIsNone(wa.PortalAdapter(environ=self.env).undo_request(
            release["body"]["prepared"]["write"], {"method": "POST", "body": {}}))

    def test_closed_unconfigured_off_route_or_asked_route_sends_nothing(self):
        route = "https://portal-write.test/api/mrp/sales-orders/so_9"
        for env, why in (({**self.env, guard.WRITES_FLAG: "bc"}, "not opened"),
                         ({**self.env, guard.PORTAL_URL_ENV: ""}, "address"),
                         ({**self.env, guard.PORTAL_URL_ENV: "https://h.test:8443"}, "address"),
                         ({**self.env, guard.PORTAL_URL_ENV: f"https://{guard.BC_HOST}"},
                          "address"),
                         ({**self.env, "PORTAL_WRITE_KEY": ""}, "key"),
                         ({**self.env, "PORTAL_WRITE_KEY": "pk-reporting"}, "workbench_write")):
            with self.subTest(why):
                t = wt.PortalWriteTransport(env, send=self.portal)
                self.assertTrue(any(why in m for m in t.missing()), t.missing())
                self.assertEqual(t({"method": "PATCH", "url": route[25:],
                                    "body": {"notes": "x"}}, "k" * 64)[0], 403)
        self.assertEqual(self.portal.calls, [])
        key = {"Idempotency-Key": "k" * 64}
        base = "https://portal-write.test"
        bad = [(("PATCH", route), {}),                                # no idempotency key
               (("PATCH", route), {**key, "If-Match": '"1"'}),
               (("DELETE", route), key),                              # never a delete
               (("GET", route), key),
               (("PUT", route), key),
               (("POST", route), key),
               (("PATCH", base + "/api/mrp/purchase-orders/line-items"), key),
               (("PATCH", "https://evil.test/api/mrp/sales-orders/so_9"), key),
               (("PATCH", route + "?x=1"), key),
               (("PATCH", route.replace("so_9", "so%2F9")), key),
               (("PATCH", base + "/api/mrp/sales-orders/so_9/lines"), key),
               (("POST", base + "/api/mrp/quality/records/q1/sign"), key),
               (("POST", base + "/workbench/v1/records/app_customer"), key),
               # an asked route is closed until the owner names it
               (("POST", base + "/api/operations/customers/c1/contacts"), key)]
        for (method, url), headers in bad:
            with self.subTest(method=method, url=url, headers=headers):
                with self.assertRaises(guard.ReadOnlyGuardError):
                    guard.check_write_request(method, url, headers,
                                              None if method in ("DELETE", "GET") else b"{}",
                                              environ=self.env)
        opened = {**self.env, guard.PORTAL_ROUTES_ENV: "customer-contact.create"}
        self.assertEqual(guard.check_write_request(
            "POST", base + "/api/operations/customers/c1/contacts", key, b"{}",
            environ=opened), "portal")
        # the Idempotency-Key header is the Portal's only: Graph and BC keep their set
        with self.assertRaises(guard.ReadOnlyGuardError):
            guard.check_write_request("PATCH", "https://graph.microsoft.com/v1.0/planner/"
                                      "tasks/t1", {**key}, b"{}",
                                      environ={guard.WRITES_FLAG: "planner"})
        # every adapter route is a guard shape, and the other way round
        adapter = wa.PortalAdapter(environ=opened)
        shapes = {r for r, _m, _p in guard.PORTAL_WRITE_SHAPES}
        self.assertEqual(shapes, set(wa.PORTAL_ROUTES))
        self.assertEqual(guard.PORTAL_LISTED,
                         {k for k, v in wa.PORTAL_ROUTES.items() if not v.get("asked")})
        for op, spec in wa.PORTAL_ROUTES.items():
            ids = {k: "x1" for k in spec["target"]}
            self.assertEqual(guard.write_shape(spec["method"], base + spec["path"].format(
                **ids), {**opened, guard.PORTAL_ROUTES_ENV: ",".join(wa.PORTAL_ROUTES)}),
                "portal", op)
        self.assertTrue(adapter.operations)

    def test_cardinality_and_a_contacts_address_is_cleared(self):
        # every note written once or held by the approver's rate limit: none lost or doubled
        for n in (1, 10, 60):
            before = len(self.portal.notes)
            got = [self.ex.run(portal_item(f"c{n}-{i}", "activity.note",
                                           {"record_type": "sales_order",
                                            "record_id": f"so_{n}_{i}"},
                                           {"content": f"Note {n}-{i}"}), NOW,
                               self.approval)[0] for i in range(n)]
            self.assertLessEqual({r["status"] for r in got}, {"done", "rate_limited"})
            done = [r for r in got if r["status"] == "done"]
            self.assertEqual(len(self.portal.notes) - before, len(done))
            self.assertEqual(len({r["key"] for r in done}), len(done))
            self.assertTrue(done)
        texts = [n[2] for n in self.portal.notes]
        self.assertEqual(len(texts), len(set(texts)))
        adapter = wa.PortalAdapter(environ=self.env)
        self.assertEqual(adapter.people({"payload": {"email": "dana@example.com",
                                                     "name": "Dana"}}),
                         ["dana@example.com"])


if __name__ == "__main__":
    unittest.main()
