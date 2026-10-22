"""Case claims: a lease that coordinates concurrent sessions on one case, and on an
objective's cases taken all or none (operating.cases).

Evidence label: ``isolated-fixture``. Two fixture sessions on the same principals
(they differ only in their session reference) share one case store with an injected
clock; the approver's break and the intent registration run on the fixture action
path (``operating.tests.test_action_path``); nothing reads a source.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from ontology.core import Actor, ValidationError
from operating import action_path as ap
from operating.cases import (CaseClaimed, CaseNotFound, CaseStore, ObjectiveRefused,
                             actor_label, writing_as)
from operating.session_identity import CHANNEL_CONSOLE, CHANNEL_MCP, fixture_session
from operating.tests.test_action_path import (EMPLOYEE, EMPLOYEE_OID, OPS, SESSION, STEPHEN,
                                              VINCE, VINCE_OID, ActionPathTestCase)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            self.now += timedelta(microseconds=1)
            return self.now

    def advance(self, seconds: int) -> None:
        with self.lock:
            self.now += timedelta(seconds=seconds)


def holder(session) -> dict[str, str]:
    return {"session": session.session_ref, "subject": actor_label(session.actor)}


class CaseClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "cases.db"
        self.clock = Clock()
        self.store = CaseStore(self.path, clock=self.clock)
        first = fixture_session("plx", {"staff:operations"}, channel=CHANNEL_MCP)
        second = fixture_session("plx", {"staff:operations"}, channel=CHANNEL_MCP)
        self.assertEqual(first.actor, second.actor)
        self.actor = first.actor
        self.a, self.b = holder(first), holder(second)
        self.case = self.store.open(self.actor, "Who confirms the late supplier order?")

    def write(self, writer, statement: str) -> dict:
        with writing_as(writer):
            return self.store.finding(self.actor, self.case, statement, "interpretation",
                                      "unknown")

    def events(self) -> int:
        return self.store.get(self.actor, self.case)["integrity"]["events"]

    def test_another_session_is_refused_until_expiry_then_takes_over(self):
        taken = self.store.claim(self.actor, self.case, self.a, 600)["claim"]
        self.assertEqual((taken["holder"], taken["active"], taken["transitions"]),
                         (self.a, True, 1))
        seen = self.store.get(self.actor, self.case)["claim"]
        self.assertEqual(seen, taken)
        until = re.escape(f"case_claimed_until:{taken['expires_at']}")
        before = self.events()
        with self.assertRaisesRegex(CaseClaimed, f"^{until}$"):
            self.store.claim(self.actor, self.case, self.b, 600)
        with self.assertRaisesRegex(CaseClaimed, f"^{until}$"):
            self.write(self.b, "B's reading")
        with self.assertRaises(CaseClaimed):   # a write naming no holder is not the holder
            self.store.close(self.actor, self.case, "done")
        other = {"session": "c" * 64, "subject": "staff:marketing"}
        with self.assertRaises(CaseClaimed):   # nor may another subject release A's claim
            self.store.claim(self.actor, self.case, other, 0, "taking over")
        self.assertEqual(self.events(), before)    # a refusal writes nothing
        self.write(self.a, "A's reading")
        self.clock.advance(300)
        renewed = self.store.claim(self.actor, self.case, self.a, 600)["claim"]
        self.assertGreater(renewed["expires_at"], taken["expires_at"])
        self.assertEqual(renewed["transitions"], 1)    # a renewal is no transition

        self.clock.advance(601)    # expiry is judged at read time
        self.assertFalse(self.store.get(self.actor, self.case)["claim"]["active"])
        took = self.store.claim(self.actor, self.case, self.b, 60)["claim"]
        self.assertEqual((took["holder"], took["transitions"]), (self.b, 2))
        self.write(self.b, "B's reading")
        with self.assertRaisesRegex(
                CaseClaimed, "^" + re.escape(f"case_claimed_until:{took['expires_at']}") + "$"):
            self.write(self.a, "A again")
        self.assertTrue(self.store.verify_chain(self.actor, self.case))

    def test_release_leaves_a_handoff_note_for_the_next_session(self):
        self.store.claim(self.actor, self.case, self.a, 600)
        released = self.store.claim(self.actor, self.case, self.a, 0,
                                    "Supplier mail read; freight evidence still open.",
                                    author_kind="agent")["claim"]
        self.assertEqual((released["holder"], released["active"], released["transitions"]),
                         (None, False, 2))
        view = self.store.get(self.actor, self.case)
        self.assertEqual((view["handoff"]["note"], view["handoff"]["by"],
                          view["handoff"]["author_kind"]),
                         ("Supplier mail read; freight evidence still open.", self.a, "agent"))
        self.assertEqual(view["timeline"][-1]["kind"], "case.released")
        self.write(self.b, "B continues")
        self.assertTrue(self.store.claim(self.actor, self.case, self.b, 60)["claim"]["active"])
        self.assertTrue(self.store.verify_chain(self.actor, self.case))

    def test_only_the_holder_employee_restarted_may_release_its_claim(self):
        self.store.claim(self.actor, self.case, self.a, 600)
        self.assertEqual(self.a["subject"], self.b["subject"])   # B: a co-member of A's group
        before = self.events()
        for lease in (600, 0):   # it may neither take over nor release A's claim
            with self.subTest(lease=lease), self.assertRaises(CaseClaimed):
                self.store.claim(self.actor, self.case, self.b, lease)
        self.assertEqual(self.events(), before)
        self.store.claim(self.actor, self.case, self.a, 0)

        # An employee's subject is that one employee: its restarted session may release
        # (never renew or take over) the claim its earlier session holds.
        employee = "employee:0000b000-0000-4000-8000-0000000000e1"
        first, restarted = ({"session": ref * 64, "subject": employee} for ref in "ef")
        self.store.claim(self.actor, self.case, first, 600)
        with self.assertRaises(CaseClaimed):
            self.store.claim(self.actor, self.case, restarted, 600)
        released = self.store.claim(self.actor, self.case, restarted, 0,
                                    "Previous session lost; supplier mail read.",
                                    author_kind="agent")["claim"]
        self.assertEqual((released["holder"], released["active"], released["transitions"]),
                         (None, False, 4))
        self.assertEqual(self.store.get(self.actor, self.case)["handoff"]["by"], restarted)
        self.write(self.a, "the case is free again")
        self.assertTrue(self.store.verify_chain(self.actor, self.case))

    def test_list_reads_the_claims_of_every_listed_case_in_one_query(self):
        connect = sqlite3.connect
        for count in (1, 10, 60):
            with self.subTest(claimed=count):
                store = CaseStore(self.path.with_name(f"list-{count}.db"), clock=self.clock)
                claimed = {store.open(self.actor, f"Order {n}"): holder(fixture_session(
                    "plx", {"staff:operations"}, channel=CHANNEL_MCP)) for n in range(count)}
                for case_id, who in claimed.items():
                    store.claim(self.actor, case_id, who, 600)
                released = store.open(self.actor, "Released")
                store.claim(self.actor, released, self.a, 600)
                store.claim(self.actor, released, self.a, 0)
                never = store.open(self.actor, "Never claimed")
                statements: list[str] = []

                def traced(*args, **kwargs):
                    db = connect(*args, **kwargs)
                    db.set_trace_callback(statements.append)
                    return db

                with mock.patch.object(sqlite3, "connect", traced):
                    listed = {item["case_id"]: item for item in store.list(self.actor, limit=200)}
                self.assertEqual(sum("case.claimed" in sql for sql in statements), 1)
                self.assertEqual(len(listed), count + 2)
                self.assertEqual({case_id: (item["claim"]["holder"], item["claim"]["active"])
                                  for case_id, item in listed.items() if case_id in claimed},
                                 {case_id: (who, True) for case_id, who in claimed.items()})
                self.assertEqual((listed[released]["claim"]["active"],
                                  listed[released]["claim"]["transitions"]), (False, 2))
                self.assertNotIn("claim", listed[never])
                outsider = Actor("plx", frozenset({"staff:marketing"}))
                self.assertEqual(store.list(outsider), [])

    def test_a_case_never_claimed_folds_with_no_claim_and_writes_as_before(self):
        self.write(None, "no session named")
        # The view of a case never claimed is unchanged: no claim or handoff key at all.
        view = self.store.get(self.actor, self.case)
        self.assertNotIn("claim", view)
        self.assertNotIn("handoff", view)
        self.assertNotIn("claim", self.store.list(self.actor)[0])
        # The store on disk, re-opened (a store written before claims has no claim events).
        reopened = CaseStore(self.path, clock=self.clock)
        self.assertNotIn("claim", reopened.get(self.actor, self.case))
        self.store.close(self.actor, self.case, "answered")

    def test_out_of_audience_reader_sees_neither_case_nor_claim(self):
        self.store.claim(self.actor, self.case, self.a, 600)
        self.assertTrue(self.store.list(self.actor)[0]["claim"]["active"])
        outsider = Actor("plx", frozenset({"staff:marketing"}))
        with self.assertRaises(CaseNotFound):
            self.store.get(outsider, self.case)
        with self.assertRaises(CaseNotFound):
            self.store.claim(outsider, self.case, self.b, 600)
        self.assertEqual(self.store.list(outsider), [])

    def test_unknown_claim_input_fails_closed(self):
        for lease in (-1, 3601, True, 1.5, "60"):
            with self.subTest(lease=lease), self.assertRaises(ValidationError):
                self.store.claim(self.actor, self.case, self.a, lease)
        for bad in ({"session": "abc", "subject": "x"}, {"session": self.a["session"]},
                    {**self.a, "extra": "1"}, "session"):
            with self.subTest(holder=bad), self.assertRaises(ValidationError):
                self.store.claim(self.actor, self.case, bad, 60)
        with self.assertRaisesRegex(ValidationError, "handoff_is_a_release_note"):
            self.store.claim(self.actor, self.case, self.a, 60, "note")
        other = self.store.open(self.actor, "Second case of the objective")
        for cases, code in (([], "invalid_objective_cases"), (self.case, "invalid_objective_cases"),
                            ([self.case, None], "invalid_objective_cases"),
                            ([self.case] * 65, "invalid_objective_cases"),
                            ([self.case, other, self.case], "duplicate_objective_case")):
            for lease in (60, 0):
                with self.subTest(cases=cases, lease=lease), \
                        self.assertRaisesRegex(ValidationError, f"^{code}$"):
                    self.store.claim_objective(self.actor, cases, self.a, lease)
        with self.assertRaisesRegex(ValidationError, "^invalid_lease_seconds$"):
            self.store.claim_objective(self.actor, [self.case, other], self.a, 3601)
        for case_id in (self.case, other):
            self.assertNotIn("claim", self.store.get(self.actor, case_id))

    def test_concurrent_claimants_never_both_hold_the_case(self):
        for count in (1, 10):
            with self.subTest(claimants=count):
                case = self.store.open(self.actor, "Concurrent claimants")
                holders = [holder(fixture_session("plx", {"staff:operations"},
                                                  channel=CHANNEL_MCP)) for _ in range(count)]
                barrier, won, refused = threading.Barrier(count), [], []

                def race(who: dict) -> None:
                    store = CaseStore(self.path, clock=self.clock)
                    barrier.wait()
                    try:
                        won.append(store.claim(self.actor, case, who, 600)["claim"]["holder"])
                    except CaseClaimed:
                        refused.append(who)

                threads = [threading.Thread(target=race, args=(who,)) for who in holders]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
                self.assertEqual((len(won), len(refused)), (1, count - 1))
                self.assertEqual(self.store.get(self.actor, case)["claim"]["holder"], won[0])
                with closing(sqlite3.connect(self.path)) as db:
                    self.assertEqual(db.execute(
                        "SELECT COUNT(*) FROM case_event WHERE case_id=? AND kind='case.claimed'",
                        (case,)).fetchone()[0], 1)

    def objective(self, count: int) -> list[str]:
        """``count`` new cases in the order every objective claim takes them."""
        return sorted(self.store.open(self.actor, f"Objective part {n}") for n in range(count))

    def claims(self, case_ids) -> dict:
        """Each case's claim as (holder session, active); None: never claimed."""
        out = {}
        for case_id in case_ids:
            claim = self.store.get(self.actor, case_id).get("claim")
            out[case_id] = claim and (claim["holder"] and claim["holder"]["session"],
                                      claim["active"])
        return out

    def test_sessions_racing_for_overlapping_objectives_end_one_holding_all(self):
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                cases = self.objective(6)
                # B lists its cases backwards: the claim order is the store's, not the caller's.
                sets, who = {"a": cases[:4], "b": cases[2:][::-1]}, {"a": self.a, "b": self.b}
                barrier, won, refused = threading.Barrier(2), {}, {}

                def race(name: str) -> None:
                    store = CaseStore(self.path, clock=self.clock)
                    barrier.wait()
                    try:
                        won[name] = store.claim_objective(self.actor, sets[name], who[name], 600)
                    except ObjectiveRefused as exc:
                        refused[name] = exc

                threads = [threading.Thread(target=race, args=(name,)) for name in sets]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
                self.assertEqual((len(won), len(refused)), (1, 1))
                (winner, result), (_, refusal) = *won.items(), *refused.items()
                # The loser met the winner on their least shared case and holds none of its own.
                self.assertEqual(refusal.case_id, cases[2])
                self.assertTrue(refusal.refusal.startswith("case_claimed_until:"))
                self.assertEqual([item["case_id"] for item in result["cases"]],
                                 sorted(sets[winner]))
                held = self.claims(cases)
                self.assertEqual({case_id for case_id, claim in held.items()
                                  if claim and claim[1]}, set(sets[winner]))
                self.assertEqual({held[case_id] for case_id in sets[winner]},
                                 {(who[winner]["session"], True)})

    def test_a_refused_objective_leaves_every_lease_exactly_as_it_was(self):
        cases = self.objective(5)
        # A's own 3600 s lease is in the objective; a refused 600 s claim never shortens it.
        a_held = self.store.claim(self.actor, cases[1], self.a, 3600)["claim"]
        b_until = self.store.claim(self.actor, cases[3], self.b, 600)["claim"]["expires_at"]
        before = {case_id: self.store.get(self.actor, case_id) for case_id in cases}
        with self.assertRaises(ObjectiveRefused) as refused:
            self.store.claim_objective(self.actor, cases[::-1], self.a, 600)
        self.assertEqual(str(refused.exception),
                         f"objective_refused:{cases[3]}:case_claimed_until:{b_until}")
        self.assertEqual(self.store.get(self.actor, cases[1])["claim"], a_held)
        self.assertEqual(self.claims(cases), {
            cases[0]: None, cases[1]: (self.a["session"], True), cases[2]: None,
            cases[3]: (self.b["session"], True), cases[4]: None})
        for case_id in cases:   # nothing journalled: the refusal rolled the claim back
            self.assertEqual(self.store.get(self.actor, case_id)["timeline"],
                             before[case_id]["timeline"])

        # A case the claimant may not see refuses as an unknown one does, and the claim
        # takes nothing before it; nothing about the unseen case is written.
        outsider = Actor("plx", frozenset({"staff:marketing"}))
        with mock.patch("operating.cases.uuid4", return_value=UUID(int=0)):   # sorts first
            own = self.store.open(outsider, "Marketing's part of the objective")
        unseen_events = self.events()
        for hidden in (self.case, "case-" + "f" * 32):
            with self.subTest(hidden=hidden), self.assertRaises(ObjectiveRefused) as hid:
                self.store.claim_objective(outsider, [own, hidden], self.b, 600)
            self.assertEqual((hid.exception.case_id, hid.exception.refusal),
                             (hidden, "case_not_found"))
            self.assertNotIn("claim", self.store.get(outsider, own))
        self.assertEqual(self.events(), unseen_events)

        # A release goes to every case it may: one another session took after expiry is
        # listed, never kept; a release every case refuses names the first.
        self.clock.advance(3601)
        b_again = self.store.claim(self.actor, cases[1], self.b, 600)["claim"]["expires_at"]
        self.store.claim_objective(self.actor, [cases[0], cases[2]], self.a, 600)
        released = self.store.claim_objective(self.actor, cases[:3], self.a, 0)
        self.assertEqual(([item["case_id"] for item in released["cases"]], released["refused"],
                          released["summary"]),
                         ([cases[0], cases[2]], [{"case_id": cases[1],
                                                  "error": f"case_claimed_until:{b_again}"}],
                          {"released": 2, "refused": 1}))
        first = min(cases[1], self.case)
        with self.assertRaisesRegex(ObjectiveRefused,
                                    f"^objective_refused:{first}:case_not_found$"):
            self.store.claim_objective(outsider, [cases[1], self.case], self.a, 0)

    def test_expiry_and_release_cover_every_case_of_an_objective(self):
        for count in (1, 10, 60):
            with self.subTest(cases=count):
                cases = self.objective(count)
                taken = self.store.claim_objective(self.actor, cases[::-1], self.a, 60)
                self.assertEqual(([item["case_id"] for item in taken["cases"]],
                                  taken["refused"]), (cases, []))
                self.assertEqual(len({item["claim"]["expires_at"] for item in taken["cases"]}), 1)
                self.assertEqual(set(self.claims(cases).values()), {(self.a["session"], True)})
                with self.assertRaises(ObjectiveRefused):
                    self.store.claim_objective(self.actor, cases, self.b, 60)
                self.clock.advance(61)   # one expiry frees every case at once
                self.assertEqual(set(self.claims(cases).values()), {(self.a["session"], False)})
                self.store.claim_objective(self.actor, cases, self.b, 60)
                released = self.store.claim_objective(
                    self.actor, cases, self.b, 0, "Freight evidence still open.",
                    author_kind="agent")
                self.assertEqual((len(released["cases"]), released["refused"]), (count, []))
                for case_id in cases:
                    view = self.store.get(self.actor, case_id)
                    self.assertEqual((view["claim"]["active"], view["handoff"]["note"],
                                      view["integrity"]["events"]),
                                     (False, "Freight evidence still open.", 4))


