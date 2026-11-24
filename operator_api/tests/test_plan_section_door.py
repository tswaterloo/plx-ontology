"""The late section from its stored row (``operator_api.plan_section_door``): the run it gives the
section's presentation is the one the live plan's verdict gives (``page_parts.late_of``), a mail
the reader may not open, a missing or unreadable row, another kind of record and any switch value
but exactly ``1`` all read the plan live."""
from __future__ import annotations

import json
import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

from operator_api import models as m
from operator_api import plan_equivalence as eq
from operator_api import plan_section_door as door
from operator_api.page_parts import late_of

OID = "PurchaseOrder:bc.purchase_orders:{\"id\":\"a\"}"
LIVE = {"verdict": {"state": "late", "text": "Late by 152 days: planned arrival 2026-06-24",
                    "subject": {"object_id": OID},
                    "facts": [{"text": "Planned arrival: 2026-06-24", "citation": "bc://a@1",
                               "openable": True},
                              {"text": "Promised by mail", "citation": "graph-mail://m/1",
                               "openable": True},
                              {"text": "no citation", "openable": True},
                              {"text": "closed to this reader", "citation": "api://x@1",
                               "openable": False}],
                    "follow_ups": []}}


DAY = date(2026, 11, 23)
RAN = "2026-11-23T15:00:00+00:00"          # 10:00 ET the same business day
def stored(run=LIVE, comm=("graph-mail://m/1",), built=RAN, current=True):
    v = run["verdict"]
    facts = [{"text": f["text"], "citation": f["citation"]} for f in v["facts"]
             if f.get("citation") and f.get("openable")]
    payload = {"family": "late_po", "state": v["state"], "text": v["text"], "facts": facts}
    return {"reads": {"plan_section": {
        "columns": ["section", "payload_text", "comm_handles", "handle", "built_at", "current"],
        "rows": [["late", json.dumps(payload), list(comm), "h", built, current]]}}}


BACKEND = SimpleNamespace(_source=lambda v, h: m.Source(system="BC", handle=h, label=h))


class View:
    def __init__(self, out, refuse=()):
        self.out, self.refuse, self.calls = out, set(refuse), []
        self.access = SimpleNamespace(decide=lambda hs: {
            h: ({"reason": "no"} if h in self.refuse else None) for h in hs})

    def _request(self):
        import contextlib
        return contextlib.nullcontext()

    def _call(self, tool, args):
        self.calls.append((tool, args))
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


