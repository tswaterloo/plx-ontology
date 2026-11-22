"""The Team view routes (who sees whom: the owner and the named Team readers see everyone,
everyone else only their own row, the owner is in no people view for anyone, the people
exclusion fails closed) and the R&D board route, through the app and the real backend (its
reader's view a stub returning a synthetic trimmed alert list), with the real people
exclusion. SYNTHETIC fixtures only; evidence label: isolated-fixture."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import alert_kinds
from operating import proactive_engine as pe
from operating import team_activity as ta
from operator_api import team_view as tv
from operator_api import backend as be
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.serving import Worker

ME = "00000000-0000-4000-8000-0000000000a1"
B = "00000000-0000-4000-8000-0000000000b1"
C = "00000000-0000-4000-8000-0000000000b2"
EXCLUDED = "00000000-0000-4000-8000-0000000000e1"
OWNER = "00000000-0000-4000-8000-0000000000f1"
NAMES = {ME: "Avery Lindqvist", B: "Bea Okafor", C: "Cal Mendes", OWNER: "Owen Ostrander",
         EXCLUDED: "Ezra Exley"}


def rd_line(i: int, project: str, customer: str = "Customer:1", state: str = "waiting_on_us"):
    return {"id": f"rd-{i}", "kind": "rd_project_line", "state": "open",
            "citations": [f"l1:rd{i}"],
            "event": {"project": project, "kind": "sample_request", "state": state, "days": 6,
                      "last_on": "2026-11-02", "messages_n": 2, "customer": customer,
                      "items": [], "owner": B, "contact": f"buyer{i}@synth-customer.example",
                      "messages": [{"id": f"Email:{i}", "subject": "Sample"}]}}


def item(i: int, owner: str, kind: str = "reply_owed", days: int = 3, cites=None,
         counterpart: str = "buyer{i}@synth-customer.example"):
    """A trimmed alert-list item as the reader's view returns it."""
    return {"id": f"g{i}", "kind": kind, "state": "open", "object_id": f"Email:{i}",
            "object_type": "Email", "summary": f"Waiting for an answer about order {i}",
            "citations": list(cites or [f"Email:{i}"]), "first_seen": "2026-11-01T00:00:00Z",
            "event": {"days": days, "asked_by": counterpart.format(i=i),
                      "messages": [{"id": f"Email:{i}", "subject": f"Sample {i}",
                                    "channel": "mail"}]},
            "follow_up": {"owner": owner,
                          "who_can_answer": ["OPS"] if kind != "reply_owed" else []}}


NAMES_OF = {"Customer:1": "Acme Foods", "Customer:2": "Borealis Naturals"}


class Backend(be.WorkspaceBackend):
    owner = False
    gaps: list = []
    refuse: set = set()

    asked: list = []
    policy_threads: list = []

    def _brief_run(self, reader, job, **_kw):
        return job(self.view())

    def view(self):
        def reader_policy():
            self.policy_threads.append(threading.current_thread().name)
            return None, SimpleNamespace(unrestricted=self.owner)

        def gap_inbox(**kw):
            self.asked.append(kw)   # the trim: a refused citation hides its whole item
            kept = [g for g in self.gaps if not set(g.get("citations") or ()) & self.refuse]
            return {"status": "read", "gaps": kept}
        view = SimpleNamespace(
            gap_inbox=gap_inbox,
            access=SimpleNamespace(
                reader_policy=reader_policy,
                decide=lambda handles: {h: ({"reason": "no"} if h in self.refuse else None)
                                        for h in handles}),
            _request=lambda: mock.MagicMock(),
            # a record's display as this reader reads it: refused, no object at all
            _call=lambda tool, args: ({"error": {"code": "not_found"}}
                                      if args.get("object_id") in self.refuse else
                                      {"object": {"display": NAMES_OF.get(
                                          args.get("object_id"), "")}}),
            bind=lambda session: None)
        return view