class ApproverBreakTests(ActionPathTestCase):
    """An approver on the path's roster breaks a lease; nobody else does."""

    @staticmethod
    def console(email, oid):
        return fixture_session("plx", OPS.principals, channel=CHANNEL_CONSOLE,
                               subject_email=email, subject_oid=oid)

    def test_an_approver_breaks_a_lease_with_a_journalled_reason(self):
        vince = self.console(VINCE, VINCE_OID)
        with self.assertRaisesRegex(ValidationError, "^no_active_claim$"):
            self.path.break_claim(OPS, self.case_id, "nothing held", session=vince)
        held = holder(SESSION)
        self.cases.claim(OPS, self.case_id, held, 600)
        events = self.cases.get(OPS, self.case_id)["integrity"]["events"]
        for session, code in (
                (None, "session_required"),
                (self.console(EMPLOYEE, EMPLOYEE_OID), "claim_breaker_not_an_approver"),
                (self.console(STEPHEN, None), "claim_breaker_not_an_approver"),   # a member
                (self.console(VINCE, "0000b000-0000-4000-8000-0000000000ff"),
                 "claim_breaker_not_an_approver")):   # an oid other than the roster's
            with self.subTest(code=code), self.assertRaisesRegex(ap.ActionPathError, code):
                self.path.break_claim(OPS, self.case_id, "reassigning", session=session)
        with self.assertRaisesRegex(ValidationError, "invalid_reason"):
            self.path.break_claim(OPS, self.case_id, " ", session=vince)
        with self.assertRaisesRegex(ValidationError, "^invalid_author_kind$"):
            self.cases.break_claim(OPS, self.case_id, "reassigning", {
                "email": VINCE, "oid": VINCE_OID, "session": vince.session_ref},
                author_kind="system")
        self.assertEqual(self.cases.get(OPS, self.case_id)["integrity"]["events"], events)

        broken = self.path.break_claim(OPS, self.case_id,
                                       "Holder session crashed; Stephen continues.",
                                       session=vince)
        record = {"holder": held, "reason": "Holder session crashed; Stephen continues.",
                  "approver": {"email": VINCE, "oid": VINCE_OID,
                               "session": vince.session_ref},
                  "author_kind": "human", "at": broken["at"]}
        self.assertEqual((broken["kind"], broken["claim"]["active"],
                          broken["claim"]["transitions"], broken["claim"]["breaks"]),
                         ("case.claim_broken", False, 2, [record]))
        view = self.cases.get(OPS, self.case_id)
        self.assertEqual(view["claim"], broken["claim"])
        self.assertEqual(view["timeline"][-1]["kind"], "case.claim_broken")

        # The broken holder may claim again at once: recorded on the break, which stays
        # in the claim's history through later releases and claims.
        again = self.cases.claim(OPS, self.case_id, held, 60)
        record["reclaimed"] = {"at": again["at"], "holder": held}
        self.assertEqual(again["claim"]["breaks"], [record])
        self.cases.claim(OPS, self.case_id, held, 0, "Stephen takes it from here.")
        self.assertEqual(self.cases.get(OPS, self.case_id)["claim"]["breaks"], [record])
        self.assertEqual(self.cases.list(OPS)[0]["claim"]["breaks"], [record])
        taken = self.cases.claim(OPS, self.case_id, holder(ANOTHER), 60)["claim"]
        self.assertEqual((taken["active"], taken["transitions"], taken["breaks"]),
                         (True, 5, [record]))
        # A later break joins the history; another holder's claim after it is no re-claim.
        second = self.path.break_claim(OPS, self.case_id, "Reassigned to Vince.",
                                       session=vince)["claim"]["breaks"]
        self.assertEqual((len(second), second[0], second[1]["holder"]),
                         (2, record, holder(ANOTHER)))
        after = self.cases.claim(OPS, self.case_id, held, 60)["claim"]["breaks"]
        self.assertEqual(after, second)
        self.assertTrue(self.cases.verify_chain(OPS, self.case_id))


