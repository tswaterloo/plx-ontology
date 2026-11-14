"""The operations alerts (``operating.proactive_ops``) through the engine and the per-reader
inbox: Mission Control tasks and approvals, Planner tasks, aged balances, production, damage
spikes and unanswered Teams asks.

SYNTHETIC fixtures only (``World`` stores, synthetic handles and ids); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
import json
import time
import unittest

from operating import proactive_engine as pe
from operating import proactive_ops as po
from operating.tests.test_proactive_business import NO_FINANCE, money
from operating.tests.test_proactive_engine import (AS_OF, EXCLUDED_OID, OURS_OID, T0, Engine,
                                                   World, people)

RULES = list(po.RULES)
OTHER_OID = "00000000-0000-4000-8000-0000000000b2"
CHANNEL = "teams:channel:synth-team/synth-channel"
# what a schema projecting these would declare (none of them is declared today)
DECLARED = {"mc_task_due": ("due_at",),
            "production_due": (("AssemblyOrder", "scheduled_date", "realized_date",
                                "status", "fx"),),
            "damage_date": (("AssemblyOrder", "fm_created_at"),),
            "ask_text": {"TeamsChatMessage": ("subject",),
                         "TeamsChannelMessage": ("subject",)}}


def day(n: int) -> str:
    """The business day ``n`` days before the run's."""
    return (AS_OF - timedelta(days=n)).isoformat()


def person(w: World, oid: str) -> str:
    return w.obj("Person", {"object_id": oid}, audience="people:dir")


def task(w: World, stage: str, *, updated: int = 1, due: int | None = None,
         who: str | None = None, audience: str = "mc:ops") -> str:
    values = {"task_id": f"TASK-{w.n + 1}", "title": "Synthetic task", "stage": stage,
              "updated_at": day(updated) + "T09:00:00Z"}
    if due is not None:
        values["due_at"] = day(due) + "T17:00:00Z"
    oid = w.obj("MCTask", values, audience=audience)
    if who:
        w.link("MCTask.with_person", oid, person(w, who))
    return oid


def gate(w: World, status: str, asked: int, *, decided: bool = False) -> str:
    values = {"gate_id": f"G-{w.n + 1}", "task_id": "TASK-1", "status": status,
              "requested_at": day(asked) + "T10:00:00Z"}
    if decided:
        values["decided_at"] = day(0) + "T10:00:00Z"
    return w.obj("MCApproval", values, audience="mc:ops")


def requisition(w: World, status: str, asked: int, *, approved: bool = False) -> str:
    values = {"pr_number": f"PR-SYNTH-{w.n + 1}", "status": status,
              "requested_date": day(asked)}
    if approved:
        values["approved_date"] = day(0)
    return w.obj("PurchaseRequisition", values, audience="portal:purchasing")


def planner(w: World, due: int, *, pct: str = "50", done: bool | int = False,
            assignees: list[str] | None = None, plan: str = "synth-plan",
            created: int = 20) -> str:
    """A Planner task due ``due`` days ago, created ``created`` days ago (negative: after the
    run's day); ``done``: completed a day ago (True) or that many days ago."""
    values = {"title": "Synthetic planner task " + "x" * 80, "plan_title": "Synthetic plan",
              "due_at": day(due) + "T12:00:00Z", "percent_complete": pct,
              "assignees": json.dumps(assignees or [OURS_OID]), "plan_id": plan,
              "created_at": day(created) + "T12:00:00Z"}
    if done is not False:
        values["completed_at"] = day(1 if done is True else done) + "T12:00:00Z"
    return w.obj("PlannerTask", values, audience="meetings:plan:synth")


