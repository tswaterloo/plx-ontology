"""session.employee_recorded (WS-16, A9): the measurement over fixture journals and audit
files (isolated-fixture), and the host's preflight of the run book's preconditions. All
identities here are synthetic; the console's Entra registration is a fixture file."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from devloop import live_verify
from operating import session_identity
from operator_ui import console_ticket, session_journal
from operator_ui.session_journal import SessionJournal

TENANT = "00000000-0000-4000-8000-0000000000f1"
EMPLOYEE = "00000000-0000-4000-8000-0000000000a1"
EVAL = "00000000-0000-4000-8000-0000000000e1"
EMAIL = "employee@example.test"
SPEC = json.loads(Path("devloop/live-checks/session.employee_recorded.json")
                  .read_text("utf-8"))
FULL_VIEW = {"timeline": 3, "originals": 1, "gaps": 0, "conflicts": 2}
CITED = {"claims": 2, "citations": 2}


def answer_of(route, counts):
    """A served answer of ``route`` carrying these item counts (the shapes the console's
    workspaces return; the contract against the real answers is tested with them)."""
    if route == "/api/object":
        return {"status": "read", **{name: {"total": n} for name, n in counts.items()
                                     if name != "conflicts"},
                "conflicts": [{}] * counts.get("conflicts", 0)}
    if route == "/api/case/":
        return {"view": "workspace", **{name: [{}] * n for name, n in counts.items()}}
    claims = [{"text": "t", "citations": [{}] * (counts["citations"] // counts["claims"])}
              for _ in range(counts["claims"])]
    if route == "/api/object-ask":
        return {"answer": {"claims": claims, "claim_checks": {"counts": {"passed": 1,
                                                                         "failed": 0}}}}
    return {"investigation": {"answer": [dict(entry, citation={"l1_handle": "h"})
                                         if entry["citations"] else entry
                                         for entry in claims]}}


def ref_of(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


class Journal:
    """A state directory with a journal and, per session, how it was proven (an Entra
    sign-in by default) and optionally a ticket audit row."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.journal = SessionJournal.for_state_dir(directory)

    def session(self, name, *, employee=EMPLOYEE, proof=session_identity.PROOF_ENTRA,
                minted_by=None, requests=()):
        ref = ref_of(name)
        who = type("S", (), {"session_ref": ref, "proof": proof, "employee": type(
            "E", (), {"principal": "employee:" + employee})()})()
        for route, counts in requests:
            self.journal.record(method="GET", path=route, status=200, session=who,
                                value=None if counts is None else answer_of(route, counts))
        if minted_by is not None:
            row = {"event": "mint", "session_ref": ref, "employee": "employee:" + employee,
                   "minted_by": minted_by}
            with open(self.dir / session_journal.AUDIT_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
        return ref


class EmployeeRecordedTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.fixture = Journal(self.state)
        self.reader = {"uid": "synthetic-uid", "email": EMAIL, "entraTenantId": TENANT,
                       "entraObjectId": EVAL}
        # The console's Entra registration (fixture: a synthetic tenant, a generated key).
        registration = Path(tempfile.mkdtemp(dir=self.state.parent))
        self.addCleanup(lambda: [path.unlink() for path in registration.iterdir()]
                        and registration.rmdir())
        (registration / "jwks.json").write_text(json.dumps({"keys": [
            {"kty": "RSA", "kid": "k1", "e": "AQAB",          # a synthetic 2048-bit modulus
             "n": base64.urlsafe_b64encode(b"\xc1" * 256).decode().rstrip("=")}]}), "utf-8")
        self.registration = registration / "console-entra.json"
        self.registration.write_text(json.dumps({
            "schema": "plx-entra-resource/v1", "tenant_id": TENANT,
            "audiences": ["api://plx-console-test"], "required_scope": "Workbench.Read",
            "resource": "http://127.0.0.1:8765/", "jwks_file": "jwks.json"}), "utf-8")

    def check(self, state=None, environ=None):
        """The spec's own command path: ``main(["check"])`` over the environment, then
        the live verifier's expectations on its printed document."""
        env = {"PLX_WORKBENCH_STATE_DIR": str(self.state if state is None else state),
               "PLX_EVAL_READER": json.dumps(self.reader),
               session_journal.ENTRA_RESOURCE_ENV: str(self.registration), **(environ or {})}
        env = {key: value for key, value in env.items() if value != ""}
        printed = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(printed):
            self.assertEqual(session_journal.main(["check"]), 0)
        document = json.loads(printed.getvalue())
        return document, live_verify.check_expectations(SPEC, 0, document), printed.getvalue()

    def good(self, name="s", **kwargs):
        return self.fixture.session(name, requests=[
            ("/api/object", FULL_VIEW), ("/api/object-ask", CITED)], **kwargs)

    def test_a_signed_in_employee_session_with_a_full_view_and_a_cited_answer_passes(self):
        ref = self.good()
        # Another session of the same host that did less never hides or replaces it.
        self.fixture.session("idle", requests=[("/api/session", None)])
        document, failures, printed = self.check()
        self.assertEqual((failures, document["qualifying_sessions"], document["sessions"],
                          document["signed_in"], document["blocked_reason"]),
                         ([], 1, 2, 2, None))
        # A case view and an investigation count the same as an object view and an Ask.
        other = self.fixture.session("c", requests=[
            ("/api/case/", {k: FULL_VIEW[k] for k in ("timeline", "originals", "conflicts")}),
            ("/api/investigate", CITED)])
        self.assertEqual(self.check()[0]["qualifying_sessions"], 2)
        # Counts and booleans only: no uid, e-mail, object id or session reference.
        for value in (EMPLOYEE, EMAIL, "synthetic-uid", ref, other):
            self.assertNotIn(value, printed)
            self.assertNotIn(value, json.dumps(SessionJournal.for_state_dir(self.state)
                                               .summary()).replace(ref, "").replace(other, ""))

    def test_each_pass_condition_fails_alone(self):
        full = dict(FULL_VIEW)
        ticket = session_identity.PROOF_TICKET
        cases = {
            # The loopback operator's stand-ins: a host-minted ticket is not a sign-in.
            "host-minted ticket": dict(proof=ticket, minted_by="host"),
            "ticket, audit row absent": dict(proof=ticket),
            "ticket minted by another": dict(proof=ticket, minted_by="portal"),
            "loopback fixture session": dict(proof=session_identity.PROOF_FIXTURE),
            "configured employee": dict(proof=session_identity.PROOF_CONFIGURED),
            "no proof recorded": dict(proof=None),
            "eval reader's own session": dict(employee=EVAL),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.fixture = Journal(Path(tempfile.mkdtemp(dir=self.state)))
                self.good(**kwargs)
                document, failures, _ = self.check(
                    self.fixture.dir, {"PLX_EVAL_READER": json.dumps(self.reader)})
                self.assertEqual(document["qualifying_sessions"], 0, document)
                self.assertTrue(failures)
        views = {"no timeline": dict(full, timeline=0), "no originals": dict(full, originals=0),
                 "no conflicts": dict(full, conflicts=0)}
        for name, view in views.items():
            with self.subTest(name):
                self.fixture = Journal(Path(tempfile.mkdtemp(dir=self.state)))
                self.fixture.session("s", requests=[("/api/object", view),
                                                    ("/api/object-ask", CITED)])
                self.assertTrue(self.check(self.fixture.dir)[1], name)
        with self.subTest("uncited answer"):
            self.fixture = Journal(Path(tempfile.mkdtemp(dir=self.state)))
            self.fixture.session("s", requests=[("/api/object", full), ("/api/object-ask",
                                                                         {"claims": 2, "citations": 0})])
            self.assertTrue(self.check(self.fixture.dir)[1])
        with self.subTest("view and answer in two sessions"):
            self.fixture = Journal(Path(tempfile.mkdtemp(dir=self.state)))
            self.fixture.session("a", requests=[("/api/object", full)])
            self.fixture.session("b", requests=[("/api/object-ask", CITED)])
            document, failures, _ = self.check(self.fixture.dir)
            self.assertEqual((document["with_view"], document["with_citations"],
                              document["qualifying_sessions"]), (1, 1, 0))
            self.assertTrue(failures)

    def test_the_eval_identity_is_required_and_decides_who_is_excluded(self):
        # Fail closed: with the eval reader unknown, nothing separates it from an employee.
        self.fixture = Journal(Path(tempfile.mkdtemp(dir=self.state)))
        self.good(employee=EVAL)
        for name, value in (("unset", ""), ("unusable", "{"), ("incomplete", "{}")):
            with self.subTest("eval session, reader " + name):
                document, failures, printed = self.check(
                    self.fixture.dir, {"PLX_EVAL_READER": value})
                self.assertEqual(document["blocked_reason"], "eval_reader_" + (
                    "unset" if name == "unset" else "unusable"))
                self.assertEqual(document["qualifying_sessions"], 0)
                self.assertTrue(failures)
        with self.subTest("eval session, reader set: excluded"):
            document, failures, _ = self.check(self.fixture.dir)
            self.assertEqual((document["blocked_reason"], document["employee_identity"],
                              document["qualifying_sessions"]), (None, 0, 0))
            self.assertTrue(failures)
        with self.subTest("employee session, reader set: qualifies"):
            self.fixture = Journal(Path(tempfile.mkdtemp(dir=self.state)))
            self.good()
            document, failures, _ = self.check(self.fixture.dir)
            self.assertEqual((failures, document["qualifying_sessions"]), ([], 1))
        with self.subTest("employee session, reader unset: blocked, not qualifying"):
            document = self.check(self.fixture.dir, {"PLX_EVAL_READER": ""})[0]
            self.assertEqual((document["blocked_reason"], document["qualifying_sessions"]),
                             ("eval_reader_unset", 0))
        self.assertIn("eval_reader_unset", SPEC["description"])

    def test_without_a_state_dir_a_journal_or_a_session_it_reads_blocked_with_a_cause(self):
        empty = Path(tempfile.mkdtemp(dir=self.state))
        unauthenticated = Path(tempfile.mkdtemp(dir=self.state))
        SessionJournal.for_state_dir(unauthenticated).record(
            method="GET", path="/api/session", status=401, refusal="session_ticket_required")
        for name, state, environ, reason in (
                ("no state dir", "", {}, "state_dir_unset"),
                ("no journal", empty, {}, "journal_absent"),
                ("no session", unauthenticated, {}, "no_session_recorded"),
                ("unusable eval reader", self.state, {"PLX_EVAL_READER": "{"},
                 "eval_reader_unusable")):
            with self.subTest(name):
                if reason == "eval_reader_unusable":
                    self.good()
                document = self.check(state, environ)[0]
                self.assertEqual(document["blocked_reason"], reason)
                self.assertEqual(document["qualifying_sessions"], 0)
                self.assertRegex(document["blocked_reason"], live_verify.ENUM_STRING)
        self.assertEqual(SPEC["blocked_when"], "blocked_reason")
        self.assertIn(SPEC["blocked_cause"], live_verify.CAUSES)

    def test_until_the_console_app_registration_exists_it_reads_blocked_as_a_later_ask(self):
        """The registration is checked first: a signed-in session cannot exist without it,
        and its absence is a human's later_ask, never a guessed tenant or client value."""
        self.good()
        (self.registration.parent / "empty-jwks.json").write_text('{"keys": []}', "utf-8")
        broken = self.registration.parent / "broken.json"
        broken.write_text(self.registration.read_text("utf-8").replace(
            '"jwks.json"', '"empty-jwks.json"'), "utf-8")
        for name, value, reason in (
                # Unset: the committed resource, whose registration fields are unset.
                ("unset", "", "entra_app_registration_incomplete"),
                ("missing file", str(self.registration.parent / "none.json"),
                 "entra_app_registration_absent"),
                ("no signing key", str(broken), "entra_app_registration_invalid"),
                ("not a resource", str(self.registration.parent / "jwks.json"),
                 "entra_app_registration_invalid")):
            with self.subTest(name):
                document, failures, _ = self.check(
                    environ={session_journal.ENTRA_RESOURCE_ENV: value})
                self.assertEqual((document["blocked_reason"], document["qualifying_sessions"]),
                                 (reason, 0))
                self.assertTrue(failures)
                self.assertEqual(SPEC["blocked_causes"][reason], "later_ask")
        # Other blocked reasons keep their own cause.
        self.assertNotIn("journal_absent", SPEC["blocked_causes"])


class PreflightTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.state = self.dir / "state"
        session_identity.bind_state_dir(self.state, fixture=False)
        self.secret = "synthetic-session-secret-" + "x" * 32
        self.reader = json.dumps({"uid": "synthetic-uid", "email": EMAIL,
                                  "entraTenantId": TENANT, "entraObjectId": EMPLOYEE})

    def host_config(self, **files):
        document = {"schema": "plx-host-config/v1", "mirror_audience": "mirror.json"}
        for key, present in files.items():
            if present:
                (self.dir / f"{key}.json").write_text("{}", "utf-8")
                document[key] = f"{key}.json"
        path = self.dir / f"host-{len(list(self.dir.glob('host-*.json')))}.json"
        path.write_text(json.dumps(document), "utf-8")
        return path

    def run_preflight(self, config, environ, state=None):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = console_ticket.main(
                ["preflight", "--state-dir", str(state or self.state), "--host-config",
                 str(config)], environ=environ)
        return code, json.loads(printed.getvalue()), printed.getvalue()

    def test_preflight_names_each_missing_precondition_and_prints_no_secret_or_identity(self):
        environ = {session_identity.SESSION_TICKET_SECRET_ENV: self.secret,
                   "PLX_EMPLOYEE_READER": self.reader}
        both = self.host_config(employee_policy=True, portal_access=True)
        code, result, _ = self.run_preflight(both, environ)
        self.assertEqual((code, result["status"], set(result["checks"].values())),
                         (0, "ready", {"ok"}))
        broken = {
            "employee_policy": (self.host_config(employee_policy=False, portal_access=True),
                                environ, None, "employee_policy_not_configured"),
            "portal_access": (self.host_config(employee_policy=True, portal_access=False),
                              environ, None, "portal_access_not_configured"),
            "session_secret": (both, {**environ, session_identity.SESSION_TICKET_SECRET_ENV:
                                      "too-short"}, None, "session_secret_too_short"),
            "state_dir": (both, environ, self.dir / "new", "state_not_live:new"),
            "reader_env": (both, {k: v for k, v in environ.items() if k != "PLX_EMPLOYEE_READER"},
                           None, "no_employee_identity"),
        }
        for name, (config, env, state, reason) in broken.items():
            with self.subTest(name):
                code, result, printed = self.run_preflight(config, env, state)
                self.assertEqual((code, result["status"]), (2, "not_ready"))
                self.assertEqual({k: v for k, v in result["checks"].items() if v != "ok"},
                                 {name: reason})
                for value in (self.secret, EMPLOYEE, EMAIL, "synthetic-uid"):
                    self.assertNotIn(value, printed)
        code, result, _ = self.run_preflight(self.dir / "missing.json", {})
        self.assertEqual((code, result["checks"]["employee_policy"],
                          result["checks"]["session_secret"]),
                         (2, "host_config_unreadable", "session_secret_unavailable"))


if __name__ == "__main__":
    unittest.main()
