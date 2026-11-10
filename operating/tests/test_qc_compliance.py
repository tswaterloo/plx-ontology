"""QC compliance (``operating.qc_compliance``, ``plx.aggregate`` mode ``qc_compliance``) and
the daily QC and audit packs built from it (``operating.briefs``): cycles by area and month,
production slots, held batches, the reader's marking, audiences and people exclusion.

SYNTHETIC fixtures only (``World`` stores, synthetic titles and codes); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace
import datetime as dt

from operating import briefs as br
from operating import qc_compliance as qc
from operating.tests.test_proactive_commitments import RecentWorld
from operating.tests import test_proactive_engine as engine_tests
from operating.tests.test_proactive_engine import AS_OF, EXCLUDED_OID, OURS_OID, people

QC = frozenset({"quality_controlled"})
PLAN = "meetings:plan:qa"
FLOOR = "fm:floor"


def day(offset: int) -> str:
    return (AS_OF - timedelta(days=offset)).isoformat() + "T12:00:00Z"


def task(w: RecentWorld, title: str, due: int, done: int | None, *, plan: str = "QA checks",
         assignee: str = OURS_OID, audience: str = PLAN) -> str:
    """A Planner task due ``due`` days before AS_OF (negative: after), done ``done`` days
    before it (None: open)."""
    values = {"title": title, "plan_id": f"P-{plan}", "plan_title": plan, "due_at": day(due),
              "assignees": json.dumps([assignee])}
    if done is not None:
        values["completed_at"] = day(done)
    return w.obj("PlannerTask", values, audience=audience)


def slot(w: RecentWorld, status: str, start: int, *, line: str = "LINE-A",
         lineage: str = "fm_bronze") -> str:
    oid = w.obj("BatchSchedule", {"status": status, "line": line}, audience=FLOOR,
                binding="portal.fm_batch_calibration")
    for row in w.rows["object_values"]:
        if row["object_id"] == oid:
            row["lineage"] = lineage
    event = f"E:{oid}"
    w.rows["events"].append({
        "event_id": event, "event_type": "BatchSchedule.starts", "time_basis": "planned",
        "time": (AS_OF - timedelta(days=start)).isoformat(), "l1_handle": f"api://slot/{w.n}",
        "field_path": "row.datestart", "digest": "d", "lineage": lineage, "markings": "[]",
        "recorded_at": "2026-10-01T00:00:00.000000Z"})
    w.rows["event_objects"].append({"event_id": event, "object_id": oid,
                                    "qualifier": "subject"})
    return oid


def batch(w: RecentWorld, quality: str, status: str = "Open") -> str:
    return w.obj("Batch", {"quality_status": quality, "status": status}, audience=FLOOR,
                 markings='["quality_controlled"]')


def world() -> tuple[RecentWorld, dict]:
    w = RecentWorld()
    ids = {}
    # a weekly calibration series: on time, late within its week, done after the next fell
    # due (missed), open past the next due (missed), open past due (overdue), not due yet
    ids["on_time"] = task(w, "Synthetic pH meter calibration - 01-08-2026", 42, 42)
    ids["late"] = task(w, "Synthetic pH meter calibration - 08-08-2026", 35, 31)
    ids["missed_done"] = task(w, "Synthetic pH meter calibration - 15-08-2026", 28, 18)
    ids["missed_open"] = task(w, "Synthetic pH meter calibration - 22-08-2026", 21, None)
    task(w, "Synthetic pH meter calibration - 29-08-2026", 14, 14)
    ids["overdue"] = task(w, "Synthetic pH meter calibration - 05-09-2026", 3, None)
    ids["upcoming"] = task(w, "Synthetic pH meter calibration - 12-09-2026", -4, None)
    # a monthly sanitation series: on time, late within its month, on time
    for due, done in ((90, 90), (60, 50), (30, 30)):
        task(w, "Synthetic monthly swabs", due, done)
    # a task of no recurring series has no cycle to keep: counted apart, never judged
    task(w, "Synthetic annual review", 60, 10)
    # outside quality plans: only a title naming calibration, swabs, sanitation or pest
    for due in (34, 27, 20):
        task(w, "Synthetic campaign review", due, None, plan="Marketing")
        task(w, "Synthetic scale calibration", due, due, plan="Maintenance")
    # an excluded person's task is never counted
    task(w, "Synthetic hidden check", 20, None, assignee=EXCLUDED_OID)
    # production slots: completed, past start and still committed, upcoming, cancelled,
    # a staging test slot and a status word no one mapped
    ids["slot_done"] = slot(w, "Completed", 40)
    ids["slot_open"] = slot(w, "Committed", 5)
    slot(w, "Allocated", -3, line="LINE-B")
    slot(w, "Cancelled", 10)
    slot(w, "Committed", 6, lineage="gold_native_staging")
    slot(w, "Paused", 7)
    # holds: one batch kept in two systems (one hold), a cancelled hold, a released batch
    a, b = batch(w, "On-Hold"), batch(w, "On-Hold")
    w.link("Batch.same_record_as", a, b)
    batch(w, "On-Hold", status="Cancelled")
    batch(w, "Released")
    return w, ids


def mode_call(book: dict, held: frozenset, may_read=lambda _k: True):
    """The reader's tool calls: the mode answered from ``book`` as that reader."""
    def call(name: str, args: dict) -> dict:
        assert name == "plx.aggregate" and args.get("mode") == qc.MODE, (name, args)
        return qc.answer(book, AS_OF, may_read=may_read, held=held,
                         view=args.get("view", "compliance"))
    return call


