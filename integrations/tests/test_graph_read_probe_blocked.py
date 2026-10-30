"""The Graph probes when Graph refuses the mail/users reads for a missing grant.

Live 2026-10-19: ``/users`` answered 403 ``Authorization_RequestDenied`` (consent
graph-mail-directory-consent outstanding) and graph.adapter_probe,
graph.adapter_probe_content, graph.directory_entitlements and profile.graph_mail read
``fail``. A 403 permission refusal of a mail or users read is now a top-level
``blocked_upstream`` part (``capture_mail.graph_block``'s shape) and the committed spec
reads ``blocked``, never ``pass``, with the SharePoint parts still measured; every other
failure (401, another 403, 5xx, transport, a malformed answer, a SharePoint failure beside
the refusal) still reads ``fail``. Each probe's real output is replayed through
``live_verify.run_spec`` with the committed spec (``FakeRoot`` echo).

Evidence label: injected-transport. No network, no credential.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from urllib.parse import unquote, urlsplit

from verify import data_profile, live_verify as lv
from tests.live_verify_helpers import FakeRoot
from integrations import graph_directory, graph_read
from integrations.graph_directory import roster_upns
from integrations.tests.test_live_source_adapters import (
    CLIENT_SECRET, DRIVE, ENV, MAILBOX, SITE_ID, SP_HOST, TEMPAUTH, USER_ID, VINCE_ID,
    FakeSender, drive_item, j, live_drive_item, message)

USERS = "/v1.0/users"
DENIED = j(403, {"error": {"code": "Authorization_RequestDenied",
                           "message": "Insufficient privileges planted-secret"}})
MAIL_DENIED = j(403, {"error": {"code": "ErrorAccessDenied", "message": "planted-secret"}})
CONTENT = b"%PDF-1.7 original"
ECHO = ("import os, sys\nprint(os.environ['FAKE_DOC'])\n"
        "sys.exit(int(os.environ['FAKE_EXIT']))\n")


def routes() -> dict:
    """Every read the probes make answers 200 (the existing adapter probe fixture)."""
    file = drive_item("F1", "Root.pdf", folder="", content=CONTENT, parent_id="ROOT")
    download = f"https://{SP_HOST}/dl?{TEMPAUTH}"
    return {
        USERS: j(200, {"value": [{"id": USER_ID, "accountEnabled": True}]}),
        f"/v1.0/users/{USER_ID}/messages": j(200, {"value": [message("m1", "ck1")]}),
        f"/v1.0/users/{USER_ID}/messages/m1": j(200, message("m1", "ck1")),
        f"/v1.0/users/{VINCE_ID}/mailFolders/INBOX-V/messages":
            j(200, {"value": [message(f"m{n}", f"ck{n}") for n in range(3)]}),
        f"/v1.0/sites/{SITE_ID}": j(200, {"id": "site"}),
        f"/v1.0/sites/{SITE_ID}/drives": j(200, {"value": [{"id": DRIVE}]}),
        f"/v1.0/drives/{DRIVE}/root": j(200, {"id": "ROOT", "name": "root", "eTag": "r",
                                              "root": {}, "folder": {"childCount": 2}}),
        f"/v1.0/drives/{DRIVE}/items/ROOT/children": j(200, {"value": [
            drive_item("D1", "Customers", folder="", is_folder=True, parent_id="ROOT"),
            file]}),
        f"/v1.0/drives/{DRIVE}/items/F1": lambda url: live_drive_item(file, url, download),
        f"/v1.0/drives/{DRIVE}/items/D1": j(200, drive_item("D1", "Customers", folder="",
                                                            is_folder=True)),
        f"/v1.0/drives/{DRIVE}/items/F1/permissions": j(200, {"value": []}),
        f"/v1.0/drives/{DRIVE}/items/D1/permissions": j(200, {"value": []}),
        f"{SP_HOST}/dl": (200, {}, CONTENT),
    }


class Graph(FakeSender):
    """The adapter fixture with some paths answered otherwise: a response, or an
    exception raised as the transport failure."""

    def __init__(self) -> None:
        super().__init__(routes())
        self.overrides: dict = {}
        self.roster: dict = {}  # an approver's UPN -> its graph.user answer

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        path = unquote(urlsplit(url).path)
        answer = self.overrides.get(path, self.roster.get(path.rpartition("/")[2].lower()))
        if isinstance(answer, BaseException):
            raise answer
        if answer is not None:
            return answer
        return super().__call__(method, url, headers, body, timeout, max_bytes)


def denied(path: str, answer=DENIED) -> Graph:
    graph = Graph()
    graph.overrides[path] = answer
    return graph


def directory_denied(*roster_answers) -> Graph:
    """/users refused; the approvers' graph.user reads answer ``roster_answers`` in turn
    (the last repeated), by default refused for the same grant."""
    graph = denied(USERS)
    upns = [upn.lower() for upn in roster_upns()]
    answers = list(roster_answers or (DENIED,))
    graph.roster = {upn: answers[min(n, len(answers) - 1)] for n, upn in enumerate(upns)}
    return graph


# (spec id, the probe's argv, its main)
GRAPH_PROBES = (("graph.adapter_probe", ["probe"], graph_read.main),
                ("graph.adapter_probe_content", ["probe", "--content"], graph_read.main))
DIRECTORY = ("graph.directory_entitlements", ["probe"], graph_directory.main)
PROFILE = ("profile.graph_mail", ["mail", "--mailbox", MAILBOX, "--folder", "inbox",
                                  "--days", "90", "--label", "purchase-order-inbox"],
           data_profile.main)
ALL = (*GRAPH_PROBES, DIRECTORY, PROFILE)


def spec_of(check_id: str) -> dict:
    return json.loads((lv.ROOT / lv.SPECS_DIR_NAME / f"{check_id}.json").read_text("utf-8"))


class ProbeBlockedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeRoot()
        self.addCleanup(self.fake.close)

    def probe(self, main, argv, sender, environ=ENV) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv, environ=environ, sender=sender)
        for leaked in (CLIENT_SECRET, "planted", USER_ID, MAILBOX, VINCE_ID, "Root.pdf"):
            self.assertNotIn(leaked.lower(), (out.getvalue() + err.getvalue()).lower())
        return code, out.getvalue(), err.getvalue()

    def replay(self, check_id: str, code: int, stdout: str, environ=ENV) -> dict:
        """The live verifier's result for this output under the committed spec, unchanged:
        its own module in the fake root prints the output and exits with the probe's code.
        The replay stands in for the live run, so an injected-transport label reads as the
        live one (the label rule itself: test_graph_directory, test_data_profile)."""
        spec = spec_of(check_id)
        module = spec["command"][2]
        package, name = module.rsplit(".", 1)
        (self.fake.root / package).mkdir(exist_ok=True)
        (self.fake.root / package / "__init__.py").write_text("")
        (self.fake.root / package / f"{name}.py").write_text(ECHO)
        text = stdout.replace('"injected-transport"', '"live-read"')
        return lv.run_spec(spec, root=self.fake.root, allowed_modules={module},
                           environ={**self.fake.env, **environ, "FAKE_DOC": text,
                                    "FAKE_EXIT": str(code)})

    def test_users_refused_blocks_the_graph_probes_and_sharepoint_is_still_measured(self):
        user_listing = {"part": "mail.user_listing", "source": "microsoft-graph",
                        "http_status": 403, "error_code": "missing_permission",
                        "cause": "host_misconfiguration", "permission": "User.Read.All"}
        mail_read = {**user_listing, "part": "mail.folder_read", "permission": "Mail.Read"}
        for check_id, argv, main in GRAPH_PROBES:
            for name, extra, sender, part in (
                    ("users", [], denied(USERS), user_listing),
                    ("mail", ["--mailbox", MAILBOX],
                     denied(f"/v1.0/users/{VINCE_ID}/messages", MAIL_DENIED), mail_read)):
                with self.subTest(check_id, refused=name):
                    code, out, err = self.probe(main, argv + extra, sender)
                    self.assertEqual((code, err), (1, ""))
                    report = json.loads(out)
                    self.assertEqual(report["blocked_upstream"], [part])
                    self.assertNotIn("mail", report)
                    self.assertEqual((report["token"], report["sharepoint"]["files"],
                                      report["sharepoint"]["folders"],
                                      report["site"]["drives_count"]), ("ok", 1, 1, 1))
                    if "--content" in argv:
                        self.assertEqual(report["sharepoint"]["content_probe"]["bytes"],
                                         len(CONTENT))
                    check = self.replay(check_id, code, out)
                    self.assertEqual(check["result"], "blocked", check)
                    self.assertEqual(check["detail"]["blocked"]["cause"],
                                     "host_misconfiguration")
                    self.assertTrue(check["detail"]["blocked"]["measured"])
                    self.assertNotIn("failures", check["detail"])

    def test_probe_without_exclusion_ids_is_blocked_later_ask_and_reads_sharepoint(self):
        # No PLX_EXCLUDED_PEOPLE_OIDS: the people guard refuses the mailbox read before
        # any request for it (fail closed). The probe names that typed block (later_ask
        # excluded-people-oids, codes only) and still measures SharePoint, so the spec
        # reads blocked, never fail and never pass (gap-501f33145a0d).
        unset = {k: v for k, v in ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
        part = {"part": "mail.people_exclusion", "source": "people-exclusion",
                "http_status": None, "error_code": "excluded_people_oids_absent",
                "cause": "later_ask", "later_ask": "excluded-people-oids"}
        for check_id, argv, main in GRAPH_PROBES:
            for extra in ([], ["--mailbox", MAILBOX]):
                with self.subTest(check_id, extra=extra):
                    graph = Graph()
                    code, out, err = self.probe(main, argv + extra, graph, unset)
                    self.assertEqual((code, err), (1, ""))
                    report = json.loads(out)
                    self.assertEqual(report["blocked_upstream"], [part])
                    self.assertNotIn("mail", report)
                    self.assertFalse([c for c in graph.calls if "/messages" in c[1]
                                      or "/mailFolders" in c[1]])
                    self.assertEqual((report["token"], report["sharepoint"]["files"],
                                      report["site"]["drives_count"]), ("ok", 1, 1))
                    check = self.replay(check_id, code, out, unset)
                    self.assertEqual(check["result"], "blocked", check)
                    self.assertEqual(check["detail"]["blocked"]["cause"], "later_ask")
                    self.assertTrue(check["detail"]["blocked"]["measured"])
                    self.assertNotIn("failures", check["detail"])
        # The mail profile names the same block, nothing measured, no error counted.
        check_id, argv, main = PROFILE
        graph = Graph()
        code, out, _err = self.probe(main, argv, graph, unset)
        report = json.loads(out)
        self.assertEqual((report["blocked_upstream"], report["errors_count"]), ([part], 0))
        self.assertFalse([c for c in graph.calls if "/users/" in c[1]])
        check = self.replay(check_id, code, out, unset)
        self.assertEqual(check["result"], "blocked", check)
        self.assertEqual(check["detail"]["blocked"]["cause"], "later_ask")
        self.assertNotIn("failures", check["detail"])

    def test_only_a_configuration_refusal_is_the_later_ask(self):
        # A guard unresolved by a failed (chained) or truncated directory listing waits
        # on Graph, not on the ID list: mail_block gives no typed part for it.
        from integrations.live_source_guard import SourceUnavailable
        from integrations.people_exclusion import ExclusionUnavailable
        chained = ExclusionUnavailable("excluded_people_unresolved")
        chained.__cause__ = SourceUnavailable("http_500")
        for exc, later_ask in ((ExclusionUnavailable("excluded_people_oids_absent"), True),
                               (ExclusionUnavailable("excluded_people_oids_invalid"), True),
                               (ExclusionUnavailable("excluded_people_unresolved"), False),
                               (chained, False), (SourceUnavailable("http_500"), False)):
            with self.subTest(code=str(exc), chained=exc.__cause__ is not None):
                self.assertIs(graph_read.exclusion_later_ask(exc), later_ask)

    def test_directory_and_mail_profile_refused_read_blocked_never_measured(self):
        for (check_id, argv, main), part, permission in (
                (DIRECTORY, "directory.user_listing", "User.Read.All"),
                (PROFILE, "mail.mailbox_lookup", "User.Read.All")):
            # The directory probe reads /users; the profile opens the mailbox by its
            # directory lookup (/users/{upn}): both refused for User.Read.All.
            sender = (directory_denied() if main is graph_directory.main
                      else denied(f"{USERS}/{MAILBOX}"))
            with self.subTest(check_id):
                code, out, err = self.probe(main, argv, sender)
                report = json.loads(out)
                self.assertEqual(report["blocked_upstream"], [
                    {"part": part, "source": "microsoft-graph", "http_status": 403,
                     "error_code": "missing_permission", "cause": "host_misconfiguration",
                     "permission": permission}])
                check = self.replay(check_id, code, out)
                self.assertEqual(check["result"], "blocked", check)
                self.assertNotIn("measured", check["detail"]["blocked"])
                self.assertNotIn("failures", check["detail"])
        self.assertEqual(report["errors_count"], 0)  # the refused target is not an error

    def test_every_other_failure_still_fails_and_is_never_masked_as_blocked(self):
        cases = (
            ("401", j(401, {"error": {"code": "InvalidAuthenticationToken"}})),
            ("403 another code", j(403, {"error": {"code": "Forbidden"}})),
            ("403 no code", j(403, {})),
            ("503", j(503, {"error": {"code": "serviceNotAvailable"}})),
            ("transport", ConnectionResetError()),
            ("200 not json", (200, {}, b"<html>")),
        )
        for check_id, argv, main in ALL:
            path = USERS if main is not data_profile.main else f"{USERS}/{MAILBOX}"
            for name, answer in cases:
                with self.subTest(check_id, failure=name):
                    code, out, _err = self.probe(main, argv, denied(path, answer))
                    if out:
                        self.assertNotIn("blocked_upstream", json.loads(out))
                    self.assertEqual(self.replay(check_id, code, out)["result"], "fail")
        # A refused /users beside an approver read failing otherwise (every one, or one of
        # them beside the others refused): the roster failure is not masked.
        for answers in ((j(503, {}),), (j(503, {}), DENIED), (DENIED, j(404, {}))):
            code, out, _err = self.probe(graph_directory.main, ["probe"],
                                         directory_denied(*answers))
            self.assertIn("blocked_upstream", json.loads(out))
            self.assertEqual(self.replay(DIRECTORY[0], code, out)["result"], "fail",
                             [a[0] for a in answers])
        # A refused users read beside a SharePoint failure: the failure is not masked.
        for check_id, argv, main in GRAPH_PROBES:
            graph = denied(USERS)
            graph.overrides[f"/v1.0/drives/{DRIVE}/root"] = j(503, {})
            code, out, _err = self.probe(main, argv, graph)
            self.assertEqual(self.replay(check_id, code, out)["result"], "fail", check_id)

    def test_every_read_answered_is_unchanged_and_passes(self):
        for check_id, argv, main in ALL:
            with self.subTest(check_id):
                code, out, err = self.probe(main, argv, Graph())
                report = json.loads(out)
                self.assertEqual(code, 0, err)
                self.assertNotIn("blocked_upstream", report)
                self.assertNotIn("ok", report)
                if main is graph_directory.main:
                    continue  # the committed roster's UPNs are not in this fixture
                check = self.replay(check_id, code, out)
                self.assertEqual(check["result"], "pass", check)


if __name__ == "__main__":
    unittest.main()