ANOTHER = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP)


class ProposeClaimRaceTests(ActionPathTestCase):
    """Registering an intent checks the case's claim and commits in one step."""

    def intents(self, intent_id: str) -> int:
        with closing(sqlite3.connect(self.dir / "actions.db")) as db:
            return db.execute("SELECT COUNT(*) FROM action_event WHERE intent_id=?",
                              (intent_id,)).fetchone()[0]

    def test_a_claim_taken_during_registration_waits_for_the_intent(self):
        proposal, seen, threads = self.propose(), [], []
        append = self.path._append

        def racing(db, intent_id, kind, *rest):
            event = append(db, intent_id, kind, *rest)
            if kind == "intent.registered":
                def claim() -> None:   # another session claims the case in between
                    self.cases.claim(OPS, self.case_id, holder(ANOTHER), 600)
                    seen.append(self.intents(intent_id))

                threads.append(threading.Thread(target=claim))
                threads[0].start()
                threads[0].join(0.5)
            return event

        with mock.patch.object(self.path, "_append", racing):
            intent = self.path.register(OPS, self.case_id, proposal, session=SESSION)
        threads[0].join(30)
        self.assertEqual(seen, [1])   # the claim committed after the intent, never before
        self.assertEqual(self.intents(intent["intent_id"]), 1)

        # The claim now refuses registration by anyone but its holder (a host writer
        # naming no holder included); a refusal writes nothing, the holder registers.
        with self.assertRaisesRegex(CaseClaimed, "^case_claimed_until:"):
            self.path.register(OPS, self.case_id, proposal, session=SESSION)
        with writing_as(holder(SESSION)), self.assertRaises(CaseClaimed):
            self.path.register(OPS, self.case_id, proposal, session=SESSION)
        with writing_as(holder(ANOTHER)):
            self.assertTrue(self.path.register(OPS, self.case_id, proposal,
                                               session=SESSION)["duplicate"])
        self.assertEqual(self.intents(intent["intent_id"]), 1)


