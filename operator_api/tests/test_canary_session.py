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


class ServedFixture(Fixture):
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


class CanarySessionTest(ServedFixture):
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


class MeasureSessionTest(ServedFixture):
    """The page measure's service session (``session_identity.SERVICE_MEASURE``): the
    screens' GET reads as the reader its claims name, and nothing else; nobody's own."""

    def measure_ticket(self, **kw):
        kw.setdefault("ttl_seconds", 30 * 60)
        return self.ticket(jti=si.service_jti(si.SERVICE_MEASURE), **kw)

    def test_a_measure_ticket_lives_an_hour_at_most(self):
        self.assertTrue(self.measure_ticket(ttl_seconds=60 * 60))
        with self.assertRaises(ValueError):
            self.measure_ticket(ttl_seconds=60 * 60 + 1)
        # one signed longer by the secret's holder is refused at the check all the same
        claims = si.verify_session_ticket(self.measure_ticket(), SECRET)
        claims["exp"] = claims["iat"] + 2 * 60 * 60
        from operating.tests.test_session_identity import _resign
        with self.assertRaises(si.SessionIdentityRefused):
            si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                ticket=_resign(claims), secret=SECRET)
        # a person's own ticket keeps its working day
        self.assertTrue(self.ticket(ttl_seconds=8 * 60 * 60))

    def test_the_measure_is_served_the_screens_reads_and_nothing_else(self):
        client, app = self.client()
        self.sign_in(client, self.measure_ticket())
        for path in ("/api/me", "/api/home", "/api/sidebar", "/api/briefing"):
            self.assertEqual(client.get(path).status_code, 200, path)
        refused = [("POST", "/api/ask", {"question": "Where is TASK-1 at?"}),
                   ("POST", "/api/briefing/seen", {}),
                   ("POST", "/api/memory", {"kind": "fact", "text": "x"}),
                   ("GET", "/api/work-log", None), ("GET", "/api/memory", None),
                   ("GET", "/api/memory/people", None), ("GET", "/api/ask/chats", None),
                   ("GET", "/api/time-saved", None), ("GET", "/api/view-as/people", None),
                   ("GET", "/api/source", None), ("GET", "/api/teams/connection", None),
                   ("GET", "/api/entries/e1", None),
                   ("GET", "/api/object/%2e%2e/%2e%2e/work-log", None),
                   ("POST", "/api/actions/confirm", {}), ("POST", "/api/watch/follow", {})]
        for method, path, body in refused:
            got = client.request(method, path, json=body, headers=PROXIED)
            self.assertEqual(got.status_code, 403, (method, path, got.text))
            self.assertEqual(got.json()["error"]["code"], "canary_route_refused",
                             (method, path))
        # a path no route takes is never served either (no route: not found)
        for path in ("/api/home/%2e%2e/work-log", "/api/unknown"):
            self.assertEqual(client.get(path).status_code, 404, path)
        # never a view-as, even on a route it is served
        viewing = client.get("/api/home", headers={**PROXIED, "X-PLX-View-As": OID_A})
        self.assertEqual(viewing.status_code, 403)
        # the Ask canary is not widened by it: still its one question only
        canary = TestClient(app, base_url="http://testserver")
        self.sign_in(canary, self.canary_ticket())
        for path in ("/api/home", "/api/sidebar", "/api/insights"):
            got = canary.get(path)
            self.assertEqual((got.status_code, got.json()["error"]["code"]),
                             (403, "canary_route_refused"), path)

    def test_the_measure_session_is_nobodys_own(self):
        session = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                      ticket=self.measure_ticket(), secret=SECRET)
        self.assertEqual((session.measure, session.canary, session.service),
                         (True, True, si.SERVICE_MEASURE))
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            session.intent_identity(session.actor)
        self.assertEqual(caught.exception.reason, "canary_session_writes_nothing")
        canary = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                     ticket=self.canary_ticket(), secret=SECRET)
        self.assertFalse(canary.measure)


