"""The live Ask canary's service session (``session_identity.SERVICE_CANARY``), end to end
through the real ticket path: a ticket the host mints with ``--service ops_canary`` signs in
as its own service session, which the app serves ``GET /api/me`` and ``POST /api/ask`` only,
that writes nothing to the reader's work log, can carry no action, and that no client can
make or unmake (its mark is the signed ticket id)."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import session_identity as si
from operating.tests.test_session_identity import EMAIL_A, OID_A, SECRET, TID, Fixture
from operator_api.app import create_app
from operator_api.auth import DevAuth, ticket_identity
from operator_api.mock import MockBackend

PROXIED = {"Host": "127.0.0.1:8796", "X-Forwarded-Host": "127.0.0.1:3097",
           "Origin": "http://127.0.0.1:3097"}


def asked_lately(client):
    home = client.get("/api/home")
    assert home.status_code == 200, home.text
    return [r["question"] for r in home.json()["recent_questions"]]


def served_backend(be, m):
    """A ``WorkspaceBackend`` whose ``ask`` runs as served down to a stand-in answer: no
    model reading, no attachment, no general part, the console box answering."""
    import queue
    import threading
    backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
    backend._lock, backend._recent, backend._answers = threading.RLock(), {}, {}
    backend.ask_served, backend.ask_jobs = True, queue.Queue()
    backend.agent_ask = backend.agent_missing = None
    backend.clock = lambda: be.datetime.now(be.timezone.utc)
    for name, value in (("_prewarm_agent", None), ("_front_read", None),
                        ("_record_read", None), ("_attached", None)):
        setattr(backend, name, lambda *a, _v=value, **k: _v)
    backend._ready_first = lambda *a, **k: True
    backend._general_served = lambda: False

    def nothing(*a, **k):
        return None
        yield   # a generator answering nothing: the next path answers

    def box(reader, request, conversation, ms):
        return m.AnswerPayload(
            answer_id="a" * 24, question=request.question, outcome="answered",
            direct_answer="TASK-1 is open.", hidden=m.Hidden(count=0),
            freshness=m.Freshness(), conversation_id=conversation, latency_ms=ms()) \
            if "TASK" in request.question else m.AnswerPayload(
            answer_id="b" * 24, question=request.question, outcome="answered",
            direct_answer="None are late.", hidden=m.Hidden(count=0),
            freshness=m.Freshness(), conversation_id=conversation, latency_ms=ms())
        yield

    backend._fast_ask = nothing
    backend._box_ask = box
    return backend


class CanarySessionTest(Fixture):
    def canary_ticket(self, **kw):
        return self.ticket(jti=si.service_jti(si.SERVICE_CANARY), **kw)

    def client(self):
        guard = si.TicketReplayGuard(self.dir / "once.db")
        dev = DevAuth(ticket_identity=ticket_identity(self.resolver(), SECRET, guard),
                      sign_in_identity=ticket_identity(self.resolver(), SECRET, guard,
                                                       single_use=True), mode="dev")
        backend = MockBackend()
        backend.state_dir = self.dir / "api-state"
        backend.state_dir.mkdir()
        app = create_app(backend, dev, secure_cookie=False)
        return TestClient(app, base_url="http://testserver"), app

    def sign_in(self, client, ticket):
        ok = client.post("/api/auth/dev-login", json={"ticket": ticket}, headers=PROXIED)
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_only_the_host_minted_ticket_id_makes_a_canary_session(self):
        canary = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                     ticket=self.canary_ticket(), secret=SECRET)
        person = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                     ticket=self.ticket(), secret=SECRET)
        self.assertEqual((canary.canary, canary.service, canary.email),
                         (True, si.SERVICE_CANARY, EMAIL_A))
        self.assertEqual((person.canary, person.service), (False, None))
        # the mark is inside the signature: rewriting the id of a person's ticket breaks it
        forged = si.mint_session_ticket(uid="cmuser1", email=EMAIL_A, tenant_id=TID,
                                        object_id=OID_A, author=si.AUTHOR_HUMAN,
                                        secret="another-secret-" + "y" * 32,
                                        jti=si.service_jti(si.SERVICE_CANARY))
        with self.assertRaises(si.SessionIdentityRefused):
            si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE, ticket=forged,
                                secret=SECRET)
        with self.assertRaises(ValueError):   # an unknown service is no service
            si.service_jti("owner")
        # it can author no intent, action or write
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            canary.intent_identity(canary.actor)
        self.assertEqual(caught.exception.reason, "canary_session_writes_nothing")
        self.assertEqual(person.intent_identity(person.actor)["sub"]["principal"],
                         "employee:" + OID_A)

    def test_the_app_serves_the_canary_its_question_and_nothing_of_the_readers_own(self):
        client, app = self.client()
        self.sign_in(client, self.canary_ticket())
        self.assertEqual(client.get("/api/me").status_code, 200)
        asked = client.post("/api/ask", json={"question": "Where is TASK-1 at?"},
                            headers=PROXIED)
        self.assertEqual(asked.status_code, 200, asked.text)
        for method, path, body in (("GET", "/api/work-log", None), ("GET", "/api/memory", None),
                                   ("GET", "/api/memory/people", None),
                                   ("GET", "/api/time-saved", None),
                                   ("POST", "/api/memory", {"kind": "fact", "text": "x"})):
            got = client.request(method, path, json=body, headers=PROXIED)
            self.assertEqual((got.status_code, got.json()["error"]["code"]),
                             (403, "canary_route_refused"), (method, path))
        # nor may it view as anyone, even on its own route
        viewing = client.post("/api/ask", json={"question": "q"},
                              headers={**PROXIED, "X-PLX-View-As": OID_A})
        self.assertEqual(viewing.status_code, 403)
        # the same reader's own session is served all of it
        person = TestClient(app, base_url="http://testserver")
        self.sign_in(person, self.ticket())
        self.assertEqual(person.get("/api/work-log").status_code, 200)
        self.assertEqual(person.get("/api/memory").status_code, 200)
        # and the canary's question is not among the reader's own asked questions; the
        # reader's own question is
        self.assertEqual(asked_lately(person), [])
        own = person.post("/api/ask", json={"question": "Which POs are late?"},
                          headers=PROXIED)
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(asked_lately(person), ["Which POs are late?"])

    def test_a_canary_ask_is_answered_and_kept_for_no_reader(self):
        """``WorkspaceBackend.ask`` (the served path) answers the canary's question and
        keeps nothing of it for the reader its claims name: no recent question, no kept
        answer to rate or continue, none of their pages made stale; the same reader's own
        question is kept as before."""
        from operator_api import backend as be
        from operator_api import models as m
        from operator_api.auth import Reader
        backend = served_backend(be, m)
        canary = Reader(si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                            ticket=self.canary_ticket(), secret=SECRET),
                        "dev", "Ana", EMAIL_A)
        person = Reader(si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                            ticket=self.ticket(), secret=SECRET),
                        "dev", "Ana", EMAIL_A)
        self.assertEqual(canary.key, person.key)   # the same reader's claims
        with mock.patch("operator_api.feedback.served_markings", return_value=[]):
            got = dict(backend.ask(canary, m.AskRequest(question="Where is TASK-1 at?")))
            self.assertEqual(got["answer"].direct_answer, "TASK-1 is open.")
            self.assertEqual((backend._recent, backend._answers,
                              backend.__dict__.get("_reader_writes", {})), ({}, {}, {}))
            from operator_api.errors import RecordNotFound
            with self.assertRaises(RecordNotFound):   # nothing to rate as the reader's own
                backend.feedback(person, m.FeedbackRequest(
                    answer_id=got["answer"].answer_id, rating=1))
            own = dict(backend.ask(person, m.AskRequest(question="Which POs are late?")))
        self.assertEqual([q.question for q in backend._recent[person.key]],
                         ["Which POs are late?"])
        self.assertEqual(list(backend._answers), [own["answer"].answer_id])
        self.assertEqual(backend._reader_writes, {person.key: 1})

    def test_the_audit_journals_a_canary_request_as_the_canary_never_the_reader(self):
        from operator_api.workspace.session_journal import SessionJournal
        import sqlite3
        journal = SessionJournal.for_state_dir(self.dir / "journal")
        for ticket in (self.canary_ticket(), self.ticket()):
            session = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                          ticket=ticket, secret=SECRET)
            journal.record(method="POST", path="/api/ask", status=200, session=session)
        with sqlite3.connect(journal.path) as db:
            rows = db.execute("SELECT employee, proof FROM request ORDER BY id").fetchall()
        self.assertEqual(rows, [(None, si.SERVICE_CANARY),
                                ("employee:" + OID_A, si.PROOF_TICKET)])
        # an unknown service on a session is journalled as "other", never as a person
        odd = mock.Mock(service="someone", employee=mock.Mock(principal="employee:" + OID_A),
                        proof=si.PROOF_TICKET, session_ref=None, access=None)
        journal.record(method="POST", path="/api/ask", status=200, session=odd)
        with sqlite3.connect(journal.path) as db:
            self.assertEqual(db.execute("SELECT employee, proof FROM request ORDER BY id DESC"
                                        " LIMIT 1").fetchone(), (None, "other"))

    def test_the_work_log_records_nothing_for_a_canary_reader(self):
        from operator_api.auth import Reader
        from operator_api import work_log as wl
        canary = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                     ticket=self.canary_ticket(), secret=SECRET)
        person = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                     ticket=self.ticket(), secret=SECRET)
        from integrations.people_exclusion import PeopleExclusion
        guard = PeopleExclusion(["99999999-0000-4000-8000-000000000009"])   # someone else
        with tempfile.TemporaryDirectory() as tmp:
            store = wl.WorkLog(Path(tmp) / "wl", guard=lambda: guard)
            self.addCleanup(store.close)
            for session in (canary, person):
                who = Reader(session, "dev", "Ana", session.email)
                store.record(who, kind="ask", verb="asked", title="Where is TASK-1 at?",
                             thread="ask:c1", source="ask", source_id=session.session_ref)
            events, total = store.events(reader=None)
            self.assertEqual(total, 1)
            self.assertEqual([e["source_id"] for e in events], [person.session_ref])


class ConsoleTicketServiceTest(Fixture):
    def test_the_mint_marks_a_service_ticket_and_its_audit_row_says_so(self):
        from operator_api.workspace import console_ticket as ct
        state = self.dir / "state"
        state.mkdir()
        env = {"PLX_WORKBENCH_SESSION_SECRET": SECRET,
               "PLX_EVAL_READER": '{"uid": "cmuser1", "email": "%s", "entraTenantId": "%s", '
               '"entraObjectId": "%s"}' % (EMAIL_A, TID, OID_A)}
        rows = []
        for service in (si.SERVICE_CANARY, None):
            args = argparse.Namespace(state_dir=state, reader_env="PLX_EVAL_READER",
                                      ttl_minutes=60, port=8765, service=service,
                                      out=self.dir / f"link-{service}")
            with mock.patch.object(ct, "_live_state", return_value=state), \
                    mock.patch.object(ct, "_resolver", return_value=self.resolver()), \
                    mock.patch.object(ct, "append_audit",
                                      side_effect=lambda *a, **k: rows.append(k)):
                ct.mint(args, env)
            ticket = args.out.read_text().strip().split(ct.URL_MARK, 1)[1]
            session = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                          ticket=ticket, secret=SECRET)
            self.assertEqual(session.service, service)
        self.assertEqual([r["service"] for r in rows], [si.SERVICE_CANARY, None])


class MintLinkCanaryTest(unittest.TestCase):
    def test_the_canary_mint_writes_its_own_fixed_file_never_the_owners(self):
        text = (Path(__file__).resolve().parents[2] / "deploy" / "ops"
                / "plx-mint-link").read_text()
        branch = text.split('if [ "${1:-}" = "--canary" ]; then', 1)[1].split("elif", 1)[0]
        self.assertIn("OUT_NAME=ask-canary-access-link", branch)
        self.assertIn("SERVICE=(--service ops_canary)", branch)
        # the one write goes to the name the mode fixed; the owner's name is only a default
        self.assertIn('out="$home/.config/plx/$OUT_NAME"', text)
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        self.assertEqual([line.strip() for line in code if "workbench-access-link" in line],
                         ["OUT_NAME=workbench-access-link"])


if __name__ == "__main__":
    unittest.main()
