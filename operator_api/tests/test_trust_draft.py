"""The Trust page (``/api/trust``) and Draft mode for the autopilot.

* Trust: only the owner and the Team readers read it (404 for anyone else); freshness per
  system with its lag, answer scores read from the eval results file ("not measured yet"
  when absent, a tampered entry dropped), alert precision with correct never above
  judged, write switches from the server's flags, and no record of any reader's queue.
* Draft: the configured person's workflows are prepared before their gate passed, for
  that person only (live follow; a replay never drafts); an unknown workflow or
  other_external refuses the whole config; the workflow rows say draft and carry the
  countdown from the counts the write gate admitted (the page and the gate agree).
* Scorecard: a reader's own prepared / accepted / edited / rejected per Eastern day.

SYNTHETIC queue, gate files, scorer output and people. Evidence label: ``isolated-fixture``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import eval_results as results_file
from integrations.people_exclusion import PeopleExclusion
from operating import autopilot as ap
from operating import autopilot_write as aw
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api import models as m
from operator_api import work_log as wl
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend
from operator_api.tests.test_autopilot_area import queued
from operator_api.tests.test_work_log import Guard

OWNER = ("ana@plx.example", "33333333-3333-4333-8333-333333333333")
TEAM = ("lee@plx.example", "44444444-4444-4444-8444-444444444444")
OTHER = ("sam@plx.example", "55555555-5555-4555-8555-555555555555")
ROLE = f"person:{TEAM[0]}"
FLOW = "internal_approval"


class People:
    """The people exclusion as the autopilot reads it: a directory and a clear check."""

    def __init__(self, directory, excluded=(), unavailable=None):
        self.directory, self.excluded, self.unavailable = directory, set(excluded), unavailable

    def identity_clear(self, oid):
        return oid not in self.excluded


class DraftConfig(unittest.TestCase):
    def write(self, doc) -> dict:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))
        self.addCleanup(os.unlink, f.name)
        return {ap.DRAFT_ENV: f.name}

    def test_the_default_config_drafts_the_three_workflows_and_never_other_external(self):
        found = ap.load_draft({})
        self.assertEqual(list(found.values()),
                         [["internal_approval", "mc_question", "teams_chat"]])
        self.assertNotIn("other_external", {f for v in found.values() for f in v})

    def test_an_unknown_or_forbidden_workflow_refuses_the_whole_file(self):
        good = {"schema": ap.DRAFT_SCHEMA, "people": {TEAM[0]: [FLOW]}}
        self.assertEqual(ap.load_draft(self.write(good)), {TEAM[0]: [FLOW]})
        for bad in ({**good, "people": {TEAM[0]: [FLOW], OTHER[0]: ["no_such_flow"]}},
                    {**good, "people": {TEAM[0]: [FLOW, "other_external"]}},
                    {**good, "people": {TEAM[0]: FLOW}},
                    {**good, "schema": "plx-autopilot-draft/v0"},
                    "{not json", [good]):
            with self.subTest(bad=bad):
                self.assertEqual(ap.load_draft(self.write(bad)), {})
        self.assertEqual(ap.load_draft({ap.DRAFT_ENV: "/nonexistent/draft.json"}), {})

    def test_draft_prepares_for_the_named_person_only_and_keeps_the_gate_for_others(self):
        directory = {TEAM[0]: TEAM[1], OTHER[0]: OTHER[1]}
        drafted = ap.draft_owners({TEAM[0]: [FLOW], "nobody@plx.example": [FLOW]},
                                  directory, People(directory))
        self.assertEqual(drafted, {TEAM[1]: frozenset({FLOW})})
        gate, _ = ap.gate_of(None)                        # nothing passed
        drafted_gate = ap.with_draft(gate, drafted)
        self.assertTrue(drafted_gate(TEAM[1], FLOW))
        self.assertTrue(drafted_gate(TEAM[1].upper(), FLOW))
        self.assertFalse(drafted_gate(TEAM[1], "other_external"))
        self.assertFalse(drafted_gate(OTHER[1], FLOW))   # same event, another owner
        # an excluded person or an unloaded exclusion: nothing in draft
        self.assertEqual(ap.draft_owners({TEAM[0]: [FLOW]}, directory,
                                         People(directory, excluded=[TEAM[1]])), {})
        self.assertEqual(ap.draft_owners({TEAM[0]: [FLOW]}, directory,
                                         People(directory, unavailable="down")), {})
        self.assertIs(ap.with_draft(gate, {}), gate)


def ledger(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE source_state (source TEXT, captured_at TEXT, configured INT, "
                 "stale_since TEXT)")
    conn.executemany("INSERT INTO source_state VALUES (?, ?, 1, NULL)", rows)
    conn.commit()
    conn.close()


def scorer_run(root: Path, role: str, *, status="complete", start="2026-11-06T07:00:00Z",
               days=7, judged=12, matched=11, members=(TEAM[1],)) -> Path:
    """A fresh-score run folder as ``fresh_score.sh`` leaves it: per role ``result.json``
    and the run's replay gate counts (``autopilot-gate.json``, held-out split)."""
    run = root / "postgres-gen11-until-2026-11-12"
    slug = role.replace(":", "_").replace("@", "_").replace(".", "_")
    (run / slug).mkdir(parents=True, exist_ok=True)
    (run / slug / "result.json").write_text(json.dumps({
        "schema": "plx-autopilot-replay/v1", "role": role, "status": status,
        "window": [start, "2026-11-12T07:00:00Z"], "data_end": "2026-11-15T07:00:00Z",
        "days_done": days, "splits": {},
        "workflows": {FLOW: {"judged": judged + 5, "matched": matched, "prepared": 20}}}))
    gate = run / ap.GATE_FILE
    doc = json.loads(gate.read_text()) if gate.exists() else {"schema": ap.GATE_SCHEMA,
                                                              "roles": {}}
    doc["roles"][role] = {"members": list(members),
                          "workflows": {FLOW: {"judged": judged, "matched": matched}}}
    gate.write_text(json.dumps(doc))
    return run


