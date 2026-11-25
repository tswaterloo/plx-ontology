"""Assist's whole-job routes (``operator_api/assist_jobs.py``): a request, the list, Stop, a piece's
decision and one change's prepare, through the app with an in-memory job log.

Checked at the boundary: a request appends one ``requested`` row (card numbers masked) and nothing else;
the list shows the reader's own jobs only and a colleague's job is a 404 on every route; the API appends
only what the person does; Stop is one row and none once the job has ended; an edit of anything but a
message's subject or body is refused; a change is prepared through the one desk path with the person's
edit applied, its card id is logged, and a second ask answers the same card (no second preparation).
Evidence label: injected-transport (a fake job log, a fake desk)."""
from __future__ import annotations

import json
import unittest

from operating import assist_job as aj
from operator_api import models as m
from operator_api.tests import test_autopilot_area as base
from operator_api.tests.test_autopilot_area import COLLEAGUE, OWNER


class Log:
    """The job log in memory, answering exactly the statements ``operating.assist_job`` and the routes make."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.committed = 0
        self._out: list = []

    def connection(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.committed += 1

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO assist.job_log"):
            job_id, person, what, detail = params
            self.rows.append({"seq": len(self.rows) + 1, "job_id": job_id, "person": person, "at": "2026-11-24T20:00:00+00:00",
                              "what": what, "detail": json.loads(detail)})
            self._out = []
        elif "WHERE job_id = %s" in s:
            self._out = [tuple(r[k] for k in ("seq", "job_id", "person", "at", "what", "detail")) for r in self.rows
                         if r["job_id"] == params[0]]
        elif "what = 'requested' AND person = ANY" in s:
            self._out = [(r["job_id"],) for r in reversed(self.rows) if r["what"] == "requested" and r["person"] in params[0]]
        elif "WHERE job_id = ANY" in s:
            self._out = [tuple(r[k] for k in ("seq", "job_id", "person", "at", "what", "detail")) for r in self.rows
                         if r["job_id"] in params[0]]
        else:
            raise AssertionError("unexpected statement: " + s)
        return self

    def fetchall(self):
        return self._out

    def fetchone(self):
        return self._out[0] if self._out else None

    def whats(self, job_id=None):
        return [r["what"] for r in self.rows if job_id in (None, r["job_id"])]


MAIL = {"tool": "plx.prepare_write", "arguments": {"system": "mail", "operation": "send_email", "target": {},
                                                   "change": {"to": ["b@x.example"], "subject": "PO", "body": "Where is it?"},
                                                   "reason": "chase"}}


class Jobs(unittest.TestCase):
    client = base.AutopilotArea.client

    def setUp(self) -> None:
        base.AutopilotArea.setUp(self)
        self.log = Log()
        self.backend.memory = type("M", (), {"pool": self.log})()

    def finished_job(self, person=OWNER[1], status="finished") -> str:
        jid = "j" * 24
        aj.append(self.log, jid, person, "requested", {"text": "chase DSM"})
        aj.append(self.log, jid, person, "started", {})
        aj.append(self.log, jid, person, "planned", {"summary": "s", "checklist": [], "found": "", "not_found": "",
                                                     "nothing": "", "pieces": 1})
        aj.append(self.log, jid, person, "piece", {"n": 1, "title": "Chase", "instruction": "i", "status": "prepared",
                                                   "proposals": [MAIL], "reads": ["po://1"], "tool_reads": []})
        if status:
            aj.append(self.log, jid, person, status, {})
        return jid

    def test_a_request_appends_one_requested_row_masks_a_card_and_writes_nothing_else(self):
        got = self.client(OWNER).post("/api/assist/jobs", json={"text": "chase DSM, card 4111 1111 1111 1111"})
        self.assertEqual(got.status_code, 200, got.text)
        body = got.json()
        self.assertEqual((body["status"], body["pieces"]), ("queued", []))
        self.assertNotIn("4111 1111 1111 1111", body["text"])
        self.assertEqual(self.log.whats(), ["requested"])
        self.assertEqual(self.log.rows[0]["person"], OWNER[1])
        self.assertGreaterEqual(self.log.committed, 1)
        self.assertEqual(self.bc.calls, [])

    def test_an_empty_job_is_refused_and_logs_nothing(self):
        for text in ("", "   "):
            self.assertIn(self.client(OWNER).post("/api/assist/jobs", json={"text": text}).status_code, (422, 400))
        self.assertEqual(self.log.rows, [])

    def test_the_list_is_the_readers_own_and_a_colleagues_job_is_404_everywhere(self):
        jid = self.finished_job(person=OWNER[1])
        mine = self.client(OWNER).get("/api/assist/jobs").json()["jobs"]
        self.assertEqual([j["job_id"] for j in mine], [jid])
        self.assertEqual(self.client(COLLEAGUE).get("/api/assist/jobs").json()["jobs"], [])
        c = self.client(COLLEAGUE)
        for method, path, kw in (("post", f"/api/assist/jobs/{jid}/stop", {}),
                                 ("post", f"/api/assist/jobs/{jid}/pieces/1/decision", {"json": {"decision": "approved"}}),
                                 ("post", f"/api/assist/jobs/{jid}/pieces/1/changes/0/prepare", {})):
            self.assertEqual(getattr(c, method)(path, **kw).status_code, 404, path)
        self.assertEqual(self.log.whats(jid), ["requested", "started", "planned", "piece", "finished"])

    def test_stop_is_one_row_and_none_once_the_job_has_ended(self):
        jid = self.finished_job(status="")
        for _ in range(2):
            got = self.client(OWNER).post(f"/api/assist/jobs/{jid}/stop")
            self.assertEqual((got.status_code, got.json()["status"]), (200, "stopping"))
        self.assertEqual(self.log.whats(jid).count("stop"), 1)
        done = self.finished_job(status="finished")
        self.assertEqual(self.client(OWNER).post(f"/api/assist/jobs/{'k' * 24}/stop").status_code, 404)
        before = len(self.log.rows)
        self.client(OWNER).post(f"/api/assist/jobs/{done}/stop")
        self.assertEqual(len(self.log.rows), before)

    def test_a_decision_is_recorded_only_and_a_wrong_edit_is_refused(self):
        jid = self.finished_job()
        c = self.client(OWNER)
        ok = c.post(f"/api/assist/jobs/{jid}/pieces/1/decision",
                    json={"decision": "edited", "text": "mine", "edits": {"0": {"body": "Hello"}}})
        self.assertEqual(ok.status_code, 200, ok.text)
        piece = ok.json()["pieces"][0]
        self.assertEqual((piece["decision"], piece["proposals"][0]["arguments"]["change"]["body"]), ("edited", "Hello"))
        for bad in ({"decision": "edited", "edits": {"0": {"to": ["z@evil.example"]}}},
                    {"decision": "edited", "edits": {"7": {"body": "x"}}},
                    {"decision": "edited"}):
            self.assertEqual(c.post(f"/api/assist/jobs/{jid}/pieces/1/decision", json=bad).status_code, 422, bad)
        self.assertEqual(c.post(f"/api/assist/jobs/{jid}/pieces/9/decision", json={"decision": "approved"}).status_code, 404)
        self.assertEqual(self.log.whats(jid)[-1], "decided")
        self.assertEqual(self.bc.calls, [])

    def test_a_change_is_prepared_once_with_the_persons_edit_and_a_reopen_answers_the_same_card(self):
        jid = self.finished_job()
        aj.append(self.log, jid, OWNER[1], "decided", {"piece": 1, "decision": "edited", "text": "",
                                                       "edits": {"0": {"body": "Edited body"}}})
        prepared: list = []
        backend = self.backend
        backend.prepare_proposal = lambda who, proposal, origin, citations=(): (prepared.append((proposal, origin, list(citations))) or "pw_1")
        desk = type("D", (), {"card": lambda s, person, pid: {"prepared_id": pid}})()
        backend._pw_desk = lambda who: desk
        backend._pw_person = lambda who: "person"
        backend._pw_do = lambda step: step()
        backend._pw_card = lambda card: type("C", (), {"model_dump": lambda s, **k: card, "prepared_id": card["prepared_id"]})()
        backend._apa_job_stage = lambda card: "open"
        backend._pw_card = lambda card: m.PreparedWrite(prepared_id=card["prepared_id"], state="prepared")
        url = f"/api/assist/jobs/{jid}/pieces/1/changes/0/prepare"
        first = self.client(OWNER).post(url)
        self.assertEqual((first.status_code, first.json()["prepared_id"]), (200, "pw_1"), first.text)
        self.assertEqual(len(prepared), 1)
        proposal, origin, cites = prepared[0]
        self.assertEqual((origin, proposal["arguments"]["change"]["body"], cites), ("assist_job", "Edited body", ["po://1"]))
        self.assertEqual(self.log.rows[-1]["what"], "prepared")
        self.assertEqual(self.log.rows[-1]["detail"], {"piece": 1, "index": 0, "prepared_id": "pw_1"})
        again = self.client(OWNER).post(url)
        self.assertEqual((again.status_code, again.json()["prepared_id"]), (200, "pw_1"))
        self.assertEqual(len(prepared), 1)                          # the same card again, no second preparation
        self.assertEqual(self.client(OWNER).post(f"/api/assist/jobs/{jid}/pieces/1/changes/5/prepare").status_code, 404)

    def test_missing_0082_table_returns_not_run_instead_of_500(self):
        class MissingTable(Exception):
            sqlstate = "42P01"

        class MissingLog(Log):
            def execute(self, sql, params=()):
                raise MissingTable('relation "assist.job_log" does not exist')

        self.backend.memory = type("M", (), {"pool": MissingLog()})()
        got = self.client(OWNER).get("/api/assist/jobs")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["status"], "not_run")
        self.assertIn("0082", got.json()["reason"])
        self.assertEqual(self.client(OWNER).post("/api/assist/jobs", json={"text": "chase"}).status_code, 409)

    def test_piece_with_more_than_four_changes_reports_over_limit_and_explains_refusal(self):
        jid = "m" * 24
        aj.append(self.log, jid, OWNER[1], "requested", {"text": "many changes"})
        aj.append(self.log, jid, OWNER[1], "started", {})
        aj.append(self.log, jid, OWNER[1], "planned", {"summary": "s", "checklist": [], "found": "", "not_found": "",
                                                       "nothing": "", "pieces": 1})
        aj.append(self.log, jid, OWNER[1], "piece", {"n": 1, "title": "Many", "instruction": "i", "status": "prepared",
                                                     "proposals": [MAIL] * 6, "reads": ["po://1"], "tool_reads": []})
        aj.append(self.log, jid, OWNER[1], "finished", {})
        listed = self.client(OWNER).get("/api/assist/jobs").json()["jobs"]
        piece = listed[0]["pieces"][0]
        self.assertEqual((len(piece["proposals"]), piece["changes_over_limit"]), (6, 2))
        over = self.client(OWNER).post(f"/api/assist/jobs/{jid}/pieces/1/changes/4/prepare")
        self.assertEqual(over.status_code, 409)
        self.assertIn("4", over.json()["error"]["message"])


class Notes(unittest.TestCase):
    def test_a_person_with_no_memory_key_has_no_notes_and_the_session_goes_on(self):
        """The prepared sessions' notes read crashed with a NameError for a person without a key (11-24 pilot)."""
        from operator_api import autopilot_context as ac
        notes = ac.person_notes(type("Mem", (), {"pool": None})())
        with self.assertLogs("operator_api.autopilot_context", level="INFO"):
            self.assertEqual(notes("not-a-person-id"), [])


if __name__ == "__main__":
    unittest.main()