def aged(w: World, kind: str, then: tuple, now: tuple, *, currency: str = "CAD",
         currency_then: str | None = None, audience: str = "bc:finance") -> str:
    """An aged record whose current snapshot is ``now`` and whose superseded one is
    ``then``: (balance, current, period1, period2, period3, as-of days back)."""
    names = ("balance_due", "current_amount", *po.BUCKETS)

    def values(snap: tuple) -> dict:
        return {**dict(zip(names, snap[:5])), "aged_as_of_date": day(snap[5]),
                "period_length": "30D", "party_number": "SYNTH-P"}
    oid = w.obj(kind, values(now), audience=audience)
    for prop in names:
        money(w, oid, prop, currency)
    older = f"api://bc.aged/{w.n}@g0"
    w.rows["objects"][-1]["first_recorded_at"] = "2026-09-01T00:00:00.000000Z"
    w.rows["object_versions"].append({
        "object_id": oid, "l1_handle": older, "digest": f"old{w.n}", "lineage": "workbench",
        "markings": "[]", "recorded_at": "2026-09-01T00:00:00.000000Z", "superseded_at": T0})
    for prop, value in values(then).items():
        w.rows["object_values"].append({
            "object_id": oid, "property": prop,
            "value_type": "money" if prop in names else "date" if prop.endswith("_date")
            else "text", "value": value, "currency": (currency_then or currency)
            if prop in names else None, "unit": None, "quantity_role": None,
            "l1_handle": older, "field_path": f"row.{prop}", "digest": f"old{w.n}",
            "lineage": "workbench", "binding_id": "fx", "parser": "fixture", "markings": "[]",
            "recorded_at": "2026-09-01T00:00:00.000000Z", "superseded_at": T0})
    return oid


def assembly(w: World, item: str, *, status: str = "Allocated", due: int | None = 3,
             finished: bool | int = False, created: int = 5, binding: str = "fx") -> str:
    """``finished``: realized a day ago (True) or that many days ago."""
    values = {"status": status, "fm_created_at": day(created) + "T08:00:00Z"}
    if due is not None:
        values["scheduled_date"] = day(due)
    if finished is not False:
        values["realized_date"] = day(1 if finished is True else finished)
    oid = w.obj("AssemblyOrder", values, audience="api:production", binding=binding)
    w.link("AssemblyOrder.makes", oid, item)
    return oid


def damage(w: World, order: str, qty: str, unit: str = "each") -> str:
    oid = w.obj("AssemblyOrderDamage", {"damaged_quantity": (qty, unit, "damaged"),
                                        "status": "Open"}, audience="api:production")
    w.link("AssemblyOrderDamage.damage_of", oid, order)
    return oid


def message(w: World, sender: str, sent: int, *, root: str | None = None,
            mentions: tuple[str, ...] = (), subject: str | None = None,
            audience: str = CHANNEL) -> str:
    values = {"id": f"m{w.n + 1}", "message_type": "message",
              "created_at": day(sent) + "T14:00:00Z", "channel_id": "synth-channel",
              "from": json.dumps({"kind": "user", "id": sender, "display_name": "Synth"}),
              "mentions": json.dumps([{"id": i, "text": "Synth", "kind": "user",
                                       "target_id": m, "display_name": "Synth"}
                                      for i, m in enumerate(mentions)])}
    if root:
        values["reply_to_id"] = root
    if subject is not None:
        values["subject"] = subject
    return w.obj("TeamsChannelMessage", values, audience=audience)


def root_id(w: World, oid: str) -> str:
    return next(r["value"] for r in w.rows["object_values"]
                if r["object_id"] == oid and r["property"] == "id")


