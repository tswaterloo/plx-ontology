"""Regression tests for Assist UX backend fixes:
1. Assist job card leaks (raw Python exceptions, snake_case codes, broken grammar, raw URIs)
   and dismissing failed/unprepared job pieces.
2. Assist prepared card spacing at source and on read.
3. PO record page Needed by consistency when header requested_date is null and lines carry
   expected_date (page_sql.order, evidence_trail.po_trail, trail.po_trails).
4. /api/insights/planner_open priority warming and brief fill wait avoiding 425 spam.
5. Slow endpoint fixes: /api/brief/weekly first_partial, meeting_followup.people_for caching
   and access_worker routing for my_action_items, and direct door read for records_list.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from operating import assist_job as aj
from operating import assist_matters as am
from operating import evidence_trail as et
from operating import meeting_followup as mf
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api import assist_jobs
from operator_api import assist_routes
from operator_api import autopilot_area
from operator_api import insights as ins
from operator_api import insights_cache as ic
from operator_api import models as m
from operator_api import page_sql
from operator_api import trail as api_trail
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.backend_screens import ScreensMixin
from operator_api.backend_serving import ServingMixin
from operator_api.briefs import BriefsMixin
from operator_api.mock import MockBackend
from operator_api.tests import test_assist_jobs as taj
from operator_api.tests import test_autopilot_area as base_apa
from operator_api.tests import test_insights as ti


class AssistJobSanitizationTests(unittest.TestCase):
    client = base_apa.AutopilotArea.client

    def setUp(self) -> None:
        base_apa.AutopilotArea.setUp(self)
        self.log = taj.Log()
        self.backend.memory = type("M", (), {"pool": self.log})()

    def test_stored_job_leaks_are_sanitized_on_read_and_failed_piece_is_dismissible(self) -> None:
        jid = "a" * 24
        owner = base_apa.OWNER[1]
        aj.append(self.log, jid, owner, "requested", {"text": "Follow up on late POs"})
        aj.append(self.log, jid, owner, "started", {})
        aj.append(self.log, jid, owner, "planned", {
            "summary": "Check the thread (teams-chat://r.7g2urkel4a) and mail://m-42",
            "checklist": [{"text": "Call supplier (teams-chat://r.7g2urkel4a)", "piece": 1}],
            "found": "Looked at record://po-106001 and (graph-mail://box/1)",
            "not_found": "No reply in (teams-channel://ch/9)",
            "nothing": "",
            "pieces": 2,
            "plan_reads": ["teams-chat://r.7g2urkel4a"],
            "fallbacks": [
                "Past cases ranked by newest, not by likeness to the job "
                "(RuntimeError: no_gpu_host); every case stays reachable with the search tool."
            ],
            "unread_reads": [
                {"why": "excluded_in_thread", "chars": 210},
                {"why": "pay_or_hr", "chars": 95},
            ],
        })
        aj.append(self.log, jid, owner, "piece", {
            "n": 1,
            "title": "Follow up on chat (teams-chat://r.7g2urkel4a)",
            "instruction": "Draft message",
            "status": "failed",
            "why": "check_dropped",
            "message": (
                "Assist prepared something, but the second check found nothing in it the "
                "records or the thread support, so it is not shown. Do this piece yourself "
                "or give the job again."
            ),
            "records_unread": ["teams-chat://r.7g2urkel4a"],
            "unread_reads": [{"why": "excluded_in_thread", "chars": 50}],
            "reads": ["teams-chat://r.7g2urkel4a"],
            "tool_reads": [],
        })
        aj.append(self.log, jid, owner, "piece", {
            "n": 2,
            "title": "Check order",
            "instruction": "Check PO",
            "status": "failed",
            "why": "piece_crashed",
            "message": "RuntimeError: database connection dropped",
            "reads": [],
            "tool_reads": [],
        })
        aj.append(self.log, jid, owner, "finished", {})

        res = self.client(base_apa.OWNER).get("/api/assist/jobs")
        self.assertEqual(res.status_code, 200, res.text)
        job = res.json()["jobs"][0]

        # 1. No raw Python exceptions or internal host codes in fallbacks
        self.assertEqual(len(job["fallbacks"]), 1)
        self.assertNotIn("RuntimeError", job["fallbacks"][0])
        self.assertNotIn("no_gpu_host", job["fallbacks"][0])

        # 2. No raw snake_case internal codes in unread_reads
        whys = [r["why"] for r in job["unread_reads"]]
        self.assertEqual(whys, ["access rules", "restricted pay or HR content"])
        self.assertNotIn("excluded_in_thread", str(job["unread_reads"]))

        # 3. No raw internal URIs in user-facing text fields
        for text in (
            job["summary"],
            job["checklist"][0]["text"],
            job["found"],
            job["not_found"],
            job["pieces"][0]["title"],
            job["pieces"][0]["records_unread"][0],
        ):
            for scheme in ("teams-chat://", "teams-channel://", "mail://", "graph-mail://", "record://"):
                self.assertNotIn(scheme, text)
        self.assertEqual(job["checklist"][0]["text"], "Call supplier")
        self.assertEqual(job["pieces"][0]["title"], "Follow up on chat")

        # 4. Fixed grammar on check_dropped and no raw Python exception on piece_crashed
        p1, p2 = job["pieces"]
        self.assertNotIn("nothing in it the records", p1["message"])
        self.assertIn("there wasn't enough verified detail", p1["message"])
        self.assertEqual(p1["unread_reads"][0]["why"], "access rules")
        self.assertNotIn("RuntimeError", p2["message"])
        self.assertNotIn("database connection dropped", p2["message"])

        # 5. Failed/unprepared piece can be dismissed cleanly via decision API
        dismissed = self.client(base_apa.OWNER).post(
            f"/api/assist/jobs/{jid}/pieces/1/decision",
            json={"decision": "dismissed"},
        )
        self.assertEqual(dismissed.status_code, 200, dismissed.text)
        self.assertEqual(dismissed.json()["pieces"][0]["decision"], "dismissed")
        after = self.client(base_apa.OWNER).get("/api/assist/jobs").json()["jobs"][0]
        self.assertEqual(after["pieces"][0]["decision"], "dismissed")

    def test_source_reasons_and_crash_messages_contain_no_raw_exceptions(self) -> None:
        self.assertNotIn("nothing in it the records", aj.REASONS["check_dropped"])
        self.assertIn("there wasn't enough verified detail", aj.REASONS["check_dropped"])
        self.assertEqual(
            assist_jobs.clean_fallback("Past cases ranked by newest (RuntimeError: no_gpu_host)"),
            "Past cases were ordered by most recent rather than similarity; all past cases remain searchable.",
        )


class AssistNoteSpacingTests(unittest.TestCase):
    def test_clean_note_spacing_fixes_punctuation_and_preserves_urls_emails_numbers(self) -> None:
        bad = "This is on Vince Alton's list,not yours.Only you can see this."
        self.assertEqual(
            am.clean_note_spacing(bad),
            "This is on Vince Alton's list, not yours. Only you can see this.",
        )
        preserved = "Total $1,250.50 sent to vince.alton@plx.example via https://plx.example/a,b.Doc"
        self.assertEqual(am.clean_note_spacing(preserved), preserved)

    def test_item_of_and_prepared_sanitize_stored_notes_and_failed_reason(self) -> None:
        now = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.timezone.utc)
        row = {
            "matter_id": "a" * 24,
            "owner_person": "vince-oid",
            "kind": "reply",
            "source": "emails",
            "sender": "buyer@vendor.example",
            "tsender": None,
            "subject": "PO 106001",
            "title": "Reply to vendor",
            "quote": "Can you confirm?",
            "detail": "Follow up today,not tomorrow.Check stock.",
            "next_act": "reply",
            "plan": {},
            "state": "open",
            "verify_note": "",
            "verified_at": now,
            "plan_status": "failed",
            "plan_reason": "RuntimeError: no_gpu_host",
            "plan_at": now,
            "requested_at": None,
            "basis_at": now,
            "due_at": now + dt.timedelta(days=2),
            "touched_at": now,
            "legacy_item_id": None,
            "sources": {},
            "audiences": [],
            "markings": [],
            "handles": [],
        }
        item = am.item_of(row, theirs=True, names={"vince-oid": "Vince Alton"}, now=now)
        self.assertIn("This is on Vince Alton's list, not yours. Only you can see this.", item["note"])
        self.assertNotIn("list,not yours", item["note"])
        self.assertNotIn("yours.Only", item["note"])
        self.assertNotIn("RuntimeError", item["note"])
        self.assertNotIn("no_gpu_host", item["note"])

        # Already-stored bad note passed through assist_routes._prepared is fixed on read
        item["note"] = "Draft ready.\nThis is on Vince Alton's list,not yours.Only you can see this."
        prepared = assist_routes._prepared(item)
        self.assertEqual(
            prepared.note,
            "Draft ready.\nThis is on Vince Alton's list, not yours. Only you can see this.",
        )

        # Already-stored bad note in autopilot_area.alert_of is fixed on read
        alert = autopilot_area.alert_of(
            {"item_id": "ap_1", "autopilot": "late_po", "created_at": "2026-11-25T10:00:00Z"},
            {
                "title": "Late PO",
                "body": {
                    "label": "Late PO",
                    "happened": {"sender": "vendor@example.com"},
                    "prepared": {
                        "summary": "PO 106001 is late",
                        "note": "This is on Vince Alton's list,not yours.Only you can see this.",
                    },
                },
            },
        )
        self.assertIsNotNone(alert)
        self.assertEqual(
            alert.note,
            "This is on Vince Alton's list, not yours. Only you can see this.",
        )


class PoNeededByConsistencyTests(unittest.TestCase):
    def test_page_sql_order_falls_back_to_open_line_expected_date(self) -> None:
        header = {
            "handle": "bc://po/106001",
            "po_number": "106001",
            "supplier": "Acme",
            "supplier_handle": "bc://vendor/1",
            "status": "open",
            "order_date": "2026-06-01",
            "requested_date": None,
            "promised_date": None,
            "verdict": "late",
            "days_late": 153,
            "total": "500.00",
            "total_currency": "USD",
            "withheld": [],
        }
        lines = [
            {
                "item_code": "A1",
                "description": "Closed line",
                "ordered_quantity": "10",
                "received_quantity": "10",
                "expected_date": "2026-05-01",
                "withheld": [],
            },
            {
                "item_code": "A2",
                "description": "Open line",
                "ordered_quantity": "10",
                "received_quantity": "0",
                "expected_date": "2026-06-24",
                "withheld": [],
            },
        ]
        page = page_sql.order(
            backend=None,
            reader=None,
            kind="purchase_order",
            handle="bc://po/106001",
            loaded=(header, lines, []),
        )
        facts = {f.label: f.value for f in page.facts}
        self.assertIn("Needed by (from lines)", facts)
        self.assertEqual(facts["Needed by (from lines)"].day, "2026-06-24")
        self.assertNotIn("Needed by", facts)

        # Hidden line expected_date is never leaked
        hidden_lines = [{**lines[1], "withheld": ["expected_date"]}]
        page_hidden = page_sql.order(
            backend=None,
            reader=None,
            kind="purchase_order",
            handle="bc://po/106001",
            loaded=(header, hidden_lines, []),
        )
        hidden_facts = {f.label: f.value for f in page_hidden.facts}
        self.assertNotIn("Needed by (from lines)", hidden_facts)
        self.assertIn("No date on record", hidden_facts["Needed by"].text or "")

    def test_evidence_trail_emits_due_date_step_when_multi_line_dates_omit_governing_date(self) -> None:
        po_id = "PurchaseOrder:bc.po:106001"
        proc_reply = {
            "meta": {"as_known_at": "2026-11-24"},
            "lateness": [{
                "root": po_id,
                "state": "late",
                "late": True,
                "days_late": 153,
                "measured_on": "2026-11-24",
                "measured_against": "today",
                "governing": "expected",
                "governing_date": None,
                "values": [
                    {
                        "property": et.LINE_EXPECTED,
                        "value": "2026-06-10",
                        "citation": {"l1_handle": "bc://po_line/1"},
                    },
                    {
                        "property": et.LINE_EXPECTED,
                        "value": "2026-06-24",
                        "citation": {"l1_handle": "bc://po_line/2"},
                    },
                ],
            }],
        }

        def fake_call(name: str, args: dict) -> dict:
            if name == "plx.process":
                return proc_reply
            if name == "plx.object":
                return {"object": {"display": "PO 106001"}, "values": []}
            return {"objects": [], "events": [], "hits": []}

        got = et.po_trail(fake_call, po_id)
        due_steps = [s for s in got["steps"] if s["kind"] == "due_date"]
        self.assertEqual(len(due_steps), 1)
        self.assertIn("2026-06-24", due_steps[0]["title"])
        self.assertEqual(got["due"]["date"], "2026-06-24")

    def test_trail_po_trails_adds_cited_due_date_from_late_list_when_missing(self) -> None:
        po_id = "PurchaseOrder:bc.po:106001"
        raw = {
            "subject": po_id,
            "label": "PO 106001",
            "state": "late",
            "days_late": 153,
            "verdict": "Late: open on 2026-11-24 (153 days).",
            "steps": [],
            "hidden": 0,
            "may_hide": False,
            "gaps": [],
        }
        late_po = m.LatePo(
            object_id=po_id,
            label="PO 106001",
            due_date="2026-06-24",
            days_late=153,
            reason="Needed by 2026-06-24, still open",
            open_url="/object/PurchaseOrder/106001",
            sources=[m.Source(system="BC", handle="bc://po/106001", label="PO 106001")],
        )

        class FakeView:
            class access:
                runtime = None
                actor = None

                @staticmethod
                def decide(handles):
                    return {h: None for h in handles}

            @staticmethod
            def _call(name, args):
                return {}

        class FakeBackend:
            @staticmethod
            def _source(_view, handle, openable=None):
                return m.Source(system="BC", handle=handle, label=handle)

        mapper = api_trail.Mapper(
            FakeBackend(),
            FakeView(),
            object_url=lambda k, o: f"/object/{k}/{o}",
            open_url=lambda h: f"/source?h={h}",
        )
        with mock.patch.object(api_trail, "stored_trail", return_value=raw):
            ev = api_trail.po_trails(mapper, FakeView(), [po_id], late={po_id: (late_po, "2026-11-24")})
        self.assertIsNotNone(ev)
        due_steps = [s for s in ev.trails[0].steps if s.kind == "due_date"]
        self.assertEqual(len(due_steps), 1)
        self.assertIn("2026-06-24", due_steps[0].title)
        self.assertEqual(ev.trails[0].due.date, "2026-06-24")


class PlannerOpenAndSlowEndpointsTests(unittest.TestCase):
    def test_planner_open_is_first_in_ahead_refs_and_returns_200_on_fast_background_fill(self) -> None:
        refs = ins.ahead_refs(ins.declared())
        self.assertEqual(refs[0], "builtin:planner_open")

        b = ti.Backend(
            ti.sql_out([["Dana", "Ops", 4, 1, 0, "p1"]],
                       ("person", "department", "open_tasks", "overdue", "overdue_90_days", "person_id")),
            everyone=True,
        )
        b.me = MockBackend().me
        b._insight_keep = ic.Keeper(
            ic.Kept(lambda: 7, fresh=300.0),
            lambda r, sql: b._insight_read(r, sql),
        )
        self.addCleanup(lambda: b._insight_keep.pool().shutdown(wait=True))

        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE)
        app = create_app(b, DevAuth(lambda: session, mode="mock"), secure_cookie=False)
        client = TestClient(app, base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)

        # First call triggers urgent read, waits briefly for fast completion, and returns 200 (not 425)
        res = client.get("/api/insights/planner_open")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["id"], "planner_open")
        self.assertEqual(res.json()["row_count"], 1)

        # A tile whose background read does not finish within the brief wait still returns 425
        slow = threading.Event()
        b._insight_keep.read = lambda r, sql: (slow.wait(5.0), b._insight_read(r, sql))[1]
        self.addCleanup(slow.set)
        with mock.patch.object(ins, "FILL_WAIT_SECONDS", 0.05):
            real_wait = b.wait_filling
            b.wait_filling = lambda r, iid, timeout=0.05: real_wait(r, iid, timeout=0.05)
            res_slow = client.get("/api/insights/team_output")
        self.assertEqual(res_slow.status_code, 425, res_slow.text)
        self.assertEqual(res_slow.json()["error"]["code"], "insight_filling")


class AssistMonitoringFilterAndDedupeTests(unittest.TestCase):
    def test_automated_it_alerts_filtered_and_vince_promise_conversation_deduped_in_assist(self) -> None:
        from operating import assist_readings as ar
        from operator_api import home_needs as hn

        for sender, subject, summary in (
            ("cos@petrasoap.com", "Automated alert: the email-relay job failed on staging", ""),
            ("", "Automated alert: the UAT Business Central sync has not run", ""),
            ("", "UAT BC sync has not run for two days", ""),
            ("", "Portal-to-BC sync has not run for two days", ""),
            ("cos@petrasoap.com", "Automated daily health alert: eight Workbench API staging relations", ""),
            ("", "Daily health check flags eight unavailable Workbench API relations", ""),
            ("", "Daily Workbench API health alert reporting degraded views", ""),
            ("cos@petrasoap.com", "Mission Control PR merge-class gate failed", ""),
        ):
            self.assertTrue(
                ar.is_automated_system_alert(sender=sender, subject=subject, summary=summary),
                f"Expected automated system alert to be filtered: {subject!r}",
            )

        # Vince's same-second promise + conversation raw matters collapse in favor of planned conversation
        t = dt.datetime(2026, 11, 25, 15, 18, 33, tzinfo=dt.timezone.utc)
        promise = {
            "owner": "vince-oid",
            "kind_of_matter": "promise",
            "basis_at": t,
            "subject": "You said: send the 8oz template to Greg",
            "summary": "You said: send the 8oz template to Greg",
            "plan_status": "none",
            "body": "",
        }
        conv = {
            "owner": "vince-oid",
            "kind_of_matter": "conversation",
            "basis_at": t,
            "subject": "Vince has the 8oz template and will send it to Greg.",
            "summary": "Vince has the 8oz template and will send it to Greg.",
            "plan_status": "planned",
            "body": "Sending the 8oz template now.",
        }
        self.assertTrue(hn.are_same_event_raw_matters(promise, conv))
        self.assertGreater(hn.raw_matter_richness(conv), hn.raw_matter_richness(promise))

    def test_priority_pool_runs_urgent_tile_before_queued_measure_tiles(self) -> None:
        gate = threading.Event()
        order: list[str] = []

        def fake_read(_r, sql):
            if sql == "blocker":
                gate.wait(2.0)
            order.append(sql)
            return {"got": {"columns": ["x"], "rows": []}, "read_at": "2026-11-25T00:00:00+00:00"}

        pool = ic._PriorityPool(max_workers=1)
        self.addCleanup(lambda: pool.shutdown(wait=True))
        keeper = ic.Keeper(ic.Kept(lambda: 7, fresh=300.0), fake_read, pool=pool)
        reader = ti.person("employee:owner")
        keeper.ahead(reader, ["blocker"])
        # Wait briefly so worker picks up blocker
        time.sleep(0.05)
        keeper.ahead(reader, ["builtin:measure_revenue", "builtin:measure_orders"])
        keeper.ahead(reader, ["builtin:planner_open"], urgent=True)
        gate.set()
        pool.shutdown(wait=True)
        self.assertEqual(order[1], "builtin:planner_open")

    def test_priority_pool_promotes_already_queued_tile_once_on_urgent_request(self) -> None:
        gate = threading.Event()
        order: list[str] = []

        def fake_read(_r, sql):
            if sql == "blocker":
                gate.wait(2.0)
            order.append(sql)
            return {"got": {"columns": ["x"], "rows": []}, "read_at": "2026-11-25T00:00:00+00:00"}

        pool = ic._PriorityPool(max_workers=1)
        self.addCleanup(lambda: pool.shutdown(wait=True))
        keeper = ic.Keeper(ic.Kept(lambda: 7, fresh=300.0), fake_read, pool=pool)
        reader = ti.person("employee:owner")
        keeper.ahead(reader, ["blocker"])
        time.sleep(0.05)
        # All three queued at background priority first (as insights_list does)
        keeper.ahead(reader, ["builtin:a", "builtin:b", "builtin:measure_target"])
        # User opens or stream requests measure_target urgently while it is still queued
        keeper.ahead(reader, ["builtin:measure_target"], urgent=True)
        gate.set()
        pool.shutdown(wait=True)
        # measure_target jumped ahead of builtin:a and builtin:b and ran exactly once
        self.assertEqual(order, ["blocker", "builtin:measure_target", "builtin:a", "builtin:b"])

    def test_weekly_review_route_runs_under_first_partial(self) -> None:
        seen_partial: list[bool] = []
        backend = MockBackend()
        real_weekly = backend.weekly_review

        def spy_weekly(reader, week):
            from operator_api.serving import FIRST_PARTIAL
            seen_partial.append(bool(FIRST_PARTIAL.get()))
            return real_weekly(reader, week)

        backend.weekly_review = spy_weekly  # type: ignore[method-assign]
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE)
        client = TestClient(
            create_app(backend, DevAuth(lambda: session, mode="mock"), secure_cookie=False),
            base_url="http://testserver",
        )
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        res = client.get("/api/brief/weekly")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(seen_partial, [True])

    def test_meeting_followup_people_for_caches_and_invalidates_on_env_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("integrations.people_exclusion.with_directory", side_effect=lambda g, d: g) as wd:
                env1 = {"PLX_EXCLUDED_PEOPLE_OIDS": "11111111-1111-4111-8111-111111111111"}
                g1 = mf.people_for(tmp, env1)
                g2 = mf.people_for(tmp, env1)
                self.assertIs(g1, g2)
                self.assertEqual(wd.call_count, 1)

                env2 = {"PLX_EXCLUDED_PEOPLE_OIDS": "22222222-2222-4222-8222-222222222222"}
                g3 = mf.people_for(tmp, env2)
                self.assertIsNot(g1, g3)
                self.assertEqual(wd.call_count, 2)

    def test_my_action_items_uses_access_worker_instead_of_reads_queue(self) -> None:
        calls: list[str] = []

        class FastWorker:
            stopped = threading.Event()

            def run(self, _reader, fn, **_kw):
                calls.append("access_worker")
                return frozenset()

        class Host(BriefsMixin):
            access_worker = FastWorker()
            state_dir = None

            def _brief_run(self, *_a, **_k):
                calls.append("brief_run")
                return []

        with tempfile.TemporaryDirectory() as tmp:
            host = Host()
            host.state_dir = Path(tmp)
            reader = SimpleNamespace(key="r", email="dana@plx.example", session=None)
            out = host.my_action_items(reader, None)
            self.assertEqual(out.items, [])
            self.assertEqual(calls, ["access_worker"])

    def test_records_list_with_doors_reads_directly_without_serving_stall(self) -> None:
        serving_called: list[bool] = []

        class DoorHost(ServingMixin):
            doors = object()

            def _serving(self, *_a, **_k):
                serving_called.append(True)
                raise AssertionError("_serving should be bypassed when doors are ready")

        fake_list = m.RecordList(
            view="customers",
            title="Customers",
            table=m.BizTable(key="customers", title="Customers", columns=["Name"], rows=[], total=0),
            column_keys=["name"],
            sort="name",
            direction="asc",
            searchable=True,
            filters=[],
            total=0,
            offset=0,
            more=False,
            next_offset=0,
        )
        with mock.patch.object(page_sql, "on", return_value=True), \
                mock.patch.object(page_sql, "record_list", return_value=fake_list):
            got = DoorHost().records_list(SimpleNamespace(key="r"), "customers")
        self.assertEqual(got.view, "customers")
        self.assertEqual(serving_called, [])

    def test_team_view_warms_planner_open_urgently(self) -> None:
        queued: list[tuple] = []

        class FakeKeeper:
            def ahead(self, reader, sqls, *, urgent=False):
                queued.append((list(sqls), urgent))
                return []

        class TeamHost(ScreensMixin):
            _team_file = None

            def _insight_keeper(self):
                return FakeKeeper()

            def _screen_inputs(self, **_k):
                return lambda: None

            def _serving(self, _r, _name, _read, _inputs=None, reading=None):
                return m.TeamView(as_of="2026-11-25T00:00:00Z", people=[], scope="everyone",
                                  state="ready", note="")

        TeamHost().team_view(SimpleNamespace(key="r"))
        self.assertEqual(queued, [(["builtin:planner_open"], True)])


if __name__ == "__main__":
    unittest.main()
