"""Assist's workflows routes (``operator_api/assist_workflows.py``) with an in-memory log.

Checked at the boundary:
- ``GET /api/assist/workflows`` returns only the signed-in reader's workflows and routines, plus templates;
  when migration 0083 is missing, returns ``status = 'not_run'`` cleanly instead of 500;
- create from plain language, from template, and from the reader's own mined routine (another person's
  routine is 404; card numbers masked);
- pause, resume, edit, run-now (appends a job to ``assist.job_log``), and stop (stops both workflow and
  any live job);
- a colleague gets 404 on every single route for another person's workflow;
- nothing writes to any business system.

Evidence label: injected-transport (fake workflow + job log)."""
from __future__ import annotations

import json
import unittest

from operator_api.tests import test_autopilot_area as base
from operator_api.tests.test_autopilot_area import COLLEAGUE, OWNER


class MissingTableError(Exception):
    sqlstate = "42P01"


class FakeLog:
    def __init__(self, *, missing_wf: bool = False) -> None:
        self.wf_rows: list[dict] = []
        self.job_rows: list[dict] = []
        self.routines: list[dict] = []
        self.missing_wf = missing_wf
        self._out: list = []

    def connection(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if "assist.workflow_log" in s and self.missing_wf:
            raise MissingTableError('relation "assist.workflow_log" does not exist')
        if s.startswith("INSERT INTO assist.workflow_log"):
            wid, person, what, detail = params
            self.wf_rows.append({"seq": len(self.wf_rows) + 1, "workflow_id": wid, "person": person,
                                 "at": "2026-11-24T20:00:00+00:00", "what": what, "detail": json.loads(detail)})
            self._out = []
        elif s.startswith("INSERT INTO assist.job_log"):
            jid, person, what, detail = params
            self.job_rows.append({"seq": len(self.job_rows) + 1, "job_id": jid, "person": person,
                                  "at": "2026-11-24T20:00:00+00:00", "what": what, "detail": json.loads(detail)})
            self._out = []
        elif "FROM assist.workflow_log WHERE workflow_id = %s" in s:
            self._out = [tuple(r[k] for k in ("seq", "workflow_id", "person", "at", "what", "detail"))
                         for r in self.wf_rows if r["workflow_id"] == params[0]]
        elif "FROM assist.workflow_log WHERE what = 'created' AND person = ANY" in s:
            self._out = [(r["workflow_id"],) for r in reversed(self.wf_rows)
                         if r["what"] == "created" and r["person"] in params[0]]
        elif "FROM assist.workflow_log WHERE workflow_id = ANY" in s:
            self._out = [tuple(r[k] for k in ("seq", "workflow_id", "person", "at", "what", "detail"))
                         for r in self.wf_rows if r["workflow_id"] in params[0]]
        elif "FROM assist.job_log WHERE job_id = %s" in s:
            self._out = [tuple(r[k] for k in ("seq", "job_id", "person", "at", "what", "detail"))
                         for r in self.job_rows if r["job_id"] == params[0]]
        elif "FROM assist.job_log WHERE job_id = ANY" in s:
            self._out = [tuple(r[k] for k in ("seq", "job_id", "person", "at", "what", "detail"))
                         for r in self.job_rows if r["job_id"] in params[0]]
        elif "FROM assist.routines WHERE scope = ANY" in s:
            self._out = [tuple(r[k] for k in ("routine_id", "scope", "kind", "trigger", "description", "mined", "validated", "status"))
                         for r in self.routines if r["scope"] in params[0]]
        else:
            raise AssertionError("unexpected statement: " + s)
        return self

    def fetchall(self):
        return self._out

    def fetchone(self):
        return self._out[0] if self._out else None


class WorkflowRoutes(unittest.TestCase):
    client = base.AutopilotArea.client

    def setUp(self) -> None:
        base.AutopilotArea.setUp(self)
        self.log = FakeLog()
        self.log.routines.append({
            "routine_id": "rt_owner", "scope": OWNER[1], "kind": "po_followup",
            "trigger": {"relation": "clock", "weekday": "Mon"}, "description": "Chase late POs",
            "mined": {"acts": 14, "days": 9}, "validated": {}, "status": "candidate",
        })
        self.log.routines.append({
            "routine_id": "rt_colleague", "scope": COLLEAGUE[1], "kind": "mail_sent",
            "trigger": {"relation": "clock", "weekday": "Fri"}, "description": "Weekly update",
            "mined": {"acts": 6, "days": 6}, "validated": {}, "status": "candidate",
        })
        self.backend.memory = type("M", (), {"pool": self.log})()

    def test_missing_migration_returns_not_run_instead_of_500(self):
        self.log.missing_wf = True
        got = self.client(OWNER).get("/api/assist/workflows")
        self.assertEqual(got.status_code, 200, got.text)
        body = got.json()
        self.assertEqual(body["status"], "not_run")
        self.assertIn("0083", body["reason"])
        self.assertGreater(len(body["templates"]), 0)

    def test_three_ways_to_create_and_isolation_between_people(self):
        c = self.client(OWNER)
        # 0. Parse preview
        parsed = c.post("/api/assist/workflows/parse",
                        json={"text": "every Monday 8 AM send me late POs by supplier"})
        self.assertEqual(parsed.status_code, 200, parsed.text)
        self.assertEqual(parsed.json()["trigger_kind"], "schedule")

        # 1. Plain language (masks card numbers)
        w1 = c.post("/api/assist/workflows",
                    json={"plain_text": "every Monday 8 AM send me late POs (card 4111 1111 1111 1111)"})
        self.assertEqual(w1.status_code, 200, w1.text)
        b1 = w1.json()
        self.assertEqual((b1["source_kind"], b1["trigger_kind"], b1["status"]), ("plain_language", "schedule", "active"))
        self.assertNotIn("4111 1111 1111 1111", b1["instruction"])

        # 2. From template
        w2 = c.post("/api/assist/workflows",
                    json={"template_id": "mail-from-sender", "trigger": {"sender": "croda.com"}})
        self.assertEqual(w2.status_code, 200, w2.text)
        self.assertEqual((w2.json()["source_kind"], w2.json()["trigger"]["event"]), ("template", "mail_from"))

        # 3. From mined routine (own works; colleague's routine is 404)
        w3 = c.post("/api/assist/workflows", json={"routine_id": "rt_owner"})
        self.assertEqual(w3.status_code, 200, w3.text)
        self.assertEqual((w3.json()["source_kind"], w3.json()["source_ref"]), ("routine", "rt_owner"))
        self.assertEqual(c.post("/api/assist/workflows", json={"routine_id": "rt_colleague"}).status_code, 404)

        # Listing shows only owner's workflows and routines
        listed = c.get("/api/assist/workflows").json()
        self.assertEqual(len(listed["workflows"]), 3)
        self.assertEqual([r["routine_id"] for r in listed["routines"]], ["rt_owner"])
        col_list = self.client(COLLEAGUE).get("/api/assist/workflows").json()
        self.assertEqual(col_list["workflows"], [])
        self.assertEqual([r["routine_id"] for r in col_list["routines"]], ["rt_colleague"])

        # Colleague gets 404 on every route for owner's workflow
        wid = b1["workflow_id"]
        cc = self.client(COLLEAGUE)
        for method, path, kw in (
            ("patch", f"/api/assist/workflows/{wid}", {"json": {"name": "hack"}}),
            ("post", f"/api/assist/workflows/{wid}/pause", {}),
            ("post", f"/api/assist/workflows/{wid}/resume", {}),
            ("post", f"/api/assist/workflows/{wid}/stop", {}),
            ("post", f"/api/assist/workflows/{wid}/run", {}),
        ):
            self.assertEqual(getattr(cc, method)(path, **kw).status_code, 404, path)

    def test_pause_resume_run_now_and_stop_cascades_to_live_job(self):
        c = self.client(OWNER)
        wid = c.post("/api/assist/workflows",
                     json={"plain_text": "when a PO goes late, draft a chase"}).json()["workflow_id"]

        paused = c.post(f"/api/assist/workflows/{wid}/pause")
        self.assertEqual((paused.status_code, paused.json()["status"]), (200, "paused"))

        resumed = c.post(f"/api/assist/workflows/{wid}/resume")
        self.assertEqual((resumed.status_code, resumed.json()["status"]), (200, "active"))

        ran = c.post(f"/api/assist/workflows/{wid}/run")
        self.assertEqual(ran.status_code, 200, ran.text)
        jid = ran.json()["job"]["job_id"]
        self.assertEqual(ran.json()["job"]["status"], "queued")
        self.assertEqual(len(ran.json()["workflow"]["runs"]), 1)

        stopped = c.post(f"/api/assist/workflows/{wid}/stop")
        self.assertEqual((stopped.status_code, stopped.json()["status"]), (200, "stopped"))
        # Stopping the workflow also requested stop on its live job
        self.assertEqual(stopped.json()["runs"][0]["job"]["status"], "stopping")
        # Stopped workflow cannot be resumed or run again
        self.assertEqual(c.post(f"/api/assist/workflows/{wid}/resume").status_code, 409)
        self.assertEqual(c.post(f"/api/assist/workflows/{wid}/run").status_code, 409)
        self.assertEqual(self.bc.calls, [])

    def test_templates_exclude_mc_approvals_and_pass_copy_rules(self):
        from operating import copy_gate
        c = self.client(OWNER)
        listed = c.get("/api/assist/workflows").json()
        for tpl in listed["templates"]:
            self.assertNotEqual(tpl["template_id"], "approval-arrived")
            self.assertNotEqual(tpl["trigger"].get("event"), "approval_arrived")
            for field in ("name", "description", "instruction", "trigger_summary"):
                res = copy_gate.check(tpl[field], surface="ui")
                self.assertTrue(res["ok"], f"{tpl['template_id']}.{field}: {res['findings']}")
        rejected = c.post(
            "/api/assist/workflows",
            json={"name": "MC approvals", "instruction": "brief approvals",
                  "trigger_kind": "event", "trigger": {"event": "approval_arrived"}},
        )
        self.assertEqual(rejected.status_code, 422)


if __name__ == "__main__":
    unittest.main()
