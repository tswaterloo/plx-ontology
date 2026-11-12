"""The owner's "view as <person>" (UI-07): the owner alone, reads only, the person's own
screens, nothing sent, approved or logged as them, every view audited. Synthetic identities;
evidence isolated-fixture."""

from __future__ import annotations

import io
import contextlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import employee_permissions as ep
from operating import session_identity as si
from operator_api import view_as as va
from operator_api import work_log as wl
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend

TID = "11111111-2222-4333-8444-555555555555"
VINCE = "aaaaaaaa-bbbb-4ccc-8ddd-000000000001"
GONE = "aaaaaaaa-bbbb-4ccc-8ddd-000000000002"     # the people exclusion names them
OWNER = "aaaaaaaa-bbbb-4ccc-8ddd-000000000003"
REFUSED = "aaaaaaaa-bbbb-4ccc-8ddd-000000000004"  # the resolver refuses them now


class Guard:
    unavailable = None

    def is_excluded(self, value):
        return False

    def decides(self, address):
        return True

    def known_domains(self):
        return {"plx.example"}


def view_session(oid, exp):
    if oid == REFUSED:
        raise si.SessionIdentityRefused("employee_inactive")
    principal = ep.EmployeePrincipal(TID, oid)
    return si.SessionIdentity(tenant="plx", channel=si.CHANNEL_CONSOLE,
                              author_kind=si.AUTHOR_HUMAN, proof=si.PROOF_VIEW_AS,
                              employee=principal, principals=frozenset({principal.principal}),
                              expires_at=exp, identities=frozenset({oid, "vince@plx.example"}))


def viewer_for(*principals):
    return va.viewer_of(type("R", (), {"session": type("S", (), {
        "principals": frozenset(principals)})()})())


class Seen(MockBackend):
    """Records the reader each served read was given."""

    def __init__(self):
        super().__init__()
        self.readers = []

    def _is_owner(self, reader):
        return "staff:owner" in reader.key

    def home(self, reader):
        self.readers.append(reader)
        return super().home(reader)