class OpsAlerts(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=RULES, ops_sources=options.pop("sources", DECLARED),
                               **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    # ------------------------------------------------------------------ Mission Control

    def test_mc_tasks_overdue_and_stuck_by_stage_and_people(self):
        w = World()
        late = task(w, "progress", due=5)
        stuck = task(w, "review", updated=20)
        task(w, "backlog", updated=90)                   # parked: never stuck
        task(w, "verified", updated=90, due=30)          # closed
        task(w, "progress", due=-3)                      # due later
        task(w, "Waiting", updated=90)                   # an unknown stage
        task(w, "progress", updated=90, due=30, who=EXCLUDED_OID)   # not cleared
        task(w, "progress", updated=1, who=OURS_OID)     # cleared, recent: nothing
        summary = self.run_rules(w)
        (overdue,) = self.kinds(po.MC_OVERDUE)
        self.assertEqual(overdue["object_id"], late)
        self.assertIn("5 days past its due date", overdue["summary"])
        (idle,) = self.kinds(po.MC_STUCK)
        self.assertEqual(idle["object_id"], stuck)
        self.assertIn("stage review with no update for 20 days", idle["summary"])
        self.assertEqual(summary["not_evaluated"][po.MC_STUCK],
                         {"not_judged": 1, "stage_unmapped": 1})
        # no due date declared by the schema: blocked, never a clean "nothing overdue"
        summary = self.run_rules(World(), sources={"mc_task_due": ()})
        self.assertEqual(summary["detectors"][po.MC_OVERDUE], "blocked")
        self.assertEqual(summary["detectors"][po.MC_STUCK], "complete")

    def test_an_approval_waiting_is_raised_and_never_decided(self):
        w = World()
        waiting = gate(w, "pending", 5)
        gate(w, "pending", 1)                            # requested yesterday
        gate(w, "approved", 9)
        gate(w, "pending", 9, decided=True)              # sources disagree: unjudged
        gate(w, "on hold", 9)
        summary = self.run_rules(w)
        (gap,) = self.kinds(po.MC_APPROVAL)
        self.assertEqual(gap["object_id"], waiting)
        self.assertIn("waited 5 days for a decision", gap["summary"])
        self.assertEqual(summary["not_evaluated"][po.MC_APPROVAL],
                         {"pending_but_decided": 1, "status_unmapped": 1})

    def test_a_portal_requisition_pending_approval_is_in_the_approvals_queue(self):
        from operating import proactive_threads as pt
        w = World()
        # AS_OF's weekday decides the calendar span of five business days
        span = next(n for n in range(5, 12) if po._business_days(AS_OF - timedelta(days=n),
                                                                  AS_OF) == 5)
        waiting = requisition(w, "PENDING_APPROVAL", span)
        requisition(w, "PENDING_APPROVAL", 1)            # requested yesterday: not due
        for status in ("APPROVED", "REJECTED", "CONVERTED_TO_PO", "CANCELLED", "DRAFT",
                       "CONVERTED_TO_CMR"):
            requisition(w, status, span)
        requisition(w, "PENDING_APPROVAL", span, approved=True)   # sources disagree
        requisition(w, "SOMETHING_NEW", span)
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][po.PORTAL_APPROVAL], "complete")
        (gap,) = self.kinds(po.PORTAL_APPROVAL)
        self.assertEqual(gap["object_id"], waiting)
        self.assertEqual(summary["not_evaluated"][po.PORTAL_APPROVAL],
                         {"pending_but_decided": 1, "status_unmapped": 1})
        queue = pt.approvals_queue(self.kinds(po.PORTAL_APPROVAL), AS_OF)
        self.assertEqual(queue["total"], 1)
        (group,) = queue["groups"]
        self.assertEqual(group["waiting_on"], "not recorded")
        (item,) = group["items"]
        self.assertEqual((item["channel"], item["days"], item["aging"]),
                         ("portal", 5, "3-5 business days"))
        self.assertEqual((item["nudge"]["status"], item["nudge"]["sent"]),
                         ("draft_not_sent", False))
        self.assertEqual(item["nudge"]["to"], [])
        # a reader without the requisition's records sees nothing of it
        self.assertEqual(self.kinds(po.PORTAL_APPROVAL,
                                    may_read=lambda a: a != "portal:purchasing"), [])

    def test_person_rules_are_blocked_without_the_people_exclusion(self):
        w = World()
        task(w, "review", updated=40)
        gate(w, "pending", 9)
        planner(w, 9)
        message(w, OURS_OID, 9, mentions=(OTHER_OID,), subject="Can you check?")
        summary = self.run_rules(w, people=people(unavailable=True))
        requisition(w, "PENDING_APPROVAL", 9)
        for rule in (po.MC_OVERDUE, po.MC_STUCK, po.MC_APPROVAL, po.PORTAL_APPROVAL, po.PLANNER,
                     po.TEAMS):
            self.assertEqual(summary["detectors"][rule], "blocked", rule)
        self.assertEqual([g for g in self.read(limit=1000)["gaps"]
                          if g["kind"] in po.KINDS], [])

    # ------------------------------------------------------------------ Planner

    def test_planner_task_overdue_clears_assignees(self):
        w = World()
        late = planner(w, 4)
        planner(w, 4, done=True)
        planner(w, 4, pct="100")
        planner(w, -2)                                  # due later
        planner(w, 4, assignees=[EXCLUDED_OID])          # never judged
        # a plan nobody created or completed a task of in 30 days is left behind
        planner(w, 200, plan="old-plan", created=300)
        # judged as the run's day saw it: completed after it (100% now) was still open
        # then, and a task created after it did not exist
        reopened = planner(w, 6, pct="100", done=-2)
        planner(w, 4, created=-1)
        # Planner's empty due date (a null cell) is undated; an unparseable one is unreadable
        for raw in (None, "next week"):
            task = planner(w, 4)
            for row in w.rows["object_values"]:
                if row["object_id"] == task and row["property"] == "due_at":
                    row["value"] = raw
        summary = self.run_rules(w)
        self.assertEqual(sorted(g["object_id"] for g in self.kinds(po.PLANNER)),
                         sorted([late, reopened]))
        gap = next(g for g in self.kinds(po.PLANNER) if g["object_id"] == late)
        self.assertEqual(gap["object_id"], late)
        self.assertIn("4 days past due", gap["summary"])
        # the title is clipped (never the whole text)
        self.assertLessEqual(len(gap["summary"]), 200)
        self.assertNotIn("x" * 70, gap["summary"])
        self.assertEqual(summary["not_evaluated"][po.PLANNER],
                         {"not_judged": 1, "due_date_unreadable": 1})

    # ------------------------------------------------------------------ aged balances

    def test_aged_balance_worsening_compares_snapshots_in_one_currency(self):
        w = World()
        worse = aged(w, "AgedReceivable", ("1000", "800", "150", "50", "0", 30),
                     ("1000", "500", "200", "200", "100", 0))
        aged(w, "AgedReceivable", ("1000", "800", "200", "0", "0", 30),     # owed more,
             ("2000", "1600", "400", "0", "0", 0))                          # same share
        aged(w, "AgedPayable", ("100", "100", "0", "0", "0", 30),
             ("100", "0", "100", "0", "0", 0), currency_then="USD")         # currency moved
        payable = aged(w, "AgedPayable", ("500", "500", "0", "0", "0", 30),
                       ("500", "100", "300", "100", "0", 0))
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][po.AGED], "complete")
        (ar,) = self.kinds(po.AGED_AR)
        self.assertEqual(ar["object_id"], worse)
        self.assertIn("500 CAD past due on " + day(0), ar["summary"])
        self.assertIn("200 CAD on " + day(30), ar["summary"])
        self.assertIn("20% to 50% of the balance", ar["summary"])
        (ap,) = self.kinds(po.AGED_AP)
        self.assertEqual(ap["object_id"], payable)
        self.assertEqual(summary["not_evaluated"][po.AGED], {"currency_changed": 1})
        # finance figures: a reader without finance sees neither, nor a figure of them
        found = self.read(held=NO_FINANCE, limit=1000)
        self.assertEqual([g for g in found["gaps"] if g["kind"] in po.KINDS], [])
        self.assertNotIn("500 CAD", json.dumps(found))

    # ------------------------------------------------------------------ production

    def test_production_late_by_declared_due_date_and_status(self):
        w = World()
        item = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        late = assembly(w, item, due=10)
        assembly(w, item, due=10, finished=True)
        assembly(w, item, due=10, status="Realized")
        assembly(w, item, due=-4)
        assembly(w, item, due=10, status="RELEASED")   # the MO's stale word: never open
        # within the allowed lag (too few realized to measure it: 7 days): on time
        on_time = assembly(w, item, due=4)
        # a mirror's copy (its id sorts first) lags the system that keeps production: a
        # mirror alone is not judged; a realized or rescheduled keeper copy wins over a
        # mirror copy still open and late
        assembly(w, item, due=40, binding="a-mirror")
        for keeper in (assembly(w, item, due=2, status="Realized"),
                       assembly(w, item, due=-6, status="Committed")):
            w.link("AssemblyOrder.same_record_as", assembly(w, item, due=30, binding="a-mirror"),
                   keeper)
        # open on paper, neither scheduled nor edited within the active window: stale
        assembly(w, item, due=400)
        summary = self.run_rules(w)
        (gap,) = self.kinds(po.PRODUCTION)
        self.assertEqual(gap["object_id"], late)
        self.assertIn("10 days past its due date", gap["summary"])
        self.assertIn("usually realized within 7 days of it", gap["summary"])
        self.assertEqual(summary["not_evaluated"][po.PRODUCTION],
                         {"status_unmapped": 1, "status_source_absent": 1})
        # the allowed lag is the type's own: 20 records realized 2 days after their
        # scheduled day make the order 4 days past its day late
        for n in range(20):
            assembly(w, item, due=40 + n, finished=38 + n)
        self.run_rules(w)
        self.assertEqual(sorted(g["object_id"] for g in self.kinds(po.PRODUCTION)),
                         sorted([late, on_time]))
        # no due date declared by the schema: blocked
        summary = self.run_rules(World(), sources={"production_due": ()})
        self.assertEqual(summary["detectors"][po.PRODUCTION], "blocked")
        self.assertEqual(summary["detector_reasons"][po.PRODUCTION], ["due_date_not_served"])

    def test_damage_spike_against_the_trailing_windows(self):
        w = World()
        spiking = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        steady = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        for item in (spiking, steady):
            for back in (40, 70, 100, 130, 160, 190):
                damage(w, assembly(w, item, created=back), "5")
        for n in range(3):
            damage(w, assembly(w, spiking, created=2 + n), "10")    # 30 of a mean of 5
        damage(w, assembly(w, steady, created=3), "6")
        fresh = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        for n in range(4):
            damage(w, assembly(w, fresh, created=2 + n), "50")     # no history: no spike
        self.run_rules(w)
        (gap,) = self.kinds(po.DAMAGE)
        self.assertEqual(gap["object_id"], spiking)
        self.assertIn("30 ea damaged in the last 30 days on 3 entries", gap["summary"])
        self.assertIn("6x the 30-day average of 5 ea", gap["summary"])
        self.assertEqual(gap["event"]["dated_by"], ["assembly_order"])
        self.assertEqual(len(gap["citations"]), 2 * 9)

    # ------------------------------------------------------------------ Teams

    def test_an_unanswered_ask_keeps_ids_and_a_clipped_subject_only(self):
        w = World()
        subject = "Could you confirm the synthetic count for the line today? " + "y" * 40
        ask = message(w, OURS_OID, 6, mentions=(OTHER_OID,), subject=subject)
        message(w, OURS_OID, 5, root=root_id(w, ask))           # the asker again: no reply
        answered = message(w, OURS_OID, 6, mentions=(OTHER_OID,), subject="When is it due?")
        message(w, OTHER_OID, 5, root=root_id(w, answered))
        message(w, OURS_OID, 6, mentions=(OTHER_OID,), subject="FYI the line is running")
        message(w, OURS_OID, 0, mentions=(OTHER_OID,), subject="Can you look?")  # too soon
        message(w, OURS_OID, 6, mentions=(OTHER_OID,))                  # no text projected
        hidden = message(w, OURS_OID, 6, mentions=(EXCLUDED_OID,), subject="Can you?")
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][po.TEAMS], "complete")
        (gap,) = self.kinds(po.TEAMS)
        self.assertEqual(gap["object_id"], ask)
        self.assertIn("no reply from them for 4 business days", gap["summary"])
        self.assertIn(subject[:40], gap["summary"])
        self.assertNotIn("y" * 10, gap["summary"])
        self.assertEqual(gap["event"]["addressee"], OTHER_OID)
        self.assertNotEqual(gap["object_id"], hidden)
        self.assertEqual(summary["not_evaluated"][po.TEAMS],
                         {"no_question_text": 1, "not_judged": 1})
        # a reader not granted the channel never sees it (nor can a grant on the message's
        # citation alone stand in for the conversation's)
        self.assertEqual(self.kinds(po.TEAMS, may_read=lambda k: k != CHANNEL), [])

    def test_a_reply_from_an_excluded_or_unreadable_sender_leaves_it_unjudged(self):
        w = World()
        ask = message(w, OURS_OID, 6, mentions=(OTHER_OID,), subject="Can you check?")
        message(w, EXCLUDED_OID, 5, root=root_id(w, ask))
        other = message(w, OURS_OID, 6, mentions=(OTHER_OID,), subject="Can you check?")
        reply = message(w, OTHER_OID, 5, root=root_id(w, other))
        for row in w.rows["object_values"]:
            if row["object_id"] == reply and row["property"] == "from":
                row["value"] = "not json"
        summary = self.run_rules(w)
        self.assertEqual(self.kinds(po.TEAMS), [])
        self.assertEqual(summary["not_evaluated"][po.TEAMS], {"not_judged": 2})
        # neither the run nor a reader is told why: no reason names people
        self.assertNotIn("cleared", json.dumps(summary["not_evaluated"]))
        self.assertNotIn("cleared", json.dumps(self.read(limit=1000)))

    # ------------------------------------------------------------------ readers

    def test_a_restricted_reader_sees_none_of_what_it_may_not_open(self):
        w = World()
        build_one_of_each(w)
        self.run_rules(w)
        every = {g["kind"] for g in self.read(limit=1000)["gaps"]}
        self.assertEqual(every, set(po.KINDS))
        for hidden in ("mc:ops", "portal:purchasing", "meetings:plan:synth", "bc:finance", "api:production",
                       CHANNEL):
            seen = self.read(may_read=lambda k, h=hidden: k != h, limit=1000)
            self.assertFalse([g for g in seen["gaps"] if g["kind"] in po.KINDS
                              and audience_of(w, g["object_id"]) == hidden], hidden)
        nothing = self.read(may_read=lambda k: k in ("api:items", "people:dir"), limit=1000)
        self.assertEqual([g for g in nothing["gaps"] if g["kind"] in po.KINDS], [])

    def test_each_alert_stops_at_its_own_window(self):
        w = World()
        build_one_of_each(w)
        summary = self.run_rules(w, budget=pe.Budget(600),
                                 detector_seconds={po.TEAMS: 0.0})
        self.assertEqual(summary["detectors"][po.TEAMS], "truncated")
        self.assertEqual(summary["detector_reasons"][po.TEAMS], [pe.TIME_BUDGET])
        self.assertEqual(summary["detectors"][po.PLANNER], "complete")

    # ------------------------------------------------------------------ cardinality

    def test_every_record_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                expected = {k: [] for k in po.KINDS}
                for _ in range(n):
                    for kind, oid in build_one_of_each(w).items():
                        expected[kind].append(oid)
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                for rule in RULES:
                    self.assertEqual(summary["detectors"][rule], "complete", rule)
                got = {k: sorted(g["object_id"] for g in self.kinds(k)) for k in po.KINDS}
                self.assertEqual(got, {k: sorted(v) for k, v in expected.items()})
                self.inbox.unlink()
                self.store.unlink()


