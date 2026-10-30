"""An Ask session kept with its case and replayed over the same evidence (AGT-02,
``operating.session_replay``).

Evidence label: ``isolated-fixture``. The world is the case console's synthetic fixture
world (its own copy per class: one test changes a captured row); the readers are fixture
employees resolved through the employee resolver; the planner is scripted (it stands in
for a model). The Ask runs through the object workspace's own path (``ObjectWorkspace.ask``
over ``host_access``), the replay through the replaying reader's own ``MCPServer``.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from operating import agent_loop
from operating import session_identity as si
from operating import session_replay
from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE, CaseRuntime
from ontology.core import ValidationError
from operating.cases import CaseNotFound
from operating.employee_permissions import (EmployeePermissionPolicy,
                                            EmployeePermissionResolver, PortalAccessSnapshot)
from operating.mcp_server import MCPServer
from operating.ontology_host import CaseHostTools
from operating.tests.test_agent_loop import model
from operating.tests.test_mcp_http_host import RULES, USERS, EmployeeDoorTestCase
from operating.tests.test_session_identity import (EMAIL_A, EMAIL_B, OID_A, OID_B,
                                                   identity_files, user_row)
from operator_ui.case_workspace import CaseWorkspace
from operator_ui.object_workspace import REPLAY_NOTE, ObjectWorkspace, host_access

QUESTION = "Who is purchase order PO5447 ordered from, and where is that recorded?"


def kept_question(runtime, server, case_id: str) -> str:
    """The question the case event kept (the Ask frames it with its object id)."""
    (session,) = runtime.cases.session_records(server.actor, case_id)[1]
    return session["question"]


class SessionReplayTests(EmployeeDoorTestCase):
    def reader(self, oid: str, users=USERS):
        """``(runtime, MCP server)`` of one fixture employee over this class's world."""
        policy, portal = identity_files(self.tmp / f"id-{self.id()}-{oid}-{id(users)}", RULES,
                                        users=users)
        resolver = EmployeePermissionResolver(EmployeePermissionPolicy.from_file(policy),
                                              PortalAccessSnapshot(portal))
        runtime = CaseRuntime(self.state, client=None, audience=self.mirror_audience,
                              tenant="plx", employee_permissions=resolver,
                              require_employee=True)
        self.addCleanup(runtime.close)
        session = si.employee_session(resolver, channel=si.CHANNEL_MCP, object_id=oid)
        server = MCPServer(case_runtime=runtime, actor=session.actor, session=session,
                           ontology=CaseHostTools(runtime, session.actor,
                                                  store_path=self.store))
        self.addCleanup(server.close)
        return runtime, server

    def asked(self, planner=None) -> tuple[CaseRuntime, MCPServer, str, dict]:
        """A asks the workspace about PO5447 on a case A opened; ``(runtime, server, case,
        the Ask's answer)``."""
        runtime, server = self.reader(OID_A)
        actor = server.actor
        found, _ = agent_loop._Session(server).call("plx.find", {"query": "PO5447"})
        object_id = found["candidates"][0]["object"]["object_id"]
        case_id = runtime.cases.open(actor, "Where is PO5447 ordered from?")
        access = host_access(runtime, self.store, actor)
        self.addCleanup(access.close)
        workspace = ObjectWorkspace(access.tools(), actor, access=access,
                                    planner=planner or model([]))
        answer = workspace.ask({"object_id": object_id, "question": QUESTION,
                                "case_id": case_id})
        return runtime, server, case_id, answer

    def workspace(self, runtime: CaseRuntime, server: MCPServer) -> ObjectWorkspace:
        """The reader's object workspace (no planner: it lists and replays, it asks not)."""
        access = host_access(runtime, self.store, server.actor)
        self.addCleanup(access.close)
        return ObjectWorkspace(access.tools(), server.actor, access=access,
                               sources=CaseWorkspace(runtime, server.actor))

    def test_a_recorded_session_replays_identically(self):
        runtime, server, case_id, answer = self.asked()
        self.assertEqual(answer["answer"]["outcome"], "answered", answer["answer"]["reason"])
        self.assertEqual(answer["session"], {"case_id": case_id, "recorded": True,
                                             "session_id": "S1"})
        (kept,) = session_replay.sessions(runtime.cases, server.actor, case_id)
        self.assertEqual([c["tool"] for c in kept["calls"]],
                         [c["tool"] for c in answer["answer"]["tool_calls"]])
        cited = sorted({c["l1_handle"] for claim in answer["answer"]["claims"]
                        for c in claim["citations"]})
        self.assertEqual([(c["handle"], c["status"]) for c in kept["citations"]],
                         [(handle, "unchanged") for handle in cited])
        self.assertIn("session.recorded",
                      [e["kind"] for e in runtime.cases.get(server.actor, case_id)["timeline"]])
        self.assertTrue(runtime.cases.verify_chain(server.actor, case_id))

        replayed = session_replay.replay(runtime.cases, self.reader(OID_A)[1], case_id, "S1")
        self.assertEqual((replayed["replayed"], replayed["identical"], replayed["divergences"],
                          replayed["calls"]),
                         (True, True, [], len(kept["calls"])))
        with self.assertRaises(KeyError):
            session_replay.replay(runtime.cases, server, case_id, "S2")

        # Replay is read-only whatever a record holds: a case write is never run again, and
        # a record of an unknown shape is refused.
        write = {"step": 1, "tool": "plx.case_open", "is_error": False,
                 "arguments": {"question": "opened by a replay?"}, "evidence": []}
        loop = {"outcome": "answered", "reason": None, "question": QUESTION, "claims": [],
                "tool_calls": [write]}
        receipt = session_replay.record(runtime.cases, server.actor, case_id, loop)
        before = len(runtime.cases.list(server.actor))
        replayed = session_replay.replay(runtime.cases, server, case_id, receipt["session_id"])
        self.assertEqual(replayed["divergences"], [{"seq": 0, "step": 1, "tool": "plx.case_open",
                                                    "kind": "tool_not_replayable"}])
        self.assertEqual(len(runtime.cases.list(server.actor)), before)
        with self.assertRaises(ValidationError):
            session_replay.record(runtime.cases, server.actor, case_id,
                                  {**loop, "tool_calls": [{**write, "result": {}}]})

    def test_a_changed_cited_record_is_reported_as_divergence(self):
        runtime, server, case_id, _ = self.asked()
        (kept,) = session_replay.sessions(runtime.cases, server.actor, case_id)
        handle = next(c["handle"] for c in kept["citations"]
                      if c["handle"].startswith("api://public.app_purchase_order"))
        db = sqlite3.connect(self.state / CASE_RUNTIME_DIR / MIRROR_FILE)
        try:
            # The captured row the session cited, changed in place (same handle).
            with db:
                changed = db.execute(
                    "UPDATE row_version SET row_json = json_set(row_json, '$.notes', "
                    "'changed after the session') WHERE relation = ?",
                    ("public.app_purchase_order",)).rowcount + db.execute(
                    "UPDATE row_current SET row_json = json_set(row_json, '$.notes', "
                    "'changed after the session') WHERE relation = ?",
                    ("public.app_purchase_order",)).rowcount
        finally:
            db.close()
        self.assertGreater(changed, 0)
        replayed = session_replay.replay(runtime.cases, self.reader(OID_A)[1], case_id, "S1")
        self.assertFalse(replayed["identical"])
        self.assertIn({"handle": handle, "kind": "citation_content_changed"},
                      replayed["divergences"])
        (now,) = session_replay.sessions(runtime.cases, server.actor, case_id)
        self.assertEqual({c["handle"]: c["status"] for c in now["citations"]}[handle],
                         "citation_content_changed")
        # The workspace's case path lists the session and replays it through the reader's
        # own agent session: the same divergence report.
        workspace = self.workspace(*self.reader(OID_A))
        (listed,) = workspace.case_records(case_id)["sessions"]
        self.assertEqual({key: listed[key] for key in ("session_id", "question", "outcome",
                                                       "call_count", "at")},
                         {"session_id": "S1", "question": kept_question(runtime, server,
                                                                        case_id),
                          "outcome": "answered", "call_count": len(kept["calls"]),
                          "at": listed["at"]})
        self.assertTrue(listed["at"])
        self.assertEqual({c["handle"]: (c["status"], c["openable"])
                          for c in listed["citations"]}[handle],
                         ("citation_content_changed", True))
        shown = workspace.replay({"case_id": case_id, "session_id": "S1"})
        self.assertEqual(shown["note"], REPLAY_NOTE)
        self.assertEqual({key: shown["replay"][key] for key in ("replayed", "identical",
                                                                "calls")},
                         {"replayed": True, "identical": False,
                          "calls": len(kept["calls"])})
        self.assertIn({"handle": handle, "kind": "citation_content_changed", "openable": True},
                      shown["replay"]["divergences"])
        for bad in ({"case_id": case_id}, {"case_id": case_id, "session_id": "S1", "x": 1}):
            with self.assertRaises(ValueError):
                workspace.replay(bad)
        with self.assertRaises(KeyError):
            workspace.replay({"case_id": case_id, "session_id": "S9"})
        # A cited record this console withholds from the reader (its grant and the case
        # store's disagree) withholds the whole session: listed as its marker, not replayed.
        workspace.access.decide = lambda handles: {
            h: ({"state": "withheld"} if h == handle else None) for h in handles}
        opened = []
        workspace.access.agent_server = lambda: opened.append(1)
        marker = {"session_id": "S1", "seq": listed["seq"], "withheld": session_replay.WITHHELD}
        self.assertEqual(workspace.case_records(case_id)["sessions"], [marker])
        self.assertEqual(workspace.replay({"case_id": case_id, "session_id": "S1"})["replay"],
                         {"session_id": "S1", "replayed": False,
                          "withheld": session_replay.WITHHELD})
        self.assertEqual(opened, [])

    def test_a_reader_who_cannot_read_the_cited_evidence_gets_no_replay(self):
        runtime, server, case_id, answer = self.asked()
        cited = [c["l1_handle"] for claim in answer["answer"]["claims"]
                 for c in claim["citations"]]
        # A again after losing purchasing (quality only): the same employee principal, so
        # the case is theirs, but the session's cited purchasing rows no longer resolve.
        revoked = [user_row(OID_A, EMAIL_A, "MANAGER", ("qa",)), USERS[1]]
        for oid, users in ((OID_A, revoked), (OID_B, USERS)):
            runtime_r, server_r = self.reader(oid, users)
            calls = []
            handle = server_r.handle
            server_r.handle = lambda message, _h=handle, **kw: (calls.append(message),
                                                                _h(message, **kw))[1]
            if oid == OID_B:   # another employee: not in the case's audience at all
                for read in (lambda: session_replay.sessions(runtime_r.cases,
                                                             server_r.actor, case_id),
                             lambda: session_replay.replay(runtime_r.cases, server_r,
                                                           case_id, "S1")):
                    with self.assertRaises(CaseNotFound):
                        read()
                workspace = self.workspace(runtime_r, server_r)
                for read in (lambda: workspace.case_records(case_id),
                             lambda: workspace.replay({"case_id": case_id,
                                                       "session_id": "S1"})):
                    with self.assertRaises(KeyError):   # the console answers 404
                        read()
                self.assertEqual(calls, [])
                continue
            visible = session_replay.sessions(runtime_r.cases, server_r.actor, case_id)
            self.assertEqual(visible, [{"session_id": "S1", "seq": visible[0]["seq"],
                                        "withheld": session_replay.WITHHELD}])
            replayed = session_replay.replay(runtime_r.cases, server_r, case_id, "S1")
            self.assertEqual(replayed, {"session_id": "S1", "replayed": False,
                                        "withheld": session_replay.WITHHELD})
            self.assertEqual(calls, [])  # no tool call ran for the restricted reader
            # The workspace's case path shows them the same marker and replays nothing: no
            # agent session is even opened for them.
            workspace = self.workspace(runtime_r, server_r)
            sessions_opened = []
            workspace.access.agent_server = lambda: sessions_opened.append(1)
            listed = workspace.case_records(case_id)["sessions"]
            self.assertEqual(listed, visible)
            shown = workspace.replay({"case_id": case_id, "session_id": "S1"})["replay"]
            self.assertEqual(shown, replayed)
            self.assertEqual(sessions_opened, [])
            self.assertFalse([h for h in cited if h in json.dumps([visible, replayed, listed,
                                                                   shown])])
            self.assertNotIn("PO5447", json.dumps([listed, shown]))

    def test_a_co_reader_who_cannot_read_the_object_in_view_sees_nothing_of_the_session(self):
        def abstain(request):  # reads, then cites nothing
            if not request["transcript"]:
                return {"action": "call", "calls": [{"tool": "plx.find",
                                                     "arguments": {"query": "PO5447"}}]}, None
            return {"action": "abstain", "reason": "not enough"}, None

        runtime, server, case_id, answer = self.asked(abstain)
        self.assertEqual((answer["answer"]["outcome"], answer["session"]["recorded"]),
                         ("abstained", True))
        object_id = answer["object_id"]
        (own,) = session_replay.sessions(runtime.cases, server.actor, case_id)
        self.assertIn(object_id, own["question"])
        # B holds quality at A's clearance: in the case's audience, no purchasing.
        users = [USERS[0], user_row(OID_B, EMAIL_B, "MANAGER", ("qa",))]
        runtime_b, server_b = self.reader(OID_B, users)
        runtime_b.cases.get(server_b.actor, case_id)  # B reads the case itself
        calls = []
        handle = server_b.handle
        server_b.handle = lambda message, **kw: (calls.append(message), handle(message, **kw))[1]
        visible = session_replay.sessions(runtime_b.cases, server_b.actor, case_id)
        self.assertEqual(visible, [{"session_id": "S1", "seq": visible[0]["seq"],
                                    "withheld": session_replay.WITHHELD}])
        replayed = session_replay.replay(runtime_b.cases, server_b, case_id, "S1")
        self.assertEqual(replayed, {"session_id": "S1", "replayed": False,
                                    "withheld": session_replay.WITHHELD})
        self.assertEqual(calls, [])
        shown = json.dumps([visible, replayed])
        for secret in (object_id, "PO5447", "plx.find"):
            self.assertNotIn(secret, shown)


