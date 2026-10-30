"""Mission Control as a capture source (integrations/mc_sources.py).

Evidence label: injected-transport. No network I/O: a fake MC sender answers behind
the real ``HubTransport`` guard and ``MissionControlMcpClient``. Tool response shapes are
upstream PLX_MC@77a5af6's: src/lib/mcp/create-http-server.ts (mc_get_context,
mc_search_tasks, mc_list_buckets), src/lib/mcp/actions.ts (actionGetContext,
actionListBuckets), src/lib/sync/task-search.ts (searchTaskPage keyset pages),
src/lib/mcp/read-actions.ts (mc_get_task, mc_search_knowledge), src/lib/compliance/repo.ts
(eventsForTask: ``seq`` is a string) and src/lib/mc-data/types.ts (Task, ApprovalGate,
Bucket). MC's per-project visibility is src/lib/permissions/project-acl.ts (and src/lib/sync/mapping.ts PROJECT_ID_RE).
"""

from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timezone
import io
import json
import os
import re
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock
import warnings

from verify import live_verify as lv
from integrations import mc as mc_module, mc_sources, plx_hub_guard
from integrations.capture_store import CaptureError, CaptureStore, CapturedRecord
from integrations.live_source_guard import LiveSourceAudience, ReadOnlyGuardError, Secret
from integrations.mc_sources import (KIND, MC_KIND, CaptureMcClient, CaptureSpec,
                                     McCaptureReader, McSourcesCapture, main, unit_of)
from integrations.plx_hub_guard import HubTransport, McConfig
from ontology.types import Actor
from operating.ingestion import IngestError, Ingestor, load_sources

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / "integrations/mc_tool_policy.json").read_text("utf-8"))
SPEC = json.loads((ROOT / "devloop/live-checks/mc.sources_capture.json").read_text("utf-8"))
MC = "https://mc.plxcustomer.io"
OPERATOR = "cos@petrasoap.com"
ENV = {"MC_MCP_API_KEY": "mc-test-key-0123456789", "MC_OPERATOR_EMAIL": OPERATOR}
INTENT = "act_" + "0123456789abcdef01234567"
TENANT = "plx"
OPS = Actor(TENANT, frozenset({"staff:ops"}), "restricted")
TASKS_ONLY = Actor(TENANT, frozenset({"staff:planner"}), "restricted")
NO_DROPS = dict.fromkeys(mc_sources.DROP_REASONS, 0)
PRINCIPAL_READ = {"q": "", "limit": 1}  # each cycle's read of the MC service principal
OUTSIDER = Actor(TENANT, frozenset({"staff:outsider"}), "restricted")


def audience():
    return LiveSourceAudience({"mc:ops:*": ["staff:ops"],
                               "mc:ops:tasks": ["staff:ops", "staff:planner"],
                               "mc:ops:project:PRJ-OPS": ["staff:ops", "staff:planner"]},
                              tenant=TENANT)


def add_hr_project(fake):
    """A second MC project (PRJ-HR) with one task, its approval gate and an event."""
    fake.buckets.append({"id": "BKT-HR", "name": "People", "owner": OPERATOR,
                         "health": "track", "project": "PRJ-HR"})
    fake.tasks["TASK-4"] = {
        "id": "TASK-4", "title": "HR review of PO100234", "stage": "progress",
        "bucket": "BKT-HR", "approvalGates": [{
            "id": "apg_hr", "status": "pending", "reason": "salary band",
            "requestedBy": OPERATOR, "requestedAt": "2026-10-01T11:00:00Z"}]}
    fake.events["TASK-4"] = [fake.event("TASK-4", "1", "task.progress", "2026-10-01T09:00:00Z")]


class FakeMc:
    """MC's MCP endpoint, answering from dicts in MC's own response shapes (PLX_MC@77a5af6, module docstring)."""

    def __init__(self, base=MC):
        self.calls, self.base = [], base
        self.tasks = {
            "TASK-1": {"id": "TASK-1", "title": "Chase supplier confirmation for PO100234",
                       "description": "Freight booked; see PO100234.", "stage": "progress",
                       "bucket": "BKT-OPS", "accountableOwner": OPERATOR,
                       "approvalGates": []},
            "TASK-2": {"id": "TASK-2", "title": "Follow up", "stage": "review",
                       "bucket": "BKT-OPS", "accountableOwner": OPERATOR,
                       "approvalGates": [{
                           "id": "apg_one", "status": "approved",
                           "reason": f"PLX Workbench action {INTENT}: mc.task.create_follow_up "
                                     "on PO100234",
                           "requestedBy": OPERATOR, "requestedAt": "2026-10-01T11:00:00Z",
                           "decidedBy": "vince@petrasoap.com",
                           "decidedAt": "2026-10-01T12:00:00Z"}]},
            "TASK-3": {"id": "TASK-3", "title": "Quiet", "stage": "backlog",
                       "bucket": "BKT-OPS", "approvalGates": []}}
        self.events = {"TASK-1": [self.event("TASK-1", str(n), "task.progress",
                                             f"2026-10-01T09:{n:02d}:00Z") for n in range(25)],
                       "TASK-2": [self.event("TASK-2", "100", "approval.decided",
                                             "2026-10-01T12:00:00Z")]}
        self.buckets = [{"id": "BKT-OPS", "name": "Operations", "owner": OPERATOR,
                         "health": "track", "project": "PRJ-OPS"}]
        self.gone = set()
        self.archived_projects = set()  # a bucket retires with "archivedAt" or its project
        self.failures = {}  # tool -> ("http", status) | ("rpc", JSON-RPC code)
        self.page_cap = 200  # MC answers at most ``limit`` rows; a smaller cap pages more
        self.principal = "sp_mcp_workbench"  # the service principal the key resolves to

    def retired(self, bucket):  # actions.ts actionGetContext / actionListBuckets (TASK-2532)
        return bool(bucket.get("archivedAt")) or bucket.get("project") in self.archived_projects

    @staticmethod
    def event(task_id, seq, kind, ts):  # compliance/repo.ts eventsForTask row
        return {"seq": seq, "ts": ts, "kind": kind, "actor": OPERATOR, "repo": None,
                "taskId": task_id, "pr": None, "payload": {}}

    def tool(self, name, args):
        if name == "mc_get_context":  # create-http-server.ts: {data, meta: {filter}}
            tasks, buckets = [dict(t) for t in self.tasks.values()], self.buckets
            if "bucket" not in args and "taskIds" not in args:  # retired work leaves the
                retired = {b["id"] for b in buckets if self.retired(b)}  # unfiltered view
                buckets = [b for b in buckets if b["id"] not in retired]
                tasks = [t for t in tasks if t["bucket"] not in retired]
            if "taskIds" in args:  # scoped by id: retired work stays
                tasks = [t for t in tasks if t["id"] in args["taskIds"]]
            projects = [{"id": p, "name": p, "status": "active"}
                        for p in sorted({b["project"] for b in buckets if b.get("project")})]
            if args.get("depth") != "full":  # actions.ts actionGetContext compact
                return {"data": {"taskCount": len(tasks), "activeCount": len(tasks),
                                 "buckets": [{"id": b["id"], "name": b["name"]}
                                             for b in buckets],
                                 "projects": projects,
                                 "topTasks": tasks[:15], "lastSweep": None},
                        "meta": {"filter": dict(args)}}
            if "bucket" in args:
                tasks = [t for t in tasks if t["bucket"] == args["bucket"]]
            return {"data": {"tasks": tasks,
                             "buckets": [dict(b, target="", started="", desc="", repos=[],
                                              prd=None) for b in buckets],
                             "projects": projects,
                             "conflicts": 0, "errors": 0, "lastSweep": None},
                    "meta": {"filter": dict(args)}}
        if name == "mc_get_task":  # read-actions.ts actionGetTask (checkout ids redacted)
            if args["id"] in self.gone:
                return {"error": {"code": "not_found", "message": "no"}}, True
            task = json.loads(re.sub(r"dsp_[A-Za-z0-9]+", lambda m: "dsp_\u2026" + m[0][-4:],
                                     json.dumps(self.tasks[args["id"]])))  # redactCheckoutIds
            return {"data": {"taskId": args["id"], "task": task,
                             "accountableOwner": task.get("accountableOwner"),
                             "evidence": None, "checkouts": [],
                             "events": self.events.get(args["id"], []),
                             "link": f"{MC}/tasks/{args['id']}"}}
        if name == "mc_list_buckets":  # actions.ts actionListBuckets: bare, no data wrapper
            listed = [dict({"archivedAt": None, "archivedBy": None, "archiveReason": None}, **b)
                      for b in self.buckets
                      if args.get("includeArchived") or not self.retired(b)]
            return {"buckets": listed, "count": len(listed)}
        if name == "mc_search_tasks":  # task-search.ts searchTaskPage: keyset on (n, id)
            def key(task_id):
                match = re.fullmatch(r"TASK-([0-9]+)", task_id)
                return (int(match[1]) if match else 0, task_id)
            rows = sorted(self.tasks.values(), key=lambda t: key(t["id"]))
            after = (json.loads(base64.urlsafe_b64decode(args["cursor"] + "==")) if
                     "cursor" in args else None)
            rest = [t for t in rows if after is None or key(t["id"]) > tuple(after)]
            page = rest[:min(args.get("limit", 50), self.page_cap)]
            more = len(rest) > len(page)
            cursor = base64.urlsafe_b64encode(json.dumps(key(page[-1]["id"])).encode()
                                              ).decode().rstrip("=") if more else None
            return {"data": {"tasks": [dict(t) for t in page], "total": len(rows),
                             "nextCursor": cursor},  # meta.actor: PLX_MC@8cd548a
                    "meta": {"filter": dict(args),  # create-http-server.ts 195-203
                             "actor": {"servicePrincipalId": self.principal}}}
        if name == "mc_search_knowledge":  # read-actions.ts + brain-ask searchBrainAsk
            return {"data": {"query": args["q"], "configured": True, "status": "ok",
                             "message": None, "hits": [
                {"id": "memory:7", "title": "PO100234 lessons", "snippet": "landed cost",
                 "score": 0.42, "namespace": "swarm", "source": "memory"}]}}
        raise AssertionError(f"unexpected tool {name}")

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, body))
        if url.startswith(self.base + "/api/cursor/mcp"):
            request = json.loads(body)
            layer, code = self.failures.get(request["params"]["name"], (None, None))
            secret = {"code": "internal", "message": "PO100234 at TASK-1"}  # never echoed
            if layer == "http":
                return code, {"content-type": "application/json"}, json.dumps(
                    {"error": secret}).encode()
            if layer == "rpc":
                return 200, {"content-type": "application/json"}, json.dumps(
                    {"jsonrpc": "2.0", "id": request["id"],
                     "error": dict(secret, code=code)}).encode()
            answer = self.tool(request["params"]["name"], request["params"]["arguments"])
            payload, is_error = answer if isinstance(answer, tuple) else (answer, False)
            # A str is the tool's raw text, as MC would send it.
            text = payload if isinstance(payload, str) else json.dumps(payload)
            result = {"content": [{"type": "text", "text": text}]}
            if is_error:
                result["isError"] = True
            return 200, {"content-type": "application/json"}, json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
        raise AssertionError(f"unexpected request {method} {url}")

    def tool_names(self):
        return [json.loads(body)["params"]["name"] for _m, url, body in self.calls
                if url.startswith(MC)]