class PlanSectionDoor(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {door.SWITCH: "1"})
        patch.start()
        self.addCleanup(patch.stop)

    def got(self, view, kind="PurchaseOrder"):
        return door.precomputed(view, kind, OID, "late_po", DAY)

    def test_the_stored_run_gives_the_section_the_live_plans_own_model(self):
        got = self.got(View(stored()))
        self.assertEqual(eq.model(got), eq.model(LIVE))
        self.assertEqual(late_of(BACKEND, SimpleNamespace(),
                                 got)[1][0].line, "Planned arrival: 2026-06-24")
        # exactly the cited lines the live run shows, in its order
        self.assertEqual([l.line for l in late_of(
            BACKEND, SimpleNamespace(), got)[1]],
            [l.line for l in late_of(BACKEND,
                                     SimpleNamespace(), LIVE)[1]])

    def test_a_stored_people_answer_gives_the_page_the_same_people_as_the_live_run(self):
        entries = [{"kind": "computed", "value": {"origin": {"citation": "graph-mail://m/1"},
                                                  "untrusted_text": n},
                    "steps": ["roles: approver, sender"]} for n in ("Ann", "Bo")]
        live = {"investigation": {"answer": entries + [{"kind": "computed", "value": {}}]}}
        payload = {"family": "people_on_record", "entries": entries, "read": ["graph-mail://m/1"]}
        out = {"reads": {"plan_section": {
            "columns": ["section", "payload_text", "comm_handles", "handle", "built_at", "current"],
            "rows": [["people", json.dumps(payload), ["graph-mail://m/1"], "h", RAN, True]]}}}
        view = View(out)
        got = door.precomputed(view, "PurchaseOrder", OID, "people_on_record", DAY)
        self.assertEqual(eq.model(got, "people_on_record", view), eq.model(live, "people_on_record", view))
        self.assertTrue(eq.model(got, "people_on_record", view))
        self.assertIsNone(door.precomputed(View(out, refuse=["graph-mail://m/1"]), "PurchaseOrder",
                                           OID, "people_on_record", DAY))
        # a row made before it listed everything its run read is no row (the page reads live)
        del payload["read"]
        old = {"reads": {"plan_section": {**out["reads"]["plan_section"], "rows": [[
            "people", json.dumps(payload), ["graph-mail://m/1"], "h", RAN, True]]}}}
        self.assertIsNone(door.precomputed(View(old), "PurchaseOrder", OID, "people_on_record", DAY))

    def test_a_stored_landed_cost_gives_the_page_the_same_figures_as_the_live_run(self):
        from operator_api.page_parts import landed_cost_of
        from operating.pg import plan_sections as ps
        from operator_api import plan_batch
        answer = [
            {"name": "unit_price_1", "kind": "property", "citation": "bc://line/1@1", "value": {"x": 1}},
            {"name": "landed_cost_line_1", "kind": "computed", "value": "125.50", "currency": "CAD",
             "unit": "CAD", "inputs": ["unit_price_1"], "steps": ["s"]},
            {"name": "landed_cost_line_2", "kind": "computed", "value": "10", "currency": "CAD",
             "citation": "graph-mail://m/1"},
            {"name": "landed_cost", "kind": "computed", "value": "135.50", "currency": "CAD",
             "inputs": ["landed_cost_line_1", "landed_cost_line_2"]},
            {"name": "landed_cost_estimate", "kind": "computed", "value": "9", "currency": "CAD"},
            {"name": "unrelated", "kind": "property", "citation": "bc://other@1"}]
        live = {"investigation": {"answer": answer, "answerability": {"state": "answerable"}},
                "verdict": {"state": "x", "follow_ups": [{"missing": "po freight not recorded"}]}}
        line = {"record_id": OID, "family": "landed_cost", **plan_batch.extract("landed_cost", live)}
        got = ps.item(line)
        self.assertIsNotNone(got)
        _, section, payload, cites = got
        self.assertEqual(section, "landed")
        self.assertEqual(sorted(cites), ["bc://line/1@1", "graph-mail://m/1"])   # only what the figures rest on
        names = [e["name"] for e in payload["entries"]]
        self.assertNotIn("unrelated", names)
        out = {"reads": {"plan_section": {
            "columns": ["section", "payload_text", "comm_handles", "handle", "built_at", "current"],
            "rows": [["landed", json.dumps(payload), ["graph-mail://m/1"], "h", RAN, True]]}}}
        view = View(out)
        ran = door.precomputed(view, "PurchaseOrder", OID, "landed_cost", DAY)
        self.assertIsNotNone(ran)
        a, b = landed_cost_of(BACKEND, view, ran), landed_cost_of(BACKEND, view, live)
        self.assertEqual(a.model_dump(), b.model_dump())
        self.assertEqual((a.status, a.total.amount, len(a.lines), a.missing),
                         ("computed", "135.50", 2, ["po freight not recorded"]))
        self.assertIsNone(door.precomputed(View(out, refuse=["graph-mail://m/1"]), "PurchaseOrder",
                                           OID, "landed_cost", DAY))

    def test_a_landed_cost_run_that_failed_is_not_stored_by_its_status_and_a_long_figure_is_kept_whole(self):
        from operating.pg import plan_sections as ps
        from operator_api import plan_batch
        base = {"entries": [{"name": "landed_cost", "kind": "computed", "value": "1", "currency": "CAD"}],
                "answerability": "answerable", "missing": []}
        for run in ({"entries": "x"}, None):
            self.assertIsNone(ps.item({"record_id": OID, "family": "landed_cost", "landed": run}), run)
        # the status is structured: the plan's words (even "step failed") decide nothing
        words = {**base, "verdict_text": "Not established: no such record, step failed"}
        ok = {"status": "ok", "reasons": []}
        self.assertEqual(ps.outcome({"record_id": OID, "family": "landed_cost", "landed": words,
                                     "run": ok, "opened": ["bc://x@1"]}), ("answer", ""))
        self.assertEqual(ps.outcome({"record_id": OID, "family": "landed_cost", "landed": words, "opened": ["bc://x@1"],
                                     "run": {"status": "failed", "reasons": ["step_failed"]}}),
                         ("failed", "the plan's run failed: step_failed"))
        long = {**base, "entries": [{"name": "landed_cost", "kind": "computed", "value": "9" * 90}]}
        got = ps.item({"record_id": OID, "family": "landed_cost", "landed": long})
        self.assertEqual(got[2]["entries"][0]["value"], "9" * 90)       # not cut, not dropped
        self.assertIsNone(plan_batch.extract("landed_cost", {"investigation": {"error": "x", "answer": []}}))
        self.assertIsNone(plan_batch.extract("landed_cost", {"investigation": {"answer": "no"}}))
        self.assertIsNotNone(ps.item({"record_id": OID, "family": "landed_cost", "landed": base}))

    def test_a_stored_trail_is_the_raw_trail_for_the_pages_own_mapper_and_never_for_a_closed_mail(self):
        from operator_api import trail as trails
        raw = {"subject": OID, "state": "late", "steps": [
            {"kind": "due_date", "title": "Requested by 2026-06-01", "handle": "bc://a@1",
             "check": "read_from_record"},
            {"kind": "confirmation", "title": "Promise", "handle": "graph-mail://m/1",
             "check": "read_from_record", "date": "2026-07-01"}], "hidden": 0, "gaps": []}
        out = {"reads": {"plan_section": {
            "columns": ["section", "payload_text", "comm_handles", "handle", "built_at", "current"],
            "rows": [["trail", json.dumps({"family": "po_trail", "raw": raw}),
                      ["graph-mail://m/1"], "h", RAN, True]]}}}
        view = View(out)
        with mock.patch.object(door, "datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(2026, 11, 23, 12, 0, tzinfo=door.BUSINESS_ZONE)
            dt.fromisoformat = __import__("datetime").datetime.fromisoformat
            got = trails.stored_trail(view, OID, "PO 1")
            self.assertEqual(got["label"], "PO 1")
            self.assertEqual(got["steps"], raw["steps"])
            with mock.patch.object(trails, "reads_every_item", return_value=False):
                self.assertTrue(trails.stored_trail(view, OID, "PO 1")["may_hide"])
            with mock.patch.object(trails, "reads_every_item", return_value=True):
                self.assertFalse(trails.stored_trail(view, OID, "PO 1")["may_hide"])
            self.assertIsNone(trails.stored_trail(View(out, refuse=["graph-mail://m/1"]), OID, "PO 1"))
        self.assertIsNone(trails.stored_trail(View(RuntimeError("down")), OID, "x"))

    def test_a_not_established_run_is_the_live_runs_empty_section(self):
        run = {"verdict": {"state": "not_established", "text": "Not established: x", "facts": []}}
        got = self.got(View(stored(run, comm=())))
        self.assertEqual(eq.model(got), eq.model(run))
        self.assertEqual(late_of(None, None, got), (None, []))

    def test_a_mail_the_reader_may_not_open_reads_the_plan_live(self):
        self.assertIsNone(self.got(View(stored(), refuse=["graph-mail://m/1"])))

    def test_no_row_a_broken_row_or_a_failed_read_reads_the_plan_live(self):
        empty = {"reads": {"plan_section": {"columns": ["section"], "rows": []}}}
        twice = stored()
        twice["reads"]["plan_section"]["rows"] *= 2
        for out in (empty, twice, {"error": {"code": "busy"}}, {}, None, RuntimeError("down")):
            self.assertIsNone(self.got(View(out)), out)
        bad = stored()
        bad["reads"]["plan_section"]["rows"][0][1] = "{not json"
        self.assertIsNone(self.got(View(bad)))
        other = stored()
        other["reads"]["plan_section"]["rows"][0][1] = json.dumps({"family": "people_on_record",
                                                                  "state": "x"})
        self.assertIsNone(self.got(View(other)))

    def test_an_old_row_is_served_with_its_age_and_never_sends_the_page_to_the_live_plan(self):
        self.assertIsNotNone(self.got(View(stored())))
        before = door.rates()
        # built 23:30 ET the evening before, built on an earlier day, and a row whose
        # dependencies moved (the view says current False): each is served, marked with its age
        for built, current in (("2026-11-23T04:30:00+00:00", True), ("2026-11-22T15:00:00+00:00", True),
                               (RAN, False)):
            with door.stale_scope() as old:
                got = self.got(View(stored(built=built, current=current)))
            self.assertIsNotNone(got, built)
            self.assertEqual(old, [built])
            self.assertEqual(got["precomputed"]["stale"], "moved" if not current else "day")
        with door.stale_scope() as old:
            self.assertIsNotNone(self.got(View(stored())))
        self.assertEqual(old, [])      # a current row carries no age
        after = door.rates()
        self.assertEqual(after.get("served_stale", 0) - before.get("served_stale", 0), 3)
        self.assertEqual(after.get("served", 0) - before.get("served", 0), 1)
        # a time that is not one: the day cannot be shown to be today's, so it is old
        self.assertEqual(self.got(View(stored(built="garbage")))["precomputed"]["stale"], "day")

    def test_the_totals_are_logged_each_hour(self):
        door._RATES_LAST[0] = door.time.monotonic() - door.RATES_LOG_EVERY - 1
        with self.assertLogs("operator_api.plan_section_door", "INFO") as logged:
            door.count("served")
        self.assertIn("plan sections read: {", logged.output[0])
        self.assertIn('"served"', logged.output[0])

    def test_a_fallback_says_why_and_is_counted(self):
        before = door.rates()
        with self.assertLogs("operator_api.plan_section_door", "WARNING") as logged:
            self.assertIsNone(self.got(View({"reads": {"plan_section": {"columns": ["section"],
                                                                         "rows": []}}})))
            self.assertIsNone(self.got(View(RuntimeError("down"))))
            self.assertIsNone(self.got(View(stored(), refuse=["graph-mail://m/1"])))
        text = "\n".join(logged.output)
        for reason in ("no_row", "read_failed", "mail_not_openable"):
            self.assertIn(reason, text)
            self.assertEqual(door.rates()["fallback:" + reason] - before.get("fallback:" + reason, 0), 1)

    def test_the_door_asks_for_the_row_alone_what_moved_is_decided_in_the_view(self):
        view = View(stored())
        self.assertIsNotNone(self.got(view))
        names = [r["name"] for r in view.calls[0][1]["reads"]]
        self.assertEqual(names, ["plan_section"])    # no global stamp, no view stamps read here

    def test_the_day_is_the_new_york_business_day_never_utc(self):
        # 22:30 ET on the 23rd is already the 24th in UTC: still today's answer on the 23rd
        self.assertIsNotNone(self.got(View(stored(built="2026-11-24T03:30:00+00:00"))))
        # 00:30 ET on the 24th is the 24th: not the 23rd's, so old
        self.assertEqual(self.got(View(stored(built="2026-11-24T05:30:00+00:00")))["precomputed"]["stale"], "day")

    def test_other_kinds_families_and_a_switch_not_exactly_1_read_live(self):
        view = View(stored())
        self.assertIsNone(self.got(view, kind="SalesOrder"))
        self.assertIsNone(door.precomputed(view, "PurchaseOrder", OID, "some_other_plan"))
        self.assertIsNone(door.precomputed(view, "PurchaseOrder", "x" * 300, "late_po"))
        self.assertEqual(view.calls, [])
        for value in ("", "0", "true", " 1", "1\n", "yes"):
            with mock.patch.dict(os.environ, {door.SWITCH: value}):
                self.assertIsNone(self.got(view), repr(value))
        self.assertEqual(view.calls, [])


class EquivalenceTool(unittest.TestCase):
    def test_only_the_capture_freshness_note_is_set_aside_and_anything_else_still_differs(self):
        a = "Late: open on 2026-11-23 (152 days)."
        b = "Late: open on 2026-11-23 (152 days) (as captured: the portal_api capture is stale)."
        self.assertEqual(eq.without_capture_note((b, 1)), eq.without_capture_note((a, 1)))
        self.assertNotEqual(eq.without_capture_note(b.replace("152", "153")), eq.without_capture_note(a))


class SectionAge(unittest.TestCase):
    def test_a_section_read_from_an_old_row_says_since_when_and_a_current_one_does_not(self):
        import contextlib
        from operator_api import backend as be
        page = SimpleNamespace(object_id=OID, type="PurchaseOrder", label="PO")
        for built, current, want in ((RAN, True, None), ("2026-11-22T15:00:00+00:00", True,
                                                         "2026-11-22T15:00:00+00:00"),
                                     (RAN, False, RAN)):
            view = View(stored(built=built, current=current))
            view._request = lambda: contextlib.nullcontext()

            def plan(backend, v, kind, oid, family):
                return {family: door.precomputed(v, kind, oid, family, DAY)}
            with mock.patch.dict(os.environ, {door.SWITCH: "1"}), \
                    mock.patch.object(be, "kept_plan_run", plan), \
                    mock.patch.object(be.presentation, "polished", lambda x: x):
                got = be.read_section(BACKEND, view, page, "late")
            self.assertEqual(got.stale_since, want, (built, current))
            self.assertEqual(got.status.state, "late")


class RecordDoorCounter(unittest.TestCase):
    def test_the_door_counts_served_and_fallbacks_and_logs_them_each_hour(self):
        from operator_api import page_sql
        page_sql._DOOR_COUNTS.update(served=0, fell_back=0)
        page_sql._DOOR_LAST[0] = page_sql.time.monotonic()
        for served in (True, True, False):
            page_sql.count_door(served)
        self.assertEqual(page_sql._DOOR_COUNTS, {"served": 2, "fell_back": 1})
        page_sql._DOOR_LAST[0] -= page_sql._DOOR_LOG_EVERY
        with self.assertLogs("operator_api.page_sql", "INFO") as logged:
            page_sql.count_door(True)
        self.assertIn("3 served, 1 fell back", logged.output[0])
        self.assertEqual(page_sql._DOOR_COUNTS, {"served": 0, "fell_back": 0})


class MissAnswersNotRun(unittest.TestCase):
    """A record page's section with no stored row is answered ``not_run`` and asked of the runner:
    no plan runs in the request. A reader who may not open what a row cites reads their own plan."""

    def setUp(self):
        import tempfile
        self.spool = tempfile.mkdtemp()
        for patch in (mock.patch.dict(os.environ, {door.SWITCH: "1", "PLX_PLAN_REQUESTS": self.spool}),):
            patch.start()
            self.addCleanup(patch.stop)

    def kept(self, view):
        from operator_api import backend as be
        live = mock.Mock(side_effect=AssertionError("a plan ran in the request"))
        with mock.patch.object(be, "plan_runs", live), door.not_run_scope() as nr:
            got = be.kept_plan_run(SimpleNamespace(today=None, page_cache=None), view,
                                   "PurchaseOrder", OID, "late_po")
        return got, nr, live

    def test_a_missing_row_is_not_run_and_requested_with_no_plan_run(self):
        got, nr, live = self.kept(View({"reads": {"plan_section": {"columns": [], "rows": []}}}))
        self.assertEqual(got, {})
        self.assertEqual([(n["family"], n["why"], n["requested"]) for n in nr],
                         [("late_po", "no_row", True)])
        live.assert_not_called()
        import operating.plan_requests as pr
        self.assertEqual([rid for _n, rid in pr.waiting(self.spool)], [OID])
        self.kept(View({"reads": {"plan_section": {"columns": [], "rows": []}}}))   # asked once
        self.assertEqual(len(pr.waiting(self.spool)), 1)

    def test_an_unreadable_request_spool_is_said_not_hidden(self):
        with mock.patch.dict(os.environ, {"PLX_PLAN_REQUESTS": "/proc/none/x"}):
            _got, nr, _live = self.kept(View({"reads": {"plan_section": {"columns": [], "rows": []}}}))
        self.assertEqual([n["requested"] for n in nr], [False])

    def test_a_stored_no_answer_is_said_typed_and_never_run(self):
        out = stored()
        payload = {"family": "late_po", "no_answer": "the plan gave no complete answer when it ran"}
        out["reads"]["plan_section"]["rows"][0][1] = json.dumps(payload)
        got, nr, live = self.kept(View(out))
        self.assertEqual(got, {})
        self.assertEqual([(n["why"], n["stored"]) for n in nr], [(payload["no_answer"], True)])
        live.assert_not_called()

    def test_a_stored_failure_is_said_as_failed_with_its_reason_and_never_run_or_shown_as_an_answer(self):
        out = stored()
        out["reads"]["plan_section"]["rows"][0][1] = json.dumps(
            {"family": "late_po", "failed": "the plan raised TimeoutError"})
        got, nr, live = self.kept(View(out))
        self.assertEqual(got, {})
        self.assertEqual([(n["status"], n["requested"], n["stored"]) for n in nr],
                         [("failed", False, True)])
        self.assertIn("the plan raised TimeoutError", nr[0]["why"])
        self.assertIn("attempt 1", nr[0]["why"])
        live.assert_not_called()

    def test_a_plan_that_raised_or_returned_an_error_is_a_failed_line_with_the_exact_reason(self):
        from unittest import mock
        from operator_api import plan_batch
        view = mock.MagicMock()
        with mock.patch("operator_api.backend.plan_runs", side_effect=KeyError("x")), \
                self.assertLogs("operator_api.plan_batch", "WARNING"):
            line = plan_batch.answer(view, OID, "late_po")
        self.assertEqual(line["failed"], "the plan raised KeyError")
        run = {"investigation": {"error": {"code": "tool_timeout"}, "answer": None}}
        with mock.patch("operator_api.backend.plan_runs", return_value={"people_on_record": run}):
            line = plan_batch.answer(view, OID, "people_on_record")
        self.assertEqual(line["failed"], "the investigation failed: tool_timeout")
        from operating.pg import plan_sections as ps
        self.assertEqual(ps.outcome(line), ("failed", "the investigation failed: tool_timeout"))
        # a run whose investigation reports a failed step is a failed line by its reason codes,
        # whatever its verdict says; one that completed with only data reasons is an answer
        verdict = {"state": "not_established", "text": "Not established: no such record, step failed"}
        bad = {"verdict": verdict, "investigation": {"answer": [], "answerability": {
            "state": "blocked", "reasons": ["root_not_found", "step_failed"]}}}
        with mock.patch("operator_api.backend.plan_runs", return_value={"late_po": bad}):
            line = plan_batch.answer(view, OID, "late_po")
        self.assertEqual(ps.outcome(line), ("failed",
                                            "the plan's run failed: root_not_found, step_failed"))
        fine = {"verdict": {**verdict, "text": "Not established: no supplier confirmation"},
                "investigation": {"answer": [], "answerability": {
                    "state": "partial", "reasons": ["no_confirmation"]}}}
        from operating.ontology_tools_policy import note_opened

        def ran(*args, **kwargs):      # a real run asks the door about the record it reads
            note_opened(["bc://x@1"])
            return {"late_po": fine}
        with mock.patch("operator_api.backend.plan_runs", side_effect=ran):
            line = plan_batch.answer(view, OID, "late_po")
        self.assertEqual(ps.outcome(line), ("answer", ""))
        # a trail is read without a time limit: a cut trail reads on
        with mock.patch("operating.evidence_trail.po_trail", return_value={"steps": []}) as trail:
            plan_batch.answer(view, OID, "po_trail")
        self.assertIsNone(trail.call_args.kwargs["seconds"])

    def test_a_reader_who_may_not_open_the_cited_mail_reads_their_own_plan(self):
        from operator_api import backend as be
        with mock.patch.object(be, "plan_runs", return_value={"late_po": {"x": 1}}) as live, \
                door.not_run_scope() as nr:
            be.page_cache = None
            got = be.kept_plan_run(SimpleNamespace(today=None, page_cache=mock.Mock(
                kept=lambda view, key, read, shown, copy: read())), View(
                    stored(), refuse=["graph-mail://m/1"]), "PurchaseOrder", OID, "late_po")
        self.assertEqual(got, {"late_po": {"x": 1}})
        self.assertEqual(nr, [])
        live.assert_called()

    def test_the_pullers_round_hands_the_requests_to_the_runner_and_clears_the_spool(self):
        from operating.pg import plan_sections_pull as pull
        import operating.plan_requests as pr
        pr.request(OID)
        sent = []
        transport = SimpleNamespace(put_ids=lambda ids, name: sent.append((list(ids), name)))
        self.assertEqual(pull.send_requests(transport, self.spool), 1)
        self.assertEqual(sent[0][0], [OID])
        self.assertTrue(sent[0][1].endswith(".ids"))
        self.assertEqual(pr.waiting(self.spool), [])
        failing = SimpleNamespace(put_ids=mock.Mock(side_effect=RuntimeError("ssh")))
        pr.request(OID)
        with self.assertRaises(RuntimeError):
            pull.send_requests(failing, self.spool)
        self.assertEqual(len(pr.waiting(self.spool)), 1)       # not delivered: waits


if __name__ == "__main__":
    unittest.main()