class QcCompliance(engine_tests.Engine):

    def book(self, w: RecentWorld, **kw) -> dict:
        w.build(self.store)
        return qc.read_book(str(self.store), AS_OF, people=kw.get("guard", people()))

    def test_cycles_slots_and_holds_are_counted_and_every_line_cites_its_record(self):
        w, ids = world()
        book = self.book(w)
        got = qc.answer(book, AS_OF, may_read=lambda _k: True, held=QC)
        self.assertTrue(got["complete"], got)
        area = {a["area"]: a for a in got["checks"]["by_area"]}
        cal = area["calibration"]
        self.assertEqual({k: cal[k] for k in qc.STATUSES},
                         {"on_time": 5, "late": 1, "missed": 2, "overdue": 1, "upcoming": 0})
        self.assertEqual(cal["judged"], 9)
        self.assertEqual(cal["on_time_share"], "55.6")   # 5 of 9
        self.assertEqual({k: area["sanitation"][k] for k in ("on_time", "late", "missed")},
                         {"on_time": 2, "late": 1, "missed": 0})
        self.assertEqual(got["checks"]["not_recurring"], 1)
        for row in [got["checks"]["totals"], *got["checks"]["by_area"],
                    *got["checks"]["by_month"]]:   # the shares of each figure add up
            self.assertEqual(sum(row[s] for s in qc.STATUSES[:4]), row["judged"])
        by_month = got["checks"]["by_month"]
        self.assertEqual(sum(r["judged"] for r in by_month), got["checks"]["totals"]["judged"])
        self.assertEqual(got["not_counted"]["person_not_cleared"], 1)
        self.assertEqual(got["not_counted"]["tasks_not_quality"], 3)
        sched = got["schedule"]
        self.assertEqual({k: sched["totals"][k] for k in qc.SLOT_STATES},
                         {"completed": 1, "not_completed": 1, "upcoming": 1, "cancelled": 1})
        self.assertEqual(sched["totals"]["completed_share"], "50.0")
        self.assertEqual(sched["late"], "not_measurable")
        self.assertEqual(got["not_counted"]["staging_slot"], 1)
        self.assertEqual(got["not_counted"]["status_unmapped"], 1)
        self.assertEqual(got["holds"]["batches_held"], 1)
        self.assertEqual(got["hold_release_queue"]["state"], "not_available")
        audit = qc.answer(book, AS_OF, may_read=lambda _k: True, held=QC, view="audit")
        series = {s["title"]["untrusted_text"][:20]: s for s in audit["series"]}
        ph = series["Synthetic pH meter c"]
        self.assertEqual(ph["cycle_days"], 7)
        self.assertEqual([e["object_id"] for e in ph["exceptions"]],
                         [ids["overdue"], ids["missed_open"], ids["missed_done"]])
        daily = qc.answer(book, AS_OF, may_read=lambda _k: True, held=QC, view="daily")
        self.assertEqual([r["object_id"] for r in daily["open_now"]],
                         [ids["missed_open"], ids["overdue"], ids["upcoming"]])
        self.assertEqual(daily["open_counts"], {"missed": 1, "overdue": 1, "upcoming": 1})
        self.assertEqual([s["object_id"] for s in daily["slots_not_completed"]],
                         [ids["slot_open"]])
        for rows in (daily["open_now"], daily["slots_not_completed"],
                     [e for s in audit["series"] for e in s["exceptions"]]):
            for row in rows:
                self.assertTrue(row["citations"], row)
        self.assertTrue(all(s["title"]["origin"]["citation"] for s in audit["series"]))
        tool = qc.QcComplianceTool.__new__(qc.QcComplianceTool)
        for view in (got, audit, daily):
            self.assertEqual(tool._checked(view), view)                 # the contract holds

    def test_a_reader_sees_only_what_it_may_read_and_unknowns_fail_closed(self):
        w, ids = world()
        book = self.book(w)
        refused = qc.answer(book, AS_OF, may_read=lambda _k: True, held=frozenset())
        self.assertEqual(refused["error"]["code"], "quality_controlled_not_held")
        floor_only = qc.answer(book, AS_OF, may_read=lambda k: k == FLOOR, held=QC,
                               view="daily")
        self.assertFalse(floor_only["complete"])
        self.assertNotIn("not_counted", floor_only)
        self.assertEqual(floor_only["checks"]["totals"]["judged"], 0)
        self.assertEqual(floor_only["open_now"], [])
        plan_only = qc.answer(book, AS_OF, may_read=lambda k: k == PLAN, held=QC)
        self.assertEqual(plan_only["schedule"]["totals"]["judged"], 0)
        self.assertEqual(plan_only["holds"]["batches_held"], 0)
        # a cited value whose markings cannot be read is never counted
        broken = dict(book, holds=[qc.Hold(h.records, tuple((x[0], x[1], "not json")
                                                            for x in h.handles))
                                   for h in book["holds"]])
        self.assertEqual(qc.answer(broken, AS_OF, may_read=lambda _k: True,
                                   held=QC)["holds"]["batches_held"], 0)
        self.assertFalse(qc.visible(book["slots"][0], {}, lambda _k: True, QC))
        # no people exclusion: no check is read, the schedule still is
        self.setUp()
        blind = qc.answer(self.book(world()[0], guard=people(unavailable=True)), AS_OF,
                          may_read=lambda _k: True, held=QC, view="audit")
        self.assertEqual(blind["checks"], {"withheld": "people_exclusion_unavailable"})
        self.assertNotIn("series", blind)
        self.assertEqual(blind["schedule"]["totals"]["completed"], 1)

    def test_one_ten_and_sixty_series_are_each_counted_once_and_fit_the_result_limit(self):
        tool = qc.QcComplianceTool.__new__(qc.QcComplianceTool)
        for n in (1, 10, 60):
            self.setUp()
            w = RecentWorld()
            for i in range(n):   # long titles and three open exceptions each: the worst size
                for due in (21, 14, 7):
                    task(w, f"Synthetic thermometer {i} calibration " + "x" * 90, due, None)
            book = self.book(w)
            got = qc.answer(book, AS_OF, may_read=lambda _k: True, held=QC, view="audit")
            self.assertEqual(got["checks"]["totals"]["missed"], 2 * n)
            self.assertEqual(got["checks"]["totals"]["overdue"], n)
            listed = len(got["series"]) + got.get("trimmed", {}).get("series", 0)
            self.assertEqual(listed, min(n, qc.MAX_SERIES))
            self.assertEqual(len({s["title"]["untrusted_text"] for s in got["series"]}),
                             len(got["series"]))
            self.assertTrue(all(s["judged"] == 3 for s in got["series"]))
            self.assertLessEqual(len(json.dumps(got)), qc.BUDGET_BYTES)
            self.assertEqual(tool._checked(got), got)
        # a tight budget drops listed lines from the end, never a figure, and says so
        small = qc.answer(book, AS_OF, may_read=lambda _k: True, held=QC, view="audit",
                          budget=4000)
        self.assertLessEqual(len(json.dumps(small)), 4000)
        self.assertEqual(small["checks"]["totals"], got["checks"]["totals"])
        self.assertEqual(len(small["series"]) + small["trimmed"]["series"], qc.MAX_SERIES)

    def test_the_mode_is_served_by_plx_aggregate_for_a_quality_reader_only(self):
        w, _ids = world()
        w.build(self.store)
        held = {"qa": QC, "ops": frozenset()}
        base = SimpleNamespace(
            store=SimpleNamespace(conn=sqlite3.connect(self.store)),
            policy=SimpleNamespace(held_markings=lambda actor: held[actor],
                                   may_read=lambda actor, key: True),
            _as_known_at=lambda _a: dt.datetime(2026, 10, 19, 6), _store_meta=lambda: {})
        self.addCleanup(base.store.conn.close)
        table = {"plx.aggregate": {"name": "plx.aggregate",
                                   "handler": lambda arguments, actor: {"groups": []}}}
        tool = qc.register(table, base)
        tool.books.read = lambda path, as_of: qc.read_book(path, as_of, people=people())
        call = table["plx.aggregate"]["handler"]
        self.assertEqual(call({"measure": {"op": "count"}, "object_type": "X"}, "qa"),
                         {"groups": []})
        self.assertEqual(call({"mode": "qc_compliance"}, "ops")["error"]["code"],
                         "quality_controlled_not_held")
        self.assertEqual(call({"mode": "qc_compliance", "view": "everything"},
                              "qa")["error"]["code"], "invalid_input")
        tool.wait = 120.0
        got = call({"mode": "qc_compliance", "view": "audit", "measure": {"op": "count"},
                    "object_type": "PlannerTask"}, "qa")
        self.assertEqual((got.get("mode"), got.get("view")), ("qc_compliance", "audit"), got)



    def test_daily_and_audit_packs_cite_every_record_line_and_keep_to_quality_readers(self):
        w, ids = world()
        w.build(self.store)
        book = qc.read_book(str(self.store), AS_OF, people=people())
        alerts = {"status": "read", "gaps": [{
            "kind": "quality_hold_aging", "summary": "Batch on hold 40 days",
            "object_id": "Batch:fx:1", "groups": [[["api://batch/1", "Batch:fx:1"]]]}]}
        daily = br.qc_daily_pack(mode_call(book, QC), QC, alerts, today=AS_OF)
        sections = {s["key"]: s for s in daily["sections"]}
        self.assertEqual([r["object_id"] for r in sections["open_checks"]["rows"]],
                         [ids["missed_open"], ids["overdue"], ids["upcoming"]])
        self.assertEqual({r["object_id"] for r in sections["calibration"]["rows"]},
                         {ids["missed_open"], ids["overdue"], ids["upcoming"]})
        self.assertEqual([r["object_id"] for r in sections["slots"]["rows"]],
                         [ids["slot_open"]])
        self.assertEqual(sections["hold_release"]["state"], "not_available")
        held_figure = sections["holds"]["figures"][0]
        self.assertEqual(held_figure["value"], "1")
        self.assertIn("No hold date is stored", held_figure["note"])
        audit = br.qc_audit_pack(mode_call(book, QC), QC, today=AS_OF)
        a_sections = {s["key"]: s for s in audit["sections"]}
        self.assertEqual(a_sections["by_area"]["figures"][0]["unit"], "percent")
        for pack in (daily, audit):
            for s in pack["sections"]:
                for row in s["rows"]:
                    if row.get("object_id"):   # a line about a record cites it
                        self.assertTrue(row["sources"], (s["key"], row))
        text = br.pack_text(audit)
        self.assertIn("Synthetic pH meter calibration", text)
        self.assertIn("api://", text)
        # an item the reader may not open is neither shown nor counted
        hidden = {r["sources"][0]["handle"] for r in sections["open_checks"]["rows"][:1]}
        decided = br.qc_daily_pack(mode_call(book, QC), QC, alerts, today=AS_OF,
                                   decide=lambda hs: {h: ({"reason": "no"} if h in hidden
                                                          else None) for h in hs})
        open_rows = {s["key"]: s for s in decided["sections"]}["open_checks"]["rows"]
        self.assertNotIn(ids["missed_open"], [r["object_id"] for r in open_rows])
        # a reader without quality_controlled: every section says so, nothing is read
        calls = []
        outside = br.qc_daily_pack(lambda n, a: calls.append(n) or {}, frozenset(), alerts,
                                   today=AS_OF)
        self.assertEqual(calls, [])
        self.assertEqual({s["state"] for s in outside["sections"]}, {"quality_marked"})
        self.assertTrue(all(not s["rows"] and not s["figures"] for s in outside["sections"]))


if __name__ == "__main__":
    import unittest
    unittest.main()