class Owner(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        store = self.dir / "store.sqlite"
        conn = sqlite3.connect(pe.inbox_path(store))
        conn.execute("CREATE TABLE gaps (kind TEXT, follow_up TEXT, state TEXT, "
                     "resolved_at TEXT)")
        rows = [("reply_owed", ME, "open", None), ("reply_owed", B, "open", None),
                ("mc_task_stuck", B, "open", None), ("price_below_cost", C, "open", None),
                ("reply_owed", EXCLUDED, "open", None), ("reply_owed", OWNER, "open", None),
                ("reply_owed", B, "resolved", "2999-01-01"),
                ("reply_owed", C, "resolved", "2000-01-01"),
                ("reply_owed", EXCLUDED, "resolved", "2999-01-01")]
        for kind, owner, state, at in rows:
            conn.execute("INSERT INTO gaps VALUES (?, ?, ?, ?)",
                         (kind, json.dumps({"owner": owner, "who_can_answer": []}), state, at))
        conn.commit()
        conn.close()
        self.backend = Backend(lambda s: None, data_label="t", store_path=store,
                               data_dir=self.dir)
        self.backend.owner, self.backend.gaps, self.backend.refuse = False, [], set()
        self.backend.asked = []
        # the access worker's own queue answers the grant sheet's owner check; the read
        # workers never run here, so a check queued behind them would never be answered
        access = self.backend.access_worker = Worker(lambda session: self.backend.view())
        threading.Thread(target=access.loop, name="access-lane", daemon=True).start()
        self.backend.policy_threads = []
        self.addCleanup(access.stopped.set)
        patcher = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED,
                                               tv.HIDDEN_ENV: OWNER, tv.READERS_ENV: ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.snapshot([ME, B, C, OWNER, EXCLUDED])

    def snapshot(self, people: list[str]) -> None:
        """A team snapshot of ``people``: each did one thing a day and works with everyone."""
        today = date.today()
        (self.dir / ta.SNAPSHOT_FILE).write_text(json.dumps({
            "schema": ta.SCHEMA, "status": "built",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window": {"from": (today - timedelta(days=34)).isoformat(),
                       "to": today.isoformat()}, "coverage": [],
            "people": {p: {"name": NAMES.get(p, f"Person {p[-4:]}"), "roles": ["purchasing"],
                           "days": {(today - timedelta(days=k)).isoformat(): {"mail_sent": 1}
                                    for k in range(35)},
                           "hours": [1] * 24, "reply": {"7": 30, "28": 40},
                           "reply_n": {"7": 2, "28": 5},
                           "with": {o: 3 for o in people if o != p}} for p in people}}))

    def client(self, oid: str = ME, login: bool = True) -> TestClient:
        # The owner holds the full-read grant over every source and mailbox (S-16:
        # ``feedback.owner_open``, the one owner test): a signed-in employee whose session
        # carries the access code the access layer sets. Anyone else holds none.
        from operating import session_identity as si
        from operating.employee_permissions import FULL_READ_SCOPES, EmployeePrincipal
        if self.backend.owner:
            employee = EmployeePrincipal("00000000-0000-4000-8000-00000000f001", oid)
            session = si.SessionIdentity(
                tenant="plx", channel=si.CHANNEL_CONSOLE, author_kind=si.AUTHOR_HUMAN,
                proof=si.PROOF_CONFIGURED, employee=employee,
                principals=frozenset({employee.principal}), identities=frozenset({oid}),
                access="full_read:" + "+".join(sorted(FULL_READ_SCOPES)) + ":mail_all")
        else:
            session = dataclasses.replace(
                fixture_identity("plx", ["staff:demo"])(),
                principals=frozenset({"staff:demo", f"employee:{oid}"}))
        client = TestClient(create_app(self.backend, DevAuth(lambda: session),
                                       secure_cookie=False), base_url="http://testserver")
        if login:
            self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    # ---- the Team view: who sees whom ------------------------------------------------------

    def team(self, oid: str = ME, **kw):
        found = self.client(oid).get("/api/team", **kw)
        self.assertEqual(found.status_code, 200, found.text)
        return found

    def listed(self, found) -> list[str]:
        return sorted(p["name"] for p in found.json()["people"])

    def test_me_carries_only_the_readers_own_roles(self):
        """The app's per-role defaults read /api/me roles: the reader's own snapshot row only,
        unknown role names dropped; no snapshot: none."""
        data = json.loads((self.dir / ta.SNAPSHOT_FILE).read_text())
        data["people"][ME]["roles"] = ["quality", "purchasing", "made_up"]
        data["people"][B]["roles"] = ["finance"]
        (self.dir / ta.SNAPSHOT_FILE).write_text(json.dumps(data))
        me = self.client().get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["roles"], ["purchasing", "quality"])
        self.assertEqual(self.client(B).get("/api/me").json()["roles"], ["finance"])
        (self.dir / ta.SNAPSHOT_FILE).unlink()
        self.assertEqual(self.client().get("/api/me").json()["roles"], [])

    def test_everyone_else_sees_only_their_own_row_whatever_the_request_claims(self):
        self.assertEqual(self.client(login=False).get("/api/team").status_code, 401)
        (self.dir / "workload-grants.json").write_text(json.dumps(
            {"teams": {B: "SALES"}, "managers": {ME: ["SALES"]}}))   # a manager grant
        for kw in ({}, {"params": {"owner": "1", "as": B}},
                   {"headers": {"X-Owner": "1", "X-Forwarded-User": "owner"}}):
            found = self.team(**kw)
            self.assertEqual(found.json()["scope"], "self")
            self.assertEqual(self.listed(found), [NAMES[ME]])
            self.assertTrue(found.json()["people"][0]["self"])
            self.assertNotIn(NAMES[B], found.text)
        client = self.client()
        self.assertEqual(client.get(f"/api/team/{tv.key_of(B)}").status_code, 404)
        mine = client.get(f"/api/team/{tv.key_of(ME)}")
        self.assertEqual(mine.status_code, 200, mine.text)
        # who they work with names colleagues, never the owner or the excluded
        self.assertEqual(sorted(x["name"] for x in mine.json()["partners"]),
                         [NAMES[B], NAMES[C]])
        self.assertEqual({x["key"] for x in mine.json()["partners"]}, {None})   # not openable
        self.assertEqual(client.post("/api/team", json={}).status_code, 405)

    def test_owner_and_named_readers_see_everyone_but_the_owner_and_the_excluded(self):
        self.backend.owner = True
        everyone = [NAMES[ME], NAMES[B], NAMES[C]]
        found = self.team(OWNER)
        self.assertEqual((found.json()["scope"], self.listed(found)), ("everyone", everyone))
        for gone in (OWNER, EXCLUDED):
            self.assertNotIn(gone, found.text)
            self.assertNotIn(NAMES[gone], found.text)
            self.assertNotIn(tv.key_of(gone), found.text)
            self.assertEqual(self.client(OWNER).get(f"/api/team/{tv.key_of(gone)}").status_code,
                             404)
        detail = self.client(OWNER).get(f"/api/team/{tv.key_of(B)}")
        self.assertEqual(sorted(x["name"] for x in detail.json()["partners"]),
                         [NAMES[ME], NAMES[C]])
        self.assertNotIn(NAMES[OWNER], detail.text)
        text = found.text.lower()
        for word in ("subject", "body", "@", ME, B, C):
            self.assertNotIn(word.lower(), text)       # names and opaque keys only, no ids
        # the owner reading as themselves, with the hidden list unset: still not listed
        with mock.patch.dict(os.environ, {tv.HIDDEN_ENV: ""}):
            self.assertEqual(self.listed(self.team(OWNER)), everyone)
        # a reader in scope sees everyone too, the owner still hidden
        self.backend.owner = False
        with mock.patch.dict(os.environ, {tv.READERS_ENV: f"x@synth.example,{C}"}):
            named = self.team(C)
            self.assertEqual((named.json()["scope"], self.listed(named)), ("everyone", everyone))
            self.assertEqual(self.client(C).get(f"/api/team/{tv.key_of(B)}").status_code, 200)
            # no setting: the owner's own sessions taught the server who the owner is, so
            # the owner stays hidden; with nobody known a named reader sees only their row
            with mock.patch.dict(os.environ, {tv.HIDDEN_ENV: ""}):
                self.assertEqual(self.listed(self.team(C)), everyone)
                (self.dir / tv.OWNER_IDS_FILE).unlink()
                self.assertEqual(self.listed(self.team(C)), [NAMES[C]])
        # the owner as a plain reader is not even shown to themselves
        self.assertEqual(self.team(OWNER).json()["people"], [])

    def test_a_snapshot_only_in_the_state_directory_is_read_with_the_same_exclusions(self):
        # the nightly unit writes <state>/team-activity.json; a host that never named the
        # file read nothing and showed "not counted yet" beside a fresh snapshot
        state = self.dir / "state"
        state.mkdir()
        (self.dir / ta.SNAPSHOT_FILE).rename(state / ta.SNAPSHOT_FILE)
        self.backend.owner = True
        self.assertEqual(self.team(OWNER).json()["state"], "not_built")
        self.backend.state_dir = str(state)
        found = self.team(OWNER)
        self.assertEqual((found.json()["state"], self.listed(found)),
                         ("ready", [NAMES[ME], NAMES[B], NAMES[C]]))
        for gone in (OWNER, EXCLUDED):
            self.assertNotIn(NAMES[gone], found.text)
        self.backend.owner = False
        self.assertEqual(self.listed(self.team()), [NAMES[ME]])
        # the data directory's own file still wins over the state directory's
        self.snapshot([ME])
        self.backend.owner = True
        self.assertEqual(self.listed(self.team(OWNER)), [NAMES[ME]])

    def test_the_owner_is_hidden_from_every_reader_with_no_setting(self):
        """PLX_TEAM_HIDDEN_PEOPLE unset: an ordinary reader's own row names no owner in
        'works with', and a drill-down lists no item that names the owner (WEB review)."""
        mine = f"/api/team/{tv.key_of(ME)}"
        self.backend.gaps = [item(1, ME, counterpart="buyer1@synth-customer.example")]
        with mock.patch.dict(os.environ, {tv.HIDDEN_ENV: ""}):
            # nobody has said who the owner is yet: name no one, show no item
            before = self.client(ME).get(mine)
            self.assertEqual((before.status_code, before.json()["partners"],
                              before.json()["items"]), (200, [], []))
            self.assertNotIn(NAMES[OWNER], before.text)
            # the owner opens the app once; every later reader's views hide them
            self.backend.owner = True
            self.assertEqual(self.client(OWNER).get("/api/team").status_code, 200)
            self.backend.owner = False
            after = self.client(ME).get(mine)
            self.assertEqual(sorted(x["name"] for x in after.json()["partners"]),
                             [NAMES[B], NAMES[C]])
            self.assertNotIn(NAMES[OWNER], after.text)
            self.assertEqual(self.listed(self.team(ME)), [NAMES[ME]])

    def test_the_hidden_list_takes_object_ids_and_addresses(self):
        data = json.loads((self.dir / ta.SNAPSHOT_FILE).read_text())
        data["people"][OWNER]["addresses"] = ["owen@synth.example"]
        (self.dir / ta.SNAPSHOT_FILE).write_text(json.dumps(data))
        self.backend.owner = False
        for listed in (OWNER, "Owen@Synth.example", f"nobody@x.example, {OWNER.upper()}"):
            with mock.patch.dict(os.environ, {tv.HIDDEN_ENV: listed, tv.READERS_ENV: ME}):
                found = self.team(ME)
                self.assertEqual(self.listed(found), [NAMES[ME], NAMES[B], NAMES[C]], listed)
        # an address nobody has resolves to nobody: nothing hidden, a named reader falls back
        with mock.patch.dict(os.environ, {tv.HIDDEN_ENV: "ghost@synth.example", tv.READERS_ENV: ME}):
            self.assertEqual(self.listed(self.team(ME)), [NAMES[ME]])

    def test_the_exclusion_fails_closed_and_the_owner_is_counted_in_no_team_total(self):
        self.backend.owner = True
        with mock.patch("operating.briefs.env_people", return_value=None):
            self.assertEqual(self.team(OWNER).json()["people"], [])
            self.assertEqual(self.client(OWNER).get(f"/api/team/{tv.key_of(B)}").status_code,
                             404)
            self.backend.owner = False   # (an owner session teaches the server its ids)
            self.assertEqual(self.team(ME).json()["people"], [])
            self.backend.owner = True
        # /api/workload's team totals: ME and B count, the owner's thread does not
        (self.dir / "workload-grants.json").write_text(json.dumps(
            {"teams": {ME: "SALES", B: "SALES", C: "SALES", OWNER: "SALES"}}))
        body = self.client(OWNER).get("/api/workload").json()
        sales = next(t for t in body["teams"] if t["team"] == "SALES")
        self.assertEqual(sales["awaiting_reply"], 2)
        self.assertNotIn(OWNER, json.dumps(body))

    def test_row_counts_open_work_and_figures(self):
        self.backend.owner = True
        by = {p["name"]: p for p in self.team(OWNER).json()["people"]}
        b = by[NAMES[B]]
        self.assertEqual((b["awaiting_reply"], b["tasks"], b["alerts_closed"]), (1, 1, 1))
        self.assertEqual((b["d7"], b["d28"]), ({"mail_sent": 7}, {"mail_sent": 28}))
        self.assertEqual((len(b["trend"]), len(b["hours"]), b["reply_minutes_7"]), (28, 24, 30))
        # no department on record: the derived role stays a role, never shown as the team
        self.assertEqual((b["team"], b["roles"]), (None, ["Purchasing"]))
        self.assertEqual(by[NAMES[C]]["alerts"], 1)
        # no snapshot, or a stale one: the view says it is not built, and lists nobody
        (self.dir / ta.SNAPSHOT_FILE).unlink()
        gone = self.team(OWNER).json()
        self.assertEqual((gone["state"], gone["people"]), ("not_built", []))

    # ---- the weekly tracker (AUDIT2-12) -----------------------------------------------------

    def _close_today(self, owner: str) -> None:
        conn = sqlite3.connect(pe.inbox_path(self.dir / "store.sqlite"))
        conn.execute("INSERT INTO gaps VALUES (?, ?, 'resolved', ?)", (
            "reply_owed", json.dumps({"owner": owner}),
            datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
        conn.close()

    def _saved(self, oid: str, n: int) -> None:
        """``n`` accepted autopilot items in ``oid``'s work log (each one entry saved)."""
        from operator_api import work_log as wl
        store = wl.WorkLog.for_backend(self.backend)
        reader = SimpleNamespace(key=f"employee:{oid},staff:demo", display_name=NAMES[oid],
                                 email=None, session=SimpleNamespace(identities=(oid,)))
        for i in range(n):
            self.assertIsNotNone(store.record(reader, kind="autopilot", verb="accepted",
                                              title="q", thread=f"autopilot:t{oid[-2:]}{i}",
                                              source="autopilot", source_id=f"a{oid[-2:]}{i}"))
        store.close()

    def test_tracker_weeks_count_work_done_closed_and_replies(self):
        self.backend.owner = True
        self._close_today(B)
        by = {p["name"]: p for p in self.team(OWNER).json()["people"]}
        # the list carries this week and last (its columns); the person's own page all four
        self.assertEqual([len(p["weeks"]) for p in by.values()], [2] * len(by))
        self.assertEqual(by[NAMES[B]]["weeks"],
                         self.client(OWNER).get(f"/api/team/{tv.key_of(B)}").json()["person"]["weeks"][-2:])
        weeks = self.client(OWNER).get(f"/api/team/{tv.key_of(B)}").json()["person"]["weeks"]
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        self.assertEqual([w["week"] for w in weeks],
                         [(monday - timedelta(days=7 * k)).isoformat() for k in (3, 2, 1, 0)])
        # one mail sent a day: 7 a full week, the current week up to today; mail received
        # is not work done
        self.assertEqual([w["done"] for w in weeks], [7, 7, 7, today.weekday() + 1])
        self.assertEqual(weeks[-1]["to"], today.isoformat())
        self.assertEqual(weeks[-1]["partial"], today.weekday() != 6)
        self.assertFalse(any(w["partial"] for w in weeks[:3]))
        # B's alert closed today counts in this week; the far-future one in no week
        self.assertEqual([w["closed"] for w in weeks], [0, 0, 0, 1])
        other = self.client(OWNER).get(f"/api/team/{tv.key_of(C)}").json()["person"]["weeks"]
        self.assertEqual([w["closed"] for w in other], [0, 0, 0, 0])
        # the snapshot carries no per-week reply figures: none invented
        self.assertTrue(all(w["reply_minutes"] is None and w["replies"] == 0 for w in weeks))
        self.assertEqual(tv.DONE_CHANNELS, frozenset(ta.CHANNELS) - {"mail_received"})

    def test_reply_weeks_are_medians_per_monday_week(self):
        until = date(2026, 11, 12)                       # a Thursday
        replies = [(date(2026, 11, 9), 10.0), (date(2026, 11, 12), 30.0),
                   (date(2026, 11, 13), 999.0),          # after the snapshot's day
                   (date(2026, 11, 2), 5.0), (date(2026, 10, 1), 1.0)]
        got = ta.reply_weeks(replies, until)
        self.assertEqual(list(got), ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"])
        self.assertEqual(got["2026-11-09"], {"median": 20, "n": 2})
        self.assertEqual(got["2026-11-02"], {"median": 5, "n": 1})
        self.assertEqual(got["2026-10-19"], {"median": None, "n": 0})

    def test_time_saved_joins_per_person_and_only_the_owner_sees_others(self):
        from operator_api import work_log as wl
        per = wl.saved_minutes()["record"]
        self._saved(B, 2)
        self._saved(C, 1)
        self._saved(OWNER, 5)
        self.backend.owner = True
        found = self.team(OWNER)
        by = {p["name"]: p["weeks"] for p in found.json()["people"]}
        self.assertEqual(by[NAMES[B]][-1]["saved_minutes"], 2 * per)
        self.assertEqual(by[NAMES[C]][-1]["saved_minutes"], per)
        self.assertEqual(by[NAMES[ME]][-1]["saved_minutes"], 0)   # counted, nothing used
        self.assertNotIn(NAMES[OWNER], found.text)                 # the owner never appears
        detail = self.client(OWNER).get(f"/api/team/{tv.key_of(B)}").json()["person"]
        self.assertEqual(detail["weeks"][-1]["saved_minutes"], 2 * per)
        # a named Team reader sees everyone's activity but only their own time saved
        self.backend.owner = False
        with mock.patch.dict(os.environ, {tv.READERS_ENV: C}):
            named = {p["name"]: p["weeks"] for p in self.team(C).json()["people"]}
        self.assertEqual(sorted(named), sorted([NAMES[ME], NAMES[B], NAMES[C]]))
        self.assertEqual(named[NAMES[C]][-1]["saved_minutes"], per)
        self.assertIsNone(named[NAMES[B]][-1]["saved_minutes"])
        self.assertIsNone(named[NAMES[ME]][-1]["saved_minutes"])
        with mock.patch.dict(os.environ, {tv.READERS_ENV: C}):
            other = self.client(C).get(f"/api/team/{tv.key_of(B)}").json()["person"]
        self.assertIsNone(other["weeks"][-1]["saved_minutes"])
        # a plain reader: their own row and their own figure only
        [mine] = self.team(B).json()["people"]
        self.assertEqual((mine["name"], mine["weeks"][-1]["saved_minutes"]), (NAMES[B], 2 * per))

    def test_rows_cardinality_one_ten_sixty(self):
        self.backend.owner = True
        for n in (1, 10, 60):
            with self.subTest(n=n):
                people = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, n + 1)]
                self.snapshot(people)
                started = time.process_time()   # the route's work (every thread of it)
                body = self.team(OWNER).json()
                self.assertLess(time.process_time() - started, 1.0)
                self.assertEqual(len(body["people"]), n)
                self.assertEqual(len({p["key"] for p in body["people"]}), n)

    # ---- the owner's workload grant sheet ---------------------------------------------------

    def grants_file(self) -> dict:
        return json.loads((self.dir / "workload-grants.json").read_text())

    def test_grant_sheet_is_the_owners_and_never_lists_the_owner_or_the_excluded(self):
        (self.dir / "workload-grants.json").write_text(json.dumps(
            {"teams": {ME: "SALES", EXCLUDED: "SALES", OWNER: "OPS"},
             "managers": {B: ["SALES"], EXCLUDED: ["OPS"]}}))
        for oid in (ME, B):
            self.assertEqual(self.client(oid).get("/api/workload/grants").status_code, 403)
        self.assertEqual(self.client(login=False).get("/api/workload/grants").status_code, 401)
        self.backend.owner = True
        # /api/me queues a background policy decision (_warm_access); stub it so only the
        # grant sheet's own owner check is measured
        self.backend._warm_access = lambda reader: False
        self.backend.policy_threads.clear()
        found = self.client(OWNER).get("/api/workload/grants")
        self.assertEqual(found.status_code, 200, found.text)
        # the owner check reads no policy at all (the session's owner code decides), so it
        # never queues behind the page reads
        self.assertEqual(self.backend.policy_threads, [])
        body = found.json()
        self.assertEqual([p["name"] for p in body["people"]], [NAMES[ME], NAMES[B], NAMES[C]])
        self.assertEqual({p["oid"]: (p["team"], p["sees"]) for p in body["people"]},
                         {ME: ("SALES", []), B: (None, ["SALES"]), C: (None, [])})
        self.assertNotIn(EXCLUDED, found.text)
        self.assertNotIn(OWNER, found.text)
        self.assertEqual(body["held_back"], 2)   # EXCLUDED (team and grant), OWNER
        # the exclusion unavailable: neither read nor written, the file as it was
        before = self.grants_file()
        with mock.patch("operating.briefs.env_people", return_value=None):
            client = self.client(OWNER)
            self.assertEqual(client.get("/api/workload/grants").status_code, 503)
            self.assertEqual(client.post("/api/workload/grants",
                                         json={"teams": {}, "managers": {}}).status_code, 503)
        self.assertEqual(self.grants_file(), before)

    def test_a_listed_full_reader_neither_reads_nor_rewrites_the_grant_sheet(self):
        # A listed full reader (PLX_FULL_READERS) holds an unrestricted read policy, but its
        # access code ends ':listed': assigning workload grants stays the owner's.
        from operating import session_identity as si
        from operating.employee_permissions import FULL_READ_SCOPES, EmployeePrincipal
        (self.dir / "workload-grants.json").write_text(json.dumps(
            {"teams": {ME: "SALES"}, "managers": {}}))
        before = self.grants_file()
        self.backend.owner = True   # the policy says unrestricted, as a listed reader's does
        employee = EmployeePrincipal("00000000-0000-4000-8000-00000000f002", B)
        session = si.SessionIdentity(
            tenant="plx", channel=si.CHANNEL_CONSOLE, author_kind=si.AUTHOR_HUMAN,
            proof=si.PROOF_CONFIGURED, employee=employee,
            principals=frozenset({employee.principal}), identities=frozenset({B}),
            access="full_read:" + "+".join(sorted(FULL_READ_SCOPES)) + ":mail_all:listed")
        client = TestClient(create_app(self.backend, DevAuth(lambda: session),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        self.assertEqual(client.get("/api/workload/grants").status_code, 403)
        self.assertEqual(client.post("/api/workload/grants", json={
            "teams": {B: "OPS"}, "managers": {B: ["SALES", "OPS"]}}).status_code, 403)
        self.assertEqual(self.grants_file(), before)

    def test_saving_the_sheet_keeps_the_entries_it_never_shows(self):
        (self.dir / "workload-grants.json").write_text(json.dumps(
            {"teams": {ME: "SALES", EXCLUDED: "SALES", OWNER: "OPS"},
             "managers": {EXCLUDED: ["OPS"]}}))
        self.backend.owner = True
        client = self.client(OWNER)
        # the sheet's whole assignment, plus an attempt to move the excluded person
        saved = client.post("/api/workload/grants", json={
            "teams": {B: "SALES", EXCLUDED: "OPS"}, "managers": {C: ["SALES"]}})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json(), {"teams": {B: "SALES"}, "managers": {C: ["SALES"]}})
        self.assertEqual(self.grants_file(), {
            "teams": {B: "SALES", EXCLUDED: "SALES", OWNER: "OPS"},
            "managers": {C: ["SALES"], EXCLUDED: ["OPS"]}})
        self.assertEqual(client.post("/api/workload/grants",
                                     json={"teams": {ME: "sales"}}).status_code, 422)
        self.backend.owner = False
        # the manager now sees the team's people by name on Workload
        seen = self.client(C).get("/api/workload").json()["people"]
        self.assertIn((B, NAMES[B]), [(p["person"], p["name"]) for p in seen])
        self.assertEqual(self.client(ME).post("/api/workload/grants",
                                              json={"teams": {}}).status_code, 403)
        self.assertEqual(self.grants_file()["teams"][B], "SALES")

    def test_grant_sheet_cardinality_one_ten_sixty(self):
        self.backend.owner = True
        for n in (1, 10, 60):
            with self.subTest(n=n):
                people = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, n + 1)]
                self.snapshot(people)
                started = time.process_time()
                body = self.client(OWNER).get("/api/workload/grants").json()
                self.assertLess(time.process_time() - started, 1.0)
                self.assertEqual(sorted(p["oid"] for p in body["people"]), people)

    # ---- the drill-down: a person's open items ----------------------------------------------

    def bound_guard(self, *excluded: str):
        """A bound people exclusion: decides every person and address, excludes ``excluded``."""
        people = mock.MagicMock()
        people.unavailable = None
        people.decides.return_value = True
        people.is_excluded.side_effect = lambda v: v in (EXCLUDED, *excluded)
        patcher = mock.patch("operating.briefs.env_people", return_value=people)
        patcher.start()
        self.addCleanup(patcher.stop)

    def items_of(self, person: str, reader: str = OWNER):
        found = self.client(reader).get(f"/api/team/{tv.key_of(person)}")
        self.assertEqual(found.status_code, 200, found.text)
        return found

    def test_drill_shows_the_items_with_their_fields_and_a_link(self):
        self.backend.owner = True
        self.bound_guard()
        self.backend.gaps = [item(1, B, days=4), item(2, B, kind="mc_task_stuck", days=9),
                             item(3, B, kind="price_below_cost", days=2), item(4, C, days=7)]
        body = self.items_of(B).json()
        by = {i["bucket"]: i for i in body["items"]}
        self.assertEqual(len(body["items"]), 3)
        one = by["awaiting_reply"]
        self.assertEqual((one["title"], one["counterpart"], one["age_days"], one["waiting_on"]),
                         ("Sample 1", "buyer1@synth-customer.example", 4, NAMES[B]))
        self.assertEqual((one["record_type"], one["record_id"]), ("Email", "Email:1"))
        self.assertEqual((by["tasks"]["age_days"], by["tasks"]["waiting_on"]), (9, "OPS"))
        self.assertEqual(by["alerts"]["kind"], "price_below_cost")
        self.assertEqual(self.backend.asked[-1].get("all_owners"), True)
        self.assertEqual(len(body["timeline"]), 28)
        self.assertNotIn(B, self.items_of(B).text)
        # a reader reading their own row reads only their own list
        self.backend.owner = False
        self.client(ME).get(f"/api/team/{tv.key_of(ME)}")
        self.assertEqual(self.backend.asked[-1].get("all_owners"), False)

    def test_excluded_counterparts_never_appear_and_it_fails_closed(self):
        self.backend.owner = True
        self.backend.gaps = [item(2, B, counterpart="x@excluded.example"), item(3, B)]
        # the guard cannot decide a customer address: that item is not shown (fail closed)
        self.bound_guard("x@excluded.example")
        found = self.items_of(B)
        self.assertEqual([i["id"] for i in found.json()["items"]], ["g3"])
        self.assertNotIn("excluded.example", found.text)

    def test_marked_items_the_reader_cannot_open_stay_withheld(self):
        self.backend.owner = True
        self.bound_guard()
        self.backend.gaps = [item(1, B), item(2, B, cites=["Payroll:9"])]
        self.backend.refuse = {"Payroll:9"}   # the normal path refuses the pay-marked record
        conn = sqlite3.connect(pe.inbox_path(self.dir / "store.sqlite"))
        conn.execute("INSERT INTO gaps VALUES ('reply_owed', ?, 'open', NULL)",
                     (json.dumps({"owner": B}),))
        conn.commit()
        conn.close()
        found = self.items_of(B)
        body = found.json()
        self.assertEqual([i["id"] for i in body["items"]], ["g1"])
        self.assertEqual(body["withheld"], 2)   # B also owns an open task the list does not hold
        self.assertNotIn("Payroll", found.text)
        self.assertNotIn("Sample 2", found.text)

    def test_drill_lists_what_the_row_counts(self):
        # live 11-18: the owner saw "41 waiting" beside an empty list and "41 more not shown:
        # marked, or not open to you": a whole-inbox read spent its limit on higher-priority
        # kinds before any reply, and kinds kept off Home were counted but never read
        self.backend.owner = True
        self.bound_guard()
        conn = sqlite3.connect(pe.inbox_path(self.dir / "store.sqlite"))
        off = sorted(alert_kinds.off_gap_kinds(()) - {"mc_task_stuck"})[0]
        conn.execute("INSERT INTO gaps VALUES (?, ?, 'open', NULL)",
                     (off, json.dumps({"owner": B})))
        conn.commit()
        conn.close()
        self.backend.gaps = [item(1, B), item(2, B, kind="mc_task_stuck"), item(3, B, kind=off)]
        row = next(p for p in self.team(OWNER).json()["people"] if p["name"] == NAMES[B])
        body = self.items_of(B).json()
        asked = self.backend.asked[-1]
        self.assertEqual((asked["kinds"], asked["include_off"]),
                         (sorted([off, "mc_task_stuck", "reply_owed"]), True))
        self.assertEqual(len(body["items"]), row["awaiting_reply"] + row["tasks"] + row["alerts"])
        self.assertEqual(body["withheld"], 0)
        # a person with nothing open is not read for at all
        before = len(self.backend.asked)
        self.snapshot([ME, B, C, OWNER, EXCLUDED, "0f0f0f0f-0000-4000-8000-000000000abc"])
        body = self.items_of("0f0f0f0f-0000-4000-8000-000000000abc").json()
        self.assertEqual((body["items"], body["withheld"], len(self.backend.asked)),
                         ([], 0, before))

    def test_drill_cardinality_one_ten_sixty(self):
        self.backend.owner = True
        self.bound_guard()
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.backend.gaps = [item(i, B, days=i) for i in range(n)]
                t0 = time.process_time()   # the route's work, which load does not add to
                body = self.items_of(B).json()
                self.assertEqual(len(body["items"]), n)
                self.assertEqual(len({i["id"] for i in body["items"]}), n)
                self.assertEqual([i["age_days"] for i in body["items"]],
                                 sorted((i["age_days"] for i in body["items"]), reverse=True))
                self.assertLess(time.process_time() - t0, 20)

    def test_removed_r_and_d_and_qc_endpoints_are_gone(self):
        for path in ("/api/rd-projects", "/api/brief/qc-daily", "/api/brief/qc-audit", "/api/brief/qc-audit.txt"):
            with self.subTest(path=path):
                self.assertIn(self.client().get(path).status_code, (404, 422))   # the generic brief route refuses an unknown type


if __name__ == "__main__":
    unittest.main()