def hub(fake):
    return HubTransport(fake, environ={}, sleep=lambda s: None, jitter=lambda: 1.0)


def mc_client(fake, operator=OPERATOR):
    return CaptureMcClient(McConfig(MC, Secret(ENV["MC_MCP_API_KEY"]), operator), hub=hub(fake))


class Clock:
    def __init__(self):
        self.minute = 0

    def __call__(self):
        self.minute += 1
        return datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc).replace(
            minute=self.minute % 60, hour=8 + self.minute // 60)


class McSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeMc()
        self.store = CaptureStore(Path(self.tmp.name) / "mc.sqlite", audience(), clock=Clock())
        self.addCleanup(self.store.close)

    def capture(self, spec, client=None):
        return McSourcesCapture(self.store, client or mc_client(self.fake)).capture(spec)

    def test_only_policy_read_tools_reach_mc_and_a_write_is_refused_before_any_request(self):
        client = mc_client(self.fake)
        for name in ("mc_checkout_task", "mc_complete_task", "mc_create_task",
                     "mc_dismiss_conflict", "mc_request_approval"):
            with self.assertRaises(ReadOnlyGuardError):
                client.call_tool(name, {"taskId": "TASK-1"})
            with self.assertRaises(ReadOnlyGuardError):
                client.call_write_tool(name, {"taskId": "TASK-1"}, authorization=None)
        for name in ("mc_list_checkouts", "mc_sync_skills"):  # reads outside the capture set
            with self.assertRaises(ReadOnlyGuardError):
                client.call_tool(name, {"query": "x"})
        # A paging cursor or archive flag MC would not send back is refused before I/O.
        for name, args in (("mc_search_tasks", {"cursor": "../x"}),
                           ("mc_search_tasks", {"cursor": ""}),
                           ("mc_list_buckets", {"includeArchived": "yes"})):
            with self.assertRaises(ReadOnlyGuardError):
                client.call_tool(name, args)
        self.assertEqual(self.fake.calls, [])
        self.capture(CaptureSpec("ops", ("landed cost",)), client)
        called = set(self.fake.tool_names())
        self.assertTrue(called)
        self.assertLessEqual(called, set(POLICY["reads"]))
        self.assertEqual({m for m, _u, _b in self.fake.calls}, {"POST"})

    def test_task_details_page_resume_and_report_completeness_per_unit(self):
        spec = CaptureSpec("ops", ("landed cost",), max_task_reads=2, page_size=1)
        first = {u["collection"]: u for u in self.capture(spec)["units"]}
        self.assertEqual(first["tasks"]["state"], "complete")
        self.assertEqual(first["tasks"]["records_current"], 3)
        self.assertEqual((first["approvals"]["state"], first["approvals"]["reason"]),
                         ("in_progress", "cap:max_task_reads"))
        self.assertEqual(first["approvals"]["pages"], 2)
        self.assertEqual((first["knowledge"]["state"], first["knowledge"]["reason"]),
                         ("partial", "ranked_search"))
        self.assertEqual(first["buckets"]["state"], "complete")
        self.fake.calls.clear()
        second = {u["collection"]: u for u in self.capture(spec)["units"]}
        # The resumed run reads only the task after the cursor.
        self.assertEqual([json.loads(b)["params"]["arguments"] for _m, u, b in self.fake.calls
                          if json.loads(b)["params"]["name"] == "mc_get_task"],
                         [{"id": "TASK-3"}])
        self.assertEqual(second["approvals"]["state"], "complete")
        self.assertEqual(second["approvals"]["records_current"], 1)
        # TASK-1 returned MC's whole 25-event window: older history was not read.
        self.assertEqual((second["task_events"]["state"], second["task_events"]["reason"]),
                         ("partial", "source_window:last_25"))
        self.assertEqual(second["task_events"]["records_current"], 26)
        # A task MC no longer shows is dropped, never a complete traversal.
        self.fake.gone.add("TASK-3")
        self.fake.events["TASK-1"] = self.fake.events["TASK-1"][:3]
        third = {u["collection"]: u for u in self.capture(CaptureSpec("ops"))["units"]}
        self.assertEqual((third["approvals"]["state"], third["approvals"]["reason"]),
                         ("partial", "withheld_or_dropped"))
        self.assertNotIn("knowledge", third)

    def test_brain_items_are_retired_for_the_one_brain_capture_path(self):
        # GOV-13: integrations.knowledge_source is the only brain-item capture path. No
        # capture, path, ingestion source or CLI flag still offers brain_items.
        with self.assertRaises(TypeError):
            CaptureSpec("ops", brain_items=True)
        with self.assertRaises(ValueError):
            unit_of("ops", "brain_items")
        with self.assertRaises(IngestError) as caught:
            load_sources({"schema": "plx-ingest-sources/v1", "tenant": TENANT,
                          "actor": {"principals": ["svc:ingest"]},
                          "sources": [{"id": "mc-ops", "kind": "mc_sources", "scope": "ops",
                                       "brain_items": True}]}, {})
        self.assertEqual(caught.exception.code, "ingest_source_unknown_field")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["capture", "--store", str(Path(self.tmp.name) / "cli.sqlite"),
                  "--scope", "ops", "--brain-items"], environ=ENV, hub=hub(self.fake))
        units = self.capture(CaptureSpec("ops", ("landed cost",)))["units"]
        self.assertEqual([u["collection"] for u in units],
                         ["tasks", "approvals", "task_events", "buckets", "knowledge"])
        self.assertTrue(all(u.startswith(MC) for _m, u, _b in self.fake.calls))

    def test_records_carry_provenance_and_the_audience_trims_every_read(self):
        self.capture(CaptureSpec("ops", ("landed cost",)))
        gates = self.store.records(OPS, "mission_control", unit_of("ops", "approvals"))
        gate = gates["items"][0]
        self.assertTrue(gate["citation"].startswith("mc://ops/approvals/apg_one@"))
        self.assertEqual(gate["row"]["workbench_intent_id"], INTENT)
        self.assertEqual(gate["row"]["task_id"], "TASK-2")
        self.assertEqual(gate["meta"]["tool"], "mc_get_task")
        self.assertEqual(gate["meta"]["ids"], {"task_id": "TASK-2", "gate_id": "apg_one"})
        self.assertTrue(gate["recorded_at"])   # as known at: the store's capture times
        self.assertNotIn(OPERATOR, json.dumps(gate["meta"]))
        hit = self.store.records(OPS, "mission_control", unit_of("ops", "knowledge"))["items"][0]
        self.assertEqual((hit["row"]["score"], hit["meta"]["trust"]["trust_tier"]), ("0.42", "T3"))
        # A planner may read the operator scope's tasks, nothing else of it; an outsider
        # nothing; a handle resolves only for a reader the audience allows.
        self.assertEqual(len(self.store.records(TASKS_ONLY, "mission_control",
                                                unit_of("ops", "tasks"))["items"]), 3)
        for actor, unit in ((TASKS_ONLY, "approvals"), (TASKS_ONLY, "knowledge"),
                            (OUTSIDER, "tasks")):
            with self.assertRaises(CaptureError) as caught:
                self.store.records(actor, "mission_control", unit_of("ops", unit))
            self.assertEqual(caught.exception.code, "not_visible")
        self.assertTrue(self.store.resolve(gate["citation"], OPS)["ok"])
        self.assertFalse(self.store.resolve(gate["citation"], TASKS_ONLY)["ok"])

    def test_every_read_trims_records_per_mc_project_and_unknown_projects_fail_closed(self):
        add_hr_project(self.fake)
        self.fake.buckets += [{"id": "BKT-LOOSE", "name": "Loose", "project": None},
                              {"id": "BKT-ODD", "name": "Odd", "project": "hr; staff"}]
        self.fake.tasks.update({  # unparented, a bucket MC has no row for, none, a malformed
            n: {"id": n, "title": n, "stage": "backlog", "bucket": b, "approvalGates": []}
            for n, b in (("TASK-5", "BKT-LOOSE"), ("TASK-6", "BKT-GONE"), ("TASK-8", None),
                         ("TASK-7", "BKT-ODD"))})
        units = {u["collection"]: u for u in self.capture(CaptureSpec("ops"))["units"]}
        self.assertEqual({c: (units[c]["records_current"], units[c]["dropped"], units[c]["state"])
                          for c in ("tasks", "buckets")},
                         {"tasks": (7, 1, "partial"), "buckets": (3, 1, "partial")})
        # TASK-7's bucket's project is no MC project id: counted by that reason.
        self.assertEqual(units["tasks"]["drops"], {**NO_DROPS, "bucket_project_invalid": 1})
        # MC has no project for a task without a bucket row and shows it as an unparented
        # bucket's (project-acl.ts canAccessTask): keyed so, never under another project.
        tasks = self.store.records(OPS, KIND, unit_of("ops", "tasks"), limit=1000)["items"]
        self.assertEqual({i["key"]["id"] for i in tasks if i["meta"]["project"] == "unparented"},
                         {"TASK-5", "TASK-6", "TASK-8"})
        hr = [i["citation"] for c in ("tasks", "approvals", "task_events", "buckets")
              for i in self.store.records(OPS, KIND, unit_of("ops", c), limit=1000)["items"]
              if i["meta"]["project"] == "PRJ-HR"]
        self.assertEqual(len(hr), 4)
        # Granted every project-scoped collection but only PRJ-OPS: nothing of PRJ-HR or of
        # the unparented bucket through records, completeness or resolve; an outsider nothing.
        team = Actor(TENANT, frozenset({"staff:ops-team"}), "restricted")
        grants = {f"mc:ops:{c}": ["staff:ops-team"]
                  for c in ("tasks", "approvals", "task_events", "buckets")}
        view = CaptureStore(self.store.path, LiveSourceAudience(
            {**grants, "mc:ops:project:PRJ-OPS": ["staff:ops-team"]}, tenant=TENANT))
        self.addCleanup(view.close)
        for collection, wanted in (("tasks", {"TASK-1", "TASK-2", "TASK-3"}),
                                   ("approvals", {"apg_one"}), ("buckets", {"BKT-OPS"})):
            unit = unit_of("ops", collection)
            self.assertEqual({i["key"]["id"] for i in view.records(team, KIND, unit)["items"]},
                             wanted)
            self.assertEqual(view.completeness(team, KIND, unit)["records_visible"], len(wanted))
        events = view.records(team, KIND, unit_of("ops", "task_events"), limit=1000)["items"]
        self.assertEqual({e["row"]["task_id"] for e in events}, {"TASK-1", "TASK-2"})
        for actor in (team, OUTSIDER):
            self.assertEqual({r.get("error") for r in view.resolve_many(hr, actor)["results"]},
                             {"not_visible"})
        # The current version's project gates every older one: TASK-1 moved into PRJ-HR
        # hides its PRJ-OPS history from the team as well.
        old = view.records(team, KIND, unit_of("ops", "tasks"))["items"][0]
        self.assertEqual(old["key"]["id"], "TASK-1")
        self.fake.tasks["TASK-1"]["bucket"] = "BKT-HR"
        self.capture(CaptureSpec("ops"))
        self.assertEqual(self.store.resolve(old["citation"], OPS)["status"], "superseded")
        self.assertEqual(view.resolve(old["citation"], team)["error"], "not_visible")
        # A version without a project key (captured before per-project keys) is hidden.
        run = self.store.begin_run(KIND, unit_of("ops", "tasks"),
                                   MC_KIND.resources(("ops", "tasks", "TASK-9")))["run_id"]
        self.store.save_page(run, [CapturedRecord(("ops", "tasks", "TASK-9"),
                                                  {"row": {"id": "TASK-9"}, "meta": {}},
                                                  "v1", "mission_control")], None)
        self.assertNotIn("TASK-9", {i["key"]["id"] for i in self.store.records(
            OPS, KIND, unit_of("ops", "tasks"), limit=1000)["items"]})

    def test_a_moved_tasks_gates_and_events_are_rekeyed_on_the_next_listing(self):
        team = Actor(TENANT, frozenset({"staff:ops-team"}), "restricted")
        grants = {f"mc:ops:{c}": ["staff:ops-team"]
                  for c in ("tasks", "approvals", "task_events", "buckets")}
        for n in (1, 10, 60):
            with self.subTest(moved=n), tempfile.TemporaryDirectory() as tmp:
                fake = FakeMc()
                add_hr_project(fake)
                moved = [f"TASK-M{i}" for i in range(n)]
                for i, task_id in enumerate(moved):
                    fake.tasks[task_id] = {"id": task_id, "title": "m", "stage": "progress",
                                           "bucket": "BKT-OPS", "approvalGates": [{
                                               "id": f"apg_m{i}", "status": "pending",
                                               "reason": "r", "requestedBy": OPERATOR,
                                               "requestedAt": "2026-10-01T11:00:00Z"}]}
                    fake.events[task_id] = [fake.event(task_id, "1", "task.progress",
                                                       "2026-10-01T09:00:00Z")]
                store = CaptureStore(Path(tmp) / "mc.sqlite", audience(), clock=Clock())
                view = CaptureStore(store.path, LiveSourceAudience(
                    {**grants, "mc:ops:project:PRJ-OPS": ["staff:ops-team"]}, tenant=TENANT))
                self.addCleanup(store.close)
                self.addCleanup(view.close)
                McSourcesCapture(store, mc_client(fake)).capture(
                    CaptureSpec("ops", max_task_reads=500))
                old = [i["citation"] for i in view.records(
                    team, KIND, unit_of("ops", "approvals"), limit=1000)["items"]]
                self.assertEqual(len(old), n + 1)
                for task_id in moved:
                    fake.tasks[task_id]["bucket"] = "BKT-HR"
                fake.calls.clear()
                # No task detail is read (max_task_reads 0): the listing alone re-keys.
                McSourcesCapture(store, mc_client(fake)).capture(
                    CaptureSpec("ops", max_task_reads=0))
                self.assertNotIn("mc_get_task", fake.tool_names())
                gates = store.records(OPS, KIND, unit_of("ops", "approvals"), limit=1000)
                self.assertEqual(sorted(i["key"]["id"] for i in gates["items"]
                                        if i["meta"]["project"] == "PRJ-HR"),
                                 sorted(["apg_hr", *(f"apg_m{i}" for i in range(n))]))
                self.assertEqual(len(gates["items"]), n + 2)  # none lost or duplicated
                self.assertEqual({i["key"]["id"] for i in view.records(
                    team, KIND, unit_of("ops", "approvals"), limit=1000)["items"]}, {"apg_one"})
                self.assertEqual({e["row"]["task_id"] for e in view.records(
                    team, KIND, unit_of("ops", "task_events"), limit=1000)["items"]},
                    {"TASK-1", "TASK-2"})
                # The pre-move versions are now gated by the new project as well.
                self.assertEqual([r.get("error") for r in view.resolve_many(old, team)
                                  ["results"]].count("not_visible"), n)

    def test_upstream_bucket_listing_is_captured_and_another_shape_fails_closed(self):
        units = {u["collection"]: u for u in self.capture(CaptureSpec("ops"))["units"]}
        self.assertEqual((units["buckets"]["state"], units["buckets"]["records_current"]),
                         ("complete", 1))
        bucket = self.store.records(OPS, "mission_control", unit_of("ops", "buckets"))["items"][0]
        self.assertTrue(bucket["citation"].startswith("mc://ops/buckets/BKT-OPS@"))
        self.assertEqual((bucket["row"]["name"], bucket["row"]["project"]),
                         ("Operations", "PRJ-OPS"))
        # A shape MC does not send (data-wrapped, or a count that disagrees) is a source
        # error, never an empty or complete listing.
        for answer in ({"data": {"buckets": self.fake.buckets}},
                       {"buckets": self.fake.buckets, "count": 2}):
            self.fake.tool = lambda name, args, real=self.fake.tool, answer=answer: (
                answer if name == "mc_list_buckets" else real(name, args))
            units = {u["collection"]: u for u in self.capture(CaptureSpec("ops"))["units"]}
            self.assertEqual((units["buckets"]["state"], units["buckets"]["reason"],
                              units["buckets"]["error"]),
                             ("in_progress", "source_error", "buckets_shape_invalid"))

    def test_an_mc_side_error_blocks_naming_its_layer_and_our_bad_arguments_fail(self):
        # Live 53285db79efc: mc_get_context{depth:full} answered upstream_error, no layer.
        real = self.fake.tool
        cases = {("http", 503): ("blocked_upstream", "http_503"),
                 ("rpc", -32603): ("blocked_upstream", "jsonrpc_-32603"),
                 ("tool", "internal"): ("blocked_upstream", "tool_error"),
                 ("rpc", -32602): ("source_error", "jsonrpc_-32602"),
                 # Our operator or call, not MC's fault (PLX_MC read-actions.ts, auth.ts),
                 # and anything unknown: a failure, never blocked.
                 ("tool", "forbidden"): ("source_error", "tool_forbidden"),
                 ("tool", "invalid_query"): ("source_error", "tool_invalid_query"),
                 ("tool", "PO100234 gone"): ("source_error", "tool_other"),
                 # Each no-retry producer names its own cause.
                 ("http", 400): ("source_error", "http_400"),
                 ("http", 404): ("source_error", "http_404"),
                 ("rpc", "oops"): ("source_error", "jsonrpc_code_invalid")}
        for (layer, code), expected in cases.items():
            with self.subTest(layer=layer, code=code):
                self.fake.failures = {"mc_get_context": (layer, code)} if layer != "tool" \
                    else {}
                self.fake.tool = lambda name, args, layer=layer, code=code: (
                    ({"error": {"code": code, "message": "PO100234"}}, True)
                    if layer == "tool" and name == "mc_get_context" else real(name, args))
                units = {u["collection"]: u for u in self.capture(CaptureSpec("ops"))["units"]}
                self.assertEqual((units["tasks"]["state"], units["tasks"]["reason"],
                                  units["tasks"]["error"]), ("in_progress", *expected))
                self.assertEqual(units["buckets"]["state"], "complete")
        # The knowledge relay: MC's own brain call down is blocked, not ours.
        self.fake.failures, self.fake.tool = {"mc_search_knowledge": ("http", 502)}, real
        units = {u["collection"]: u for u in
                 self.capture(CaptureSpec("ops", ("PLX",)))["units"]}
        self.assertEqual((units["knowledge"]["reason"], units["knowledge"]["error"]),
                         ("blocked_upstream", "http_502"))

    def big_context(self, buckets, tasks=60, description=72_000):
        """``tasks`` tasks over ``buckets`` buckets in two MC projects, whose full context
        answer exceeds the 4,000,000-byte bound (with the default ``description``)."""
        self.fake.buckets = [{"id": f"BKT-P{n:02d}", "name": f"Bucket {n}", "owner": OPERATOR,
                              "health": "track", "project": "PRJ-HR" if n % 2 else "PRJ-OPS"}
                             for n in range(buckets)]
        self.fake.tasks = {f"TASK-{n}": {"id": f"TASK-{n}", "title": f"Task {n}",
                                         "stage": "progress", "bucket": f"BKT-P{n % buckets:02d}",
                                         "description": f"{n} " + "x" * description,
                                         "approvalGates": []} for n in range(tasks)}
        self.fake.events = {}

    def over_bound(self):
        """MC's full context answer exceeds the byte bound whatever the tasks."""
        real = self.fake.tool

        def tool(name, args):
            answer = real(name, args)
            if name == "mc_get_context" and args == {"depth": "full"}:
                answer["data"]["lastSweep"] = "x" * 4_000_001
            return answer
        self.fake.tool = tool

    def tasks_unit(self, spec=None):
        return next(u for u in self.capture(spec or CaptureSpec("ops", max_task_reads=0))[
            "units"] if u["collection"] == "tasks")

    def reads(self, name):
        """The arguments of each ``name`` call; for ``mc_search_tasks`` the task reads only,
        never a cycle's principal read (``PRINCIPAL_READ``, asserted where it matters)."""
        return [args for args in (json.loads(b)["params"]["arguments"]
                                  for _m, _u, b in self.fake.calls
                                  if json.loads(b)["params"]["name"] == name)
                if not (name == "mc_search_tasks" and args == PRINCIPAL_READ)]

    def test_a_full_context_over_the_bound_is_read_as_task_search_pages(self):
        # Live 9826f60e9039 (2,397 visible tasks): the full context exceeds the bound on
        # every run, and a read per bucket never returned a task without a bucket row.
        for tasks in (1, 10, 60):
            with self.subTest(tasks=tasks):
                self.setUp()
                self.big_context(10, tasks, description=10)
                self.fake.tasks[f"TASK-{tasks}"] = {  # no bucket row: MC shows it unparented
                    "id": f"TASK-{tasks}", "title": "loose", "stage": "backlog", "bucket": "",
                    "approvalGates": []}
                self.over_bound()
                self.fake.page_cap = 7
                unit = self.tasks_unit()
                self.assertEqual(self.fake.tool_names()[:4],
                                 ["mc_search_tasks", "mc_get_context", "mc_list_buckets",
                                  "mc_list_buckets"])  # the principal read comes first
                pages = self.reads("mc_search_tasks")
                self.assertEqual(len(pages), tasks // 7 + 1)
                # Both bucket lists before and after the pages, then the buckets unit's.
                self.assertEqual(self.reads("mc_list_buckets"),
                                 [{"includeArchived": True}, {}] * 2 + [{}])
                self.assertEqual((unit["state"], unit["reason"], unit["error"],
                                  unit["records_current"], unit["dropped"]),
                                 ("complete", None, None, tasks + 1, 0))
                items = self.store.records(OPS, KIND, unit_of("ops", "tasks"),
                                           limit=1000)["items"]
                self.assertEqual(
                    {i["row"]["id"]: (i["row"], i["meta"]["project"], i["meta"]["tools"])
                     for i in items},
                    {t["id"]: ({**{k: v for k, v in t.items() if k != "approvalGates"},
                                "scope": "ops"},
                               "unparented" if not t["bucket"] else
                               "PRJ-HR" if int(t["bucket"][-2:]) % 2 else "PRJ-OPS",
                               ["mc_get_context", "mc_search_tasks"])
                     for t in self.fake.tasks.values()})
                run = self.store.run(KIND, unit_of("ops", "tasks"))
                self.assertEqual({k: v for k, v in json.loads(run["detail"]).items()
                                  if k in ("tool", "arguments")},
                                 {"tool": "mc_search_tasks", "arguments": {}})
                # Per-project keys hold through the pages: the planner reads PRJ-OPS only.
                planner = self.store.records(TASKS_ONLY, KIND, unit_of("ops", "tasks"),
                                             limit=1000)["items"]
                self.assertEqual({i["meta"]["project"] for i in planner}, {"PRJ-OPS"})
                self.assertEqual(len(planner), (tasks + 1) // 2)
        # A page over the bound is read again at half the limit, from the same cursor.
        self.setUp()
        self.big_context(10)
        unit = self.tasks_unit()
        self.assertEqual([p["limit"] for p in self.reads("mc_search_tasks")], [200, 100, 50, 50])
        self.assertEqual(len({p.get("cursor") for p in self.reads("mc_search_tasks")}), 2)
        self.assertEqual((unit["state"], unit["records_current"], unit["dropped"]),
                         ("complete", 60, 0))

    def test_retired_work_is_recorded_retired_in_both_read_modes_never_missing(self):
        """PLX_MC@2f653cf TASK-2532: the unfiltered context and the bucket list leave
        retired work out; a search, or a context scoped by taskIds, keeps it. A captured
        task whose bucket or project was archived stays current with the reason; one MC no
        longer shows at all goes missing."""
        # One full read (3 tasks; 120, read again in batches of 50 ids); search pages.
        for buckets, tasks in ((1, 3), (1, 120), (10, 180)):
            with self.subTest(buckets=buckets, tasks=tasks):
                self.setUp()
                self.big_context(buckets, tasks, description=72_000 if buckets > 1 else 5)
                self.assertEqual(self.tasks_unit()["records_current"], len(self.fake.tasks))
                self.fake.buckets[0]["archivedAt"] = "2026-10-10T07:38:35Z"  # BKT-P00
                self.fake.archived_projects.add("PRJ-HR")  # retires every odd bucket too
                reasons = {t: "bucket_archived" if row["bucket"] == "BKT-P00"
                           else "project_archived" for t, row in self.fake.tasks.items()
                           if row["bucket"] == "BKT-P00" or int(row["bucket"][-2:]) % 2}
                del self.fake.tasks["TASK-1"]
                reasons.pop("TASK-1", None)
                self.fake.calls.clear()
                unit = self.tasks_unit()
                self.assertEqual((unit["state"], unit["reason"], unit["dropped"],
                                  unit["records_current"], unit["drops"]),
                                 ("complete", None, 0, len(self.fake.tasks), NO_DROPS))
                self.assertEqual(bool(self.reads("mc_search_tasks")), buckets > 1)
                if buckets == 1:  # the context's absentees, read again by id, once each
                    again = self.reads("mc_get_context")[1:]
                    self.assertEqual([len(a["taskIds"]) for a in again],
                                     [3] if tasks == 3 else [50, 50, 20])
                    self.assertEqual(sorted(i for a in again for i in a["taskIds"]),
                                     sorted(f"TASK-{n}" for n in range(tasks)))
                items = self.store.records(OPS, KIND, unit_of("ops", "tasks"), limit=1000,
                                           include_missing=True)["items"]
                self.assertEqual({i["row"]["id"]: i["row"].get("retired_in_mc")
                                  for i in items if i["row"].get("retired_in_mc")}, reasons)
                self.assertEqual([i["row"]["id"] for i in items
                                  if i["missing_from_source_since"]], ["TASK-1"])
                self.assertEqual({i["row"]["id"] for i in items if i["row"]["id"] in reasons
                                  and i["meta"]["project"] != (
                                      "PRJ-HR" if int(i["row"]["bucket"][-2:]) % 2
                                      else "PRJ-OPS")}, set())
                # Unchanged on the next read: no new version.
                self.assertEqual(self.tasks_unit()["new_versions"], 0)
                buckets_unit = next(u for u in self.capture(CaptureSpec("ops", max_task_reads=0))[
                    "units"] if u["collection"] == "buckets")
                self.assertEqual((buckets_unit["state"], buckets_unit["records_current"]),
                                 ("complete", 0 if buckets == 1 else 4))

    def test_tasks_moving_during_a_paged_read_or_a_misshapen_answer_are_never_complete(self):
        guards = {"no_buckets": "context_lacks_buckets", "no_total": "search_tasks_shape_invalid",
                  "endless": "search_page_empty", "listing": "buckets_shape_invalid"}
        for change in ("delete", "new_bucket", "twice", "withheld", *guards):
            with self.subTest(change=change):
                self.setUp()
                self.big_context(4, tasks=20, description=10)
                self.fake.tasks["TASK-20"] = dict(self.fake.tasks["TASK-0"], id="TASK-20",
                                                  bucket="BKT-NEW")
                self.fake.page_cap = 5
                self.over_bound()
                pages, real = [], self.fake.tool

                def tool(name, args, change=change):
                    if change == "no_buckets" and args == {"depth": "full"}:
                        return {"data": {"tasks": []}}
                    if change == "listing" and name == "mc_list_buckets":
                        return {"buckets": [], "count": 1}
                    answer = real(name, args)
                    if name != "mc_search_tasks" or args == PRINCIPAL_READ:
                        return answer
                    pages.append(args)
                    if change == "no_total":
                        del answer["data"]["total"]
                    if change == "withheld":
                        answer["data"]["total"] += 2
                    if change == "endless":  # a cursor with nothing behind it
                        answer["data"].update(tasks=[], nextCursor="AAAA")
                    if len(pages) == 1 and change == "delete":  # a read page's task goes
                        del self.fake.tasks["TASK-1"]
                    if len(pages) == 1 and change == "new_bucket":  # TASK-20's bucket appears
                        self.fake.buckets.append({"id": "BKT-NEW", "name": "New",
                                                  "project": "PRJ-HR"})
                    if len(pages) == 2 and change == "twice":
                        answer["data"]["tasks"].append(dict(self.fake.tasks["TASK-1"]))
                    return answer
                self.fake.tool = tool
                unit = self.tasks_unit()
                if change in guards:  # fails closed naming its guard, nothing recorded
                    self.assertEqual((unit["state"], unit["reason"], unit["error"],
                                      unit["records_current"]),
                                     ("in_progress", "source_error", guards[change], 0))
                    continue
                # delete: TASK-1 stays current though MC deleted it; new_bucket: TASK-20 is
                # not keyed unparented by a bucket list that moved; twice: counted once;
                # withheld: MC counts two tasks no page serves this principal.
                self.assertEqual((unit["state"], unit["reason"], unit["records_current"],
                                  unit["dropped"], unit["drops"]),
                                 {"delete": ("partial", "source_changed_during_read", 21, 0,
                                             NO_DROPS),
                                  "new_bucket": ("partial", "source_changed_during_read", 20, 1,
                                                 {**NO_DROPS, "no_bucket_row": 1}),
                                  "twice": ("partial", "source_changed_during_read", 21, 1,
                                            {**NO_DROPS, "other": 1}),
                                  "withheld": ("partial", "withheld_or_dropped", 21, 2,
                                               {**NO_DROPS, "not_permitted": 2})}
                                 [change])

    def test_a_full_context_crossing_the_bound_re_versions_no_task(self):
        # One full read, then (the same tasks) an answer over the bound read as search
        # pages, and back: the run names the tool that read it, the tasks keep one version.
        real, over = self.fake.tool, [False]

        def tool(name, args):
            answer = real(name, args)
            if over[0] and args == {"depth": "full"}:
                answer["data"]["lastSweep"] = "x" * 4_000_001
            return answer
        self.fake.tool = tool
        units, tools = [], []
        for mode in (False, True, True, False):
            over[0] = mode
            units.append(self.tasks_unit())
            tools.append(json.loads(self.store.run(KIND, unit_of("ops", "tasks"))["detail"])[
                "tool"])
        self.assertEqual([(u["state"], u["records_current"], u["new_versions"])
                          for u in units], [("complete", 3, 3)] + [("complete", 3, 0)] * 3)
        self.assertEqual(tools, ["mc_get_context", "mc_search_tasks", "mc_search_tasks",
                                 "mc_get_context"])

    def test_a_search_cursor_holding_dsp_pages_on_unchanged(self):
        # MC's keyset cursor is opaque base64url (task-search.ts searchTaskPage) and may hold
        # "dsp_" by chance: it goes back to MC as MC sent it, never redacted or refused.
        self.big_context(4, tasks=20, description=10)
        self.over_bound()
        self.fake.page_cap = 5
        real, prefix = self.fake.tool, "dsp_Zq9xW2vB7k"

        def tool(name, args):
            if name == "mc_search_tasks" and "cursor" in args:
                if not args["cursor"].startswith(prefix):  # MC: invalid_request
                    return {"error": {"code": "invalid_request", "message": "cursor"}}, True
                args = dict(args, cursor=args["cursor"][len(prefix):])
            answer = real(name, args)
            if name == "mc_search_tasks" and answer["data"]["nextCursor"]:
                answer["data"]["nextCursor"] = prefix + answer["data"]["nextCursor"]
            return answer
        self.fake.tool = tool
        unit = self.tasks_unit()
        self.assertEqual((unit["state"], unit["error"], unit["records_current"]),
                         ("complete", None, 20))
        self.assertEqual(len(self.reads("mc_search_tasks")), 4)
        self.assertNotIn(prefix, json.dumps(self.store.records(
            OPS, KIND, unit_of("ops", "tasks"), limit=1000)))

    def test_event_ids_use_mcs_string_seq_and_checkout_ids_are_redacted_as_mc_does(self):
        # mc_get_context returns tasks unredacted (PLX_MC@236fd50 create-http-server.ts,
        # actions.ts actionGetContext); the capture applies MC's own redactCheckoutIds
        # (read-actions.ts) to the whole answer, so a full id anywhere is stored reduced.
        dsp = "dsp_" + "A1b2C3d4E5f6"
        ref = "dsp_\u2026E5f6"
        gate = self.fake.tasks["TASK-2"]["approvalGates"][0]
        gate["reason"] = f"{gate['reason']} (checkout {dsp})"
        self.fake.tasks["TASK-3"]["description"] = f"resume with {dsp}"
        self.fake.tasks["TASK-1"]["evidence"] = [{"prBodyLine": f"MC-Checkout: {dsp}"}]
        # Bucket and knowledge answers are redacted by the same rule (every capture answer).
        self.fake.buckets[0]["archiveReason"] = f"held by {dsp}"
        real, hit_key = self.fake.tool, [None]

        def tool(name, args):
            answer = real(name, args)
            if name == "mc_search_knowledge":
                answer["data"]["hits"][0]["snippet"] = f"checkout {dsp} landed"
                if hit_key[0]:
                    answer["data"]["hits"][0][hit_key[0]] = 1
            return answer
        self.fake.tool = tool
        result = self.capture(CaptureSpec("ops", ("PLX Agent Workbench",)))
        self.assertEqual([(u["collection"], u["reason"]) for u in result["units"]
                          if u["reason"] == "source_error"], [])
        self.assertEqual(len(result["units"]), 5)
        self.assertIn("mc_get_task", result["tools_called"])
        events = self.store.records(OPS, "mission_control", unit_of("ops", "task_events"),
                                    limit=1000)["items"]
        self.assertIn("TASK-2~100", {e["row"]["event_id"] for e in events})
        self.assertIn("TASK-1~24", {e["row"]["event_id"] for e in events})
        tasks = self.store.records(OPS, "mission_control", unit_of("ops", "tasks"))
        self.assertEqual(len(tasks["items"]), 3)
        self.assertNotIn(dsp, json.dumps(tasks))
        rows = {t["row"]["id"]: t["row"] for t in tasks["items"]}
        self.assertEqual(rows["TASK-3"]["description"], f"resume with {ref}")
        self.assertEqual(rows["TASK-1"]["evidence"], [{"prBodyLine": f"MC-Checkout: {ref}"}])
        gates = self.store.records(OPS, "mission_control", unit_of("ops", "approvals"))
        self.assertEqual([g["row"]["reason"].endswith(f"(checkout {ref})")
                          for g in gates["items"]], [True])
        bucket, hit = (self.store.records(OPS, KIND, unit_of("ops", c))["items"][0]["row"]
                       for c in ("buckets", "knowledge"))
        self.assertEqual((bucket["archiveReason"], hit["snippet"]),
                         (f"held by {ref}", f"checkout {ref} landed"))
        self.assertEqual(self.store.run(KIND, unit_of("ops", "buckets"))["state"], "complete")
        self.assertEqual(self.store.run("mission_control", unit_of("ops", "tasks"))["state"],
                         "complete")
        # MC's rule leaves keys as they are: a usable id there still fails the unit closed,
        # the backstop naming its own guard.
        self.fake.tasks["TASK-3"]["labels"] = {dsp: True}
        self.fake.buckets[0][dsp] = True
        hit_key[0] = dsp
        units = {u["collection"]: u for u in self.capture(CaptureSpec("ops", ("PLX",)))["units"]}
        self.assertEqual({c: (units[c]["reason"], units[c]["error"])
                          for c in ("tasks", "buckets", "knowledge")},
                         dict.fromkeys(("tasks", "buckets", "knowledge"),
                                       ("source_error", "checkout_credential_leaked")))
        self.assertNotIn("approvals", units)
        self.assertNotIn(dsp, json.dumps(self.store.records(OPS, KIND, unit_of(
            "ops", "knowledge"))))

    def test_each_client_guard_that_refuses_an_answer_is_recorded_by_its_code(self):
        # Every client-side guard (integrations/mc.py and this module) names itself in the
        # unit's error, never the bare class (live afddbcdf36f9: "SourceUnavailable").
        real = self.fake.tool
        cases = {"context_lacks_tasks": ("tasks", "mc_get_context", {"data": {"buckets": []}}),
                 "tool_result_not_json": ("tasks", "mc_get_context", "PO100234 <html>"),
                 "tool_result_not_object": ("tasks", "mc_get_context", "[1]"),
                 "task_answer_invalid": ("approvals", "mc_get_task",
                                         {"data": {"task": {"id": "TASK-9"}}}),
                 "buckets_shape_invalid": ("buckets", "mc_list_buckets", {"buckets": []}),
                 "knowledge_lacks_hits": ("knowledge", "mc_search_knowledge",
                                          {"data": {"status": "ok"}})}
        for code, (collection, tool, answer) in cases.items():
            with self.subTest(code):
                self.fake.tool = lambda name, args, tool=tool, answer=answer: (
                    answer if name == tool else real(name, args))
                units = {u["collection"]: u for u in
                         self.capture(CaptureSpec("ops", ("PLX",)))["units"]}
                self.assertEqual((units[collection]["reason"], units[collection]["error"]),
                                 ("source_error", code))

    def test_a_scope_stays_one_mc_view_its_operator_and_service_principal(self):
        """PLX_MC@8cd548a TASK-3008: MC decides the view from the key's service principal
        (and, in shadow mode, the operator email), so a scope is pinned to both before any
        record is saved, and the pin survives a failed cycle."""
        def refused(code, spec=CaptureSpec("ops"), client=None):
            before = len(self.fake.calls)
            with self.assertRaises(CaptureError) as caught:
                self.capture(spec, client)
            self.assertEqual(caught.exception.code, code)
            return self.fake.tool_names()[before:]

        def unit_states(summary):
            return {u["collection"]: (u["state"], u["reason"], u["error"],
                                      u["records_current"]) for u in summary["units"]}

        first = self.capture(CaptureSpec("ops"))
        self.assertEqual(self.fake.tool_names()[0], "mc_search_tasks")
        self.assertEqual(self.reads("mc_search_tasks"), [])  # the principal read only
        self.assertEqual(json.loads(self.store.run(KIND, unit_of("ops", "tasks"))["detail"])[
            "principal"], "sp_mcp_workbench")
        # A cycle whose principal read fails reads nothing else, saves nothing, and keeps
        # the pin: the old run-detail check lost it once the latest runs were paused.
        self.fake.failures["mc_search_tasks"] = ("http", 401)
        self.fake.calls.clear()
        failed = self.capture(CaptureSpec("ops", ("landed cost",)))
        self.assertEqual(self.fake.tool_names(), ["mc_search_tasks"])
        self.assertEqual(unit_states(failed), {
            "tasks": ("in_progress", "source_error", "auth_refused", 3),
            "buckets": ("in_progress", "source_error", "auth_refused", 1),
            "knowledge": ("in_progress", "source_error", "auth_refused", 0)})
        del self.fake.failures["mc_search_tasks"]
        self.assertEqual(refused("scope_operator_mismatch",
                                 client=mc_client(self.fake, "stephen@petrasoap.com")), [])
        # Another key (another principal) would mix two views in one scope: refused after
        # the principal read, before any other read.
        self.fake.principal = "sp_mcp_cos"
        self.assertEqual(refused("scope_principal_mismatch"), ["mc_search_tasks"])
        self.fake.principal = "sp_mcp_workbench"
        again = self.capture(CaptureSpec("ops"))
        self.assertEqual(unit_states(again)["tasks"], ("complete", None, None, 3))
        self.assertEqual([u["new_versions"] for u in again["units"]],
                         [0] * len(first["units"]))
        # An answer naming no valid principal fails closed: the units fail, nothing saved.
        for principal in (None, "", "cos@petrasoap.com", "sp_" + "x" * 61):
            with self.subTest(principal=principal):
                self.fake.principal = principal
                summary = self.capture(CaptureSpec("fresh"))
                self.assertEqual(unit_states(summary), {
                    c: ("in_progress", "source_error", "search_tasks_actor_unbound", 0)
                    for c in ("tasks", "buckets")})
        self.fake.principal = "sp_mcp_workbench"
        self.assertEqual(unit_states(self.capture(CaptureSpec("fresh")))["tasks"],
                         ("complete", None, None, 3))
        # A scope captured before its view was pinned (runs, no pin) has no known
        # principal; a pin the store cannot read is refused too. Neither reads MC.
        self.store.begin_run(KIND, unit_of("legacy", "tasks"), ("mc:legacy:tasks",))
        self.assertEqual(refused("scope_principal_unknown", CaptureSpec("legacy")), [])
        for raw in ("{", "{}", json.dumps({"operator": "x", "principal": "sp_MCP"})):
            with self.subTest(raw=raw):
                self.store.set_state(mc_sources.VIEW_STATE + "bad", raw)
                self.assertEqual(refused("scope_view_invalid", CaptureSpec("bad")), [])

    def test_the_ingestion_worker_runs_an_mc_sources_unit_per_collection(self):
        _tenant, actor, sources = load_sources({
            "schema": "plx-ingest-sources/v1", "tenant": TENANT,
            "actor": {"principals": ["svc:ingest"]},
            "sources": [{"id": "mc-ops", "kind": "mc_sources", "scope": "ops",
                         "knowledge_queries": ["landed cost"], "max_task_reads": 2}]}, {})
        state = Path(self.tmp.name) / "state"
        reader = McCaptureReader(state, audience(), mc_client(self.fake))
        ingestor = Ingestor(state, sources, actor=actor, mc=reader)
        self.addCleanup(ingestor.close)
        units = {u["unit_id"]: u for u in ingestor.run()["units"]}
        self.assertEqual(units["mc-ops.tasks"]["state"], "complete")
        self.assertEqual(units["mc-ops.approvals"]["reasons"],
                         ["cap:max_task_reads", "traversal_in_progress"])
        self.assertEqual(units["mc-ops.knowledge"]["reasons"], ["ranked_search"])
        self.assertEqual(units["mc-ops.tasks"]["counts"]["new"], 3)
        self.assertTrue((state / "mc-capture" / "capture.sqlite").is_file())

    def test_records_no_reader_may_see_for_a_missing_project_grant_are_reported(self):
        add_hr_project(self.fake)
        _tenant, actor, sources = load_sources({
            "schema": "plx-ingest-sources/v1", "tenant": TENANT,
            "actor": {"principals": ["svc:ingest"]},
            "sources": [{"id": "mc-ops", "kind": "mc_sources", "scope": "ops"}]}, {})
        # Every collection granted to the planner, PRJ-OPS too, but PRJ-HR only to someone
        # without the collection grant: PRJ-HR records are captured and readable by nobody.
        grants = LiveSourceAudience(
            {**{f"mc:ops:{c}": ["staff:planner"]
                for c in ("tasks", "approvals", "task_events", "buckets")},
             "mc:ops:project:PRJ-OPS": ["staff:planner"],
             "mc:ops:project:PRJ-HR": ["staff:hr"]}, tenant=TENANT)

        class Opaque:  # an audience that cannot list its principals
            tenant = TENANT

            def allows(self, who, resource):
                return grants.allows(who, resource)

        for name, live in (("grants", grants), ("opaque", Opaque())):
            state = Path(self.tmp.name) / name
            ingestor = Ingestor(state, sources, actor=actor,
                                mc=McCaptureReader(state, live, mc_client(self.fake)))
            self.addCleanup(ingestor.close)
            units = {u["unit_id"]: u for u in ingestor.run()["units"]}
            withheld = {c: [r for r in units[f"mc-ops.{c}"]["reasons"]
                            if r.startswith("withheld:")]
                        for c in ("tasks", "approvals", "task_events", "buckets")}
            if name == "grants":
                self.assertEqual(withheld, {c: ["withheld:project_grant:1"] for c in withheld})
                self.assertEqual(units["mc-ops.tasks"]["counts"]["withheld"], 1)
                self.assertEqual(units["mc-ops.tasks"]["state"], "incomplete")
            else:  # not measured is never reported as nothing withheld
                self.assertEqual(withheld, {c: ["withheld:project_grant:not_measured"]
                                            for c in withheld})


class LiveCheckSpecTests(unittest.TestCase):
    def test_spec_expectations_hold_on_the_injected_run_except_the_live_label(self):
        self.assertEqual(lv.validate_spec(SPEC, stem="mc.sources_capture"), [])
        with tempfile.TemporaryDirectory() as tmp:
            args = [a.replace("{tmp}", tmp) for a in SPEC["command"][3:]]
            out = io.StringIO()
            fake = FakeMc()
            single = io.StringIO()  # one MC project: the per-project probe is not measured
            with contextlib.redirect_stdout(single):
                single_code = main([a.replace("mc-capture", "single") for a in args],
                                   environ=ENV, hub=hub(fake))
            add_hr_project(fake)
            # mc_get_context sends a full checkout id unredacted; the spec still holds.
            fake.tasks["TASK-3"]["description"] = "MC-Checkout: dsp_" + "A1b2C3d4E5f6"
            # The live shape (9826f60e9039, no business rows): the full context is over the
            # byte bound and some visible tasks have no bucket row.
            for n, bucket in ((10, ""), (11, None), (12, "BKT-DELETED")):
                fake.tasks[f"TASK-{n}"] = {"id": f"TASK-{n}", "title": "t", "stage": "backlog",
                                           "bucket": bucket, "approvalGates": []}
            real = fake.tool
            fake.tool = lambda name, args: (
                dict(real(name, args), padding="x" * 4_000_001)
                if name == "mc_get_context" and args == {"depth": "full"} else real(name, args))
            with contextlib.redirect_stdout(out):
                code = main(args, environ=ENV, hub=hub(fake))
            # The per-project probe is not vacuous: without the project check the
            # one-project reader sees the other project and the spec fails.
            leaky = io.StringIO()
            with contextlib.redirect_stdout(leaky), mock.patch.object(
                    CaptureStore, "_item_resources", lambda *a: ()):
                main([a.replace("mc-capture", "leaky") for a in args], environ=ENV,
                     hub=hub(fake))
        document = json.loads(out.getvalue())
        self.assertEqual(document["evidence_label"], "injected-transport")
        self.assertEqual(lv.check_expectations(SPEC, code, document),
                         ["evidence_label != expected"])
        self.assertEqual(document["_summary"]["projects_seen"], 3)  # with unparented
        self.assertEqual(lv.completeness(SPEC, document), {"complete": True})
        one = json.loads(single.getvalue())
        self.assertEqual((one["_summary"]["projects_seen"], one["_summary"]["project_trim"]),
                         (1, "not_measured"))
        self.assertEqual(lv.check_expectations(SPEC, single_code, one),
                         ["evidence_label != expected"])
        self.assertEqual(lv.completeness(SPEC, one),
                         {"complete": False, "incomplete": ["_summary.project_trim"]})
        self.assertIn("_summary.ungranted_project_records != expected",
                      lv.check_expectations(SPEC, 0, json.loads(leaky.getvalue())))
        self.assertTrue(document["_summary"]["audience_trim_ok"])
        # The second pass re-reads unchanged records without a new version.
        self.assertEqual(document["_summary"]["new_versions_per_pass"][1], 0)
        self.assertNotIn(OPERATOR, out.getvalue())
        self.assertNotIn("PO100234", out.getvalue())
        self.assertNotIn("dsp_", out.getvalue())
        # Every registry tool bound to the tasks it read is called (mc_get_context over the
        # bound, then the search pages), and each pass completes the tasks unit.
        self.assertEqual(document["tools_called"], sorted(mc_sources.CAPTURE_TOOLS))
        self.assertEqual([(u["state"], u["dropped"]) for p in document["passes"]
                          for u in p["units"] if u["collection"] == "tasks"],
                         [("complete", 0)] * 2)
        # The drop breakdown the live run records: no task dropped, by any reason.
        self.assertEqual(document["_summary"]["tasks_dropped_by_reason"], NO_DROPS)
        self.assertEqual((document["blocked_upstream"], lv.blocked_parts(document)), ([], []))

    def test_an_mc_side_error_is_recorded_blocked_naming_the_layer_and_ours_failed(self):
        def run(failure, tools=("mc_get_context",)):
            fake = FakeMc()
            add_hr_project(fake)
            fake.failures = {tool: failure for tool in tools}
            out = io.StringIO()
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
                code = main([a.replace("{tmp}", tmp) for a in SPEC["command"][3:]],
                            environ=ENV, hub=hub(fake))
            self.assertNotIn("PO100234", out.getvalue())
            return code, json.loads(out.getvalue())

        code, document = run(("http", 503))
        self.assertEqual(lv.blocked_parts(document), [
            {"part": "tasks", "source": "mission-control-mcp", "error_code": "http_503",
             "cause": "upstream_outage"}])
        # The live verifier then checks only expect_blocked: blocked, never pass or fail.
        self.assertEqual(lv.check_expectations(SPEC, code, document, SPEC["expect_blocked"]),
                         ["evidence_label != expected"])
        self.assertIn("_summary.units_total != expected",
                      lv.check_expectations(SPEC, code, document))
        # Every MC read down: still blocked naming the layers, never failed.
        code, document = run(("http", 503), ("mc_get_context", "mc_list_buckets",
                                             "mc_search_knowledge"))
        self.assertEqual([(p["part"], p["error_code"]) for p in lv.blocked_parts(document)],
                         [("tasks", "http_503"), ("buckets", "http_503"),
                          ("knowledge", "http_503")])
        self.assertEqual(lv.check_expectations(SPEC, code, document, SPEC["expect_blocked"]),
                         ["evidence_label != expected"])
        code, document = run(("rpc", -32602))
        self.assertEqual((code, lv.blocked_parts(document)), (1, []))
        self.assertIn("_summary.units_failed != expected",
                      lv.check_expectations(SPEC, code, document))

    def test_the_spec_command_run_as_main_resolves_handles_and_prints_json(self):
        # The live verifier runs the command as ``__main__`` (the live verifier
        # ENV_GUARD, ``runpy``). Resolving the captured handles loads the capture
        # adapters, which import this module again: two registered ``mission_control``
        # kinds raised ``source_kind_conflict`` after a complete capture, so the run
        # printed no report at all. The hub is injected through the module global, so the
        # run takes the live branch of the label; the records are the fake MC's. MC_BASE_URL
        # is a .invalid host, the only MC host the guard accepts here: were the injection
        # lost, no request could reach the real MC with the test key.
        fake = FakeMc(base="https://mc.workbench-test.invalid")
        add_hr_project(fake)
        transport = hub(fake)
        hosts = frozenset({"mc.workbench-test.invalid"})
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["integrations.mc_sources",
                    *[a.replace("{tmp}", tmp) for a in SPEC["command"][3:]]]
            with mock.patch.object(mc_sources, "HubTransport", lambda **kw: transport), \
                    mock.patch.object(plx_hub_guard, "MC_HOSTS", hosts), \
                    mock.patch.object(mc_module, "MC_HOSTS", hosts), \
                    mock.patch.dict(os.environ, dict(ENV, MC_BASE_URL=fake.base),
                                    clear=True), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(out), \
                    warnings.catch_warnings(), \
                    self.assertRaises(SystemExit) as exit_:
                # In-process only: the verifier's own run imports nothing beforehand.
                warnings.simplefilter("ignore", RuntimeWarning)
                runpy.run_module("integrations.mc_sources", run_name="__main__",
                                 alter_sys=True)
        self.assertEqual(exit_.exception.code, 0, out.getvalue()[:400])
        summary = json.loads(out.getvalue())["_summary"]
        self.assertEqual((summary["units_failed"], summary["resolve_all_ok"],
                          summary["project_trim"], summary["ungranted_project_records"]),
                         (0, True, "measured", 0))
        self.assertGreaterEqual(summary["handles_resolved"], 1)

    def test_an_unusable_environment_and_a_bad_scope_print_a_json_verdict(self):
        # The verifier's own path (ENV_GUARD runs the module as ``__main__``) with its
        # environment shape. The host is one the pack does not name, so the run fails
        # closed before any request and still prints its JSON verdict, never a traceback.
        entry = lv.run_spec(SPEC, environ={"PATH": os.environ.get("PATH", ""),
                                           "MC_MCP_API_KEY": "not-a-real-key",
                                           "MC_BASE_URL": "https://unnamed-host.invalid"})
        self.assertEqual((entry["result"], entry["exit_code"]), ("fail", 1), entry)
        self.assertNotIn("stderr", entry["detail"])
        self.assertEqual((entry["detail"]["fields"]["_summary.units_total"],
                          entry["detail"]["fields"]["requests_sent"]), (0, 0), entry)
        # A configuration error is a code on stdout, not a traceback and not stderr.
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            code = main(["capture", "--store", f"{tmp}/s.sqlite", "--scope", "bad scope"],
                        environ={})
        self.assertEqual((code, json.loads(out.getvalue())),
                         (1, {"ok": False, "error": "mc_scope_invalid",
                              "error_class": "ValueError"}))
        # A ValueError that is not one of this module's codes (a library's, code-shaped
        # or not) carries no message out.
        for message in ("/home/op/secret.json", "op_secret_token_value"):
            out = io.StringIO()
            with mock.patch.object(mc_sources, "run_cli",
                                   mock.Mock(side_effect=ValueError(message))), \
                    contextlib.redirect_stdout(out):
                code = main(["capture", "--store", "x", "--scope", "live"], environ={})
            self.assertEqual((code, json.loads(out.getvalue())),
                             (1, {"ok": False, "error": "configuration_invalid",
                                  "error_class": "ValueError"}))


if __name__ == "__main__":
    unittest.main()