class ServiceSessionKnownReaderTest(Fixture):
    """A service session (the Ask canary every 20 minutes, the page measure) never takes
    the place of the reader's own known session: live, the canary's /api/me replaced the
    owner's entry (its ticket reference, its opened pages emptied) on every run."""

    def test_a_service_session_leaves_the_readers_own_entry_and_pages(self):
        import shutil
        from operator_api import backend as be
        from operator_api.auth import Reader
        tmp = Path(tempfile.mkdtemp(prefix="app-known-svc-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        backend = be.WorkspaceBackend(lambda s: None, data_label="test", data_dir=tmp)
        own = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                  ticket=self.ticket(), secret=SECRET)
        reader = Reader(own, "dev", "Ana", EMAIL_A)
        backend._pages_seen[reader.key] = [("purchase_order", "PO1")]
        backend.remember_reader(reader)
        before = backend.known_readers()
        self.assertEqual(before[OID_A]["ref"], own.session_ref)
        for service in (si.SERVICE_CANARY, si.SERVICE_MEASURE):
            session = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                          ticket=self.ticket(jti=si.service_jti(service),
                                                             ttl_seconds=30 * 60),
                                          secret=SECRET)
            backend.remember_reader(Reader(session, "dev", "Ana", EMAIL_A))
        self.assertEqual(backend.known_readers(), before)
        self.assertEqual(before[OID_A]["pages"], [["purchase_order", "PO1"]])


    def test_a_reader_seen_after_the_last_write_is_read_ahead_and_an_expired_one_only_while_recent(self):
        """11-21: the file held one entry, expired 25 h before, so every deploy's new API
        prewarmed nobody and each first page after it was read cold. A page-measure session
        with no own session held is a known reader while valid (never in the place of the
        reader's own one, which replaces it); the canary never is. A reader whose session
        ended stays read ahead (``prewarm_people``, no ticket to check) while they used
        the app within ``RECENT_USER_SECONDS``, then drops out."""
        import json
        import shutil
        import time
        from operator_api import backend as be
        from operator_api.auth import Reader
        from operator_api.backend_common import DIRECTORY_WARM_SECONDS
        from operator_api.serving import KNOWN_READERS_FILE, RECENT_USER_SECONDS
        tmp = Path(tempfile.mkdtemp(prefix="app-known-new-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        backend = be.WorkspaceBackend(lambda s: None, data_label="test", data_dir=tmp)
        backend.revoked = lambda ref: False   # no ticket revoked (unset: every one is)
        canary = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE, secret=SECRET,
                                     ticket=self.ticket(jti=si.service_jti(si.SERVICE_CANARY)))
        backend.remember_reader(Reader(canary, "dev", "Ana", EMAIL_A))
        self.assertEqual(backend.known_readers(), {})
        measure = si.employee_session(
            self.resolver(), channel=si.CHANNEL_CONSOLE, secret=SECRET,
            ticket=self.ticket(jti=si.service_jti(si.SERVICE_MEASURE), ttl_seconds=30 * 60))
        backend.remember_reader(Reader(measure, "dev", "Ana", EMAIL_A))
        # read ahead for while valid, and nothing else: no known reader (the recurring
        # instructions and the door's keep run as known readers only), no recent use
        self.assertEqual(backend.known_readers(), {})
        self.assertEqual(backend.recent_users(), [])
        self.assertEqual(backend.prewarm_people(),
                         [(OID_A, measure.expires_at, measure.session_ref)])
        own = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                  ticket=self.ticket(), secret=SECRET)
        backend.remember_reader(Reader(own, "dev", "Ana", EMAIL_A))
        self.assertEqual(backend.known_readers()[OID_A]["ref"], own.session_ref)
        self.assertEqual(backend.prewarm_people(),       # the own session, not the measure
                         [(OID_A, own.expires_at, own.session_ref)])
        # signed out: a measure session never clears the mark nor is read ahead for
        backend.forget_reader(Reader(own, "dev", "Ana", EMAIL_A))
        seen = json.loads((tmp / KNOWN_READERS_FILE).read_text())[OID_A]
        backend._remember_measure(OID_A, "d" * 64, int(time.time()) + 600)
        backend.remember_reader(Reader(measure, "dev", "Ana", EMAIL_A))
        self.assertEqual(json.loads((tmp / KNOWN_READERS_FILE).read_text())[OID_A], seen)
        self.assertEqual(backend.signed_out(), {OID_A})
        self.assertEqual(backend.prewarm_people(), [])
        # an earlier signed-in entry again, for the ended-session part below
        backend.remember_reader(Reader(own, "dev", "Ana", EMAIL_A))
        # sessions that ended: one used the app an hour ago, one past the recent window
        path = tmp / KNOWN_READERS_FILE
        other, gone = "1" * 8 + OID_A[8:], "2" * 8 + OID_A[8:]
        now = time.time()
        rows = json.loads(path.read_text())
        rows[other] = {"seen": now - 3600, "ref": "a" * 64, "exp": int(now) - 60, "pages": []}
        rows[gone] = {"seen": now - RECENT_USER_SECONDS - 60, "ref": "b" * 64,
                      "exp": int(now) - 60, "pages": []}
        path.write_text(json.dumps(rows))
        self.assertEqual(set(backend.known_readers()), {OID_A})
        people = {oid: (exp, ref) for oid, exp, ref in backend.prewarm_people()}
        self.assertEqual(set(people), {OID_A, other})
        self.assertIsNone(people[other][1])               # no ticket of theirs to check
        self.assertGreater(people[other][0], now)         # a read-ahead identity's life
        self.assertLessEqual(people[other][0], now + DIRECTORY_WARM_SECONDS + 5)


