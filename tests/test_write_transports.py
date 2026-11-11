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


if __name__ == "__main__":
    unittest.main()