class SessionReplayReportAssertionTests(unittest.TestCase):
    """DOC-08 / ING-08 assertions of ``scripts.object_workspace_probe`` on synthetic
    inputs: the pass rule, the re-sync's duplicate accounting and its typed
    ``not_run`` / ``not_exercised`` states, and the live spec's own expectations."""

    SPEC = "session.replay"

    def base(self, **over):
        report = {"sessions_recorded": 1, "replay_identical": 1, "divergences": 0,
                  "recorder_withheld": 0, "withheld_reader_markers": 1,
                  "withheld_reader_shown": 0, "withheld_replayed": 0,
                  "withheld_sessions_opened": 0, "withheld_leaks": 0, "ask_errors": 0,
                  "budget_exhausted": False, "outsider_not_found": True,
                  "reextraction": {"new_version": 1, "changed_earlier": 0,
                                   "bytes_changed": 0, "refused": 0},
                  "resync": {"duplicate_rows": 0, "not_incremental": 0}}
        report.update(over)
        return report

    def test_passes_only_with_a_new_version_and_no_changed_earlier_citation(self):
        from scripts import object_workspace_probe as probe
        self.assertTrue(probe.session_replay_passed(self.base()))
        for field, value in (("new_version", 0), ("new_version", 2), ("changed_earlier", 1),
                             ("bytes_changed", 1), ("refused", 1)):
            with self.subTest(field=field, value=value):
                report = self.base()
                report["reextraction"] = {**report["reextraction"], field: value}
                self.assertFalse(probe.session_replay_passed(report))

    def test_a_resync_that_duplicates_rows_or_is_not_incremental_fails(self):
        from scripts import object_workspace_probe as probe
        for field in ("duplicate_rows", "not_incremental"):
            report = self.base(resync={"duplicate_rows": 0, "not_incremental": 0, field: 1})
            self.assertFalse(probe.session_replay_passed(report))

    def test_the_spec_refuses_each_failure(self):
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        spec = wiring.spec(self.SPEC)
        good = {"session_replay": {**self.base(), "reextraction": self.base()["reextraction"],
                                   "divergences_by_kind": {}, "resync": {
                                       "status": "measured", "duplicate_rows": 0,
                                       "not_incremental": 0}},
                "sources": {}, "blocked_upstream": []}
        failures = lv.check_expectations(spec, 0, good)
        self.assertFalse([f for f in failures if "reextraction" in f or "resync" in f],
                         failures)
        for path, value in (("reextraction.changed_earlier", 1), ("reextraction.new_version", 2),
                            ("reextraction.bytes_changed", 1), ("resync.duplicate_rows", 3),
                            ("resync.not_incremental", 1)):
            with self.subTest(path=path):
                bad = json.loads(json.dumps(good))
                group, name = path.split(".")
                bad["session_replay"][group][name] = value
                self.assertTrue([f for f in lv.check_expectations(spec, 0, bad) if path in f])

    def mirror_db(self, path, *, rows=10, watermark="2026-01-01T00:00:00Z"):
        with sqlite3.connect(path) as db:
            db.executescript(
                "CREATE TABLE relation_state (relation TEXT PRIMARY KEY, row_count INTEGER, "
                "watermark TEXT, since_column TEXT, key_unique INTEGER);"
                "CREATE TABLE capture (capture_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "relation TEXT, mode TEXT, rows_received INTEGER, keys_new INTEGER, "
                "keys_changed INTEGER, keys_unchanged INTEGER);")
            db.execute("INSERT INTO relation_state VALUES ('s.t', ?, ?, 'updated', 1)",
                       (rows, watermark))
        sqlite3.connect(path).close()

    def resync(self, tmp, *, grow, new, received, unchanged, mode="incremental", env=True):
        import os
        from unittest import mock
        from scripts import object_workspace_probe as probe
        path = os.path.join(tmp, "m.sqlite")
        self.mirror_db(path)

        class FakeMirror:
            def __init__(inner, db, *a, **k):
                inner.db = db

            def __enter__(inner):
                return inner

            def __exit__(inner, *a):
                return False

            def sync(inner, actor, relations, full, budget_seconds=None):
                with sqlite3.connect(inner.db) as db:
                    db.execute("UPDATE relation_state SET row_count = row_count + ?", (grow,))
                    db.execute("INSERT INTO capture (relation, mode, rows_received, keys_new, "
                               "keys_changed, keys_unchanged) VALUES ('s.t', ?, ?, ?, 0, ?)",
                               (mode, received, new, unchanged))

        environ = {"PLX_WORKBENCH_API_URL": "https://portal.invalid",
                   "PLX_WORKBENCH_API_KEY": "plxwb_" + "x" * 20} if env else {}
        with mock.patch("integrations.source_mirror.SourceMirror", FakeMirror):
            report = probe.check_resync(path, environ)
        return path, report

    def test_overlap_rows_re_read_with_no_growth_add_no_duplicates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path, report = self.resync(tmp, grow=0, new=0, received=4, unchanged=4)
            self.assertEqual((report["status"], report["incremental"], report["overlap_rows"],
                              report["duplicate_rows"]), ("measured", 1, 4, 0))
            with sqlite3.connect(path) as db:   # the shared mirror was never written
                self.assertEqual(db.execute("SELECT COUNT(*) FROM capture").fetchone()[0], 0)

    def test_growth_beyond_new_keys_and_unaccounted_rows_are_duplicates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.resync(tmp, grow=3, new=1, received=4, unchanged=4)
            self.assertEqual(report["duplicate_rows"], 2 + 0)
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.resync(tmp, grow=0, new=0, received=6, unchanged=4)
            self.assertEqual(report["duplicate_rows"], 2)

    def test_a_full_traversal_instead_of_an_incremental_read_is_not_incremental(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.resync(tmp, grow=0, new=0, received=10, unchanged=10,
                                    mode="full_traversal")
            self.assertEqual((report["incremental"], report["not_incremental"]), (0, 1))

    def test_no_portal_is_not_run_and_no_watermarked_relation_is_not_exercised(self):
        import os
        import tempfile
        from scripts import object_workspace_probe as probe
        with tempfile.TemporaryDirectory() as tmp:
            _, report = self.resync(tmp, grow=0, new=0, received=0, unchanged=0, env=False)
            self.assertEqual(report["status"], "not_run")
            path = os.path.join(tmp, "empty.sqlite")
            self.mirror_db(path, watermark=None)
            env = {"PLX_WORKBENCH_API_URL": "https://portal.invalid",
                   "PLX_WORKBENCH_API_KEY": "plxwb_" + "x" * 20}
            self.assertEqual(probe.check_resync(path, env)["status"], "not_exercised")


if __name__ == "__main__":
    unittest.main()
