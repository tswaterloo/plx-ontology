"""Approver identities: roster UPN -> Entra object id through the GET-only Graph client.

Evidence label: ``injected-transport``. A recorded ``/users/{upn}`` sender sits behind the
real ``GuardedTransport``/``SourceApi``; no network, no credential.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from verify import live_verify
from integrations import graph_directory, graph_read
from integrations.graph_directory import (APPROVER_OIDS_DIR, APPROVER_OIDS_FILE,
                                          GraphDirectoryReader, resolve_roster,
                                          resolved_approver_oids, roster_upns)
from integrations.graph_read import build_graph
from operating.goal2_mc_approval import (APPROVER_ROSTER_SCHEMA, ApproverRoster,
                                         load_approver_roster)

ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": "11111111-2222-4333-8444-555555555555",
       "MICROSOFT_GRAPH_CLIENT_ID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
       "MICROSOFT_GRAPH_CLIENT_SECRET": "s3cr3t~CLIENT~value~do-not-leak"}
FOUND, MISSING, OTHER = ("approver.one@example-plx.com", "approver.two@example-plx.com",
                         "approver.three@example-plx.com")
OID = "c0ffee00-0000-4000-8000-0000000000a1"
OTHER_OID = "c0ffee00-0000-4000-8000-0000000000a2"
FOLLOW_UP = "mc.task.create_follow_up"   # one catalog action type the roster approves


def j(status, value):
    return status, {}, json.dumps(value).encode()


class RecordedUsers:
    """Recorded Graph ``/users`` answers: ``users[path segment]`` is the user object
    Graph returned for that UPN (a Graph alias may answer as another user); else 404.
    The probe's first enabled user (``OID``, no groups) is always there."""

    def __init__(self, users):
        self.users = {key.lower(): value for key, value in users.items()}
        self.users[OID] = user("member@example-plx.com", OID)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": "eyJ.token-do-not-leak.sig", "expires_in": 3599})
        path = unquote(parts.path)
        if path == "/v1.0/users":
            return j(200, {"value": [{"id": OID, "accountEnabled": True}]})
        if path.startswith(f"/v1.0/users/{OID}/"):
            return j(200, {"value": []})
        user = self.users.get(path.removeprefix("/v1.0/users/").lower())
        if isinstance(user, int):   # a recorded failure status for that UPN
            return j(user, {"error": {"code": "serviceNotAvailable"}})
        return j(200, user) if user else j(404, {"error": {"code": "Request_ResourceNotFound"}})


def user(upn, oid, enabled=True):
    return {"id": oid, "accountEnabled": enabled, "userPrincipalName": upn, "mail": upn,
            "department": "Operations"}


class RosterResolutionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.roster = self.root / "roster.json"
        entry = {"name": "Approver", "mc_role": "owner", "mc_role_source": "test roster",
                 "oid": None, "oid_source": None, "action_types": "all"}
        self.roster.write_text(json.dumps({
            "schema": APPROVER_ROSTER_SCHEMA, "note": "test", "source_ref": "test roster",
            "approvers": {FOUND: entry, MISSING: entry, OTHER: entry}}), "utf-8")

    def reader(self, sender):
        return GraphDirectoryReader(build_graph(ENV, sender=sender)[0])

    def test_roster_upn_resolves_to_its_oid_and_an_unresolved_approver_stays_unverified(self):
        # OTHER's UPN answers as another user (an alias): conservative identity refuses it.
        graph = RecordedUsers({FOUND: user(FOUND.upper(), OID.upper()),
                               OTHER: user("someone.else@example-plx.com", OTHER_OID)})
        summary = resolve_roster(self.reader(graph), self.state, roster=self.roster)
        self.assertEqual(summary, {"entries": 3, "resolved": 1, "kept": 0, "unresolved": 2,
                                   "reasons": {"user_mismatch": 1, "user_not_found": 1},
                                   "state_file": "written", "status": "unverified"})
        self.assertEqual({m for m, _ in graph.calls}, {"GET", "POST"})   # POST: the token
        held = self.state / APPROVER_OIDS_DIR / APPROVER_OIDS_FILE
        self.assertEqual(stat.S_IMODE(held.stat().st_mode), 0o600)
        approvers = load_approver_roster(self.roster, state_dir=self.state)
        roster, read_at = approvers.mc_roster, resolved_approver_oids(self.state)[FOUND]["read_at"]
        self.assertEqual((roster.oid_of(FOUND), roster.oid_of(OTHER)), (OID, None))
        self.assertEqual(roster.oid_provenance(FOUND), {"source": "graph_users",
                                                        "read_at": read_at})
        # Never copied into the per-call approvers: the roster gives it at each check.
        self.assertEqual(approvers.for_action(FOLLOW_UP),
                         {FOUND: None, MISSING: None, OTHER: None})
        self.assertIsNone(load_approver_roster(self.roster).mc_roster.oid_of(FOUND))
        # A held id expires at the check, however long ago the roster was loaded, and a
        # host start that resolves nobody drops it.
        later = graph_directory._utc_now() + timedelta(hours=25)
        self.assertEqual((roster.oid_of(FOUND, later), roster.oid_unverified(FOUND, later)),
                         (None, True))
        summary = resolve_roster(None, self.state, roster=self.roster)
        self.assertEqual((summary["resolved"], summary["reasons"]),
                         (0, {"graph_unavailable": 3}))
        self.assertIsNone(load_approver_roster(self.roster, state_dir=self.state)
                          .for_action(FOLLOW_UP)[FOUND])

    def test_a_transient_graph_failure_keeps_a_fresh_earlier_oid_and_nothing_else(self):
        start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        resolve_roster(self.reader(RecordedUsers({FOUND: user(FOUND, OID),
                                                  MISSING: user(MISSING, OTHER_OID)})),
                       self.state, roster=self.roster, clock=lambda: start)
        # Next host start: Graph answers 503 for FOUND and 404 for MISSING (now gone).
        flaky = RecordedUsers({FOUND: 503})
        restart = start + timedelta(hours=2)
        summary = resolve_roster(self.reader(flaky), self.state, roster=self.roster,
                                 clock=lambda: restart)
        self.assertEqual((summary["resolved"], summary["kept"], summary["unresolved"],
                          summary["status"]), (0, 1, 2, "unverified"))
        self.assertEqual(resolved_approver_oids(self.state),
                         {FOUND: {"oid": OID, "read_at": "2026-10-01T12:00:00Z"}})
        # Kept with its first read time, so it still expires 24 h after that read.
        roster = load_approver_roster(self.roster, state_dir=self.state).mc_roster
        self.assertEqual(roster.oid_of(FOUND, restart), OID)
        self.assertIsNone(roster.oid_of(FOUND, start + timedelta(hours=24, seconds=1)))
        summary = resolve_roster(self.reader(flaky), self.state, roster=self.roster,
                                 clock=lambda: start + timedelta(hours=25))
        self.assertEqual((summary["kept"], resolved_approver_oids(self.state)), (0, {}))
        # Fail closed: an unknown provenance or a malformed read time is no roster; a read
        # time in the future (clock skew, a hand-edited file) is never fresh.
        for bad in ({"oid_source": "portal"}, {"oid_source": "graph_users",
                                                "oid_read_at": "2026-10-01"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ApproverRoster({FOUND: {"role": "owner", "oid": OID, "action_types": (),
                                        **bad}}, source_ref="t")
        ahead = ApproverRoster({FOUND: {"role": "owner", "oid": OID, "action_types": (),
                                        "oid_source": "graph_users",
                                        "oid_read_at": "2026-10-01T13:00:00Z"}}, source_ref="t")
        self.assertEqual((ahead.oid_of(FOUND, start), ahead.oid_unverified(FOUND, start)),
                         (None, True))

    def test_a_mismatched_future_or_foreign_resolution_resolves_nobody(self):
        resolve_roster(self.reader(RecordedUsers({FOUND: user(FOUND, OID)})), self.state,
                       roster=self.roster)
        held = self.state / APPROVER_OIDS_DIR / APPROVER_OIDS_FILE
        written = json.loads(held.read_text("utf-8"))
        self.assertEqual(load_approver_roster(self.roster, state_dir=self.state)
                         .mc_roster.oid_of(FOUND), OID)   # the control
        now = graph_directory._utc_now()
        ahead = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # A person's committed oid that Graph contradicts counts no more than the read.
        pinned = json.loads(self.roster.read_text("utf-8"))
        pinned["approvers"][FOUND] = {**pinned["approvers"][FOUND], "oid": OTHER_OID,
                                      "oid_source": "a person's list (test)"}
        pinned_file = self.root / "pinned.json"
        pinned_file.write_text(json.dumps(pinned), "utf-8")
        for name, roster, document in (
                ("committed oid mismatch", pinned_file, written),
                ("future read_at", self.roster,
                 {**written, "approvers": {FOUND: {"oid": OID, "read_at": ahead}}}),
                ("foreign schema", self.roster, {**written, "schema": "plx-approver-oids/v2"})):
            with self.subTest(name):
                held.write_text(json.dumps(document), "utf-8")
                approvers = load_approver_roster(roster, state_dir=self.state)
                mc_roster, later = approvers.mc_roster, now + timedelta(hours=2)
                # Unknown, not unverified, now and once the future stamp has passed:
                # call_approver_oid_unknown / decider_oid_unknown.
                self.assertEqual((mc_roster.oid_of(FOUND), mc_roster.oid_of(FOUND, later),
                                  mc_roster.oid_unverified(FOUND, later),
                                  approvers.for_action(FOLLOW_UP)[FOUND]),
                                 (None, None, False, None))

    def test_an_unwritable_state_dir_is_a_host_status_not_an_exception(self):
        self.state.mkdir()
        (self.state / APPROVER_OIDS_DIR).write_text("not a directory", "utf-8")
        graph = RecordedUsers({FOUND: user(FOUND, OID), MISSING: user(MISSING, OTHER_OID),
                               OTHER: user(OTHER, OID.replace("a1", "a3"))})
        summary = resolve_roster(self.reader(graph), self.state, roster=self.roster)
        self.assertEqual((summary["resolved"], summary["state_file"], summary["status"]),
                         (3, "write_failed", "unverified"))
        self.assertEqual(resolved_approver_oids(self.state), {})

    def test_probe_counts_the_committed_roster_and_prints_no_oid_or_upn(self):
        upns = roster_upns()
        oids = [OTHER_OID[:-2] + f"{n:02x}" for n in range(len(upns))]
        graph = RecordedUsers({upn: user(upn, oid) for upn, oid in zip(upns, oids)})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(graph_directory.main(["probe"], environ=ENV, sender=graph), 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["roster_entries"], report["roster_resolved"],
                          report["roster_unresolved"], report["roster_reason"]),
                         (len(upns), len(upns), 0, "none"))
        for value in (OID, *oids, *(upn.lower() for upn in upns), "do-not-leak"):
            self.assertNotIn(value, out.getvalue().lower())
        spec = json.loads((live_verify.ROOT / live_verify.SPECS_DIR_NAME
                           / "graph.directory_entitlements.json").read_text("utf-8"))
        # Injected output is never the live-read label; nothing else fails.
        self.assertEqual(live_verify.check_expectations(spec, 0, report),
                         ["evidence_label != expected"])
        out = io.StringIO()
        with redirect_stdout(out):
            graph_directory.main(["probe"], environ=ENV, sender=RecordedUsers({}))
        self.assertEqual(json.loads(out.getvalue())["roster_reason"], "user_not_found")


class DeniedDirectory:
    """Graph answers every read with a 403 (by default the one an app without
    User.Read.All gets, graph-403-diagnose): the body's message carries planted text."""

    PLANTED = "Insufficient privileges planted-https://graph.microsoft.com/v1.0/users"

    def __init__(self, code: str = "Authorization_RequestDenied") -> None:
        self.code = code

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if urlsplit(url).hostname == "login.microsoftonline.com":
            return j(200, {"access_token": "eyJ.token-do-not-leak.sig", "expires_in": 3599})
        return j(403, {"error": {"code": self.code, "message": self.PLANTED}})


class ProbeFailureClassTest(unittest.TestCase):
    """A probe Graph fails prints the failure's class (status, error-code token,
    detail) on stderr so the live record names it; never the message. The users
    listing's permission refusal is the blocked part instead (exit 1, stdout)."""

    def test_both_graph_probes_print_the_403_class_and_no_message(self):
        expected = {"error": "SourceUnavailable", "error_class": "SourceUnavailable",
                    "http_status": 403, "error_code": "forbidden", "detail": "http_403"}
        for main, part in ((graph_directory.main, "directory.user_listing"),
                           (graph_read.main, "mail.user_listing")):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["probe"], environ=ENV,
                                      sender=DeniedDirectory("Forbidden")), 1)
            self.assertEqual(json.loads(err.getvalue()), expected, main.__module__)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                self.assertEqual(main(["probe"], environ=ENV, sender=DeniedDirectory()), 1)
            self.assertEqual(err.getvalue(), "", main.__module__)
            self.assertEqual(json.loads(out.getvalue())["blocked_upstream"], [
                {"part": part, "source": "microsoft-graph", "http_status": 403,
                 "error_code": "missing_permission", "cause": "host_misconfiguration",
                 "permission": "User.Read.All"}])
            for value in ("planted", "do-not-leak", "graph.microsoft.com"):
                self.assertNotIn(value, err.getvalue() + out.getvalue())


if __name__ == "__main__":
    unittest.main()