class ReadBackClaimTests(ActionPathTestCase):
    """The read-back of a dispatched write is never refused by a claim."""

    def test_a_dispatched_intent_is_read_back_while_another_session_holds_the_case(self):
        iid = self.authorized()
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")
        until = self.cases.claim(OPS, self.case_id, holder(ANOTHER), 600)["claim"]["expires_at"]
        events = self.cases.get(OPS, self.case_id)["integrity"]["events"]
        with writing_as(holder(SESSION)):
            result = self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
            self.assertEqual((result["state"], result["case_verification_id"]),
                             ("verified", "V1"))
            # Every other write of this session stays refused while the claim lasts: a
            # finding, and a verification that reads back no dispatched write (only a
            # flag that is True exempts one).
            refused = re.escape(f"case_claimed_until:{until}")
            with self.assertRaisesRegex(CaseClaimed, f"^{refused}$"):
                self.cases.finding(OPS, self.case_id, "The PO is set.", "interpretation",
                                   "unknown")
            for flag in ({}, {"dispatched_read_back": "yes"}):
                with self.subTest(flag=flag), \
                        self.assertRaisesRegex(CaseClaimed, f"^{refused}$"):
                    self.cases.verification(OPS, self.case_id,
                                            self.path.get(OPS, iid)["intent"]["proposal_id"],
                                            "effect_observed", None, {}, [self.handle], **flag)
        case = self.cases.get(OPS, self.case_id)
        self.assertEqual((case["integrity"]["events"], case["claim"]["holder"],
                          case["claim"]["active"]), (events + 1, holder(ANOTHER), True))
        self.assertEqual([(v["id"], v["outcome"]) for v in case["verifications"]],
                         [("V1", "effect_observed")])
        self.assertEqual(self.kinds(iid)[-1], "readback.recorded")
        self.assertEqual(self.path.get(OPS, iid)["state"], "verified")


if __name__ == "__main__":
    unittest.main()