def audience_of(w: World, oid: str) -> str:
    return next(r["audience_key"] for r in w.rows["objects"] if r["object_id"] == oid)


def build_one_of_each(w: World) -> dict[str, str]:
    """One record raising each kind (its subject's id by kind)."""
    item = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
    for back in (40, 70, 100):
        damage(w, assembly(w, item, created=back, due=None), "5")
    for n in range(3):
        damage(w, assembly(w, item, created=2 + n, due=None), "10")
    ask = message(w, OURS_OID, 6, mentions=(OTHER_OID,), subject="Can you check?")
    return {po.MC_OVERDUE: task(w, "progress", due=5),
            po.MC_STUCK: task(w, "qa", updated=30),
            po.MC_APPROVAL: gate(w, "pending", 6),
            po.PORTAL_APPROVAL: requisition(w, "PENDING_APPROVAL", 9),
            po.PLANNER: planner(w, 5),
            po.AGED_AR: aged(w, "AgedReceivable", ("100", "100", "0", "0", "0", 30),
                             ("100", "0", "100", "0", "0", 0)),
            po.AGED_AP: aged(w, "AgedPayable", ("100", "100", "0", "0", "0", 30),
                             ("100", "0", "0", "0", "100", 0)),
            po.PRODUCTION: assembly(w, item, due=10, created=400),
            po.DAMAGE: item,
            po.TEAMS: ask}


if __name__ == "__main__":
    unittest.main()