class ConsoleTicketServiceTest(Fixture):
    def test_the_mint_marks_a_service_ticket_and_its_audit_row_says_so(self):
        from operator_api.workspace import console_ticket as ct
        state = self.dir / "state"
        state.mkdir()
        env = {"PLX_WORKBENCH_SESSION_SECRET": SECRET,
               "PLX_EVAL_READER": '{"uid": "cmuser1", "email": "%s", "entraTenantId": "%s", '
               '"entraObjectId": "%s"}' % (EMAIL_A, TID, OID_A)}
        rows = []
        for service in (si.SERVICE_CANARY, si.SERVICE_MEASURE, None):
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
        self.assertEqual([r["service"] for r in rows],
                         [si.SERVICE_CANARY, si.SERVICE_MEASURE, None])
        long_measure = argparse.Namespace(state_dir=state, reader_env="PLX_EVAL_READER",
                                          ttl_minutes=61, port=8765, service=si.SERVICE_MEASURE,
                                          out=self.dir / "link-long")
        with mock.patch.object(ct, "_live_state", return_value=state), \
                mock.patch.object(ct, "_resolver", return_value=self.resolver()), \
                mock.patch.object(ct, "append_audit", side_effect=lambda *a, **k: rows.append(k)):
            with self.assertRaises(ct.TicketRefused):
                ct.mint(long_measure, env)
        self.assertFalse((self.dir / "link-long").exists())
        self.assertEqual(len(rows), 3)


class MintLinkCanaryTest(unittest.TestCase):
    def test_the_canary_mint_writes_its_own_fixed_file_never_the_owners(self):
        text = (Path(__file__).resolve().parents[2] / "deploy" / "ops"
                / "plx-mint-link").read_text()
        branch = text.split('if [ "${1:-}" = "--canary" ]; then', 1)[1].split("elif", 1)[0]
        self.assertIn("OUT_NAME=ask-canary-access-link", branch)
        self.assertIn("SERVICE=(--service ops_canary)", branch)
        # the one write goes to the name the mode fixed; the owner's name is only a default
        self.assertIn('out="$home/.config/plx/$OUT_NAME"', text)
        measure = text.split('elif [ "${1:-}" = "--measure" ]; then', 1)[1].split("elif", 1)[0]
        self.assertIn("OUT_NAME=page-measure-access-link", measure)
        self.assertIn("SERVICE=(--service ops_measure)", measure)
        self.assertIn("DEFAULT_TTL=30; MAX_TTL=60", measure)
        self.assertIn('[ "$TTL" -le "$MAX_TTL" ]', text)
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        self.assertEqual([line.strip() for line in code if "workbench-access-link" in line],
                         ["OUT_NAME=workbench-access-link"])


if __name__ == "__main__":
    unittest.main()
