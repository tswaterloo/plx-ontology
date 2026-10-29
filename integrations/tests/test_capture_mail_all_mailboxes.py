"""Directory-wide mail capture (owner directive c75dfaba0f8e) over an injected Graph.

Every mailbox in the tenant directory except the excluded people's and the HR / payroll
deny list (``integrations.mail_directory_scope``), captured by the existing resumable
capture. No network and no credentials: every request goes to the in-process fake Graph
of ``test_capture_mail`` with a directory listing that carries departments and proxy
addresses. Synthetic ids and ``example-plx.com`` addresses only. Evidence label:
``injected-transport`` (live-unverified until ``graph.mail_all_mailboxes`` passes).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from devloop import live_verify as lv
from integrations import graph_read
from integrations import mail_directory_scope as mds
from integrations.capture_mail import CaptureError, MailCaptureStore
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.tests.test_capture_mail import ENV, FakeGraph, file_attachment, j
from ontology.types import Actor

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "devloop/live-checks/graph.mail_all_mailboxes.json")
                  .read_text(encoding="utf-8"))
AUDIENCE = ROOT / "examples/mail_all_mailboxes_audience.live-check.json"
EXCLUDED = ENV["PLX_EXCLUDED_PEOPLE_OIDS"]
EXCLUDED_UPN = "member.x1@example-plx.com"
EXCLUDED_ALIAS = "x1.alias@example-plx.com"


def oid(n: int) -> str:
    return f"12345678-aaaa-bbbb-cccc-{n:012d}"


class DirectoryGraph(FakeGraph):
    """The fake Graph with a tenant directory: members (each an inbox and a sent items
    folder), the excluded person, the HR / payroll mailboxes, a guest and a user without
    mail; the ``/users`` listing returns department and proxy addresses, paged."""

    def __init__(self, members: int = 8, messages: int = 1) -> None:
        super().__init__()
        self.users, self.folders, self.messages = {}, {}, {}
        self.users_page = 5
        for n in range(1, members + 1):
            self.add(oid(n), f"box{n}@example-plx.com", department="Operations")
        # The excluded person: listed with an upper-case id, a second SMTP alias.
        self.add(EXCLUDED, EXCLUDED_UPN, aliases=[EXCLUDED_ALIAS], department="Sales",
                 listed_id=EXCLUDED.upper())
        self.denied = [
            self.add(oid(901), "hr@example-plx.com"),                      # token
            self.add(oid(902), "payroll.team@example-plx.com"),            # dotted token
            self.add(oid(903), "people-ops@example-plx.com"),              # joined token
            self.add(oid(904), "desk.d4@example-plx.com",                  # proxy token
                     aliases=["benefits@example-plx.com"]),
            self.add(oid(905), "member.d5@example-plx.com",                # department
                     department="Human Resources & Payroll"),
            self.add(oid(906), "member.d6@example-plx.com",                # 'and' folded
                     department="People and Culture"),
            self.add(oid(907), "member.d7@example-plx.com",                # unknown shape
                     department=["Operations"]),
        ]
        # Near misses the deny list must keep: 'hr' inside a word, never a token.
        self.near = [self.add(oid(951), "threshold.ops@example-plx.com",
                              department="Three Rivers Plant")]
        self.add(oid(990), "guest_x@partner.example", user_type="Guest")
        self.add(oid(991), None)                                           # no mailbox
        for box in self.users:
            for m in range(1, messages + 1):
                self.put(box, f"M-{box[-4:]}-{m}", "CK-1")
        self.put(self.near[0], "M-near-att", "CK-1", attachments=[file_attachment("A1")])

    def add(self, box: str, mail: str | None, *, aliases=(), department=None,
            user_type="Member", listed_id: str | None = None) -> str:
        self.users[box] = {
            "id": box, "userPrincipalName": mail or f"nomail{box[-3:]}@example-plx.com",
            "mail": mail, "userType": user_type, "department": department,
            "_listed_id": listed_id or box,
            "proxyAddresses": [f"SMTP:{mail}"] * bool(mail) + [f"smtp:{a}" for a in aliases]}
        self.folders[box] = {"inbox": f"AAMkIN-{box[-3:]}=", f"aamkin-{box[-3:]}=":
                             f"AAMkIN-{box[-3:]}=", "sentitems": f"AAMkSENT-{box[-3:]}=",
                             f"aamksent-{box[-3:]}=": f"AAMkSENT-{box[-3:]}="}
        self.messages[box] = {}
        return box

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        if parts.hostname == "graph.microsoft.com" and unquote(parts.path) == "/v1.0/users":
            self.calls.append((method, url, dict(headers)))
            query = parse_qs(parts.query)
            listed = [{"id": u["_listed_id"], "mail": u["mail"], "userType": u["userType"],
                       "userPrincipalName": u["userPrincipalName"],
                       "department": u["department"], "proxyAddresses": u["proxyAddresses"]}
                      for u in self.users.values()]
            select = query.get("$select", ["id"])[0]
            skip = int(query.get("$skiptoken", ["0"])[0])
            page = {"value": listed[skip:skip + self.users_page]}
            if skip + self.users_page < len(listed):
                page["@odata.nextLink"] = (f"https://graph.microsoft.com/v1.0/users?"
                                           f"$select={select}&$top=999"
                                           f"&$skiptoken={skip + self.users_page}")
            return j(200, page)
        return super().__call__(method, url, headers, body, timeout, max_bytes)

    def _public(self, raw: dict) -> dict:
        return {k: v for k, v in raw.items() if not k.startswith("_")}

    def _delta(self, user, folder, query, headers):
        if "sent" in folder.lower():
            # Sent items: empty in this fake, but a real delta round with its link.
            link = (f"https://graph.microsoft.com/v1.0/users/{user}/mailFolders('{folder}')"
                    f"/messages/delta?$deltatoken=sent")
            return j(200, {"value": [], "@odata.deltaLink": link})
        return super()._delta(user, folder, query, headers)


def excluded_in(url: str) -> bool:
    text = unquote(url).lower()
    return any(token in text for token in (EXCLUDED, EXCLUDED_UPN, EXCLUDED_ALIAS))


class AllMailboxesCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def argv(self, *extra: str, state: Path | None = None) -> list[str]:
        return ["mail-all-mailboxes", "--live-audience", str(AUDIENCE), "--state-dir",
                str(state or self.state), "--page-size", "2", *extra]

    def run_cli(self, graph, *extra: str, environ=ENV, monotonic=None, state=None) -> dict:
        parser = argparse.ArgumentParser()
        mds.add_cli_arguments(parser)
        args = parser.parse_args(self.argv(*extra, state=state)[1:])
        kwargs = {"monotonic": monotonic} if monotonic else {}
        return mds.run_cli(args, environ=environ, sender=graph, sleep=lambda s: None,
                           **kwargs)

    def main(self, graph, *extra: str, environ=ENV) -> tuple[int, dict | None]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = graph_read.main(self.argv(*extra), environ=environ, sender=graph)
        return code, (json.loads(out.getvalue()) if out.getvalue() else None)


class ExclusionTests(AllMailboxesCase):
    def test_excluded_person_never_reaches_a_request_url_and_the_log_proves_it(self):
        graph = DirectoryGraph()
        code, report = self.main(graph)
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertEqual(report["evidence_label"], "injected-transport")
        # The fake saw every request: none named the excluded id or either address.
        self.assertEqual([u for _m, u, _h in graph.calls if excluded_in(u)], [])
        self.assertEqual(report["request_log"], {
            "requests_logged": report["requests_sent"], "complete": True,
            "excluded_requested": 0})
        self.assertEqual(report["requests_sent"], len(graph.calls))   # token POST too
        self.assertEqual(report["directory"]["mailboxes_excluded"], 1)
        # Counts only: no id or address of anyone in the report.
        text = json.dumps(report).lower()
        for user in graph.users.values():
            self.assertNotIn(user["id"], text)
            self.assertNotIn(user["userPrincipalName"], text)
        self.assertEqual(lv.check_expectations(SPEC, code, report), [])

    def test_the_request_log_counts_a_url_naming_an_excluded_person(self):
        # The negative control of excluded_requested: the log reads the URL, not results.
        graph = DirectoryGraph()
        api, _transport = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None))
        people = api.people
        log = mds.RequestLog()
        for url in (f"https://graph.microsoft.com/v1.0/users/{EXCLUDED}/mailFolders/inbox",
                    "https://graph.microsoft.com/v1.0/users('X1.ALIAS%40example-plx.com')",
                    f"https://graph.microsoft.com/v1.0/users/{oid(1)}/messages/delta"):
            log("GET", url)
        self.assertIsNone(log.excluded_requests(people))       # unbound: cannot decide
        people.bind(api)
        self.assertEqual(log.excluded_requests(people), 2)
        self.assertIsNone(log.excluded_requests(PeopleExclusion(
            unavailable="excluded_people_oids_absent")))

    def test_an_unset_or_empty_exclusion_list_refuses_before_any_request(self):
        for value in (None, "", " , "):
            env = {k: v for k, v in ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
            if value is not None:
                env["PLX_EXCLUDED_PEOPLE_OIDS"] = value
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                graph = DirectoryGraph()
                self.state = Path(tmp) / "state"
                code, report = self.main(graph, environ=env)
                self.assertEqual(code, 1)
                self.assertEqual(graph.calls, [])
                self.assertFalse(report["ok"])
                self.assertEqual(len(report["blocked_upstream"]), 1)
                part = report["blocked_upstream"][0]
                self.assertEqual((part["part"], part["error_code"], part["cause"]),
                                 ("mail.people_exclusion", "excluded_people_oids_absent",
                                  "later_ask"))
                self.assertEqual((report["runs"], report["directory"]["status"],
                                  report["request_log"]["excluded_requested"]),
                                 ([], None, None))
                self.assertEqual(lv.check_expectations(SPEC, code, report,
                                                       SPEC["expect_blocked"]), [])
                self.assertNotEqual(lv.check_expectations(SPEC, code, report), [])

    def test_a_refused_directory_listing_is_blocked_with_the_permission(self):
        graph = DirectoryGraph()
        real = graph.__call__

        def refuse_listing(method, url, headers, body, timeout, max_bytes):
            if urlsplit(url).path == "/v1.0/users":
                graph.calls.append((method, url, dict(headers)))
                return j(403, {"error": {"code": "Authorization_RequestDenied"}})
            return real(method, url, headers, body, timeout, max_bytes)

        code, report = self.main(refuse_listing)
        self.assertEqual(code, 1)
        self.assertEqual([(p["part"], p.get("permission")) for p in report["blocked_upstream"]],
                         [("mail.directory_listing", "User.Read.All")])
        self.assertEqual([u for _m, u, _h in graph.calls
                          if "/users/" in urlsplit(u).path], [])
        self.assertEqual(lv.check_expectations(SPEC, code, report, SPEC["expect_blocked"]), [])


class DenyListTests(AllMailboxesCase):
    def test_hr_and_payroll_mailboxes_are_never_requested_and_near_misses_are_kept(self):
        graph = DirectoryGraph(members=3)
        report = self.run_cli(graph)
        self.assertTrue(report["ok"], report)
        named = {unquote(u).lower().split("/users/")[1].split("/")[0].split("?")[0]
                 for _m, u, _h in graph.calls if "/users/" in u}
        self.assertEqual(named & set(graph.denied), set())
        self.assertEqual(named, {oid(1), oid(2), oid(3), *graph.near})
        self.assertEqual(report["directory"], {
            "status": "complete", "mailboxes_listed": 3 + 1 + 7 + 1,
            "mailboxes_excluded": 1, "mailboxes_denied": 7, "mailboxes_enumerated": 4,
            "folders_per_mailbox": 2})
        self.assertEqual(report["summary"]["mailboxes"], 4)

    def test_the_shipped_scope_denies_by_token_and_department(self):
        deny = mds.DirectoryScope.from_file(mds.DEFAULT_SCOPE).deny
        for user, denied in (
                ({"mail": "HR@example-plx.com"}, True),
                ({"mail": "a@example-plx.com", "proxyAddresses": ["smtp:payroll@x.example"]},
                 True),
                ({"mail": "human.resources@example-plx.com"}, True),
                ({"mail": "a@example-plx.com", "department": "Talent Acquisition"}, True),
                ({"mail": "a@example-plx.com", "department": "HR/Admin"}, True),
                ({"mail": "a@example-plx.com", "department": None}, False),
                ({"mail": "chrome.line@example-plx.com", "department": "Shrink Wrap"}, False),
                ({"mail": "a@example-plx.com", "proxyAddresses": ["X500:/o=hr"]}, False)):
            with self.subTest(user=user):
                self.assertIs(deny.denies(user), denied)

    def test_a_scope_without_a_usable_deny_list_fails_closed_before_any_request(self):
        good = json.loads(mds.DEFAULT_SCOPE.read_text(encoding="utf-8"))
        broken = {
            "scope_document_invalid": [{k: v for k, v in good.items() if k != "deny"},
                                       {**good, "mailboxes": []}, {**good, "schema": "x"}],
            "deny_list_invalid": [{**good, "deny": {**good["deny"], "allow": ["x"]}}],
            "deny_address_tokens_invalid": [
                {**good, "deny": {**good["deny"], "address_tokens": []}},
                {**good, "deny": {**good["deny"], "address_tokens": ["h r"]}}],
            "deny_departments_invalid": [
                {**good, "deny": {**good["deny"], "departments": [" - "]}}],
            "deny_reason_missing": [{**good, "deny": {**good["deny"], "reason": " "}}]}
        for code, documents in broken.items():
            for document in documents:
                with self.subTest(code=code, document=document):
                    with self.assertRaises(mds.DirectoryScopeError) as caught:
                        mds.DirectoryScope.from_json(document)
                    self.assertEqual(caught.exception.code, code)
        with self.assertRaises(CaptureError):
            mds.DirectoryScope.from_json({**good, "folders": ["deleteditems/../x"]})
        path = Path(self.tmp.name) / "scope.json"
        path.write_text(json.dumps({k: v for k, v in good.items() if k != "deny"}))
        graph = DirectoryGraph()
        with self.assertRaises(mds.DirectoryScopeError):
            self.run_cli(graph, "--scope", str(path))
        self.assertEqual(graph.calls, [])


class MarkingAndResumeTests(AllMailboxesCase):
    def test_every_row_carries_its_owning_mailbox_and_readers_see_only_their_own(self):
        graph = DirectoryGraph(members=3, messages=2)
        report = self.run_cli(graph)
        rows = report["summary"]["messages_current"]
        self.assertEqual(rows, 4 * 2 + 1)
        self.assertEqual(report["owner_marking"], {"with": rows, "without": 0})
        self.assertEqual(report["leak_check"], {
            "withheld_actor_mailboxes": 0, "withheld_actor_rows": 0,
            "withheld_actor_counts": 0, "withheld_actor_resolved": 0})
        self.assertEqual(report["owner_probe"], {
            "mailboxes_visible": 1, "rows_visible": 2, "only_own_mailbox": True,
            "rows_match_own_mailbox": True})
        self.assertEqual(report["resolve_check"]["resolved_offline"], rows)
        # A row whose marking names another mailbox (or none) is counted, and fails ok.
        store = MailCaptureStore(self.state)
        try:
            # The new consumer is gated like every other read: no grant, nothing counted.
            audience = LiveSourceAudience.from_file(AUDIENCE)
            self.assertEqual(store.owner_marking(Actor("plx", frozenset({"staff:none"}),
                                                       "restricted"), audience),
                             {"with": 0, "without": 0})
            row = store.db.execute("SELECT rowid, record FROM messages "
                                   "WHERE superseded_at IS NULL LIMIT 2").fetchall()
            first = json.loads(row[0]["record"])
            first["source_acl"]["identities"] = [oid(999)]
            second = json.loads(row[1]["record"])
            second.pop("source_acl")
            for (rowid, _r), record in zip(row, (first, second)):
                store.db.execute("UPDATE messages SET record = ? WHERE rowid = ?",
                                 (json.dumps(record), rowid))
            store.db.commit()
        finally:
            store.close()
        report = self.run_cli(graph, "--runs", "1")
        self.assertEqual(report["owner_marking"], {"with": rows - 2, "without": 2})
        self.assertFalse(report["ok"])
        self.assertIn("owner_marking.without != expected",
                      " ".join(lv.check_expectations(SPEC, 0, report)))

    def test_a_budget_cut_run_resumes_from_its_stored_cursor_and_loses_nothing(self):
        graph = DirectoryGraph(members=2, messages=7)       # 4 delta pages of 2 per inbox

        def pages() -> int:
            return sum(1 for _m, u, _h in graph.calls if "/messages/delta" in u)

        first = self.run_cli(graph, "--budget-seconds", "6", monotonic=pages)
        run1, run2 = first["runs"]
        self.assertFalse(run1["complete"])
        cut = [f for m in run1["mailboxes"] for f in m["folders"]
               if f["reason"] == "time_budget"]
        self.assertTrue(cut)
        resumed = [f["resumed_from"] for m in run2["mailboxes"] for f in m["folders"]]
        self.assertIn("next_url", resumed)
        self.assertGreaterEqual(first["summary"]["resumed_from_stored_cursor"], 1)
        # The next day (a new invocation on the same state): every folder resumes.
        later = self.run_cli(graph, "--runs", "2")
        while not later["runs"][-1]["complete"]:
            later = self.run_cli(graph, "--runs", "2")
        initial = [u for _m, u, _h in graph.calls if "/messages/delta" in u
                   and "$filter" in parse_qs(urlsplit(u).query)]
        rounds = [unquote(urlsplit(u).path).split("/")[3:6:2] for u in initial]
        self.assertEqual(len(rounds), 3 * 2, rounds)       # one first round per folder
        self.assertEqual(len({tuple(r) for r in rounds}), 3 * 2, rounds)
        self.assertEqual(later["summary"]["messages_current"], 3 * 7 + 1)
        self.assertEqual(later["summary"]["versions"], 3 * 7 + 1)
        self.assertEqual({f["resumed_from"] for m in later["runs"][-1]["mailboxes"]
                          for f in m["folders"]}, {"delta_url"})
        self.assertEqual(lv.completeness(SPEC, later), {"complete": True})



class BudgetEndTests(AllMailboxesCase):
    """gap-86a8a6debd2e: the whole command, enumeration, runs and read-back probes, ends
    within ``--budget-seconds`` (the verifier's timeout never kills it); a run cut by
    the budget over a store this invocation did not find kept is blocked later_ask
    live-host-persistent-state-dir, never an untyped fail. Fake clocks: one second per
    request (and, in the probe test, per probe step)."""

    BUDGET = 40

    def shares(self):
        from unittest.mock import patch
        from integrations import capture_mail as cm
        seen: list[tuple[float, float | None]] = []
        real = cm.MailCapture

        class Recorded(real):
            def __init__(inner, *a, budget_seconds=None, monotonic=None, **k):
                seen.append((monotonic(), budget_seconds))
                super().__init__(*a, budget_seconds=budget_seconds, monotonic=monotonic, **k)
        return seen, patch.object(cm, "MailCapture", Recorded)

    def test_two_runs_and_their_probes_end_by_the_budget_cold_cut_blocked(self):
        from devloop.tests.spec_verdict import verdict
        graph = DirectoryGraph(members=6, messages=7)

        def clock() -> int:
            return len(graph.calls)

        seen, patched = self.shares()
        with patched:
            report = self.run_cli(graph, "--runs", "2", "--budget-seconds", str(self.BUDGET),
                                  monotonic=clock)
        stamps = list(range(len(graph.calls)))     # request k starts at second k
        # No request starts at or after the budget. Past the capture's share only the
        # read a page's first message makes (each page captures one message at least,
        # so a cut run still moves on) starts: never a page, mailbox or folder lookup;
        # the probes' reserve covers it.
        self.assertTrue(stamps)
        self.assertLess(stamps[-1], self.BUDGET)
        capture_end = self.BUDGET - self.BUDGET * mds.PROBE_RESERVE_SHARE
        late = [urlsplit(u).path for k, (_m, u, _h) in enumerate(graph.calls)
                if k >= capture_end]
        self.assertLessEqual(len(late), 1, late)
        self.assertTrue(all("/messages/" in p and "/delta" not in p for p in late), late)
        # The second run gets only what the first left of the capture share.
        self.assertEqual(len(report["runs"]), 2)
        (t1, share1), (t2, share2) = seen
        self.assertAlmostEqual(share1, (capture_end - t1) / 2)
        self.assertAlmostEqual(share2, capture_end - t2)
        self.assertGreaterEqual(t2, t1)
        # The probes ran to the end inside the reserve: nothing null.
        self.assertFalse(report["probes_cut"])
        self.assertEqual(report["leak_check"], {
            "withheld_actor_mailboxes": 0, "withheld_actor_rows": 0,
            "withheld_actor_counts": 0, "withheld_actor_resolved": 0})
        # Progress shown: what the runs captured before the cut.
        self.assertGreater(report["summary"]["messages_current"], 0)
        # Cut over a store nothing carried: blocked later_ask, never an untyped fail.
        self.assertFalse(report["runs"][-1]["complete"])
        self.assertFalse(report["ok"])
        self.assertTrue(report["blocked_upstream"])
        self.assertEqual({(p["cause"], p["later_ask"]) for p in report["blocked_upstream"]},
                         {("later_ask", "live-host-persistent-state-dir")})
        self.assertEqual(verdict(SPEC, 1, report), ("blocked", []))

    def test_probes_the_budget_cuts_are_null_and_fail_never_blocked(self):
        from unittest.mock import patch
        from devloop.tests.spec_verdict import verdict
        graph = DirectoryGraph(members=6, messages=1)
        probing = {"on": False, "ticks": 0}

        def clock() -> int:
            if probing["on"]:
                probing["ticks"] += 1     # each probe step takes a second
            return len(graph.calls) + probing["ticks"]

        real = mds._probes

        def probes(*a, **k):
            probing["on"] = True
            try:
                return real(*a, **k)
            finally:
                probing["on"] = False
        # The capture's own requests, measured on a twin graph and store: the budget
        # leaves the capture its share and the probes fewer seconds than their steps.
        twin = DirectoryGraph(members=6, messages=1)
        self.run_cli(twin, "--runs", "1", state=Path(self.tmp.name) / "twin")
        budget = int(len(twin.calls) / (1 - mds.PROBE_RESERVE_SHARE)) + 2
        with patch.object(mds, "_probes", probes):
            report = self.run_cli(graph, "--runs", "1", "--budget-seconds", str(budget),
                                  monotonic=clock)
        self.assertEqual(len(graph.calls), len(twin.calls))
        self.assertTrue(report["runs"][-1]["complete"])
        self.assertLessEqual(clock(), budget + 1)   # the step that found it spent
        self.assertTrue(report["probes_cut"])
        # An unfinished probe is null, never a clean zero.
        self.assertIn(None, [*report["leak_check"].values(),
                             *report["exclusion_check"].values(),
                             *report["resolve_check"].values()])
        self.assertFalse(report["ok"])
        self.assertEqual(report["blocked_upstream"], [])
        result, failures = verdict(SPEC, 1, report)
        self.assertEqual(result, "fail")
        self.assertTrue(failures)


class CardinalityTests(AllMailboxesCase):
    def test_one_ten_and_sixty_mailboxes_are_each_captured_once(self):
        for members in (1, 10, 60):
            with self.subTest(members=members), tempfile.TemporaryDirectory() as tmp:
                graph = DirectoryGraph(members=members)
                report = self.run_cli(graph, state=Path(tmp) / "state")
                kept = members + len(graph.near)
                self.assertTrue(report["ok"], report)
                self.assertEqual(report["directory"]["mailboxes_enumerated"], kept)
                self.assertEqual(report["summary"]["mailboxes"], kept)
                self.assertEqual(report["summary"]["folders"], 2 * kept)
                self.assertEqual(report["summary"]["messages_current"], kept + 1)
                self.assertEqual(report["summary"]["versions"], kept + 1)
                self.assertEqual(report["owner_marking"], {"with": kept + 1, "without": 0})
                self.assertEqual(report["request_log"]["excluded_requested"], 0)
                self.assertEqual(report["summary"]["resumed_from_stored_cursor"], 2 * kept)
                # Linear in mailboxes: two directory listings, then per mailbox a lookup,
                # two folder lookups and two deltas per run (no request per other box).
                listing = -(-len(graph.users) // graph.users_page)
                self.assertLessEqual(report["requests_sent"] - 2 * listing, 14 * kept + 8)
                self.assertEqual(lv.completeness(SPEC, report), {"complete": True})
                if members == 10:
                    self.assertEqual(lv.check_expectations(SPEC, 0, report), [])
                if members == 1:
                    # Fewer than the owner's seven mailboxes is not a pass of the check.
                    self.assertTrue(any("mailboxes_enumerated" in f for f in
                                        lv.check_expectations(SPEC, 0, report)))


if __name__ == "__main__":
    unittest.main()
