"""The whole autopilot write path, end to end, against faithful fakes of each system
(``operating.tests.write_fakes``): readiness probe -> owner's go-live file -> gate and the
person's own opt-in -> routine rules -> adapter -> the real guarded write transport and
guard -> fake system -> audit row -> undo; kill switch, missing flag, unverified or
PHSI Group company, stale probe and missing scopes refuse.

Evidence label: isolated-fixture (no network; every token is a fixture string)."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest

from integrations import live_source_guard as guard
from integrations import write_transports as wt
from integrations.plx_hub_guard import HubTransport
from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating import autopilot_write_readiness as rd
from operating.tests.test_autopilot_write import (BC_COMPANY, NOW, OWNER, Base, guard as people,
                                                  po_item)
from operating.tests.test_autopilot_write_systems import CASES, item
from operating.tests.write_fakes import FakeMC, FakeSystems

TENANT = "44444444-4444-4444-8444-444444444444"
PHSI = "55555555-5555-4555-8555-555555555555"
SANDBOX = "Sandbox2"
OPERATOR = "workbench.operator@isolated-fixture.example"
CASE = {name: prepared for name, prepared, _w, _u in CASES}


def token(*roles: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"roles": list(roles)}).encode()).decode()
    return "fixture." + body.rstrip("=") + ".fixture"


GRAPH_TOKEN = token("Calendars.ReadWrite", "Tasks.ReadWrite.All", "Sites.Selected")
BC_TOKEN = token("API.ReadWrite.All")


class Wired(Base):
    """The real guarded transports over the fakes (the tests are in the subclasses)."""

    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeSystems({BC_COMPANY: "PLX", PHSI: "PHSI Group"}, SANDBOX)
        self.mc_fake = FakeMC({"TASK-7"})
        self.graph_token, self.bc_token = GRAPH_TOKEN, BC_TOKEN
        self.env = {aw.MASTER_ENV: "1", "PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY,
                    "BC_TENANT_ID": TENANT, "PLX_BC_SANDBOX_ENVIRONMENT": SANDBOX,
                    "MC_MCP_API_KEY": "isolated-fixture-no-credential",
                    "MC_OPERATOR_EMAIL": OPERATOR,
                    "MC_WRITE_LEDGER": str(Path(self.tmp.name) / "mc-ledger.jsonl")}

    # ------------------------------------------------------------ wiring
    def companies(self, url: str, bearer: str):
        self.assertIn(f"/{self.fake.environment}/api/v2.0/companies", url)
        return {"value": [{"id": BC_COMPANY, "name": "PLX"}, {"id": PHSI, "name": "PHSI Group"}]}

    def probe(self, env=None, **kw):
        # recorded at the real time: the executor trusts only a probe of the last 45 min
        found = rd.probe(self.state, env or self.env,
                         now=kw.pop("now", datetime.now(timezone.utc)),
                         graph_token=kw.pop("graph_token", lambda: self.graph_token),
                         bc_token=kw.pop("bc_token", lambda: self.bc_token),
                         bc_get=kw.pop("bc_get", self.companies))
        rd.record(self.state, found, env or self.env)
        return found

    def executor_e2e(self, env=None) -> aw.Executor:
        """The executor with the real guarded transports over the fakes, built from the
        environment the executor itself opened (go-live files, the recorded probe)."""
        env = env or self.env
        opened = aw.Executor(self.state, environ=env, adapters={}).environ
        on = guard.writes_enabled(opened)

        def graph(system):
            return wt.GraphWriteTransport(system, opened, bearer=lambda: self.graph_token,
                                          send=self.fake.send) if system in on else None
        bc = wt.BCWriteTransport(opened, bearer=lambda: self.bc_token, send=self.fake.send) \
            if "bc" in on else None
        hub = HubTransport(self.mc_fake.send, environ={}, clock=lambda: NOW,
                           sleep=lambda _s: None, jitter=lambda: 1.0)
        mc = wt.MCTaskTransport(opened, hub=hub, clock=lambda: NOW) \
            if opened.get(aw.MC_LIVE_ENV) == "1" else None
        adapters = {"bc_write": wa.BCAdapter(bc, environ=opened),
                    "planner_write": wa.PlannerAdapter(graph("planner"), environ=opened),
                    "calendar_write": wa.CalendarAdapter(graph("calendar"), environ=opened),
                    "sharepoint_write": wa.SharePointAdapter(graph("sharepoint"), environ=opened),
                    "mc_task": wa.MCTaskAdapter(mc, environ=opened)}
        return aw.Executor(self.state, environ=env, adapters=adapters, guard=people())

    def all_live(self) -> None:
        self.probe()
        self.go_live(("customer_order_intake",))


class EndToEnd(Wired):
    # ------------------------------------------------------------ the path
    def test_every_system_writes_audits_and_undoes_against_its_fake(self):
        self.all_live()
        when = iter(NOW + timedelta(hours=h) for h in range(40))   # under the hourly limit
        ex = self.executor_e2e()

        # BC (Sandbox2, company PLX): a new sales order, then its header edited, both undone
        created = ex.run(po_item(draft=False), next(when))[0]
        self.assertEqual((created["status"], created["mode"]), ("done", "live"), created)
        doc_id = next(iter(self.fake.docs))
        self.assertEqual(self.fake.docs[doc_id]["customerNumber"], "C-1")
        self.assertIn(f"/{SANDBOX}/api/v2.0/companies({BC_COMPANY})/salesOrders",
                      self.fake.requests[0]["url"])
        etag = self.fake.docs[doc_id]["@odata.etag"]
        edit = item("bc-edit", write={
            "system": "bc", "operation": "sales_order_update",
            "target": {"document_id": doc_id},
            "payload": {"requestedDeliveryDate": "2026-11-20"},
            "precondition": {"etag": etag, "status": "Draft",
                             "prior": {"requestedDeliveryDate": "2026-11-18"}}})
        edited = ex.run(edit, next(when))[0]
        self.assertEqual(edited["status"], "done", edited)
        self.assertEqual(self.fake.docs[doc_id]["requestedDeliveryDate"], "2026-11-20")
        self.assertEqual(ex.undo(edited["key"], next(when))["status"], "done")
        self.assertEqual(self.fake.docs[doc_id]["requestedDeliveryDate"], "2026-11-18")
        # the create's undo carries the etag BC answered; after the edit and its undo the
        # document moved on, so BC refuses it (412) and nothing is deleted
        stale = ex.undo(created["key"], next(when))
        self.assertEqual(stale["status"], "failed")
        self.assertIn(doc_id, self.fake.docs)
        # a second order, undone at once: BC deletes it with the etag it answered
        second = po_item(draft=False)
        second["item_id"] = "it_po_2"
        second["body"]["prepared"]["write"]["payload"]["externalDocumentNumber"] = "P-2"
        again = ex.run(second, next(when))[0]
        self.assertEqual(again["status"], "done", again)
        self.assertEqual(len(self.fake.docs), 2)
        self.assertEqual(ex.undo(again["key"], next(when))["status"], "done")
        self.assertEqual(list(self.fake.docs), [doc_id])

        # Planner, calendar, SharePoint: create, then undo each
        for name, store in (("planner_create", "tasks"), ("calendar_create", "events"),
                            ("sp_item_create", "items")):
            with self.subTest(name):
                row = ex.run(item(name, **CASE[name]), next(when))[0]
                self.assertEqual((row["status"], row["mode"]), ("done", "live"), row)
                self.assertEqual(len(getattr(self.fake, store)), 1)
                self.assertEqual(ex.undo(row["key"], next(when))["status"], "done")
                self.assertEqual(getattr(self.fake, store), {})

        # MC: a routine note through the MCP door, authorized by the person's own opt-in
        note = ex.run(item("mc", **CASE["mc_note"]), next(when))[0]
        self.assertEqual(note["status"], "done", note)
        self.assertEqual(self.mc_fake.comments,
                         [{"taskId": "TASK-7", "notes": "Shipping docs filed in the job folder."}])
        self.assertIsNone(note["inverse"])

        # the audit: one attempt before each send, one outcome after, hash chain intact
        rows = ex.journal.rows()
        outcomes = [r for r in rows if r.get("event") == "outcome"]
        self.assertEqual(sum(r.get("event") == "attempt" for r in rows), len(outcomes))
        self.assertTrue(all(r.get("key") and r.get("workflow") and r.get("owner") == OWNER
                            for r in outcomes))
        audit = (self.state / aw.AUDIT_FILE).read_text()
        for secret in (GRAPH_TOKEN, BC_TOKEN, "isolated-fixture-no-credential"):
            self.assertNotIn(secret, audit)

    def test_kill_switch_file_beside_the_queue_stops_every_system(self):
        self.all_live()
        (aw.control_dir(self.state, {}) / "autopilot.off").write_text("")
        ex = self.executor_e2e()
        for name in ("planner_create", "calendar_create", "sp_item_create", "mc_note"):
            self.assertEqual(ex.run(item(name, **CASE[name]), NOW)[0]["status"], "killed")
        self.assertEqual(ex.run(po_item(draft=False), NOW)[0]["status"], "killed")
        self.assertEqual((self.fake.requests, self.mc_fake.requests), ([], []))

    def test_dry_run_per_system_until_its_flag_exists(self):
        self.probe()
        self.pass_gate()
        self.opt_in("customer_order_intake")
        self.flag("planner")                       # only Planner is opened
        ex = self.executor_e2e()
        self.assertEqual(ex.run(item("p", **CASE["planner_create"]), NOW)[0]["status"], "done")
        for name in ("calendar_create", "sp_item_create", "mc_note"):
            row = ex.run(item(name, **CASE[name]), NOW)[0]
            self.assertEqual(row["status"], "dry_run", name)
            self.assertTrue(row["reasons"][0].startswith("system_not_live:live-"), row)
        self.assertEqual(ex.run(po_item(draft=False), NOW)[0]["reasons"],
                         ["system_not_live:live-bc"])
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.mc_fake.requests, [])
        # the flag placed later counts at the next executor (no restart)
        self.flag("calendar")
        self.assertEqual(self.executor_e2e().run(item("c2", **CASE["calendar_create"]), NOW
                                                 )[0]["status"], "done")

    def test_bc_opens_only_for_a_verified_allowed_sandbox_company(self):
        self.go_live(("customer_order_intake",))
        # no probe recorded: BC stays closed even with its go-live file
        ex = self.executor_e2e()
        self.assertNotIn("bc", guard.writes_enabled(ex.environ))
        self.assertEqual(ex.run(po_item(draft=False), NOW)[0]["status"], "unavailable")
        # PHSI Group configured: the probe blocks it, BC stays closed
        phsi = {**self.env, "PLX_AUTOPILOT_BC_COMPANY_ID": PHSI}
        found = self.probe(phsi)["systems"]["bc"]
        self.assertEqual(found["status"], "blocked")
        self.assertIn("never PHSI Group", " ".join(found["missing"]))
        self.assertNotIn("bc", guard.writes_enabled(self.executor_e2e(phsi).environ))
        # Production as the target: live-bc alone does not open it (the second file is
        # the owner's; Production is written in BcProduction below)
        prod = {**self.env, "PLX_AUTOPILOT_BC_ENVIRONMENT": "Production"}
        self.fake.environment = "Production"
        got = self.probe(prod)["systems"]["bc"]
        self.assertEqual((got["status"], got["production_flag"]), ("ready-dry-run", False))
        opened = self.executor_e2e(prod).environ
        self.assertNotIn("bc", guard.writes_enabled(opened))
        self.assertNotIn("PLX_WRITES_SANDBOX_LIVE", opened)
        self.assertNotIn(aw.BC_PRODUCTION_ENV, opened)
        self.fake.environment = SANDBOX
        # a probe that verified PLX does not open BC for another company id or tenant
        # configured after it (review f90e2811ba): nothing is sent to PHSI Group
        self.probe()
        for changed in ({"PLX_AUTOPILOT_BC_COMPANY_ID": PHSI},
                        {"BC_TENANT_ID": "77777777-7777-4777-8777-777777777777"}):
            env = {**self.env, **changed}
            ex = self.executor_e2e(env)
            self.assertNotIn("bc", guard.writes_enabled(ex.environ))
            self.assertEqual(ex.run(po_item(draft=False), NOW)[0]["status"], "unavailable")
        # the host's own BC switch (the desk's) does not open it here either
        host = {**self.env, "PLX_AUTOPILOT_BC_COMPANY_ID": PHSI,
                guard.WRITES_FLAG: "bc,planner", "PLX_WRITES_SANDBOX_LIVE": "1",
                "PLX_WRITES_SCOPES_GRANTED": "bc"}
        opened = guard.writes_enabled(self.executor_e2e(host).environ)
        self.assertNotIn("bc", opened)
        self.assertIn("planner", opened)
        # a verified probe older than three runs no longer counts
        self.probe()
        self.assertTrue(rd.bc_verified(self.state, self.env))
        self.probe(now=datetime.now(timezone.utc) - rd.MAX_AGE - timedelta(minutes=1))
        self.assertFalse(rd.bc_verified(self.state, self.env))
        self.assertEqual(self.fake.requests, [])

    def test_probe_reports_each_status_with_the_exact_missing_permission(self):
        # nothing configured: blocked everywhere, and never a token in the record
        bare = rd.probe(self.state, {aw.CONTROL_ENV: str(aw.control_dir(self.state, {})),
                                     "PLX_ACTIONS_GRAPH_CREDENTIALS": "NOPE"}, now=NOW,
                        graph_token=lambda: self.graph_token)
        self.assertEqual({s: v["status"] for s, v in bare["systems"].items()},
                         {s: "blocked" for s in rd.SYSTEMS})
        # tokens without the write roles: scope missing, naming the permission
        self.graph_token, self.bc_token = token("Mail.Send"), token("app_access")
        got = self.probe()["systems"]
        self.assertEqual(got["planner"]["status"], "ready-but-scope-missing")
        self.assertIn("Tasks.ReadWrite.All", got["planner"]["missing"][0])
        self.assertIn("Calendars.ReadWrite", got["calendar"]["missing"][0])
        self.assertIn("Sites.Selected", got["sharepoint"]["missing"][0])
        self.assertEqual(got["bc"]["status"], "ready-but-scope-missing")
        self.assertIn("API.ReadWrite.All", got["bc"]["missing"][0])
        # the BC app not registered in BC: its company read answers 403
        self.graph_token, self.bc_token = GRAPH_TOKEN, BC_TOKEN

        class Refused(Exception):
            status = 403

        def refused(url, bearer):
            raise Refused()
        got = self.probe(bc_get=refused)["systems"]["bc"]
        self.assertIn("not registered in BC", got["missing"][-1])
        # every scope present: dry run until the owner's file, then live
        got = self.probe()["systems"]
        self.assertEqual({s: v["status"] for s, v in got.items()},
                         {s: "ready-dry-run" for s in rd.SYSTEMS})
        self.flag("bc", "calendar")
        got = self.probe()["systems"]
        self.assertEqual((got["bc"]["status"], got["calendar"]["status"],
                          got["planner"]["status"]), ("ready-live", "ready-live", "ready-dry-run"))
        text = (aw.control_dir(self.state, {}) / rd.READINESS_FILE).read_text()
        for secret in (GRAPH_TOKEN, BC_TOKEN, "isolated-fixture-no-credential"):
            self.assertNotIn(secret, text)
        self.assertEqual(oct((aw.control_dir(self.state, {}) / rd.READINESS_FILE)
                             .stat().st_mode & 0o777), "0o600")

    def test_not_routine_and_malformed_bc_actions_never_reach_the_system(self):
        self.all_live()
        ex = self.executor_e2e()
        doc = "66666666-6666-4666-8666-666666666666"
        # a delete of an open order: Ricardo's scope allows it, but it has no undo: never
        # automatic
        delete = item("del", write={"system": "bc", "operation": "sales_order_delete",
                                    "target": {"document_id": doc},
                                    "precondition": {"etag": "W/\"1\"", "status": "Open",
                                                     "shipped": 0}})
        self.assertEqual(ex.run(delete, NOW)[0]["reasons"],
                         ["operation_not_routine:sales_order_delete"])
        # a price on a new order's line
        self.assertEqual(ex.run(po_item(price=True, draft=False), NOW)[0]["status"],
                         "not_routine")
        # a header edit without the prior values, or of a field outside the closed set
        for payload, pre in (({"requestedDeliveryDate": "2026-11-20"},
                              {"etag": "W/\"1\"", "status": "Open"}),
                             ({"customerNumber": "C-2"},
                              {"etag": "W/\"1\"", "status": "Open",
                               "prior": {"customerNumber": "C-1"}})):
            row = ex.run(item(f"e{len(payload)}{len(pre)}", write={
                "system": "bc", "operation": "sales_order_update",
                "target": {"document_id": doc}, "payload": payload, "precondition": pre}),
                NOW)[0]
            self.assertIn(row["status"], ("not_routine", "refused"), row)
        self.assertEqual(self.fake.requests, [])

    def test_bc_delete_shape_needs_an_open_unshipped_order(self):
        bc = wa.BCAdapter(environ={"PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY,
                                   "PLX_BC_SANDBOX_ENVIRONMENT": SANDBOX})
        doc = "66666666-6666-4666-8666-666666666666"

        def action(**pre):
            return {"operation": "purchase_order_delete", "target": {"document_id": doc},
                    "precondition": {"etag": "W/\"1\"", **pre}}
        req = bc.request(action(status="Open", received=0))
        self.assertEqual((req["method"], req["headers"]), ("DELETE", {"If-Match": "W/\"1\""}))
        self.assertTrue(req["url"].endswith(f"/{SANDBOX}/api/v2.0/companies({BC_COMPANY})/"
                                            f"purchaseOrders({doc})"))
        self.assertIsNone(bc.undo_request(action(status="Open", received=0), req))
        for pre in ({"status": "Released", "received": 0}, {"status": "Open", "received": 2},
                    {"status": "Open"}, {"status": "Open", "received": True}):
            with self.assertRaises(wa.AdapterRefused):
                bc.request(action(**pre))
        self.assertEqual(guard.write_shape("DELETE", "https://api.businesscentral.dynamics.com"
                                           + req["url"].replace("{tenant}", TENANT)), "bc")

    def test_mc_note_needs_a_checkout_and_never_sends_other_tools(self):
        self.all_live()
        ex = self.executor_e2e()
        other = {"write": {**CASE["mc_note"]["write"], "target": {"task_id": "TASK-8"}}}
        row = ex.run(item("mc8", **other), NOW)[0]
        self.assertIn(row["status"], ("failed", "unknown"), row)
        self.assertEqual(self.mc_fake.comments, [])
        # mc_update_task is never sent, whatever is opened
        transport = ex.adapters["mc_task"].transport
        status, body = transport({"body": {"params": {"name": "mc_update_task",
                                                      "arguments": {"taskId": "TASK-7"}}},
                                  "acting_for": OWNER}, "k")
        self.assertEqual((status, body["code"]), (400, "tool_never_sent"))
        # no authorizing person: refused before any call
        status, body = transport(wa.MCTaskAdapter().request(
            {"operation": "task_note", "target": {"task_id": "TASK-7"},
             "payload": {"notes": "x"}}), "k2")
        self.assertEqual((status, body["code"]), (400, "no_authorizing_person"))
        self.assertEqual([r["arguments"]["taskId"] for r in self.mc_fake.requests], ["TASK-8"])


class BcProduction(Wired):
    """AUDIT2-07/V-21 (owner 11-11: BC writes go to the real company, no sandbox): the
    executor writes BC Production behind ``live-bc`` and ``live-bc-production``, a probe
    of Production that verified an allowed company, and only on a named person's
    confirmed approval of the exact item; the unattended gate never writes it."""

    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeSystems({BC_COMPANY: "PLX", PHSI: "PHSI Group"}, "Production")
        self.env = {k: v for k, v in self.env.items() if k != "PLX_BC_SANDBOX_ENVIRONMENT"}
        self.env[aw.APPROVED_ENV] = "bc_write"

    def production_file(self) -> None:
        (aw.control_dir(self.state, {}) / aw.BC_PRODUCTION_FLAG).write_text("")

    def approve(self, ex, row, now, **kw):
        return ex.run(row, now, aw.Approval(by=(OWNER,), **kw))[0]

    def test_only_both_files_a_verified_probe_and_a_confirmed_approval_write_it(self):
        self.all_live()                       # gate passed, opted in, live-bc placed
        self.assertEqual(rd.bc_environment(self.env), "Production")
        # live-bc alone: Production stays closed, the approval cannot reach BC
        ex = self.executor_e2e()
        self.assertNotIn("bc", guard.writes_enabled(ex.environ))
        self.assertEqual(self.approve(ex, po_item(draft=False), NOW,
                                      confirmed_live=True)["status"], "unavailable")
        self.production_file()
        got = self.probe()["systems"]["bc"]
        self.assertEqual((got["status"], got["production_flag"]), ("ready-live", True))
        ex = self.executor_e2e()
        self.assertEqual(ex.environ.get("PLX_WRITES_LIVE"), "1")
        self.assertNotIn("PLX_WRITES_SANDBOX_LIVE", ex.environ)
        # the unattended gate: a dry run naming why, nothing sent
        gated = ex.run(po_item(draft=False), NOW)[0]
        self.assertEqual((gated["status"], gated["reasons"]),
                         ("dry_run", ["bc_production_needs_approval"]), gated)
        # an approval not yet confirmed live: nothing sent either
        unconfirmed = self.approve(ex, po_item(draft=False), NOW)
        self.assertEqual(unconfirmed["status"], "needs_confirm", unconfirmed)
        self.assertEqual(self.fake.requests, [])
        # the item's owner approved and confirmed: written to Production, then undone
        done = self.approve(ex, po_item(draft=False), NOW, confirmed_live=True)
        self.assertEqual((done["status"], done["mode"], done["live_by"]),
                         ("done", "live", "approval"), done)
        self.assertIn(f"/Production/api/v2.0/companies({BC_COMPANY})/salesOrders",
                      self.fake.requests[0]["url"])
        self.assertEqual(len(self.fake.docs), 1)
        self.assertEqual(ex.undo(done["key"], NOW + timedelta(minutes=5))["status"], "done")
        self.assertEqual(self.fake.docs, {})

    def test_a_confirmed_accept_of_a_gate_live_item_writes_it_the_unattended_run_does_not(self):
        # the Autopilot page's path: no per-kind switch, the item is live by its gate and
        # the owner accepts it; the confirmed accept is the approval for Production
        self.env.pop(aw.APPROVED_ENV)
        self.all_live()
        self.production_file()
        self.probe()
        ex = self.executor_e2e()
        self.assertIn("bc", guard.writes_enabled(ex.environ))
        gated = ex.run(po_item(draft=False), NOW)[0]
        self.assertEqual((gated["status"], gated["reasons"]),
                         ("dry_run", ["bc_production_needs_approval"]), gated)
        unconfirmed = self.approve(ex, po_item(draft=False), NOW)
        self.assertEqual((unconfirmed["status"], unconfirmed["reasons"]),
                         ("needs_confirm", ["live_needs_confirm:gate"]), unconfirmed)
        self.assertEqual(self.fake.requests, [])
        done = self.approve(ex, po_item(draft=False), NOW, confirmed_live=True)
        self.assertEqual((done["status"], done["mode"], done["live_by"]),
                         ("done", "live", "gate"), done)
        self.assertIn(f"/Production/api/v2.0/companies({BC_COMPANY})/salesOrders",
                      self.fake.requests[0]["url"])
        # the same item is not written twice, and the unattended run stays a dry run
        again = self.approve(ex, po_item(draft=False), NOW, confirmed_live=True)
        self.assertEqual(again["status"], "duplicate", again)
        self.assertEqual(ex.run(po_item(draft=False), NOW)[0]["reasons"],
                         ["bc_production_needs_approval"])
        self.assertEqual(len(self.fake.requests), 1)

    def test_production_stays_closed_for_phsi_a_stale_probe_or_a_removed_file(self):
        self.all_live()
        self.production_file()
        # PHSI Group configured: the probe of Production blocks it
        phsi = {**self.env, "PLX_AUTOPILOT_BC_COMPANY_ID": PHSI}
        self.assertEqual(self.probe(phsi)["systems"]["bc"]["status"], "blocked")
        ex = self.executor_e2e(phsi)
        self.assertNotIn("bc", guard.writes_enabled(ex.environ))
        self.assertEqual(self.approve(ex, po_item(draft=False), NOW,
                                      confirmed_live=True)["status"], "unavailable")
        # the host's own desk switches do not open Production without the owner's files
        self.probe()
        (aw.control_dir(self.state, {}) / aw.BC_PRODUCTION_FLAG).unlink()
        host = {**self.env, guard.WRITES_FLAG: "bc", "PLX_WRITES_LIVE": "1",
                "PLX_WRITES_SCOPES_GRANTED": "bc", aw.BC_PRODUCTION_ENV: "1"}
        opened = self.executor_e2e(host).environ
        self.assertNotIn("bc", guard.writes_enabled(opened))
        self.assertNotIn(aw.BC_PRODUCTION_ENV, opened)
        # a symlink is not the owner's file
        target = Path(self.tmp.name) / "elsewhere"
        target.write_text("")
        (aw.control_dir(self.state, {}) / aw.BC_PRODUCTION_FLAG).symlink_to(target)
        self.assertFalse(aw.bc_production_on(self.state, self.env))
        self.assertNotIn("bc", guard.writes_enabled(self.executor_e2e().environ))
        # the real file but a probe older than 45 min: closed
        (aw.control_dir(self.state, {}) / aw.BC_PRODUCTION_FLAG).unlink()
        self.production_file()
        self.probe(now=datetime.now(timezone.utc) - rd.MAX_AGE - timedelta(minutes=1))
        self.assertNotIn("bc", guard.writes_enabled(self.executor_e2e().environ))
        self.assertEqual(self.fake.requests, [])

    def test_a_request_to_an_unexpected_environment_is_held_to_the_production_rule(self):
        sandboxed = {"PLX_BC_SANDBOX_ENVIRONMENT": SANDBOX}
        for url, expect in ((f"/v2.0/{{tenant}}/{SANDBOX}/api/v2.0/x", False),
                            ("/v2.0/{tenant}/Production/api/v2.0/x", True),
                            ("/v2.0/{tenant}/Other/api/v2.0/x", True), ("", True)):
            with self.subTest(url):
                self.assertIs(aw._bc_production("bc_write", {"url": url}, sandboxed), expect)
        self.assertIs(aw._bc_production("bc_write", {"url": "/v2.0/{tenant}/Production/x"},
                                        {"PLX_BC_SANDBOX_ENVIRONMENT": "Production"}), True)
        self.assertIs(aw._bc_production("planner_write", {"url": ""}, {}), False)


class EnvFiles(unittest.TestCase):
    def test_env_files_add_credentials_only_never_switches(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "vince-graph.env"
            good.write_text("AZURE_CLIENT_ID=abc\nexport BC_TENANT_ID='t'\n"
                            "PLX_WRITES_LIVE_SYSTEMS=bc\nPLX_AUTOPILOT_WRITES=1\n"
                            "PLX_WRITES_SANDBOX_LIVE=1\nMC_MCP_API_KEY=k\n")
            os.chmod(good, 0o600)
            loose = Path(tmp) / "vince-loose.env"
            loose.write_text("AZURE_CLIENT_SECRET=s\n")
            os.chmod(loose, 0o666)
            env = rd.write_environ({rd.ENV_FILES_ENV: str(Path(tmp) / "vince-*.env"),
                                    "MC_MCP_API_KEY": "process"})
            self.assertEqual((env["AZURE_CLIENT_ID"], env["BC_TENANT_ID"], env["MC_MCP_API_KEY"]),
                             ("abc", "t", "process"))
            for switch in ("PLX_WRITES_LIVE_SYSTEMS", "PLX_AUTOPILOT_WRITES",
                           "PLX_WRITES_SANDBOX_LIVE", "AZURE_CLIENT_SECRET"):
                self.assertNotIn(switch, env)
            # an injected environment without the setting reads no host file
            ex = aw.Executor(Path(tmp) / "state", environ={}, adapters={})
            self.assertNotIn("AZURE_CLIENT_ID", ex.environ)


if __name__ == "__main__":
    unittest.main()