class TrustAndDraft(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name)
        self.state = root / "autopilot-write"
        self.state.mkdir()
        path = root / "queue" / ap.QUEUE_FILE
        q = ap.Queue(path)
        with q.conn:
            q.add_item({**queued("ap_secret_item", TEAM[1]), "title": "PO P-77 secret title"})
        q.close()
        (path.parent / ap.GATE_FILE).write_text(json.dumps({"schema": ap.GATE_SCHEMA, "roles": {
            ROLE: {"members": [TEAM[1]], "workflows": {FLOW: {"judged": 33, "matched": 29}}},
            "receivables": {"members": [OTHER[1]],
                            "workflows": {"ar_collections": {"judged": 20, "matched": 15}}}}}))
        draft = root / "draft.json"
        draft.write_text(json.dumps({"schema": ap.DRAFT_SCHEMA,
                                     "people": {TEAM[0]: [FLOW, "teams_chat"]}}))
        guard = PeopleExclusion(["99999999-9999-4999-8999-999999999999"])
        guard.attach_directory([(o, a, []) for a, o in (OWNER, TEAM, OTHER)])
        b = self.backend = MockBackend()
        b.writes_environ = {ap.DRAFT_ENV: str(draft)}
        b.autopilot_queue = path
        b.autopilot_write_state = self.state
        b.autopilot_grant = (lambda aud: True, frozenset(), lambda hs: {h: None for h in hs})
        b.autopilot_owner = lambda reader: reader.session.subject_oid == OWNER[1]
        b._is_owner = lambda reader: reader.session.subject_oid == OWNER[1]
        b.autopilot_guard = guard
        b.autopilot_adapters = {}
        b.state_dir = root / "state"
        ledger(b.state_dir / "host" / "host.sqlite",
               [("bc", "2026-11-11T18:00:00Z"), ("mail", "2026-11-11T18:30:00Z"),
                ("sharepoint", "2026-11-11T17:00:00Z"),
                ("sharepoint_lists", "2026-11-11T12:00:00Z")])
        b.freshness = lambda *a, **k: m.Freshness(sources=[
            m.FreshnessSource(system="BC", as_of="2026-11-11T16:00:00Z", capture="complete"),
            m.FreshnessSource(system="Email", as_of="2026-11-07T06:45:00Z", stale=True,
                              capture="partial")])
        env = mock.patch.dict(os.environ, {"PLX_TEAM_VIEW_READERS": TEAM[0]})
        env.start()
        self.addCleanup(env.stop)
        for name in ("PLX_ACTIONS_LIVE", "PLX_ACTIONS_SENDERS", "PLX_WRITES_LIVE_SYSTEMS",
                     results_file.RESULTS_ENV):
            os.environ.pop(name, None)

    def client(self, who) -> TestClient:
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email=who[0], subject_oid=who[1])
        client = TestClient(create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def test_only_the_owner_and_team_readers_read_it_and_no_record_leaks(self):
        self.assertEqual(self.client(OTHER).get("/api/trust").status_code, 404)
        for who in (OWNER, TEAM):
            got = self.client(who).get("/api/trust")
            self.assertEqual(got.status_code, 200, got.text)
            self.assertNotIn("ap_secret_item", got.text)
            self.assertNotIn("P-77", got.text)
        with mock.patch.dict(os.environ, {"PLX_TEAM_VIEW_READERS": ""}):
            self.assertEqual(self.client(TEAM).get("/api/trust").status_code, 404)
            self.assertEqual(self.client(OWNER).get("/api/trust").status_code, 200)

    def test_the_gate_words_come_from_the_write_rules(self):
        rules = aw.load_rules()
        line = "Autopilot sends and writes"
        page = self.client(TEAM).get("/api/trust").json()
        g = rules["gate"]
        self.assertEqual(page["autopilot_gate"], {"need_judged": int(g["min_judged"]),
                                                  "need_percent": float(g["precision"]) * 100})
        stricter = {**rules, "gate": {**g, "min_judged": 20, "precision": "0.95"}}
        with mock.patch.object(aw, "load_rules", return_value=stricter):
            page = self.client(TEAM).get("/api/trust").json()
        self.assertEqual(page["autopilot_gate"], {"need_judged": 20, "need_percent": 95.0})
        words = next(x["words"] for x in page["lines"] if x["words"].startswith(line))
        self.assertIn("after 20 judged held-out items at 95% or better", words)
        self.assertNotIn("approves", words)
        # API-020: kinds opened to person-approved writes skip the gate, and it says so
        self.backend.writes_environ = {**self.backend.writes_environ,
                                       aw.APPROVED_ENV: "bc_write,send_mail"}
        words = next(x["words"] for x in self.client(TEAM).get("/api/trust").json()["lines"]
                     if x["words"].startswith(line))
        self.assertIn("or for bc write, send mail when a person approves and confirms", words)
        with mock.patch.object(aw, "load_rules", side_effect=aw.RulesInvalid("schema")):
            page = self.client(TEAM).get("/api/trust").json()
        self.assertIsNone(page["autopilot_gate"])
        words = next(x["words"] for x in page["lines"] if x["words"].startswith(line))
        self.assertIn("no workflow acts", words)

    def test_freshness_answers_alerts_and_switches(self):
        page = self.client(OWNER).get("/api/trust").json()
        fresh = {r["system"]: r for r in page["freshness"]}
        self.assertEqual((fresh["BC"]["captured_at"], fresh["BC"]["served_at"],
                          fresh["BC"]["lag_seconds"]),
                         ("2026-11-11T18:00:00Z", "2026-11-11T16:00:00Z", 7200))
        self.assertTrue(fresh["Email"]["stale"])
        # a system is as fresh as its least fresh source: SharePoint's lists at 12:00
        self.assertEqual((fresh["File"]["captured_at"], fresh["File"]["served_at"],
                          fresh["File"]["lag_seconds"]), ("2026-11-11T12:00:00Z", None, None))
        # no results file: no set, the page says held-out is not measured
        self.assertEqual(page["answers"], [])
        self.assertIn("Held-out answer scores: not measured yet.",
                      [line["words"] for line in page["lines"]])
        # alert precision: correct never above judged; an unmeasured type has no numbers
        for a in page["alerts"]:
            if a["judged"] is None:
                self.assertIsNone(a["correct"])
                self.assertIsNone(a["percent"])
            elif a["correct"] is not None:
                self.assertLessEqual(a["correct"], a["judged"])
                self.assertAlmostEqual(a["correct"] * 100 / a["judged"], a["percent"], delta=0.6)
        # 21% of 59 is no whole count: the rate is shown, no invented count of items
        rough = next(a for a in page["alerts"] if a["type"] == "customer_order_late")
        self.assertEqual((rough["judged"], rough["correct"], rough["percent"]), (59, None, 21.0))
        for a in page["alerts"]:
            self.assertFalse(a["on"] and (a["judged"] or 0) < 15, a)
        late = next(a for a in page["alerts"] if a["type"] == "po_late")
        self.assertEqual((late["judged"], late["correct"], late["on"]), (15, 15, True))
        # every write switch off by default; read-only systems have none
        states = {s["system"]: s["state"] for s in page["switches"]}
        self.assertEqual(states["Email and Teams messages"], "off")
        self.assertEqual({states[k] for k in ("Business Central", "SharePoint", "Planner",
                                               "Calendar")}, {"off"})
        self.assertEqual(states["Mission Control"], "none")
        self.backend.writes_environ = {**self.backend.writes_environ,
                                       "PLX_ACTIONS_LIVE": "1",
                                       "PLX_ACTIONS_SENDERS": "a@plx.example",
                                       "PLX_WRITES_LIVE_SYSTEMS": "bc,unknown"}
        states = {s["system"]: s["state"] for s in
                  self.client(OWNER).get("/api/trust").json()["switches"]}
        self.assertEqual((states["Email and Teams messages"], states["Business Central"],
                          states["Planner"]), ("on", "on", "off"))

    def test_answer_scores_come_from_the_results_file_and_a_tampered_entry_is_dropped(self):
        path = self.backend.state_dir / results_file.RELATIVE
        at = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
        outcomes = {"runs": 40, "correct": 31, "answered": 36, "unscored": 0}
        results_file.record(path, "ask_general", "dev", outcomes, items=20,
                            evidence_label="live-captures", at=at)
        results_file.record(path, "ask_general", "held_out",
                            {**outcomes, "correct": 9, "answered": 10, "runs": 12}, items=12,
                            evidence_label="live-captures", at=at)
        results_file.record(path, "hard42", "dev", outcomes, items=20,
                            evidence_label="isolated-fixture", at=at)
        with self.assertRaises(ValueError):
            results_file.record(path, "x", "test", outcomes, items=1,
                                evidence_label="live-captures")
        with self.assertRaises(ValueError):
            results_file.record(path, "x", "dev", {**outcomes, "correct": 37}, items=1,
                                evidence_label="live-captures")
        doc = json.loads(path.read_text())
        doc["sets"]["hard42"]["dev"]["accuracy"] = "0.990"   # not what its counts say
        path.write_text(json.dumps(doc))
        sets = {s["name"]: s for s in self.client(TEAM).get("/api/trust").json()["answers"]}
        self.assertEqual(sorted(sets), ["ask_general"])
        dev, held = sets["ask_general"]["dev"], sets["ask_general"]["held_out"]
        self.assertEqual((dev["accuracy"], dev["coverage"], dev["runs"], dev["at"]),
                         (86.1, 90.0, 40, "2026-11-10T15:00:00Z"))
        self.assertEqual((held["accuracy"], held["items"]), (90.0, 12))

    def test_draft_rows_and_the_fresh_day_countdown(self):
        rules = aw.load_rules()
        # nothing admitted yet: no countdown, never a number
        rows = [w for w in self.client(OWNER).get("/api/autopilot").json()["workflows"]
                if w["role"] == ROLE]
        self.assertEqual({w["workflow"]: (w["draft"], w["countdown"]) for w in rows},
                         {FLOW: (True, None), "teams_chat": (True, None)})
        # the scorer's run: one role complete and fresh, one partial, one tuned-on window,
        # admitted into the write gate's own counts file (what the gate reads)
        run = scorer_run(self.root / "fresh", ROLE)
        scorer_run(self.root / "fresh", "receivables", status="partial", members=(OTHER[1],))
        scorer_run(self.root / "fresh", "owner", start="2026-10-26T07:00:00Z")
        out = aw.record_scores(run, self.state / aw.WRITE_GATE_FILE, rules)
        self.assertEqual(out, {"admitted": [ROLE], "refused": {
            "owner": "not_held_out", "receivables": "status:partial"}})
        team = self.client(TEAM).get("/api/autopilot").json()["workflows"]
        row = next(w for w in team if w["role"] == ROLE and w["workflow"] == FLOW)
        self.assertEqual((row["draft"], row["state"], row["dev"]["percent"]),
                         (True, "dry_run", 87.9))
        # the gate's counts (12 judged held-out), not result.json's whole-window 17
        self.assertEqual({k: row["countdown"][k] for k in (
            "judged", "matched", "need_judged", "need_percent", "percent", "passes",
            "data_end")},
            {"judged": 12, "matched": 11, "need_judged": 15, "need_percent": 90.0,
             "percent": 91.7, "passes": False, "data_end": "2026-11-15T07:00:00Z"})
        # a reader outside the role sees none of it
        self.assertEqual([w["role"] for w in self.client(OTHER).get("/api/autopilot")
                          .json()["workflows"]], ["receivables"])
        # the Trust page shows the same rows and says draft is live
        page = self.client(TEAM).get("/api/trust").json()
        self.assertIn(FLOW, {w["workflow"] for w in page["autopilot"] if w["draft"]})
        self.assertTrue(any(line["state"] == "live" and line["words"].startswith("Draft:")
                            for line in page["lines"]))
        # counts that do not add up are refused, the admitted entry kept
        bad = scorer_run(self.root / "bad", ROLE, judged=3, matched=4)
        self.assertEqual(aw.record_scores(bad, self.state / aw.WRITE_GATE_FILE, rules)
                         ["refused"], {ROLE: "counts"})
        self.assertEqual(aw.gate_counts(self.state, rules)[ROLE]["workflows"][FLOW]
                         ["matched"], 11)
        # the refusal shows beside the older counts it did not replace
        row = next(w for w in self.client(TEAM).get("/api/autopilot").json()["workflows"]
                   if w["role"] == ROLE and w["workflow"] == FLOW)
        self.assertEqual((row["countdown"]["matched"], row["countdown"]["run"],
                          row["countdown"]["refused"], row["countdown"]["refused_run"]),
                         (11, run.name, "counts", bad.name))
        # 15 judged at 14 passes on the countdown and the gate alike; 13 fails both
        for matched, passes in ((14, True), (13, False)):
            good = scorer_run(self.root / f"m{matched}", ROLE, judged=15, matched=matched)
            aw.record_scores(good, self.state / aw.WRITE_GATE_FILE, rules)
            row = next(w for w in self.client(TEAM).get("/api/autopilot").json()["workflows"]
                       if w["role"] == ROLE and w["workflow"] == FLOW)
            self.assertEqual(row["countdown"]["passes"], passes)
            self.assertIsNone(row["countdown"]["refused"])     # admitted: refusal cleared
            self.assertEqual((ROLE, FLOW) in aw.WriteGate(
                self.state / aw.WRITE_GATE_FILE, rules).passing(), passes)
            # admitting counts switched nothing to live: the owner's flag is still unset
            self.assertNotEqual(row["state"], "live")
        # unreadable write rules: the page still answers, says nothing is performed and
        # shows no countdown (review 81cfd5a948: it raised before)
        with mock.patch.object(aw, "load_rules", side_effect=aw.RulesInvalid("schema")):
            got = self.client(TEAM).get("/api/autopilot")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertIn("nothing is performed", json.dumps(got.json()))
        self.assertTrue(all(w["countdown"] is None for w in got.json()["workflows"]))
        # an unreadable gate file: no countdown, and the gate passes nothing
        (self.state / aw.WRITE_GATE_FILE).write_text("{not json")
        row = next(w for w in self.client(TEAM).get("/api/autopilot").json()["workflows"]
                   if w["role"] == ROLE and w["workflow"] == FLOW)
        self.assertIsNone(row["countdown"])
    def test_the_scorer_admits_a_run_through_the_repo_script(self):
        """tools/autopilot_admit_scores.sh: the scorer's one admission step. A complete fresh
        run lands in the gate's counts; a run without its gate counts admits nothing."""
        import subprocess
        import sys
        script = Path(__file__).resolve().parents[2] / "tools" / "autopilot_admit_scores.sh"
        env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
        empty = self.root / "empty-run"
        empty.mkdir()
        got = subprocess.run(["bash", str(script), str(empty), str(self.state)], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(got.returncode, 2, got.stdout + got.stderr)
        self.assertFalse((self.state / aw.WRITE_GATE_FILE).exists())
        run = scorer_run(self.root / "fresh", ROLE)
        scorer_run(self.root / "fresh", "receivables", status="partial", members=(OTHER[1],))
        got = subprocess.run(["bash", str(script), str(run), str(self.state)], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(json.loads(got.stdout)["admitted"], [ROLE])
        counts = aw.gate_counts(self.state, aw.load_rules())
        self.assertEqual(sorted(counts), [ROLE])
        self.assertEqual(json.loads((self.state / aw.WRITE_GATE_FILE).read_text())
                         ["refused"]["receivables"]["reason"], "status:partial")

    def test_a_draft_reply_with_live_send_on_needs_its_confirm(self):
        """Review 20dcbd014b: a Draft item (prepared for the drafted person before any gate
        passed) accepted while live send is on: a single accept answers a confirm token and
        sends nothing, a bulk accept is refused, someone else's confirm sends nothing, the
        owner's confirm sends once and a repeat never sends twice."""
        from operating import autopilot_write_adapters as wa

        class Sender:
            calls = 0

            def dispatch(self, kind, sender, payload):
                Sender.calls += 1
                return {"status": "sent", "http_status": 202, "missing": []}

        class FromMailbox(wa.MailAdapter):
            def request(self, action):
                return super().request({**action, "target": {"sender": TEAM[0]}})

        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.add_item({**queued("ap_draft", TEAM[1]), "autopilot": "approval_request",
                        "title": "Approval asked", "body": {
                            "label": "Approval asked",
                            "happened": {"event_type": "message.received",
                                         "object_type": "Email", "sender": OTHER[0],
                                         "subject": "Approve PO P-9?"},
                            "prepared": {"summary": "Approve PO P-9", "gaps": [], "draft": {
                                "channel": "email", "to": [{"address": OTHER[0]}],
                                "subject": "RE: Approve PO P-9", "body": "Approved."}}}})
        q.close()
        live = {"PLX_ACTIONS_LIVE": "1", "PLX_ACTIONS_SENDERS": TEAM[0],
                aw.APPROVED_ENV: "send_mail", aw.MASTER_ENV: "1"}
        self.backend.writes_environ = {**self.backend.writes_environ, **live}
        self.backend.autopilot_adapters = {
            "send_mail": FromMailbox(Sender(), environ=self.backend.writes_environ)}
        lee = self.client(TEAM)
        listed = {i["item_id"]: i for i in lee.get("/api/autopilot").json()["items"]}
        self.assertEqual((listed["ap_draft"]["workflow"], listed["ap_draft"]["live"],
                          listed["ap_draft"]["live_kinds"]),
                         (FLOW, True, ["send_mail"]))
        url = "/api/autopilot/items/ap_draft/decision"
        bulk = lee.post(url, json={"decision": "accept", "bulk": True})
        self.assertEqual((bulk.status_code, Sender.calls), (409, 0), bulk.text)
        first = lee.post(url, json={"decision": "accept"})
        self.assertEqual(first.status_code, 200, first.text)
        token = first.json()["confirm_token"]
        self.assertTrue(token)
        self.assertEqual((first.json()["state"], Sender.calls), ("waiting", 0))
        # the app owner holding the token is not the item's owner: nothing is sent
        other = self.client(OWNER).post(url, json={"decision": "accept",
                                                   "confirm_token": token})
        self.assertEqual((other.status_code, Sender.calls), (404, 0), other.text)
        done = lee.post(url, json={"decision": "accept", "confirm_token": token})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual((done.json()["decision"]["outcome"], Sender.calls), ("done", 1))
        lee.post(url, json={"decision": "accept", "confirm_token": token})
        self.assertEqual(Sender.calls, 1)


class Scorecard(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
        self.guard = Guard()
        self.store = wl.WorkLog(None, guard=lambda: self.guard, clock=lambda: self.now)

    def reader(self, who):
        return type("R", (), {"key": f"plx:staff:{who}", "display_name": who.title(),
                              "email": f"{who}@plx.example",
                              "session": type("S", (), {"identities": frozenset(),
                                                        "subject_oid": None})()})()

    def put(self, who, verb, item, at):
        self.store.clock = lambda: at
        self.store.record(self.reader(who), kind="autopilot", verb=verb, title="t",
                          thread=f"autopilot:{item}", source="autopilot_queue", source_id=item)
        self.store.clock = lambda: self.now

    def test_per_eastern_day_own_items_once_per_verb_at_any_volume(self):
        late = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)   # 10 Nov in Eastern
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.store = wl.WorkLog(None, guard=lambda: self.guard, clock=lambda: self.now)
                for i in range(n):
                    self.put("ana", "prepared", f"a{i}", self.now)
                    self.put("ana", "prepared", f"a{i}", self.now)       # seen twice: once
                    self.put("ana", ("accepted", "edited", "rejected")[i % 3], f"a{i}", self.now)
                    self.put("sam", "prepared", f"s{i}", self.now)      # another reader's
                self.put("ana", "prepared", "old", late)
                days = wl.autopilot_days(self.store, reader="plx:staff:ana", days=3)
                self.assertEqual([d["day"] for d in days],
                                 ["2026-11-11", "2026-11-10", "2026-11-09"])
                today = days[0]
                self.assertEqual(today["prepared"], n)
                self.assertEqual(today["accepted"] + today["edited"] + today["rejected"], n)
                self.assertEqual((days[1]["prepared"], days[2]["prepared"]), (1, 0))
        self.guard.unavailable = "not loaded"
        self.assertIsNone(wl.autopilot_days(self.store, reader="plx:staff:ana"))

    def test_the_route_is_the_readers_own(self):
        from operator_api.tests.test_work_log import client_for
        backend = MockBackend()
        backend.work_log = self.store
        self.store.clock = lambda: datetime.now(timezone.utc)
        ana = client_for(backend, "staff:ana")
        self.put("plx:staff:ana".split(":")[-1], "prepared", "x1", datetime.now(timezone.utc))
        got = ana.get("/api/work-log/autopilot-days", params={"days": 2}).json()
        self.assertTrue(got["available"])
        self.assertEqual(len(got["days"]), 2)
        sam = client_for(backend, "staff:sam").get("/api/work-log/autopilot-days").json()
        self.assertEqual(sum(d["prepared"] for d in sam["days"]), 0)
        self.assertEqual(ana.get("/api/work-log/autopilot-days",
                                 params={"days": 61}).status_code, 422)
        self.guard.unavailable = "not loaded"
        self.assertEqual(ana.get("/api/work-log/autopilot-days").json(),
                         {"available": False, "days": []})


if __name__ == "__main__":
    unittest.main()


from operating import proactive_engine as pe  # noqa: E402
from operating import workflows as wf  # noqa: E402
from operating.tests.test_autopilot import NOW, Fixture  # noqa: E402
from operating.tests.test_proactive_engine import OURS_OID  # noqa: E402
from operating.tests.test_proactive_threads import OURS, people  # noqa: E402


class DraftPrepares(Fixture):
    """The autopilot's own run into a queue people read (``autopilot.main``): with no
    workflow over its replay gate, live follow in Draft prepares the configured person's
    workflows and nothing else; with no draft for them nothing is prepared; a replay
    (a past window into the same shared queue) never drafts."""

    def run_into(self, name: str, draft: dict, command: str = "follow") -> int:
        import contextlib
        import io
        texts = ("Please send the COA for lot 9.", "Where is order SO-2? When will it ship?")
        ids = [self.message(70 + n, t) for n, t in enumerate(texts)]
        store = self.w.build(self.dir / f"{name}.sqlite")
        queue = self.dir / name / ap.QUEUE_FILE
        queue.parent.mkdir()
        conf = self.dir / f"{name}-draft.json"
        conf.write_text(json.dumps({"schema": ap.DRAFT_SCHEMA, "people": draft}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.dict(os.environ, {ap.DRAFT_ENV: str(conf)}), \
                mock.patch.object(ap, "window_ids", return_value=ids), \
                mock.patch.object(ap, "layer_ids", return_value=(set(ids), [])), \
                mock.patch.object(pe, "_people", return_value=people()):
            code = ap.main([command, "--store", str(store), "--queue", str(queue)]
                           + (["--until", ap._iso(NOW)] if command == "replay" else []))
        self.assertEqual(code, 0, out.getvalue())
        q = ap.Queue(queue)
        try:
            rows = q.conn.execute("SELECT owner FROM items").fetchall()
        finally:
            q.close()
        self.assertTrue(all(r[0] == OURS_OID for r in rows), rows)
        return len(rows)

    def test_draft_prepares_only_the_configured_workflows(self):
        texts = ("Please send the COA for lot 9.", "Where is order SO-2? When will it ship?")
        flows = sorted({wf.of_item("customer_request", {"subject": "PO", "text": t,
                                                        "side": "customer"}) for t in texts}
                       - ap.DRAFT_NEVER)
        self.assertTrue(flows)
        self.assertGreater(self.run_into("drafted", {OURS: flows}), 0)
        other = sorted(wf.names() - set(flows) - ap.DRAFT_NEVER)[0]
        self.assertEqual(self.run_into("other", {OURS: [other]}), 0)
        self.assertEqual(self.run_into("nobody", {"nobody@plx.example": flows}), 0)
        # the same draft in a replay prepares nothing into the shared queue
        self.assertEqual(self.run_into("replayed", {OURS: flows}, "replay"), 0)
