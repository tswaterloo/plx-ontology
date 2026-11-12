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
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import proactive_engine as pe
from operating import team_activity as ta
from operator_api import team_view as tv
from operator_api import backend as be
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity

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


class Backend(be.WorkspaceBackend):
    owner = False
    gaps: list = []
    refuse: set = set()

    asked: list = []

    def _brief_run(self, reader, job):
        def gap_inbox(**kw):
            self.asked.append(kw)   # the trim: a refused citation hides its whole item
            kept = [g for g in self.gaps if not set(g.get("citations") or ()) & self.refuse]
            return {"status": "read", "gaps": kept}
        view = SimpleNamespace(
            gap_inbox=gap_inbox,
            access=SimpleNamespace(
                reader_policy=lambda: (None, SimpleNamespace(unrestricted=self.owner)),
                decide=lambda handles: {h: ({"reason": "no"} if h in self.refuse else None)
                                        for h in handles}),
            _request=lambda: mock.MagicMock())
        return job(view)


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
                ("mc_task_overdue", B, "open", None), ("price_below_cost", C, "open", None),
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
        self.assertEqual((b["team"], b["roles"]), ("Purchasing", ["Purchasing"]))
        self.assertEqual(by[NAMES[C]]["alerts"], 1)
        # no snapshot, or a stale one: the view says it is not built, and lists nobody
        (self.dir / ta.SNAPSHOT_FILE).unlink()
        gone = self.team(OWNER).json()
        self.assertEqual((gone["state"], gone["people"]), ("not_built", []))

    def test_rows_cardinality_one_ten_sixty(self):
        self.backend.owner = True
        for n in (1, 10, 60):
            with self.subTest(n=n):
                people = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, n + 1)]
                self.snapshot(people)
                started = time.monotonic()
                body = self.team(OWNER).json()
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(len(body["people"]), n)
                self.assertEqual(len({p["key"] for p in body["people"]}), n)

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
        self.backend.gaps = [item(1, B, days=4), item(2, B, kind="mc_task_overdue", days=9),
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

    def test_drill_cardinality_one_ten_sixty(self):
        self.backend.owner = True
        self.bound_guard()
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.backend.gaps = [item(i, B, days=i) for i in range(n)]
                t0 = time.monotonic()
                body = self.items_of(B).json()
                self.assertEqual(len(body["items"]), n)
                self.assertEqual(len({i["id"] for i in body["items"]}), n)
                self.assertEqual([i["age_days"] for i in body["items"]],
                                 sorted((i["age_days"] for i in body["items"]), reverse=True))
                self.assertLess(time.monotonic() - t0, 20)

    # ---- the R&D board --------------------------------------------------------------------

    def test_board_draft_card_is_never_sent_and_hides_addresses_elsewhere(self):
        self.backend.gaps = [rd_line(1, "p1")]
        found = self.client().get("/api/rd-projects")
        self.assertEqual(found.status_code, 200, found.text)
        body = found.json()
        (project,) = body["projects"]
        self.assertEqual((body["sent"], body["writes"], project["draft"]["sent"]), (0, 0, False))
        self.assertEqual(project["card_id"], "rd:p1")
        self.assertEqual(project["draft"]["to"], [{"address": "buyer1@synth-customer.example"}])
        self.assertEqual(found.text.count("synth-customer.example"), 1)   # only inside the draft
        self.assertNotIn("contact", project)
        self.assertEqual((project["waiting_on"], project["owner"]), ("us", B))

    def test_restricted_reader_loses_a_project_whose_record_it_cannot_open(self):
        self.backend.gaps = [rd_line(1, "p1", "Customer:1"), rd_line(2, "p2", "Customer:2")]
        self.backend.refuse = {"Customer:2"}
        found = self.client().get("/api/rd-projects")
        ids = [p["id"] for p in found.json()["projects"]]
        self.assertEqual(ids, ["p1"])
        self.assertNotIn("Customer:2", found.text)
        self.assertNotIn("buyer2@", found.text)
        # and its card cannot be rebuilt by guessing the id
        card = self.client().post("/api/actions/preview", json={
            "card_id": "rd:p2", "kind": "reply_in_thread", "payload": {}})
        self.assertEqual(card.status_code, 404)

    def test_board_cardinality_one_ten_sixty(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.backend.gaps = [rd_line(i, f"p{i}", f"Customer:{i}") for i in range(n)]
                t0 = time.monotonic()
                body = self.client().get("/api/rd-projects").json()
                self.assertEqual(len(body["projects"]), n)
                self.assertEqual(len({p["id"] for p in body["projects"]}), n)
                self.assertEqual(len({p["customer"] for p in body["projects"]}), n)
                self.assertLess(time.monotonic() - t0, 20)


if __name__ == "__main__":
    unittest.main()