class ViewAsApi(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.backend = Seen()
        self.backend.work_log = wl.WorkLog(None, guard=lambda: Guard())
        self.backend.view_as = va.ViewAs(
            candidates=lambda: [(VINCE, "Vince.Example@plx.example"), (GONE, "g@plx.example"),
                                (OWNER, "owner@plx.example"), (REFUSED, "r@plx.example"),
                                ("not-an-id", "x@plx.example")],
            excluded=lambda: (lambda oid: oid == GONE), identity=view_session,
            audit=va.Audit(self.state))
        self.owner = self.client("staff:owner", "owner@plx.example", OWNER)
        self.ana = self.client("staff:ana", "ana@plx.example", None)

    def client(self, principal, email, oid):
        session = si.fixture_session("plx", [principal], channel=si.CHANNEL_CONSOLE,
                                     subject_email=email, subject_oid=oid)
        client = TestClient(create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False))
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def audit(self):
        path = self.state / "view-as" / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_the_owner_sees_the_persons_own_screens_and_nothing_is_written_as_them(self):
        people = self.owner.get("/api/view-as/people").json()
        self.assertTrue(people["available"])
        # the excluded person, the owner and an unusable id are never offered
        self.assertEqual([p["name"] for p in people["people"]],
                         ["R", "Vince Example"])
        vince = next(p["key"] for p in people["people"] if p["name"] == "Vince Example")
        self.assertNotIn(VINCE, json.dumps(people))
        as_vince = {va.HEADER: vince}
        home = self.owner.get("/api/home", headers=as_vince)
        self.assertEqual(home.status_code, 200)
        self.assertEqual(home.json()["greeting_name"], "Vince")
        served = self.backend.readers[-1]
        self.assertTrue(served.session.view_as)
        self.assertEqual(served.session.principals, {f"employee:{VINCE}"})
        with self.assertRaises(si.SessionIdentityRefused):
            served.session.intent_identity(served.session.actor)
        # the drafts read as Vince log nothing in his work log
        self.assertEqual(self.owner.get("/api/autopilot", headers=as_vince).status_code, 200)
        self.assertEqual(self.backend.work_log.events(reader=None)[1], 0)
        # nothing can be sent, approved, noted or asked as him
        for method, path, body in (
                ("post", "/api/autopilot/items/x/decision", {"decision": "accept"}),
                ("post", "/api/actions/confirm", {}),
                ("post", "/api/feedback/note", {"kind": "record", "note": "x"}),
                ("post", "/api/ask", {"question": "which orders are late"}),
                ("post", "/api/watch/alerts/read", {"alert_ids": []}),
                ("delete", "/api/watch/rules/r1", None)):
            with self.subTest(path=path):
                got = getattr(self.owner, method)(path, headers=as_vince,
                                                  **({"json": body} if body is not None else {}))
                self.assertEqual(got.status_code, 403)
                self.assertEqual(got.json()["error"]["code"], "view_as_refused")
        self.assertEqual(self.backend.work_log.events(reader=None)[1], 0)
        # beyond My work, alerts and drafts: refused, not the owner's own page
        for path in ("/api/work-log", "/api/object/po/PO-90412", "/api/me"):
            self.assertEqual(self.owner.get(path, headers=as_vince).status_code, 403, path)
        # every view and every refusal is audited, the file the owner's alone
        rows = self.audit()
        self.assertEqual(len(rows), 2 + 6 + 3)
        self.assertEqual({r["viewed"] for r in rows}, {vince})
        self.assertEqual({r["viewer"] for r in rows}, {viewer_for("staff:owner")})
        self.assertEqual([r["status"] for r in rows[:2]], [200, 200])
        self.assertEqual(rows[0]["route"], "/api/home")
        self.assertEqual(stat.S_IMODE(os.stat(self.state / "view-as" / "audit.jsonl")
                                      .st_mode), 0o600)
        # without the header the owner reads as themselves again
        self.owner.get("/api/home")
        self.assertFalse(getattr(self.backend.readers[-1].session, "view_as", False))

    def test_only_the_owner_and_only_people_who_may_be_viewed(self):
        vince = va.key_of(VINCE)
        self.assertEqual(self.ana.get("/api/view-as/people").status_code, 403)
        got = self.ana.get("/api/home", headers={va.HEADER: vince})
        self.assertEqual(got.status_code, 403)
        self.assertEqual(self.backend.readers, [])
        for key in (va.key_of(GONE), va.key_of(OWNER), va.key_of(REFUSED), "nobody", ""):
            with self.subTest(key=key):
                self.assertEqual(self.owner.get("/api/home", headers={va.HEADER: key})
                                 .status_code, 404)
        self.assertEqual(self.backend.readers, [])
        self.assertEqual(len(self.audit()), 1 + 5)
        self.assertEqual(self.audit()[0]["viewer"], viewer_for("staff:ana"))

    def test_no_exclusion_or_no_resolver_offers_nobody(self):
        def broken():
            raise RuntimeError("people exclusion unavailable")
        for viewer in (va.ViewAs(candidates=lambda: [(VINCE, "v@plx.example")],
                                 excluded=broken, identity=view_session),
                       va.ViewAs()):
            self.backend.view_as = viewer
            owner = self.client("staff:owner", "owner@plx.example", OWNER)
            self.assertEqual(owner.get("/api/view-as/people").json()["people"], [])
            self.assertEqual(owner.get("/api/home", headers={va.HEADER: va.key_of(VINCE)})
                             .status_code, 404)

    def test_an_audit_that_cannot_be_written_shows_nothing(self):
        (self.state / "view-as").mkdir()
        (self.state / "view-as" / "audit.jsonl").mkdir()   # not writable as a file
        got = self.owner.get("/api/home", headers={va.HEADER: va.key_of(VINCE)})
        self.assertEqual(got.status_code, 503)
        self.assertEqual(self.backend.readers, [])

    def test_the_audit_names_no_object_id_for_a_real_shaped_owner_session(self):
        owner_principal = ep.EmployeePrincipal(TID, OWNER)
        owner = type("R", (), {"session": type("S", (), {
            "principals": frozenset({owner_principal.principal}),
            "identities": frozenset({OWNER, "owner@plx.example"})})(), "mode": "entra"})()
        viewer = self.backend.view_as
        got = viewer.reader_for(owner, va.key_of(VINCE), method="GET", route="/api/home",
                                is_owner=lambda r: True, reader_type=lambda *a: a)
        self.assertEqual(got[2], "Vince Example")
        viewer.mark(owner, va.key_of(VINCE), "stop", is_owner=lambda r: True)
        text = (self.state / "view-as" / "audit.jsonl").read_text()
        self.assertNotIn(OWNER, text)
        self.assertNotIn(TID, text)
        self.assertNotRegex(text, r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        self.assertEqual({r["viewer"] for r in self.audit()}, {va.viewer_of(owner)})

    def test_starting_and_stopping_a_view_is_audited_for_the_owner_only(self):
        vince = va.key_of(VINCE)
        self.assertEqual(self.owner.post("/api/view-as/start", json={"key": vince}).json(),
                         {"recorded": True, "event": "start"})
        self.assertEqual(self.owner.get("/api/home", headers={va.HEADER: vince}).status_code,
                         200)
        self.assertEqual(self.owner.post("/api/view-as/stop", json={"key": vince}).json(),
                         {"recorded": True, "event": "stop"})
        self.assertEqual([(r["route"], r["status"]) for r in self.audit()],
                         [("view-as:start", 200), ("/api/home", 200), ("view-as:stop", 200)])
        # not the owner; an excluded person; an unknown event; the marks write nothing else
        self.assertEqual(self.ana.post("/api/view-as/start", json={"key": vince}).status_code,
                         403)
        self.assertEqual(self.owner.post("/api/view-as/start",
                                         json={"key": va.key_of(GONE)}).status_code, 404)
        self.assertEqual(self.owner.post("/api/view-as/pause", json={"key": vince}).status_code,
                         422)
        self.assertEqual(self.backend.work_log.events(reader=None)[1], 0)
        # a mark cannot itself be made while viewing as someone
        self.assertEqual(self.owner.post("/api/view-as/stop", json={"key": vince},
                                         headers={va.HEADER: vince}).status_code, 403)

    def test_a_person_excluded_after_the_list_was_read_cannot_be_viewed(self):
        gone = {GONE}
        self.backend.view_as = va.ViewAs(
            candidates=lambda: [(VINCE, "vince@plx.example")],
            excluded=lambda: (lambda oid: oid in gone), identity=view_session,
            audit=va.Audit(self.state))
        owner = self.client("staff:owner", "owner@plx.example", OWNER)
        self.assertEqual(len(owner.get("/api/view-as/people").json()["people"]), 1)  # cached
        gone.add(VINCE)
        # SEC-011: the kept list is re-asked of the exclusion: no longer named
        self.assertEqual(owner.get("/api/view-as/people").json()["people"], [])
        gone.discard(VINCE)
        self.assertEqual(len(owner.get("/api/view-as/people").json()["people"]), 1)
        self.backend.view_as.excluded = lambda: None          # no exclusion now: nobody
        self.assertEqual(owner.get("/api/view-as/people").json()["people"], [])
        self.backend.view_as.excluded = lambda: (lambda oid: oid in gone)
        gone.add(VINCE)
        self.assertEqual(owner.get("/api/home", headers={va.HEADER: va.key_of(VINCE)})
                         .status_code, 404)
        self.assertEqual(owner.post("/api/view-as/start", json={"key": va.key_of(VINCE)})
                         .status_code, 404)
        self.assertEqual(self.backend.readers, [])

    def test_cardinality_of_the_people_list(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                ids = [f"aaaaaaaa-bbbb-4ccc-8ddd-{i:012d}" for i in range(100, 100 + n)]
                viewer = va.ViewAs(candidates=lambda ids=ids: [(i, f"p{i[-4:]}@plx.example")
                                                               for i in ids],
                                   excluded=lambda: (lambda oid: False), identity=view_session)
                people = viewer.people({OWNER})
                self.assertEqual(len(people), n)
                self.assertEqual(len({p["key"] for p in people}), n)


class ViewAsSession(unittest.TestCase):
    """``session_identity.view_as_session`` over a real resolver and Portal snapshot."""

    def test_resolves_as_the_sign_in_does_and_writes_nothing(self):
        from scripts import eval_owner_host as tool
        from operator_api.backend import view_as_for
        oid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        claims = {"uid": "cmreader0001", "email": "reader@example.com",
                  "entraTenantId": TID.upper(), "entraObjectId": oid}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "host"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(tool.main(["--out", str(out), "--role", "purchasing"],
                                           environ={"PLX_EVAL_READER": json.dumps(claims)}), 0)
            policy = ep.EmployeePermissionPolicy.from_file(out / "employee_policy.json")
            resolver = ep.EmployeePermissionResolver(
                policy, ep.PortalAccessSnapshot(out / "portal_access.json"))
            exp = int(time.time()) + va.SESSION_SECONDS
            session = si.view_as_session(resolver, oid, expires_at=exp)
            self.assertTrue(session.view_as)
            self.assertFalse(session.warm_ahead)
            self.assertEqual(session.principals, {f"employee:{oid}"})
            self.assertEqual(session.describe()["proof"], "owner_view_as")
            with self.assertRaises(si.SessionIdentityRefused):
                session.intent_identity(session.actor)
            with self.assertRaises(si.SessionIdentityRefused):
                si.view_as_session(resolver, "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff",
                                   expires_at=exp)
            with self.assertRaises(si.SessionIdentityRefused):
                si.view_as_session(resolver, oid, expires_at=int(time.time()) - 1)
            with self.assertRaises(si.SessionIdentityRefused):
                si.view_as_session(object(), oid, expires_at=exp)
            # the served wiring: the snapshot's active users, the exclusion fail closed
            viewer = view_as_for(resolver, tmp)

            class People:
                unavailable = None

                def is_excluded(self, value):
                    return False
            with mock.patch("operating.briefs.env_people", lambda: People()):
                self.assertEqual([p["name"] for p in viewer.people(set())], ["Reader"])
            viewer._people = None
            with mock.patch("operating.briefs.env_people", lambda: None):
                self.assertEqual(viewer.people(set()), [])


if __name__ == "__main__":
    unittest.main()
