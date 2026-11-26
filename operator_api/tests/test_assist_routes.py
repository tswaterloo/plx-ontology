"""Assist's routes (``operator_api/assist_routes.py``, ASSIST specs 2.3): the list read from
matters through the reader's door (decided once for the whole list), the status words when
matters are off or stale, a matter id answered here and any other id by the queue's own
routes, draft on request writing only a ``requested`` log row, and refusals said.

SYNTHETIC matters through a fake pool and a fake ``as_items``; the SQL itself is tested
in ``operating/tests/test_assist_matters`` against a real Postgres. Evidence label:
``injected-transport``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from operator_api.tests import test_autopilot_area as base
from operator_api.tests.test_autopilot_area import COLLEAGUE, OWNER

M_OK, M_CUT, M_MARK, M_OLD = ("a" * 24, "b" * 24, "c" * 24, "d" * 24)
NOW = datetime.now(timezone.utc) - timedelta(seconds=5)


def matter(matter_id: str, **over) -> dict:
    base = {"item_id": matter_id, "owner": OWNER[1], "kind_of_matter": "conversation",
            "sender": "buyer@customer.example", "channel": "mail", "subject": "PO P-1",
            "message": "can you confirm", "kind": "todo", "summary": "Confirm PO P-1",
            "note": "Reply", "body": "", "to": [], "queue_card": False, "state": "open",
            "verified_at": NOW, "plan_status": "deferred", "plan_reason": "", "basis_at": NOW,
            "due_at": None, "audiences": ["mail:" + OWNER[1]], "markings": [],
            "citations": ["graph-mail://x/m1@1"]}
    return {**base, **over}


class Pool:
    """A fake pool: records the inserts, answers the one matter row lookup."""

    def __init__(self, legacy=None) -> None:
        self.rows: list[tuple[str, tuple]] = []
        self.committed = False
        self.legacy = legacy

    def connection(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.rows.append((sql, params))
        return self

    def commit(self):
        self.committed = True

    def fetchone(self):
        return (self.legacy, OWNER[1])

    def logged(self) -> list[tuple[str, dict]]:
        return [(p[1], json.loads(p[2])) for s, p in self.rows if "INSERT INTO assist.matter_log" in s]


class AssistRoutes(unittest.TestCase):
    client = base.AutopilotArea.client

    def setUp(self) -> None:
        base.AutopilotArea.setUp(self)
        self.pool = Pool()
        self.backend.memory = type("M", (), {"pool": self.pool})()
        self.decided: list[list[str]] = []
        self.envelope = {"items": [matter(M_OK), matter(M_CUT, citations=["graph-mail://x/m2@1"]),
                                   matter(M_MARK, markings=["finance"])],
                         "older": 4, "status": "ok", "reason": "", "as_of": NOW}
        self.calls: list[dict] = []

        def decide(handles):
            self.decided.append(list(handles))
            return {h: (None if h.endswith("m1@1") else "refused") for h in handles}
        self.backend.autopilot_grant = (lambda aud: True, frozenset(), decide)

        def as_items(pg, owners, **kw):
            self.calls.append({"owners": set(owners), **{k: v for k, v in kw.items() if k != "door"}})
            env = dict(self.envelope)
            if env["status"] == "off":
                return env
            allowed = kw["door"](env["items"])
            env["items"] = [i for i in env["items"] if i["item_id"] in allowed]
            return env
        patcher = patch("operating.assist_matters.as_items", as_items)
        patcher.start()
        self.addCleanup(patcher.stop)
        shown = patch("operating.assist_matters.shown_to", lambda *a, **k: True)
        shown.start()
        self.addCleanup(shown.stop)

    @staticmethod
    def matters(got) -> list:
        """The matters of a list (the queue's own cards, ap_..., stay beside them until L1)."""
        return [i["item_id"] for i in got["items"] if not i["item_id"].startswith("ap_")]

    def listed(self, who=OWNER, **query):
        got = self.client(who).get("/api/autopilot/drafts", params=query)
        self.assertEqual(got.status_code, 200, got.text)
        return got.json()

    def test_the_list_is_the_matters_through_one_door_over_the_whole_list(self) -> None:
        got = self.listed()
        # the cited handle the reader may not read, and the marking they do not hold: not shown
        # the queue's own cards stay beside the matters until L1 (ap_...)
        self.assertEqual([i["item_id"] for i in got["items"] if not i["item_id"].startswith("ap_")],
                         [M_OK])
        # one decision for all the matters (the queue's own cards are decided apart)
        once = [d for d in self.decided if "graph-mail://x/m2@1" in d]
        self.assertEqual(len(once), 1)
        self.assertEqual(sorted(once[0]), ["graph-mail://x/m1@1", "graph-mail://x/m2@1"])
        self.assertEqual((got["older"], got["status"]), (4, "ok"))
        item = next(i for i in got["items"] if i["item_id"] == M_OK)
        self.assertEqual((item["state"], item["plan_status"]), ("open", "deferred"))
        self.assertNotIn("citations", item)                        # the door's inputs stay here
        self.assertNotIn("audiences", item)

    def test_an_undecidable_door_shows_nothing_and_a_missing_grant_shows_nothing(self) -> None:
        def boom(handles):
            raise RuntimeError("decider down")
        self.backend.autopilot_grant = (lambda aud: True, frozenset(), boom)
        self.assertEqual(self.matters(self.listed()), [])
        self.backend.autopilot_grant = (lambda aud: True, frozenset(), lambda hs: {})
        self.assertEqual(self.matters(self.listed()), [])               # no verdict for a handle

    def test_off_and_stale_are_said_not_hidden(self) -> None:
        self.envelope.update(status="off", items=[], reason="not_opted_in", older=0)
        got = self.listed(COLLEAGUE)
        self.assertEqual((got["status"], got["reason"], self.matters(got)), ("off", "not_opted_in", []))
        self.envelope.update(status="stale", reason="re-checked 2 days ago", items=[matter(M_OK)])
        got = self.listed()
        self.assertEqual(got["status"], "stale")
        self.assertTrue(got["stale_since"])
        self.assertEqual(self.matters(got), [M_OK])

    def test_all_ages_and_the_person_ids_reach_the_read(self) -> None:
        self.listed(all_ages="true")
        self.assertTrue(self.calls[-1]["all_ages"])
        self.assertIn(OWNER[1], self.calls[-1]["owners"])
        self.listed(COLLEAGUE)
        self.assertNotIn(OWNER[1], self.calls[-1]["owners"])       # another person's id never

    def test_view_as_names_lookup_reaches_the_matters_read(self) -> None:
        from operator_api import view_as as va
        self.backend.view_as = va.ViewAs(
            candidates=lambda: [(COLLEAGUE[1], COLLEAGUE[0])],
            identity=lambda oid, seconds: None,
            excluded=lambda: (lambda oid: False))
        self.listed(OWNER)
        resolver = self.calls[-1].get("names_of")
        self.assertTrue(callable(resolver))
        self.assertEqual(resolver([COLLEAGUE[1]]), {COLLEAGUE[1]: "Sam"})

    def test_no_store_says_not_run_and_still_lists_the_queues_cards(self) -> None:
        self.backend.memory = None
        from operating import autopilot as ap
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.add_item(base.ReplyFromLine.card("ap_card_x", OWNER[1]))
        q.close()
        got = self.listed()
        self.assertEqual(got["status"], "not_run")
        self.assertTrue(got["reason"])
        self.assertIn("ap_card_x", [i["item_id"] for i in got["items"]])

    def test_draft_on_request_logs_one_requested_row_and_writes_nothing_else(self) -> None:
        got = self.client(OWNER).post(f"/api/autopilot/items/{M_OK}/draft")
        self.assertEqual((got.status_code, got.json()), (200, {"status": "working"}))
        self.assertEqual([w for w, _ in self.pool.logged()], ["requested"])
        self.assertTrue(self.pool.committed)
        self.assertEqual(self.bc.calls, [])
        writes = [s for s, _ in self.pool.rows if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(len(writes), 1)                           # only the log row

    def test_a_matter_not_on_the_readers_list_is_404_and_logs_nothing(self) -> None:
        for item in (M_CUT, M_MARK, "e" * 24):                     # refused, unheld, unknown
            got = self.client(OWNER).post(f"/api/autopilot/items/{item}/draft")
            self.assertEqual(got.status_code, 404, got.text)
        self.assertEqual(self.pool.logged(), [])

    def test_a_queue_id_falls_through_to_the_queues_own_route(self) -> None:
        got = self.client(OWNER).post("/api/autopilot/items/ap_own/decision",
                                      json={"decision": "reject"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(self.pool.logged(), [])

    def test_done_on_a_matter_is_a_log_row_and_hand_and_take_of_one_are_refused_said(self) -> None:
        owner = self.client(OWNER)
        edit = owner.post(f"/api/autopilot/items/{M_OK}/decision", json={"decision": "edit", "text": "x"})
        self.assertEqual(edit.status_code, 409)
        for route, body in (("hand", {"to": "x"}), ("take", None)):
            got = owner.post(f"/api/autopilot/items/{M_OK}/{route}", **({"json": body} if body else {}))
            self.assertEqual(got.status_code, 409, got.text)
        got = owner.post(f"/api/autopilot/items/{M_OK}/decision", json={"decision": "accept"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["state"], "accepted")
        self.assertEqual([(w, d["decision"]) for w, d in self.pool.logged()], [("closed", "accept")])
        self.assertEqual(len(self.pool.logged()), 1)

    def test_later_on_a_matter_puts_it_off_and_done_takes_it_off_the_list_at_once(self) -> None:
        from datetime import timedelta
        owner = self.client(OWNER)
        self.envelope["items"].append(matter(M_OLD))
        until = (NOW + timedelta(days=1)).isoformat()
        got = owner.post(f"/api/autopilot/items/{M_OK}/decision", json={"decision": "snooze", "until": until})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertNotIn(M_OK, self.matters(self.listed()))        # off the list until its time
        self.assertEqual(self.pool.logged(), [])                   # still open: nothing closed
        bad = owner.post(f"/api/autopilot/items/{M_OK}/decision",
                         json={"decision": "snooze", "until": (NOW - timedelta(days=1)).isoformat()})
        self.assertIn(bad.status_code, (404, 422))
        # Done (the cycle closes it later) and Not needed: gone from the list on the next read
        got = owner.post(f"/api/autopilot/items/{M_OLD}/decision", json={"decision": "reject"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertNotIn(M_OLD, self.matters(self.listed()))
        self.assertEqual([(w, d["decision"]) for w, d in self.pool.logged()], [("closed", "reject")])

    def test_a_newer_message_brings_a_closed_matter_back(self) -> None:
        owner = self.client(OWNER)
        owner.post(f"/api/autopilot/items/{M_OK}/decision", json={"decision": "accept"})
        self.assertNotIn(M_OK, self.matters(self.listed()))
        from datetime import timedelta
        self.envelope["items"][0]["basis_at"] = datetime.now(timezone.utc) + timedelta(seconds=5)
        self.assertIn(M_OK, self.matters(self.listed()))

    def test_mission_control_approvals_are_not_listed_and_questions_are(self) -> None:
        # decided in Mission Control (owner 11-24): worked from the matter's kind, never text
        self.envelope["items"] += [matter(M_OLD, kind_of_matter="approval", subject="Approve PO"),
                                   matter("e" * 24, kind_of_matter="task", subject="MC question")]
        listed = self.matters(self.listed())
        self.assertNotIn(M_OLD, listed)
        self.assertIn("e" * 24, listed)
        from operating import assist_matters as am
        flows = am.mc_approval_workflows()
        self.assertIn("job_approval", flows)
        self.assertNotIn("mail_approval_request", flows)      # an approval asked in mail stays

    def test_a_line_is_kept_and_the_draft_requested(self) -> None:
        got = self.client(OWNER).post(f"/api/autopilot/items/{M_OK}/reply-from-line",
                                      json={"line": "yes, ok for 400"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual([w for w, _ in self.pool.logged()], ["person_line", "requested"])

    def test_decide_matter_and_reopen_matter_work_directly_for_home_propagation(self) -> None:
        from operator_api import assist_routes, models as m
        from operating.session_identity import CHANNEL_CONSOLE, fixture_session
        who = type("R", (), {
            "key": OWNER[1], "email": OWNER[0],
            "session": fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                       subject_email=OWNER[0], subject_oid=OWNER[1]),
        })()
        until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        res = assist_routes.decide_matter(
            self.backend, who, M_OK, m.AutopilotDecisionRequest(decision="snooze", until=until, note="later"),
        )
        self.assertEqual(res.item_id, M_OK)
        self.assertNotIn(M_OK, self.matters(self.listed()))
        self.assertTrue(assist_routes.reopen_matter(self.backend, who, M_OK, reason="undo"))
        self.assertIn(M_OK, self.matters(self.listed()))


if __name__ == "__main__":
    unittest.main()


try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


@unittest.skipIf(psycopg is None, "psycopg not installed")
class LogIsKept(unittest.TestCase):
    """The log row goes through the real pool (whose hand-back rolls back what is not
    committed) and is read back on a fresh connection."""

    def test_a_requested_row_survives_the_pool_hand_back(self) -> None:
        from operating import assist_readings as ar
        from operating.pg.migrate import MIGRATIONS_DIR
        from operating.pg.pool import Pool
        from operating.tests import pg_testdb
        from operator_api import assist_routes
        name, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        with psycopg.connect(dsn, autocommit=True) as admin:
            for stmt in ar.ddl():
                admin.execute(stmt)
            admin.execute((MIGRATIONS_DIR / "0063_assist_work.sql").read_text("utf-8"))
        pool = Pool(dsn, max_size=2, configure=lambda conn: None)
        backend = type("B", (), {"memory": type("M", (), {"pool": pool})()})()
        assist_routes._log(backend, M_OK, "requested", {"by": ["ann"]})
        with psycopg.connect(dsn) as fresh:
            got = fresh.execute("SELECT matter_id, what, detail FROM assist.matter_log").fetchall()
        self.assertEqual(got, [(M_OK, "requested", {"by": ["ann"]})])
