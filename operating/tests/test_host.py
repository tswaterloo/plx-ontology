"""``python3 -m operating.host``: capture cycles into one state directory, the projection
built from them, and the MCP start-up path over it.

Evidence label: ``injected-transport``. Fake Graph (mail, SharePoint, Teams, directory), BC and
Portal Workbench senders sit behind the real GET-only clients; no network, no credential.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import io
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from devloop import live_verify as lv
from integrations import capture_sharepoint as cs
from integrations import capture_teams
from integrations.capture_sharepoint import CaptureScope
from integrations.graph_directory import roster_upns
from integrations.http import SourceHttpError
from integrations.tests import test_capture_mail as mail_t
from integrations.tests import test_capture_sharepoint as sp_t
from integrations.tests import test_capture_teams as teams_t
from integrations.tests.test_capture_breadth import DeltaDrive
from integrations.tests.test_document_understanding import process_gone, wait_for_pid
from integrations.tests.test_bc_capture import BOOKS, CO, FakeBc
from integrations.tests.test_live_source_adapters import ENV as SOURCE_ENV
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.tests.test_source_mirror_persisted import Outage
from ontology import project_incremental as inc
from ontology.core import Actor
from ontology.tests.test_resolve import CATALOG, tables
from operating import host as host_module
from operating.case_mail_scope import MailScope
from operating import action_path as ap
from operating import session_identity as si
from operating.case_runtime import CASE_RUNTIME_DIR, CASES_FILE
from operating.cases import CaseNotFound, CaseStore
from operating.tests.test_action_path import EFFECT, OPS, REL, SESSION, ActionPathTestCase
from operating.tests.test_session_identity import EMAIL_A, OID_A, SECRET, Fixture, identity_files

ROOT = Path(__file__).resolve().parents[2]
SVC = "svc:workbench-host"
# The probe case's group and the one co-worker in it who resumes the case after the kill:
# no source grant (the projected fact is a withheld marker for them), the intent readable.
GROUP, COWORKER = "group:host-check-case", "staff:host-check-coworker"
CASE_COWORKER = {"group": GROUP, "principal": COWORKER, "member_of": [GROUP]}
ENV = {**SOURCE_ENV, "SHAREPOINT_SITE_ID": sp_t.SITE_ID, "PLX_WORKBENCH_API_KEY": KEY}
HTTP_500 = SourceHttpError("read-only source GET failed: HTTPError", status=500,
                           error_code="internal")
# The ``run`` child ``check --interrupt-after-seconds`` kills: the real host whose every
# Graph, BC and Portal read blocks, so its cycle is running when the kill arrives.
HANGING_RUN = (sys.executable, "-I", "-c", "import sys, time; sys.path.insert(0, {!r}); "
               "from operating import host; hang = lambda *a, **k: time.sleep(600); "
               "sys.exit(host.main(sys.argv[1:], environ={!r}, sender=hang, "
               "workbench_fetch=hang))".format(str(ROOT), ENV))


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


class Router:
    """One injected sender for every Graph and BC request, recording each call."""

    def __init__(self) -> None:
        self.bc = FakeBc(companies=CO)   # pinned companies only: a complete listing
        self.mail, self.drive = mail_t.FakeGraph(), sp_t.seeded_drive()
        self.teams = teams_t.FakeTeamsGraph()   # members resolve through the mail directory
        self.teams.post_root(teams_t.GENERAL, "1001", replies=1)
        self.teams.post_root(teams_t.PRIVATE, "2001")
        self.teams.post_chat("3001")
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        parts = urlsplit(url)
        path = unquote(parts.path)
        if parts.hostname == "login.microsoftonline.com":
            return mail_t.j(200, {"access_token": "eyJ.TOKEN-do-not-leak.sig",
                                  "expires_in": 3599})
        if "businesscentral" in (parts.hostname or ""):
            return self.bc(method, url, headers, body, timeout, max_bytes)
        if parts.hostname == sp_t.SP_HOST or path.startswith(("/v1.0/drives", "/v1.0/sites")):
            return self.drive(method, url, headers, body, timeout, max_bytes)
        if path.startswith(("/v1.0/teams/", "/v1.0/chats/", "/v1.0/reports/")) \
                or path == "/v1.0/teams" \
                or re.fullmatch(r"/v1\.0/users/[^/]+/(?:joinedTeams|chats)", path):
            return self.teams(method, url, headers, body, timeout, max_bytes)
        return self.mail(method, url, headers, body, timeout, max_bytes)

    def mark(self) -> int:
        return len(self.calls)

    def since(self, mark: int) -> list[tuple[str, str]]:
        return self.calls[mark:]


class HostCase(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Synthetic mailboxes: the allowlist opt-out of test_capture_mail (the shipped
        # scope is bound to operating/case_mail_scope.json).
        patcher = patch.object(MailScope, "builtin", return_value=mail_t.SYNTHETIC)
        patcher.start()
        self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.router = Router()
        self.clock = Clock()
        received = (self.clock.now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.router.mail.put(mail_t.BOX_A, "m1", "ck1", received=received)
        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(CATALOG))
        fake.tables = tables()
        for rows in fake.tables.values():   # timestamp-utc columns are served naive
            for row in rows:
                row["updated_at"] = row["updated_at"].rstrip("Z")
        self.workbench = fake
        self.portal = Outage(fake)
        self.config = self.write_config()
        self.confirm_teams(self.state)

    def confirm_teams(self, state: Path, channels=(teams_t.GENERAL, teams_t.PRIVATE),
                      chats=(teams_t.CHAT,)) -> Path:
        """The owner's Teams content confirmation beside ``state``'s census (decision
        2026-10-21-teams-host-confirmation-gate): without it a cycle reads no message."""
        path = capture_teams.confirmation_path(state)
        for parent in reversed(path.parents):
            if not parent.exists():
                parent.mkdir(mode=0o700)
        path.write_text(json.dumps({
            "schema": capture_teams.CONFIRMATION_SCHEMA, "confirmed_by": "owner",
            "confirmed_at": "2026-10-21",
            "channels": [{"team_id": teams_t.TEAM, "channel_id": c} for c in channels],
            "chats": list(chats)}), encoding="utf-8")
        return path

    def probe_dir(self, state: Path) -> None:
        """``state`` as a probe directory the check marked before, holding only the owner's
        Teams confirmation (an unmarked non-empty directory is refused as real state)."""
        shutil.rmtree(state, ignore_errors=True)
        host_module.claim_probe_state(state)
        self.confirm_teams(state)

    def write(self, name: str, value) -> Path:
        path = self.root / "config" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), "utf-8")
        return path

    def write_config(self, **extra) -> Path:
        self.write("capture-audience.json", {
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "default_classification": "restricted",
            "audiences": {f"mail:{mail_t.UPN_A}": [SVC, "staff:operations"],
                          "sharepoint:*": [SVC], "bc:production:*": [SVC],
                          "teams:*": [SVC]},
            "classifications": {"mail:*": "restricted"}})
        self.write("mirror-audience.json", {
            "schema": "plx-source-mirror-audience/v1", "tenant": "plx",
            "audiences": {"public.*": [SVC, "staff:operations"]}})
        self.write("mail-scope.json", {
            "schema": "plx-mail-capture-scope/v1", "window_days": 90,
            "internal_domains": ["example-plx.com"],
            "mailboxes": [{"address": mail_t.UPN_A, "folders": ["inbox"]}]})
        self.write("sharepoint-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "targets": [{"id": "quality", "drive_id": sp_t.DRIVE,
                         "folders": [sp_t.QUALITY_FOLDER], "max_depth": 3,
                         "max_folders": 50}]})
        for name, chats in (("teams-scope.json", "none"),
                            ("teams-chats-scope.json", "mail_allowlist")):
            self.write(name, {"schema": "plx-teams-capture-scope/v1", "members": [
                {"address": mail_t.UPN_A, "joined_teams": True}], "member_chats": chats})
        self.write("books.json", {"schema": "plx-bc-company-books/v1",
                                  "environments": BOOKS})
        policy, portal = identity_files(self.root / "config" / "identity",
                                        {"staff:operations": ("VIEW_OPERATIONS",
                                                              "operations.sales", "ops")})
        return self.write("host.json", {
            "schema": host_module.CONFIG_SCHEMA, "tenant": "plx", "capture_principal": SVC,
            "capture_audience": "capture-audience.json",
            "live_audience": "capture-audience.json",
            "mirror_audience": "mirror-audience.json",
            "employee_policy": str(policy), "portal_access": str(portal),
            "portal": {"base_url": BASE, "relations": sorted(CATALOG)},
            "bc": {"entity_sets": ["purchaseReceipts"], "books": "books.json"},
            "mail": {"scope": "mail-scope.json"},
            "sharepoint": {"scope": "sharepoint-scope.json"},
            "teams": {"scope": "teams-scope.json"}, **extra})

    def main(self, *argv, environ=None, sender=None, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = host_module.main(
                    list(argv), environ=ENV if environ is None else environ,
                    sender=self.router if sender is None else sender,
                    workbench_fetch=self.portal, clock=self.clock, sleep=lambda s: None,
                    portal_options={"clock": self.clock, "retry_delay_seconds": 0}, **kwargs)
        except SystemExit as exit_:   # the MCP host's usage errors (argparse)
            code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def run_cycle(self) -> dict:
        code, out, err = self.main("run", "--state-dir", str(self.state),
                                   "--config", str(self.config))
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def status(self) -> dict:
        code, out, err = self.main("status", "--state-dir", str(self.state))
        self.assertEqual(code, 0, err)
        return {entry["source"]: entry for entry in json.loads(out)["sources"]} | {
            "_": json.loads(out)}


class CycleTests(HostCase):
    def test_second_cycle_reads_only_deltas_and_every_request_is_a_get(self):
        first = self.run_cycle()
        self.assertEqual({name: s["state"] for name, s in first["sources"].items()},
                         {"portal": "captured", "bc": "captured", "mail": "captured",
                          "sharepoint": "captured", "teams": "captured"}, first)
        self.assertEqual(first["projection"]["state"], "built", first)
        # Teams is captured here, so its channel messages are projected beside the rest.
        self.assertEqual(first["projection"]["sources"],
                         ["bc", "graph_mail", "portal_api", "sharepoint", "teams"])
        downloads = self.router.drive.downloads()
        self.assertGreater(downloads, 0)
        self.assertEqual(self.workbench.since_log, [])

        self.clock.advance(900)
        self.router.mail.put(mail_t.BOX_A, "m2", "ck2", received=(
            self.clock.now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        mark = self.router.mark()
        second = self.run_cycle()
        self.assertEqual(second["state"], "complete", second)
        sent = self.router.since(mark)

        # Mail resumes from each folder's stored deltaLink: no new round, no window filter.
        delta = [unquote(u) for _m, u in sent if "/messages/delta" in u]
        self.assertTrue(delta)
        self.assertTrue(all("$deltatoken=" in u for u in delta), delta)
        self.assertEqual(second["sources"]["mail"]["resumed_from"], {"delta_url": 1})
        self.assertEqual(second["sources"]["mail"]["counts"]["versions_new"], 1)
        # BC: modified-since reads after the first full pass, never a full listing.
        listings = [parse_qs(urlsplit(u).query) for _m, u in sent
                    if "businesscentral" in u and u.split("?")[0].endswith("/purchaseReceipts")]
        self.assertTrue(listings)
        self.assertTrue(all(q.get("$filter", [""])[0].startswith("lastModifiedDateTime ge ")
                            for q in listings), listings)
        # (posted receipt lines are re-read per header: Undo Receipt edits them in place)
        self.assertEqual(second["sources"]["bc"]["modes"], {"incremental": 3, "full": 3})
        self.assertEqual(second["sources"]["portal"]["state"], "captured", second)
        # Portal: every relation refreshed through its since column.
        self.assertEqual(sorted({name for name, _ in self.workbench.since_log}),
                         sorted(CATALOG))
        # SharePoint re-walks, but an unchanged file is not downloaded again.
        self.assertEqual(self.router.drive.downloads(), downloads)
        self.assertEqual(second["projection"]["previous_generation"], first["cycle"])
        # The projection is built by generation: only units whose captures changed (the
        # new mail) are projected again, and the file served is the committed generation.
        self.assertEqual(first["projection"]["mode"], "cold")
        self.assertEqual(second["projection"]["mode"], "incremental", second["projection"])
        self.assertLess(0, second["projection"]["units_rebuilt"])
        self.assertLess(second["projection"]["units_rebuilt"], second["projection"]["units"])
        committed = inc.current_generation(host_module.projection_path(self.state).parent)
        self.assertTrue(host_module.projection_path(self.state).samefile(committed.store))

        # GET-only: the only other method is the OAuth token POST.
        for method, url in self.router.calls:
            if urlsplit(url).hostname == "login.microsoftonline.com":
                self.assertEqual(method, "POST")
            else:
                self.assertEqual(method, "GET", url)

    def test_portal_500_serves_the_last_generation_labelled_stale_since(self):
        self.run_cycle()
        before = self.status()["portal"]
        self.assertEqual((before["served_from"], before["stale_since"]), ("live", None))
        self.clock.advance(3600)
        self.portal.down = HTTP_500
        report = self.run_cycle()
        portal = report["sources"]["portal"]
        self.assertEqual(portal["state"], "persisted_capture", portal)
        self.assertEqual(portal["reasons"], ["live_source_unavailable:500:internal"])
        # The other sources keep going.
        self.assertEqual(report["sources"]["mail"]["state"], "captured")
        now = self.status()
        self.assertEqual(now["portal"]["generation"], before["generation"])
        self.assertEqual(now["portal"]["served_from"], "persisted_capture")
        self.assertTrue(now["portal"]["stale_since"].startswith("2026-09-29T12:00:00"),
                        now["portal"])
        self.assertEqual(now["portal"]["age_seconds"], 3600)
        self.assertEqual(now["mail"]["generation"], 2)
        # The projection generation built in that cycle says so for the Portal.
        store = host_module.projection_path(self.state)
        conn = sqlite3.connect(store)
        try:
            meta = dict(conn.execute("SELECT key, value FROM store_meta"))
        finally:
            conn.close()
        (fresh,) = [f for f in json.loads(meta["freshness"]) if f["source"] == "portal_api"]
        self.assertEqual((fresh["stale"], fresh["served_from"]), (True, "persisted_capture"))
        self.assertIn("live source unavailable (500, internal)", fresh["label"])

    def test_a_check_over_n_configured_sources_reports_exactly_n_complete_status_entries(self):
        # The live config reads four of the five known sources (no Teams): status lists
        # those four, each complete after a full cycle, and names the fifth apart, so the
        # check's complete_when never waits on a source no configuration feeds.
        spec = json.loads((ROOT / "devloop/live-checks/host.cycles.json").read_text())
        self.config = self.write_config(teams=None)
        configured = set(host_module.HostConfig.from_file(self.config).sources)
        self.assertEqual(len(configured), 4)

        def make_host():
            return host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                    environ=ENV, sender=self.router,
                                    workbench_fetch=self.portal, clock=self.clock,
                                    sleep=lambda s: None,
                                    portal_options={"clock": self.clock,
                                                    "retry_delay_seconds": 0})
        document = host_module.cycles_check(make_host, cycles=2)
        entries = document["status"]["sources"]
        self.assertEqual(sorted(e["source"] for e in entries), sorted(configured), entries)
        self.assertEqual([(e["configured"], e["complete"]) for e in entries],
                         [(True, True)] * len(configured), entries)
        self.assertEqual(document["status"]["not_configured"], ["teams"])
        self.assertEqual(document["checks"]["sources_configured"], len(entries))
        self.assertEqual(lv.completeness(spec, document), {"complete": True})

    def test_status_per_source_and_a_restart_continues_the_same_state(self):
        # Before any cycle the ledger records no configured source: none is listed.
        empty = self.status()["_"]
        self.assertEqual((empty["sources"], empty["not_configured"]),
                         ([], list(host_module.SOURCES)))
        self.assertIsNone(empty["projection"])
        # A host holding its config lists each configured source, not yet run, as
        # incomplete at generation 0 (a check never reads a source it never ran as done).
        held = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ=ENV).status()
        self.assertEqual([(e["source"], e["generation"], e["complete"])
                          for e in held["sources"]],
                         [(s, 0, None) for s in host_module.SOURCES])
        self.run_cycle()
        self.clock.advance(120)
        status = self.status()
        for name in host_module.SOURCES:
            entry = status[name]
            self.assertEqual((entry["configured"], entry["generation"], entry["served_from"],
                              entry["complete"], entry["age_seconds"], entry["stale_since"]),
                             (True, 1, "live", True, 120, None), entry)
        self.assertEqual(status["_"]["projection"]["generation"], 1)
        self.assertEqual(status["_"]["last_cycle"]["state"], "complete")
        # The console the host serves re-gates the captured mail and SharePoint per item.
        argv = host_module.console_arguments(host_module.Host(
            self.state, host_module.HostConfig.from_file(self.config), environ=ENV), 0)
        for flag in ("--mail-capture-dir", "--sharepoint-capture-dir"):
            self.assertEqual(argv[argv.index(flag) + 1], str(self.state.resolve()), argv)
        # A source failing is stale since its last capture; its generation stays.
        broken = self.write_config(bc={"entity_sets": ["purchaseReceipts"],
                                       "books": "missing-books.json"})
        code, out, err = self.main("run", "--state-dir", str(self.state),
                                   "--config", str(broken))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sources"]["bc"]["state"], "failed")
        bc = self.status()["bc"]
        self.assertEqual((bc["generation"], bc["served_from"], bc["stale_since"]),
                         (1, "capture", status["bc"]["captured_at"]), bc)
        self.assertTrue(bc["last_error"])
        # A new process on the same state directory (restart) reads only deltas.
        self.config = self.write_config()
        mark = self.router.mark()
        third = self.run_cycle()
        self.assertEqual(third["cycle"], 3)
        delta = [unquote(u) for _m, u in self.router.since(mark) if "/messages/delta" in u]
        self.assertTrue(delta and all("$deltatoken=" in u for u in delta), delta)
        self.assertEqual(self.status()["mail"]["generation"], 3)

    def test_host_cycles_check_meets_its_live_spec_and_blocks_only_on_an_outage(self):
        spec = json.loads((ROOT / "devloop/live-checks/host.cycles.json").read_text())
        lv.validate_spec(spec, stem="host.cycles")
        interrupt = ["--interrupt-after-seconds", "5"]
        self.assertEqual(spec["command"][-2], interrupt[0])
        config = self.write_config(case_coworker=CASE_COWORKER)
        self.probe_dir(self.state)
        code, out, err = self.main("check", "--state-dir", str(self.state),
                                   "--config", str(config), *interrupt,
                                   child=HANGING_RUN)
        document = json.loads(out)
        # Two cycles on one state directory: chained generations, fewer units rebuilt,
        # nothing projected in the output, 0 objects for a reader without any grant; a
        # real child cycle SIGKILLed between them, marked interrupted by the next cycle,
        # which builds on the last committed generation; the probe case opened and
        # claimed before the kill reads back whole after it.
        self.assertEqual(lv.check_expectations(spec, code, document), [], (err, document))
        self.assertNotIn("blocked_upstream", document)
        self.assertEqual(document["interrupt"], {"signal": "SIGKILL", "after_seconds": 5.0,
                                                 "killed_running": True,
                                                 "state": "killed_running"})
        self.assertEqual(document["case_resume"], {
            "opened_before_restart": True, "chain_ok": True, "findings_readable": 2,
            "citations_current": True, "seq_unchanged": True, "claim_visible": True,
            "withheld_visible": 0, "intent_parked": True, "intent_chain_ok": True,
            "intent_dispatched": False, "intent_state": "proposed",
            "approval_state": "none", "coworker_read": True, "coworker_chain_ok": True,
            "coworker_intent_state": "proposed", "coworker_approval_state": "none",
            "coworker_withheld_markers": 1})
        # The case is the group's: the co-worker reads it after the restart, a principal
        # outside the group (staff:operations, granted the sources it cites) gets not found.
        path = self.state / CASE_RUNTIME_DIR / CASES_FILE
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            (case_id,) = {r[0] for r in conn.execute("SELECT case_id FROM case_event")}
        finally:
            conn.close()
        cases = CaseStore(path)
        self.assertEqual(cases.get(Actor("plx", frozenset({COWORKER, GROUP}), "restricted"),
                                   case_id)["case_id"], case_id)
        with self.assertRaises(CaseNotFound):
            cases.get(Actor("plx", frozenset({"staff:operations", COWORKER}), "restricted"),
                      case_id)
        conn = sqlite3.connect(self.state / "host" / "host.sqlite")
        try:
            states = [r[0] for r in conn.execute("SELECT state FROM cycle ORDER BY cycle_id")]
        finally:
            conn.close()
        self.assertEqual(states, ["complete", "interrupted", "complete"])
        self.assertEqual(document["cycles"][1]["projection"]["previous_generation"], 1)
        self.assertEqual(lv.completeness(spec, document), {"complete": True})
        # The probe case stays in this directory: no host serves it to an employee.
        for command in (("serve", "--no-console", "--max-cycles", "1"),
                        ("mcp", "--session-ticket-env", "UNSET")):
            code, _out, err = self.main(command[0], "--state-dir", str(self.state),
                                        "--config", str(self.config), *command[1:])
            self.assertEqual((code, json.loads(err)),
                             (2, {"error": "host_probe_state_not_served"}))
        # Teams runs in every cycle (channels whole, then from their delta links) and its
        # channel messages are projected; with chats off for the member no chat is listed
        # or read.
        teams = [cycle["sources"]["teams"] for cycle in document["cycles"]]
        self.assertEqual([(t["state"], t["counts"]["conversations"]) for t in teams],
                         [("captured", 2), ("captured", 2)], teams)
        self.assertEqual([t["counts"]["versions_new"] for t in teams], [3, 0], teams)
        self.assertIn("teams", document["cycles"][-1]["projection"]["sources"])
        gets = [unquote(urlsplit(u).path) for m, u in self.router.calls if m == "GET"]
        self.assertTrue(any("/channels/" in g and g.endswith("/messages/delta") for g in gets))
        self.assertEqual([g for g in gets if "/chats" in g], [])
        # The same host with the allowlist's chats turned on does list them.
        config = self.write_config(teams={"scope": "teams-chats-scope.json"},
                                   case_coworker=CASE_COWORKER)
        self.confirm_teams(self.root / "chats")
        code, out, err = self.main("run", "--state-dir", str(self.root / "chats"),
                                   "--config", str(config))
        self.assertEqual(json.loads(out)["sources"]["teams"]["state"], "captured", err)
        self.assertIn(f"/v1.0/users/{mail_t.BOX_A}/chats",
                      [unquote(urlsplit(u).path) for _m, u in self.router.calls])
        # The leak check sees a projected identifier that reaches the output.
        conn = sqlite3.connect(host_module.projection_path(self.state))
        try:
            raws = [r[0] for r in conn.execute("SELECT raw FROM identifiers")]
        finally:
            conn.close()
        raw = next(r for r in raws if len(r) >= 8 and not r.islower())
        self.assertGreaterEqual(host_module.projection_row_check(
            host_module.projection_path(self.state), out + raw)["leaked"], 1)
        # The Portal down with no persisted copy yet and Graph refusing Teams messages (a
        # missing permission): blocked on those parts, the rest held.
        self.portal.down = HTTP_500
        self.router.teams.forbidden = [r"/messages"]
        self.probe_dir(self.root / "outage")
        code, out, err = self.main("check", "--state-dir", str(self.root / "outage"),
                                   "--config", str(config), *interrupt,
                                   child=HANGING_RUN)
        document = json.loads(out)
        self.assertEqual(lv.blocked_parts(document), [
            {"part": "portal", "source": "portal-workbench-api",
             "error_code": "missing_mirror", "cause": "upstream_outage"},
            {"part": "teams", "source": "microsoft-graph", "http_status": 403,
             "error_code": "missing_permission", "cause": "host_misconfiguration"}])
        self.assertEqual(lv.check_expectations(spec, code, document, spec["expect_blocked"]),
                         [], document["checks"])
        self.assertNotEqual(lv.check_expectations(spec, code, document), [])

    def test_a_parked_intent_dispatched_lost_or_broken_after_the_restart_fails_the_check(self):
        """The probe's parked ``plx.act`` intent (test mode, isolated owner): read back
        after a restart it is proposed, unapproved and never sent; a write that reached
        the owner, a lost action store or a broken event chain each fails the spec."""
        from operating import action_path as ap
        from operating.goal2_exec_owner import IsolatedOwnerSystem, dispatch_payload
        from operating.mcp_server import ACTION_PATH_DIR, ACTION_PATH_FILE

        spec = json.loads((ROOT / "devloop/live-checks/host.cycles.json").read_text())
        self.run_cycle()
        config = self.write_config(case_coworker=CASE_COWORKER)
        host = lambda state: host_module.Host(  # noqa: E731 - a new host per read: a restart
            state, host_module.HostConfig.from_file(config), environ=ENV,
            sender=self.router, clock=self.clock)
        probe = host_module._probe_case(host(self.state))
        self.assertIn("case_id", probe, probe)
        store = ACTION_PATH_DIR + "/" + ACTION_PATH_FILE

        def write_reached_owner(state):
            conn = sqlite3.connect(state / store)
            try:
                row = conn.execute("SELECT payload_json FROM action_event WHERE "
                                   "kind='intent.registered'").fetchone()
            finally:
                conn.close()
            intent = ap.ActionIntent(**json.loads(row[0]))
            IsolatedOwnerSystem(state / ACTION_PATH_DIR / host_module.PROBE_OWNER_FILE
                                ).dispatch(dispatch_payload(intent), intent.idempotency_key)

        def chain_broken(state):   # the file edited past its append-only trigger
            conn = sqlite3.connect(state / store)
            with conn:
                conn.execute("DROP TRIGGER action_event_no_update")
                conn.execute("UPDATE action_event SET at='2000-01-01T00:00:00+00:00'")
            conn.close()

        unread = {"intent_parked": False, "intent_chain_ok": False,
                  "intent_dispatched": None, "intent_state": None, "approval_state": None,
                  "coworker_read": False, "coworker_chain_ok": False,
                  "coworker_intent_state": None, "coworker_approval_state": None}
        cases = {"kept": (None, {}),
                 "dispatched": (write_reached_owner, {"intent_dispatched": True}),
                 "lost": (lambda state: (state / store).unlink(), unread),
                 "chain_broken": (chain_broken, unread)}
        for name, (spoil, changed) in cases.items():
            with self.subTest(name):
                state = self.root / f"resume-{name}"
                shutil.copytree(self.state, state)
                if spoil is not None:
                    spoil(state)
                resumed = host_module._resumed_case(host(state), probe,
                                                    host_module.WITHHELD_PRINCIPAL)
                expected = {"intent_parked": True, "intent_chain_ok": True,
                            "intent_dispatched": False, "intent_state": "proposed",
                            "approval_state": "none", "coworker_read": True,
                            "coworker_chain_ok": True, "coworker_intent_state": "proposed",
                            "coworker_approval_state": "none", **changed}
                self.assertEqual({key: resumed[key] for key in expected}, expected, resumed)
                self.assertEqual((resumed["chain_ok"], resumed["withheld_visible"]), (True, 0))
                failed = [item.split(" ")[0] for item in lv.check_expectations(
                    spec, 0, {"case_resume": resumed}) if item.startswith("case_resume.")]
                self.assertEqual(failed, [f"case_resume.{key}" for key in changed])

    def test_a_case_coworker_outside_its_group_is_refused_at_start(self):
        """``case_coworker`` fails closed: a co-worker whose principals do not hold the
        group, a wildcard, a non-staff co-worker or the host itself never opens a case."""
        refused = {"not_a_member": {**CASE_COWORKER, "member_of": ["group:other"]},
                   "wildcard_group": {**CASE_COWORKER, "group": "*", "member_of": ["*"]},
                   "service_coworker": {**CASE_COWORKER, "principal": SVC},
                   "host_is_the_group": {**CASE_COWORKER, "group": SVC, "member_of": [SVC]},
                   "withheld_coworker": {**CASE_COWORKER,
                                         "principal": host_module.WITHHELD_PRINCIPAL},
                   "unknown_key": {**CASE_COWORKER, "clearance": "restricted"},
                   "not_an_object": [GROUP, COWORKER]}
        for name, value in refused.items():
            with self.subTest(name):
                with self.assertRaises(host_module.HostError) as caught:
                    host_module.HostConfig.from_file(self.write_config(case_coworker=value))
                self.assertEqual(caught.exception.code, "host_config_invalid:case_coworker")
        self.assertIsNone(host_module.HostConfig.from_file(self.write_config()).case_coworker)

    def test_an_interrupt_that_kills_no_running_cycle_fails_the_check(self):
        spec = json.loads((ROOT / "devloop/live-checks/host.cycles.json").read_text())
        for child, after, state in (
                ((sys.executable, "-I", "-c", "pass"), "60", "finished_before_kill"),
                ((sys.executable, "-I", "-c", "import time; time.sleep(60)"), "1",
                 "killed_before_cycle")):
            code, out, err = self.main("check", "--state-dir", str(self.root / state),
                                       "--config", str(self.config), "--sources", "mail",
                                       "--interrupt-after-seconds", after, child=child)
            document = json.loads(out)
            self.assertEqual(document["interrupt"]["state"], state, err)
            self.assertFalse(document["interrupt"]["killed_running"])
            self.assertEqual(document["checks"]["cycles_interrupted"], 0)
            self.assertFalse(document["checks"]["resumed_after_interrupt"])
            failures = " ".join(map(str, lv.check_expectations(spec, code, document)))
            for field in ("interrupt.state", "interrupt.killed_running",
                          "checks.cycles_interrupted", "checks.resumed_after_interrupt"):
                self.assertIn(field, failures)
        code, _out, err = self.main("check", "--state-dir", str(self.root / "bad"),
                                    "--config", str(self.config),
                                    "--interrupt-after-seconds", "0")
        self.assertEqual((code, json.loads(err)), (2, {"error": "host_check_interrupt_invalid"}))

    def test_a_killed_or_failed_cycle_child_leaves_no_ocr_grandchild(self):
        """The killed ``run`` child takes the OCR children it
        started with it, and a wait that fails other than by the timeout still kills
        and reaps the child and its OCR children before the error propagates."""
        pidfile = self.root / "ocr.pid"
        # A run child that starts a long OCR child (it records its pid) and hangs.
        child = (sys.executable, "-I", "-c", "import subprocess, sys, time; "
                 "ocr = subprocess.Popen([sys.executable, '-I', '-c', "
                 "'import time; time.sleep(60)']); "
                 f"open({str(pidfile)!r}, 'w').write(str(ocr.pid)); time.sleep(60)")
        code, out, err = self.main("check", "--state-dir", str(self.root / "killed"),
                                   "--config", str(self.config), "--sources", "mail",
                                   "--interrupt-after-seconds", "2", child=child)
        self.assertEqual(json.loads(out)["interrupt"]["state"], "killed_before_cycle", err)
        self.assertTrue(process_gone(wait_for_pid(pidfile)))
        pidfile.unlink()
        real_wait, started = subprocess.Popen.wait, []

        def failing_wait(popen, timeout=None):
            if timeout is not None and list(popen.args[:len(child)]) == list(child):
                started.append(popen.pid)
                wait_for_pid(pidfile)
                raise RuntimeError("wait failed")
            return real_wait(popen, timeout)

        with patch.object(subprocess.Popen, "wait", failing_wait), \
                self.assertRaisesRegex(RuntimeError, "wait failed"):
            self.main("check", "--state-dir", str(self.root / "failed"), "--config",
                      str(self.config), "--sources", "mail", "--interrupt-after-seconds",
                      "30", child=child)
        self.assertTrue(process_gone(started[0]))
        self.assertTrue(process_gone(wait_for_pid(pidfile)))

    def test_the_probe_check_refuses_a_real_state_directory(self):
        """The probe case it opens would stay where employees and agents read: a state
        directory a host already wrote is refused before anything is written to it."""
        code, _out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                    str(self.config), "--sources", "mail")
        self.assertEqual(code, 0, err)
        before = sorted((p, p.stat().st_mtime_ns) for p in self.state.rglob("*"))
        code, out, err = self.main("check", "--state-dir", str(self.state), "--config",
                                   str(self.config), "--sources", "mail",
                                   "--interrupt-after-seconds", "5", child=HANGING_RUN)
        self.assertEqual((code, out, json.loads(err)),
                         (2, "", {"error": "host_check_probe_on_real_state"}))
        self.assertEqual(sorted((p, p.stat().st_mtime_ns) for p in self.state.rglob("*")),
                         before)
        self.assertFalse(host_module.probe_state(self.state))

    def test_a_fixture_state_directory_and_a_second_cycle_are_refused(self):
        si.bind_state_dir(self.state, fixture=True)
        code, _out, err = self.main("run", "--state-dir", str(self.state),
                                    "--config", str(self.config))
        self.assertEqual(code, 2)
        self.assertIn("live_session_on_fixture_state", err)
        other = self.root / "live"
        held = host_module.Host(other, host_module.HostConfig.from_file(self.config))
        with held._cycle_lock():
            code, _out, err = self.main("run", "--state-dir", str(other),
                                        "--config", str(self.config))
        self.assertEqual(code, 2)
        self.assertIn("host_cycle_running", err)


class OneWriterTests(HostCase):
    """The host's ingest mode and its own readers over one state directory: one cycle
    lock, one ledger, one projection, one check."""

    def test_ingest_and_host_cycles_exclude_each_other(self):
        import threading
        import uuid

        from integrations.people_exclusion import ENV_IDS
        from operating import ingest_forever as forever
        from operating.tests.test_ingest_forever import FAKE, fake_table

        script = self.root / "fake_capture.py"
        script.write_text(FAKE, encoding="utf-8")
        table = fake_table(script, "slow")
        ingest = self.write("ingest.json", {
            "schema": host_module.CONFIG_SCHEMA, "capture_principal": SVC,
            "mirror_audience": "mirror-audience.json",
            "ingest": {"mode": forever.MODE, "grace_seconds": 1},
            "slow": {"mode": "slow"}})
        env = {**ENV, ENV_IDS: str(uuid.uuid4())}
        self.addCleanup(lambda: (self.state / "release").touch())

        def ingest_host():
            return host_module.Host(self.state, host_module.HostConfig.from_file(
                ingest, capture_sources=table), environ=env, clock=self.clock)

        # An ingest cycle holds the lock: host serve waits with a typed report, run refuses.
        results = []
        runner = threading.Thread(target=lambda: results.append(ingest_host().cycle()))
        runner.start()
        self.addCleanup(runner.join, 60)
        started = self.state / "fake-slow" / "started"
        for _ in range(600):
            if started.exists():
                break
            runner.join(0.05)
        self.assertTrue(started.exists())
        code, text, err = self.main("serve", "--no-console", "--max-cycles", "1",
                                    "--state-dir", str(self.state), "--config",
                                    str(self.config))
        self.assertEqual(code, 0, err)
        waiting = json.loads(text)
        self.assertEqual({k: waiting[k] for k in ("schema", "state", "reason", "cycle")},
                         {"schema": host_module.CYCLE_SCHEMA, "state": "waiting",
                          "reason": "host_cycle_running", "cycle": None})
        code, text, err = self.main("run", "--state-dir", str(self.state), "--config",
                                    str(self.config))
        self.assertEqual((code, text, json.loads(err)), (2, "", {"error": "host_cycle_running"}))
        (self.state / "release").touch()
        runner.join(60)
        self.assertEqual([(r["cycle"], r["runner"], r["state"]) for r in results],
                         [(1, "ingest", "complete")])
        (self.state / "release").unlink()
        # A host cycle holds the lock (its Graph reads held): ingest serve waits, ingest
        # run refuses, and no capture CLI starts.
        entered, release = threading.Event(), threading.Event()

        def held(*args):
            entered.set()
            release.wait(60)
            return self.router(*args)

        host_results = []
        reader = threading.Thread(target=lambda: host_results.append(host_module.Host(
            self.state, host_module.HostConfig.from_file(self.config), environ=ENV,
            sender=held, workbench_fetch=self.portal, clock=self.clock,
            sleep=lambda s: None,
            portal_options={"clock": self.clock, "retry_delay_seconds": 0}).cycle()))
        reader.start()
        self.addCleanup(reader.join, 60)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(60))
        code, text, err = self.main("run", "--state-dir", str(self.state), "--config",
                                    str(ingest), environ=env, capture_sources=table)
        self.assertEqual((code, text, json.loads(err)), (2, "", {"error": "host_cycle_running"}))
        with patch.object(forever, "ephemeral", return_value=False):
            code, text, err = self.main("serve", "--no-console", "--max-cycles", "1",
                                        "--state-dir", str(self.state), "--config",
                                        str(ingest), environ=env, capture_sources=table)
        self.assertEqual(code, 0, err)
        self.assertEqual((json.loads(text)["state"], json.loads(text)["reason"]),
                         ("waiting", "host_cycle_running"))
        self.assertEqual((self.state / "fake-slow" / "ran").read_text(), "1\n")
        release.set()
        reader.join(60)
        self.assertEqual([(r["cycle"], r["runner"], r["state"]) for r in host_results],
                         [(2, "host", "complete")])
        # One ledger: the host's status and projection carry the ingest source beside its
        # own, and the host's check over this directory counts both runners, none
        # overlapping another.
        status = self.status()
        self.assertIn("slow", status)
        self.assertEqual(status["_"]["projection"]["generation"], 2)
        self.assertEqual(host_module.cycle_overlaps(host_module.Host(self.state)),
                         {"cycles_recorded": 2, "cycles_overlapping": 0,
                          "cycles_by_runner": {"host": 1, "ingest": 1}})
        code, text, err = self.main("check", "--state-dir", str(self.state), "--config",
                                    str(self.config))
        self.assertEqual(code, 0, err)
        checks = json.loads(text)["checks"]
        self.assertEqual((checks["cycles_overlapping"], checks["cycles_by_runner"]),
                         (0, {"host": 3, "ingest": 1}))
        self.assertEqual(checks["mail_folders_restarted"], 0)


class QcqaLibraryTests(HostCase):
    """The QCQA library as a served, incremental capture (``capture.qcqa_library``): the
    host config names a library-root scope, later cycles read only new and changed files,
    and ``sharepoint_coverage`` lists every folder under the root against the store."""

    def setUp(self) -> None:
        super().setUp()
        drive = DeltaDrive()   # nested folders under the library root, with a change feed
        drive.folder("01QUALITY", "Quality")
        drive.folder("01AOS", "AOs", parent="01QUALITY")
        drive.folder("01AOS2026", "2026 - AOs", parent="01AOS")
        drive.folder("01POLICY", "Policies", parent="01QUALITY")
        drive.file("01TOP", "index.pdf", sp_t.ROOT_ID)
        drive.file("01AO1", "AO-1.pdf", "01AOS2026")
        drive.file("01AO2", "AO-2.pdf", "01AOS2026", b"%PDF-1.7 second")
        drive.file("01RISK", "risk.txt", "01POLICY", b"risk review", mime="text/plain")
        self.router.drive = self.drive = drive
        self.write("library-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 8,
                                                "max_folders": 5000}]})
        self.config = self.write_config(sharepoint={"scope": "library-scope.json",
                                                    "budget_seconds": 600})

    def check(self, *extra):
        code, out, err = self.main("check", "--state-dir", str(self.state), "--config",
                                   str(self.config), "--sources", "sharepoint", *extra)
        return code, json.loads(out) if out else None, err

    def test_a_file_added_between_cycles_is_the_only_one_read_and_coverage_is_whole(self):
        shipped = CaptureScope.from_file(
            ROOT / "examples/sharepoint_capture_scope.qcqa_library.json")
        self.assertEqual([t.folders for t in shipped.targets], [("/",)])   # the listing's
        spec = json.loads((ROOT / "devloop/live-checks/capture.qcqa_library.json").read_text())
        lv.validate_spec(spec, stem="capture.qcqa_library")
        # Three cycles, each bounded by the shipped config's capture budget, and the
        # coverage census, by its own, leave the timeout room for the projections and the
        # trim: a verdict.
        shipped_config = host_module.HostConfig.from_file(
            ROOT / spec["command"][spec["command"].index("--config") + 1])
        shipped = shipped_config.sources["sharepoint"]
        self.assertEqual(Path(shipped["scope"]).resolve(),
                         (ROOT / "examples/sharepoint_capture_scope.qcqa_library.json").resolve())
        self.assertLessEqual(3 * shipped["budget_seconds"] + shipped["census_budget_seconds"],
                             spec["timeout_seconds"] - 600)
        code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                   str(self.config), "--sources", "sharepoint")
        first = json.loads(out)
        self.assertEqual({n: s["state"] for n, s in first["sources"].items()},
                         {"portal": "not_configured", "bc": "not_configured",
                          "mail": "not_configured", "sharepoint": "captured",
                          "teams": "not_configured"}, err)
        self.assertEqual(first["sources"]["sharepoint"]["rounds"], {"full": 1})
        self.assertEqual(first["sources"]["sharepoint"]["counts"]["versions_new"], 4)
        downloads = self.drive.downloads()

        self.drive.file("01AO3", "AO-3.pdf", "01AOS2026", b"%PDF-1.7 third")
        mark = self.router.mark()
        code, document, err = self.check("--cycles", "3")
        sharepoint = [c["sources"]["sharepoint"] for c in document["cycles"]]
        # Only the new file is read: every cycle follows the change feed, never re-walks.
        self.assertEqual([s["rounds"] for s in sharepoint], [{"incremental": 1}] * 3)
        self.assertEqual([(s["counts"]["files_seen"], s["counts"]["versions_new"])
                          for s in sharepoint], [(1, 1), (0, 0), (0, 0)])
        self.assertEqual(self.drive.downloads(), downloads + 1)
        coverage = document["sharepoint_coverage"]
        listings = [u for _m, u in self.router.since(mark) if "/children" in u]
        self.assertEqual(len(listings), coverage["folders_listed"])   # the listing's only
        self.assertEqual((coverage["roots"], coverage["folders_listed"],
                          coverage["files_seen"], coverage["files_current"],
                          coverage["complete"]), (1, 5, 5, 5, True), coverage)
        self.assertEqual(lv.check_expectations(spec, code, document), [], (err, document))
        self.assertEqual(lv.completeness(spec, document), {"complete": True})

        # A file the change feed never reports and one deleted without a feed entry: the
        # listing finds both, so the check fails rather than serving a whole root.
        sp_t.FakeDrive.file(self.drive, "01LOST", "AO-4.pdf", "01AOS")
        sp_t.FakeDrive.delete(self.drive, "01RISK")
        code, document, err = self.check("--cycles", "3")
        coverage = document["sharepoint_coverage"]
        self.assertEqual((coverage["files_seen"], coverage["files_current"],
                          coverage["files_missed"], coverage["files_current_not_seen"],
                          coverage["complete"]), (5, 5, 1, 1, False), coverage)
        failures = lv.check_expectations(spec, code, document)
        self.assertEqual(sorted(f.split()[0] for f in failures),
                         ["sharepoint_coverage.files_current_not_seen",
                          "sharepoint_coverage.files_missed"], failures)
        # A source the config does not name, or a budget that is not a positive number of
        # seconds, is refused, never silently skipped, run unbounded or never run.
        code, document, err = self.check("--sources", "sharepoint,nope")
        self.assertEqual((code, document), (2, None))
        self.assertIn("host_sources_invalid", err)
        # 0 would never read a file; a census without a finite bound would never end.
        for budget in ({"budget_seconds": "600"}, {"budget_seconds": 0},
                       {"census_budget_seconds": None}, {"census_budget_seconds": 0},
                       {"census_budget_seconds": float("inf")}):
            self.config = self.write_config(sharepoint={"scope": "library-scope.json",
                                                        **budget})
            code, document, err = self.check()
            self.assertEqual((code, document), (2, None))
            self.assertIn("host_config_invalid:sharepoint", err)

    def test_a_walk_cut_by_the_budget_resumes_in_the_next_cycle_never_restarts(self):
        # A library larger than one cycle's budget (QA-01 live: ~63 files per 1,200 s
        # against 32,644): each cycle resumes the stored walk position, never starts over.
        for n in range(8):
            self.drive.folder(f"01BULK{n}", f"Bulk {n}", parent="01QUALITY")
            for m in range(3):
                self.drive.file(f"01BULK{n}F{m}", f"bulk-{n}-{m}.txt", f"01BULK{n}",
                                b"bulk %d %d" % (n, m), mime="text/plain")
        self.config = self.write_config(sharepoint={"scope": "library-scope.json",
                                                    "budget_seconds": 40})
        config = host_module.HostConfig.from_file(self.config).only(["sharepoint"])
        ticks = itertools.count()   # one second per budget check

        def make_host():
            return host_module.Host(self.state, config, environ=ENV, sender=self.router,
                                    clock=self.clock, monotonic=lambda: next(ticks))

        document = host_module.cycles_check(make_host, cycles=4)
        sharepoint = [c["sources"]["sharepoint"] for c in document["cycles"]]
        resumed = [s["resumed_from"] for s in sharepoint]
        self.assertEqual(resumed[0], {"new_walk": 1}, sharepoint)
        self.assertEqual(sharepoint[0]["walk_positions_held"], 1, sharepoint)
        # Every later cycle until the walk finishes resumes it; none starts a new walk.
        finished = next(i for i, s in enumerate(sharepoint) if s["complete"])
        self.assertGreater(finished, 1, sharepoint)
        self.assertEqual(resumed[1:finished + 1], [{"walk_position": 1}] * finished,
                         sharepoint)
        self.assertEqual(document["checks"]["sharepoint_roots_restarted"], 0)
        # Every file is read exactly once across the cycles: progress is kept, not redone.
        self.assertEqual(sum(s["counts"]["versions_new"] for s in sharepoint), 4 + 24)
        self.assertEqual(self.drive.downloads(), 4 + 24)

        # A cycle that drops the held walk position and starts over is counted.
        later = {"sources": {"sharepoint": {"resumed_from": {"new_walk": 1}}}}
        earlier = {"sources": {"sharepoint": {"walk_positions_held": 1}}}
        self.assertEqual(host_module._sharepoint_restarted(earlier, later), 1)
        self.assertEqual(host_module._sharepoint_restarted({"sources": {}}, later), 0)

    def test_a_census_cut_by_a_scope_bound_fails_the_check_never_blocked(self):
        # QA-01 live 2026-10-21: a library over max_folders. The bound is the repo's own
        # scope (agent-fixable), not an upstream value: the check fails on census_complete
        # and the document carries no blocked part to hide it behind.
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE, "folders": ["/"],
                         "max_depth": 8, "max_folders": 2}]})
        config = host_module.HostConfig.from_file(self.write_config(
            sharepoint={"scope": "bound-scope.json", "budget_seconds": 600})).only(["sharepoint"])
        document = host_module.cycles_check(lambda: host_module.Host(
            self.state, config, environ=ENV, sender=self.router, clock=self.clock), cycles=2)
        coverage = document["sharepoint_coverage"]
        self.assertEqual((coverage["census_complete"], coverage["reasons"]),
                         (False, ["folder_bound"]))
        self.assertEqual(lv.blocked_parts(document), [])
        spec = json.loads((ROOT / "devloop/live-checks/capture.qcqa_library.json").read_text())
        failures = lv.check_expectations(spec, 0, document)
        self.assertTrue([f for f in failures if "census_complete" in f], failures)
        from devloop.tests.spec_verdict import verdict
        self.assertEqual(verdict(spec, 0, document)[0], "fail")

    def test_a_census_claims_a_file_gone_only_where_it_listed_the_folder_whole(self):
        # A library of more files than one capture budget lists (QA-01: the census the
        # capture's 400 s cut reported 12,220 files): the census's own budget bounds it.
        self.drive.folder("01BULK", "Bulk")
        for n in range(60):
            self.drive.file(f"01BULK{n}", f"bulk-{n}.txt", "01BULK", b"bulk %d" % n,
                            mime="text/plain")
        code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                   str(self.config), "--sources", "sharepoint")
        self.assertEqual(json.loads(out)["sources"]["sharepoint"]["counts"]["versions_new"],
                         64, err)
        keys = ("census_complete", "complete", "reasons", "roots_listed", "folders_listed",
                "files_seen", "files_current", "files_not_captured", "files_current_not_seen")

        def census(ticks=None, **sharepoint) -> dict:
            config = self.write_config(sharepoint={"scope": "library-scope.json",
                                                   **sharepoint})
            host = host_module.Host(self.state, host_module.HostConfig.from_file(config),
                                    environ=ENV, sender=self.router, clock=self.clock)
            ticks = iter(range(1000) if ticks is None else ticks)
            coverage = host_module.sharepoint_coverage(host, monotonic=lambda: next(ticks))
            return {key: coverage[key] for key in keys}

        # Six folders take seven steps (one second each): a 2 s capture budget no longer
        # cuts the census.
        self.assertEqual(census(budget_seconds=2, census_budget_seconds=60),
                         {"census_complete": True, "complete": True, "reasons": [],
                          "roots_listed": 1, "folders_listed": 6, "files_seen": 64,
                          "files_current": 64, "files_not_captured": 0,
                          "files_current_not_seen": 0})
        # The census's budget spent after the root: complete false with the one file it
        # listed, and the 63 in the folders it never reached are not claimed gone (no
        # earlier census's verdict to keep: QcqaCensusVerdictTests).
        spent = [0, 0] + [host_module.CENSUS_BUDGET_SECONDS] * 10
        (self.state / host_module.HOST_DIR / host_module.CENSUS_FILE).unlink()
        self.assertEqual(census(spent),
                         {"census_complete": False, "complete": False,
                          "reasons": ["time_budget"], "roots_listed": 0, "folders_listed": 1,
                          "files_seen": 1, "files_current": 64, "files_not_captured": 0,
                          "files_current_not_seen": 0})
        # A root with a permanent gap (folders below max_depth 1 are never listed): a file
        # gone from a folder it listed whole (the root, Bulk) is reported, one gone from a
        # folder it never reached is not.
        self.write("library-depth1.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE, "folders": ["/"],
                         "max_depth": 1, "max_folders": 5000}]})
        for item_id in ("01TOP", "01BULK0", "01AO1"):
            sp_t.FakeDrive.delete(self.drive, item_id)
        self.assertEqual(census(scope="library-depth1.json"),
                         {"census_complete": False, "complete": False,
                          "reasons": ["depth_bound"], "roots_listed": 0, "folders_listed": 3,
                          "files_seen": 59, "files_current": 64, "files_not_captured": 0,
                          "files_current_not_seen": 2})


    def test_a_census_lists_a_folder_larger_than_one_listing_whole(self):
        # gap-375b698206de: the census listed a folder only up to LIST_MAX_ITEMS (5000)
        # and reported listing_truncated on every run. A folder of 12,000 children is
        # listed in three listings (5,000 + 5,000 + 2,000), each resuming at the page the
        # one before it stopped: every page read once, every file seen once, no gap.
        self.drive.folder("01BIG", "Big")
        for n in range(12_000):
            self.drive.file(f"01BIG{n:05d}", f"b{n}.txt", "01BIG", b"x", mime="text/plain")
        host = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ=ENV, sender=self.router, clock=self.clock)
        ticks = itertools.count()
        coverage = host_module.sharepoint_coverage(host, monotonic=lambda: next(ticks))
        self.assertEqual({key: coverage[key] for key in (
            "census_complete", "reasons", "folders_listed", "files_seen",
            "files_not_captured")},
                         {"census_complete": True, "reasons": [], "folders_listed": 6,
                          "files_seen": 12_004, "files_not_captured": 12_004})
        pages = [u for _m, u in self.drive.calls
                 if urlsplit(u).path.endswith("/items/01BIG/children")]
        self.assertEqual((len(pages), len(set(pages))), (60, 60))


    def census_library(self, folders: int, files: int = 3) -> int:
        """``folders`` more folders under the root, ``files`` files in each; the number
        of files the library then holds."""
        for n in range(folders):
            self.drive.folder(f"01CF{n:03d}", f"Census {n}", parent="01QUALITY")
            for m in range(files):
                self.drive.file(f"01CF{n:03d}F{m}", f"c-{n}-{m}.txt", f"01CF{n:03d}",
                                b"census %d %d" % (n, m), mime="text/plain")
        return 4 + folders * files

    def capture_all(self) -> None:
        code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                   str(self.config), "--sources", "sharepoint")
        self.assertEqual(code, 0, err)

    def census_host(self, **sharepoint):
        config = self.write_config(sharepoint={"scope": sharepoint.pop("scope",
                                                                       "library-scope.json"),
                                               **sharepoint})
        return host_module.Host(self.state, host_module.HostConfig.from_file(config),
                                environ=ENV, sender=self.router, clock=self.clock)

    def listings(self, since: int = 0) -> list[str]:
        return [unquote(u) for _m, u in self.drive.calls[since:] if "/children" in u]

    def test_a_census_cut_by_the_folder_bound_resumes_and_lists_each_folder_once(self):
        total = self.census_library(10)
        self.capture_all()
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 8,
                                                "max_folders": 4}]})
        mark = len(self.drive.calls)
        runs = []
        for _call in range(10):
            coverage = host_module.sharepoint_coverage(
                self.census_host(scope="bound-scope.json", census_budget_seconds=60))
            runs.append(coverage)
            if coverage["census_complete"]:
                break
        # 15 folders at 4 a call: four calls; every cut one names its bound and carries.
        self.assertEqual([r["reasons"] for r in runs], [["folder_bound"]] * 3 + [[]], runs)
        self.assertEqual([r["folders_listed"] for r in runs], [4, 8, 12, 15])
        self.assertEqual([r["census"]["carried"] for r in runs], [False, True, True, True])
        self.assertEqual([r["census"]["calls"] for r in runs], [1, 2, 3, 4])
        self.assertEqual([r["census"]["folders_listed_before"] for r in runs], [0, 4, 8, 12])
        final = runs[-1]
        self.assertEqual((final["files_seen"], final["files_current"], final["complete"],
                          final["census"]["folders_pending"]), (total, total, True, 0))
        # No folder listed twice and none skipped: 15 listings, 15 distinct folders.
        firsts = [u for u in self.listings(mark) if "skiptoken" not in u]
        self.assertEqual((len(firsts), len(set(firsts))), (15, 15))
        # A finished census is consumed: the next call lists afresh from the root, and
        # while that listing is cut by its own bound the finished census's verdict is
        # kept inside its age window, with the cut named.
        again = host_module.sharepoint_coverage(
            self.census_host(scope="bound-scope.json", census_budget_seconds=60))
        self.assertEqual((again["census"]["carried"], again["census"]["calls"],
                          again["reasons"], again["census"]["verdict"],
                          again["census"]["refresh_reasons"], again["folders_listed"]),
                         (False, 1, [], "ended_kept", ["folder_bound"], 4))
        # Past the window the verdict is gone: the cut fails census_complete again.
        self.clock.advance(25 * 3600)
        later = host_module.sharepoint_coverage(
            self.census_host(scope="bound-scope.json", census_budget_seconds=60))
        self.assertEqual((later["census_complete"], later["reasons"],
                          later["census"]["verdict"]), (False, ["folder_bound"], "this_census"))

    def test_a_census_cut_by_its_time_budget_resumes_inside_the_folder_it_was_in(self):
        # Pages of 5 children: a folder of 12 files is three listings; the budget (three
        # listings a call) cuts inside folders, and the next call carries on at the page
        # it stopped at, never from the folder's start.
        self.census_library(6, files=12)
        self.capture_all()
        mark = len(self.drive.calls)
        runs = []
        with patch.object(cs, "LIST_PAGE_SIZE", 5):
            for _call in range(40):
                start = len(self.listings())
                coverage = host_module.sharepoint_coverage(
                    self.census_host(census_budget_seconds=3),
                    monotonic=lambda: float(len(self.listings()) - start))
                runs.append(coverage)
                if coverage["census_complete"]:
                    break
            end = len(self.drive.calls)
            # The same library listed in one go (the finished census was consumed).
            whole = host_module.sharepoint_coverage(self.census_host(
                census_budget_seconds=10_000), monotonic=lambda: 0.0)
        self.assertGreater(len(runs), 3)
        self.assertTrue(all(r["reasons"] == ["time_budget"] for r in runs[:-1]), runs)
        self.assertTrue(runs[-1]["census_complete"], runs[-1])
        keys = ("folders_listed", "files_seen", "files_current", "files_not_captured",
                "files_current_not_seen", "complete")
        self.assertEqual({k: runs[-1][k] for k in keys}, {k: whole[k] for k in keys})
        self.assertEqual(runs[-1]["files_seen"], 4 + 72)
        rising = [r["folders_listed"] for r in runs]
        self.assertEqual(rising, sorted(rising))
        # Every listing page was read once across the carried calls, and the carried
        # census read exactly the pages the uninterrupted one did.
        carried = [unquote(u) for _m, u in self.drive.calls[mark:end] if "/children" in u]
        self.assertEqual(len(carried), len(set(carried)), carried)
        uninterrupted = [unquote(u) for _m, u in self.drive.calls[end:] if "/children" in u]
        self.assertEqual(sorted(carried), sorted(uninterrupted))

    def test_a_stale_foreign_or_unreadable_census_restarts_and_never_resumes(self):
        self.census_library(6)
        self.capture_all()
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 8,
                                                "max_folders": 3}]})

        def call(scope="bound-scope.json"):
            return host_module.sharepoint_coverage(
                self.census_host(scope=scope, census_budget_seconds=60))

        kept = self.state / host_module.HOST_DIR / host_module.CENSUS_FILE
        first, second = call(), call()
        self.assertEqual((first["census"]["carried"], second["census"]["carried"],
                          second["folders_listed"]), (False, True, 6))
        # Older than the capture's full_walk_hours: the listing starts over.
        self.clock.advance(25 * 3600)
        third = call()
        self.assertEqual((third["census"]["carried"], third["census"]["restarted_stale"],
                          third["folders_listed"], third["census"]["calls"]), (False, True, 3, 1))
        # Another scope's bounds: not the same census.
        self.write("other-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 7,
                                                "max_folders": 3}]})
        other = call("other-scope.json")
        self.assertEqual((other["census"]["carried"], other["folders_listed"]), (False, 3))
        # A kept file that is not a census (or has lost a field) is never trusted.
        for damaged in ("{not json", json.dumps({"schema": "x"}),
                        json.dumps({**json.loads(kept.read_text()), "roots": {"0:/": {}}})):
            call("other-scope.json")
            kept.write_text(damaged, "utf-8")
            fresh = call("other-scope.json")
            self.assertEqual((fresh["census"]["carried"], fresh["folders_listed"]), (False, 3))
        self.assertEqual(kept.stat().st_mode & 0o777, 0o600)

    def bounded_scope(self, max_folders: int, **extra) -> str:
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, **extra,
            "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE, "folders": ["/"],
                         "max_depth": 8, "max_folders": max_folders}]})
        return "bound-scope.json"

    def test_a_census_carries_at_the_checks_weekly_cadence_within_its_own_age_window(self):
        self.census_library(6)
        self.capture_all()
        scope = self.bounded_scope(4, census_max_age_hours=720)
        outs = []
        for _ in range(4):
            cov = host_module.sharepoint_coverage(
                self.census_host(scope=scope, census_budget_seconds=60))
            outs.append((cov["census_complete"], cov["census"]["carried"],
                         cov["census"]["restarted_stale"]))
            self.clock.advance(7 * 24 * 3600)
            if cov["census_complete"]:
                break
        self.assertEqual(outs, [(False, False, False), (False, True, False),
                                (True, True, False)])
        # Without its own window the census keeps the capture's full_walk_hours.
        host_module.sharepoint_coverage(self.census_host(
            scope=self.bounded_scope(4), census_budget_seconds=60))
        self.clock.advance(7 * 24 * 3600)
        again = host_module.sharepoint_coverage(self.census_host(
            scope=self.bounded_scope(4), census_budget_seconds=60))
        self.assertEqual((again["census"]["carried"], again["census"]["restarted_stale"]),
                         (False, True))

    def test_a_capture_stopped_by_its_own_folder_bound_is_named_never_a_pass(self):
        self.census_library(6)          # 11 folders; the capture's walk bound is 8
        scope = self.bounded_scope(8)
        config = self.write_config(sharepoint={"scope": scope})
        code, _out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                    str(config), "--sources", "sharepoint")
        self.assertEqual(code, 0, err)
        cov = {}
        for _ in range(4):
            cov = host_module.sharepoint_coverage(
                self.census_host(scope=scope, census_budget_seconds=60))
            if cov["census_complete"]:
                break
        self.assertEqual((cov["census_complete"], cov["complete"], cov["folders_listed"],
                          cov["capture_incomplete"]), (True, False, 11,
                                                       ["capture_folder_bound"]))
        self.assertGreater(cov["files_not_captured"], 0)

    # ------------- one rule for a budget cut

    def census_cut_check(self, census_listings: int = 3) -> dict:
        """``check`` of three cycles over this test's state, its coverage census cut by
        its own time budget after ``census_listings`` folder listings (a fake clock: one
        second a listing), the capture itself complete."""
        self.census_library(10)
        config = host_module.HostConfig.from_file(self.write_config(sharepoint={
            "scope": "library-scope.json", "budget_seconds": 600,
            "census_budget_seconds": census_listings})).only(["sharepoint"])
        coverage = host_module.sharepoint_coverage
        start = [0]

        def census(host, **_kw):
            start[0] = len(self.listings())
            return coverage(host, monotonic=lambda: float(len(self.listings()) - start[0]))

        with patch.object(host_module, "sharepoint_coverage", census):
            return host_module.cycles_check(lambda: host_module.Host(
                self.state, config, environ=ENV, sender=self.router, clock=self.clock),
                cycles=3)

    def test_cold_census_cut_is_blocked_later_ask(self):
        from devloop.tests.spec_verdict import verdict
        spec = json.loads((ROOT / "devloop/live-checks/capture.qcqa_library.json").read_text())
        lv.validate_spec(spec, stem="capture.qcqa_library")
        self.assertFalse(cs.store_exists(self.state))
        cold = self.census_cut_check()
        coverage = cold["sharepoint_coverage"]
        self.assertEqual((coverage["census_complete"], coverage["reasons"],
                          coverage["folders_listed"], coverage["census"]["carried"]),
                         (False, ["time_budget"], 3, False), coverage)
        # The capture is whole; only the census, cut on a state no run left, falls short:
        # blocked on the human step that keeps the state, its progress shown.
        self.assertEqual(cold["blocked_upstream"], [
            {"part": "sharepoint.census", "source": "microsoft-graph", "cause": "later_ask",
             "later_ask": "live-host-persistent-state-dir"}])
        self.assertEqual(verdict(spec, 0, cold), ("blocked", []))
        # A leak beside the cut is never hidden by it.
        leaked = json.loads(json.dumps(cold))
        leaked["trim"]["withheld_leaks"] = 1
        self.assertEqual(verdict(spec, 0, leaked)[0], "fail")
        # The same cut on the next run, which found the census and the store kept: the
        # cut resumes by itself, so it is a typed fail until it finishes, never blocked.
        carried = self.census_cut_check()
        self.assertTrue(carried["sharepoint_coverage"]["census"]["carried"])
        self.assertGreater(carried["sharepoint_coverage"]["census"]["folders_listed_before"], 0)
        self.assertNotIn("blocked_upstream", carried)
        result, failures = verdict(spec, 0, carried)
        self.assertEqual(result, "fail")
        self.assertTrue([f for f in failures if "census_complete" in f], failures)
        # A cold cut beside a scope bound or a gap is a typed fail too (the one rule).
        self.assertEqual(host_module.coverage_cut_block(
            {"cycles": cold["cycles"], "sharepoint_coverage": {
                **coverage, "reasons": ["folder_bound", "time_budget"]}}, carried=False), [])
        self.assertEqual(host_module.coverage_cut_block(
            {"cycles": cold["cycles"], "sharepoint_coverage": {
                **coverage, "files_missed": 1}}, carried=False), [])
        self.assertEqual(host_module.coverage_cut_block(
            {"cycles": cold["cycles"], "sharepoint_coverage": {
                "census_complete": False, "complete": False, "reasons": ["time_budget"]}},
            carried=False), [])   # coverage not measured: never blocked

    def test_ended_census_verdict_kept_within_window(self):
        # A library larger than one call's census: once a census ended complete, a later
        # call whose fresh census its own bound cuts keeps that verdict inside the age
        # window, and judges what it re-listed; it never keeps it past a change or the
        # window.
        self.census_library(10)
        self.capture_all()
        scope = self.bounded_scope(4, census_max_age_hours=720)

        def call():
            return host_module.sharepoint_coverage(
                self.census_host(scope=scope, census_budget_seconds=60))

        runs = [call() for _ in range(4)]
        self.assertEqual([r["census_complete"] for r in runs], [False] * 3 + [True])
        self.assertEqual([r["census"]["verdict"] for r in runs], ["this_census"] * 4)
        self.clock.advance(7 * 24 * 3600)
        mark = len(self.listings())
        kept = call()
        self.assertEqual((kept["census_complete"], kept["complete"], kept["reasons"],
                          kept["census"]["verdict"], kept["census"]["refresh_reasons"],
                          kept["census"]["carried"], kept["folders_listed"]),
                         (True, True, [], "ended_kept", ["folder_bound"], False, 4))
        self.assertEqual(len(self.listings()) - mark, 4)   # it re-listed, never skipped it
        # A file the change feed never reported, in a folder the fresh census lists on
        # its next call: found, and the kept verdict is dropped (a missed file, the
        # census cut): never a pass on the earlier listing.
        sp_t.FakeDrive.file(self.drive, "01SILENT", "AO-9.pdf", "01CF001")
        self.clock.advance(7 * 24 * 3600)
        changed = call()
        self.assertEqual((changed["census_complete"], changed["census"]["verdict"],
                          changed["reasons"], changed["files_missed"]),
                         (False, "this_census", ["folder_bound"], 1), changed)
        # Once the file reaches the capture (here: the feed reports it), the fresh census
        # ends complete, and its own verdict is kept no longer than the window.
        self.drive._logged(sp_t.DRIVE, "01SILENT")
        self.capture_all()
        for _ in range(4):
            ended = call()
            if ended["census"]["verdict"] == "this_census" and ended["census_complete"]:
                break
        self.assertEqual((ended["census_complete"], ended["complete"],
                          ended["census"]["verdict"]), (True, True, "this_census"), ended)
        self.assertEqual(call()["census"]["verdict"], "ended_kept")
        self.clock.advance(721 * 3600)
        stale = call()
        self.assertEqual((stale["census_complete"], stale["census"]["verdict"],
                          stale["reasons"]), (False, "this_census", ["folder_bound"]))

    def test_an_invalid_census_window_is_refused(self):
        for bad in (0, -1, 721, True, "24", 1.5):
            with self.subTest(bad=bad), self.assertRaises(cs.CaptureError):
                CaptureScope([{"id": "a", "drive_id": "d", "folders": ["/"]}],
                             census_max_age_hours=bad)

    def test_an_exception_in_a_folders_listing_never_skips_the_folder(self):
        from integrations import graph_read
        total = self.census_library(6)
        self.capture_all()
        scope = self.bounded_scope(50)
        orig = graph_read.SharePointReader.list_folder
        for failing in (4, 1):
            with self.subTest(failing=failing):
                kept = self.state / host_module.HOST_DIR / host_module.CENSUS_FILE
                kept.unlink(missing_ok=True)
                calls = {"n": 0}

                def boom(reader, *a, **k):
                    calls["n"] += 1
                    if calls["n"] == failing:
                        raise RuntimeError("interrupted")
                    return orig(reader, *a, **k)
                with patch.object(graph_read.SharePointReader, "list_folder", boom):
                    with self.assertRaises(RuntimeError):
                        host_module.sharepoint_coverage(
                            self.census_host(scope=scope, census_budget_seconds=60))
                cov = host_module.sharepoint_coverage(
                    self.census_host(scope=scope, census_budget_seconds=60))
                self.assertEqual((cov["census_complete"], cov["folders_listed"],
                                  cov["files_seen"], cov["files_current_not_seen"]),
                                 (True, 5 + 6, total, 0))

    def test_a_carried_census_loses_and_repeats_no_folder_at_any_size(self):
        for folders in (1, 10, 60):
            with self.subTest(folders=folders):
                self.setUp()
                total = self.census_library(folders, files=2)
                self.capture_all()
                self.write("bound-scope.json", {
                    "schema": "plx-sharepoint-capture-scope/v1",
                    "max_content_bytes": 1_000_000, "targets": [{
                        "id": "qcqa.library", "drive_id": sp_t.DRIVE, "folders": ["/"],
                        "max_depth": 8, "max_folders": 7}]})
                mark = len(self.drive.calls)
                for _call in range(30):
                    coverage = host_module.sharepoint_coverage(self.census_host(
                        scope="bound-scope.json", census_budget_seconds=60))
                    if coverage["census_complete"]:
                        break
                self.assertEqual((coverage["census_complete"], coverage["folders_listed"],
                                  coverage["files_seen"], coverage["files_current"],
                                  coverage["complete"]),
                                 (True, 5 + folders, total, total, True), coverage)
                firsts = [u for u in self.listings(mark) if "skiptoken" not in u]
                self.assertEqual((len(firsts), len(set(firsts))), (5 + folders,) * 2)

    def test_the_check_keeps_its_state_by_name_and_passes_once_the_census_completes(self):
        self.census_library(8)
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 8,
                                                "max_folders": 8}]})
        self.config = self.write_config(sharepoint={
            "scope": "bound-scope.json", "budget_seconds": 600, "census_budget_seconds": 60})
        spec = json.loads((ROOT / "devloop/live-checks/capture.qcqa_library.json").read_text())
        name = spec["command"][spec["command"].index("--state-name") + 1]
        workbench = self.root / "workbench"
        env = {**ENV, "PLX_WORKBENCH_STATE_DIR": str(workbench)}

        def run():
            code, out, err = self.main("check", "--state-name", name, "--config",
                                       str(self.config), "--sources", "sharepoint",
                                       "--cycles", "2", environ=env)
            return code, json.loads(out or "null"), err

        code, first, err = run()
        self.assertEqual(code, 0, err)
        self.assertTrue((workbench / f"host-check-{name}" / "live" / "host").is_dir())
        coverage = first["sharepoint_coverage"]
        self.assertEqual((coverage["census_complete"], coverage["reasons"],
                          coverage["census"]["carried"]), (False, ["folder_bound"], False))
        failures = lv.check_expectations(spec, 0, first)
        self.assertTrue([f for f in failures if "census_complete" in f], failures)
        code, second, err = run()
        coverage2 = second["sharepoint_coverage"]
        self.assertTrue(coverage2["census"]["carried"], coverage2)
        self.assertGreater(coverage2["folders_listed"], coverage["folders_listed"])
        self.assertTrue(coverage2["census_complete"], coverage2)
        self.assertEqual([f for f in lv.check_expectations(spec, code, second)
                          if "census_complete" in f or "reasons" in f], [])
        # --state-name and --state-dir together, or a name that is no name: refused.
        for argv in (("--state-name", name, "--state-dir", str(self.state)),
                     ("--state-name", "../x")):
            code, out, err = self.main("check", *argv, "--config", str(self.config),
                                       environ=env)
            self.assertEqual((code, out), (2, ""), err)
            self.assertIn("state_name_invalid", err)


class TeamsConfirmationTests(HostCase):
    """The host runs the ``teams-capture`` CLI's gate (decision
    2026-10-21-teams-host-confirmation-gate): census first, then message content only of
    the channels and chats a person confirmed and the census cleared."""

    def teams_paths(self, mark: int = 0) -> list[str]:
        return [unquote(urlsplit(u).path) for _m, u in self.router.since(mark)
                if "/teams" in u or "/chats" in u or "/reports/" in u]

    def test_without_a_confirmation_no_message_is_read_and_teams_is_blocked(self):
        capture_teams.confirmation_path(self.state).unlink()
        first = self.run_cycle()
        teams = first["sources"]["teams"]
        self.assertEqual(teams["state"], "blocked", teams)
        self.assertIn("later_ask:" + capture_teams.CONFIRMATION_LATER_ASK, teams["reasons"])
        self.assertEqual([p["part"] for p in teams["blocked_upstream"]],
                         [capture_teams.CONFIRMATION_PART], teams)
        self.assertEqual(teams["blocked_upstream"][0]["cause"], "later_ask")
        self.assertEqual((teams["counts"]["messages_read"], teams["counts"]["conversations"]),
                         (0, 0), teams)
        self.assertGreaterEqual(teams["counts"]["channels_cleared"], 2, teams)
        paths = self.teams_paths()
        self.assertIn("/v1.0/teams", paths)   # the census ran
        self.assertEqual([p for p in paths if "/messages" in p], [], paths)
        self.assertTrue(capture_teams.census_path(self.state).is_file())
        # The other sources captured: a blocked source does not fail the cycle.
        self.assertEqual(first["sources"]["mail"]["state"], "captured", first)
        # The owner confirms: the next cycle reads the cleared channels.
        self.confirm_teams(self.state)
        self.assertEqual(self.run_cycle()["sources"]["teams"]["state"], "captured")

    def test_a_confirmation_reads_only_the_named_cleared_channels(self):
        self.confirm_teams(self.state, channels=(teams_t.GENERAL,), chats=())
        teams = self.run_cycle()["sources"]["teams"]
        self.assertEqual(teams["state"], "captured", teams)
        messages = [p for p in self.teams_paths() if "/messages" in p]
        self.assertTrue([p for p in messages if teams_t.GENERAL in p], messages)
        self.assertEqual([p for p in messages if teams_t.PRIVATE in p], [], messages)

    def test_a_channel_with_an_excluded_member_is_refused_before_any_request(self):
        excluded = ENV["PLX_EXCLUDED_PEOPLE_OIDS"].split(",")[0]
        self.router.teams.channel_members[teams_t.PRIVATE].append(
            (excluded, "excluded.person@petrasoap.com"))
        teams = self.run_cycle()["sources"]["teams"]   # both channels confirmed
        self.assertEqual(teams["state"], "captured", teams)
        paths = self.teams_paths()
        self.assertNotIn(f"/v1.0/teams/{teams_t.TEAM}/channels/{teams_t.PRIVATE}", paths)
        self.assertEqual([p for p in paths if teams_t.PRIVATE in p and "/messages" in p], [])
        self.assertTrue([p for p in paths if teams_t.GENERAL in p and "/messages" in p])
        census = json.dumps(json.loads(capture_teams.census_path(self.state).read_text()))
        self.assertNotIn(teams_t.PRIVATE, census)


class TrimTests(HostCase):
    """``trim`` reads under the policy the MCP tools are served with."""

    def served_objects(self, principal: str) -> int:
        """Objects ``plx.object`` shows a reader holding ``principal`` on tools built as
        ``mcp_server.main`` builds them over the host's arguments."""
        from integrations.people_exclusion import PeopleExclusion
        from operating.case_runtime import CaseRuntime, build_live_readers
        from operating.case_tools_host import CaptureCaseHostTools

        config = host_module.HostConfig.from_file(self.config)
        actor = Actor("plx", frozenset({principal}), "restricted")
        # The live readers and the people guard read the same environ, as on a host.
        runtime = CaseRuntime(self.state, client=None, audience=config.mirror_audience,
                              tenant="plx", live=build_live_readers(
                                  config.live_audience, ENV, graph_sender=self.router,
                                  bc_sender=self.router),
                              people_exclusion=PeopleExclusion.from_env(ENV))
        tools = CaptureCaseHostTools(runtime, actor,
                                     store_path=host_module.projection_path(self.state),
                                     mail_capture_dir=self.state,
                                     sharepoint_capture_dir=self.state)
        try:
            with sqlite3.connect(host_module.projection_path(self.state)) as conn:
                ids = [r[0] for r in conn.execute("SELECT object_id FROM objects")]
            shown = 0
            for object_id in ids:
                read = tools.call("plx.object", {"object_id": object_id}, actor)
                shown += "error" not in read
            return shown
        finally:
            tools.close()
            runtime.close()

    def trimmed(self, principal: str) -> int:
        host = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ=ENV, sender=self.router, clock=self.clock)
        trim = host.trim_check(principal)
        self.assertEqual(trim["state"], "checked")
        return trim["withheld_principal_objects"]

    def test_trim_shows_each_reader_what_serving_shows_and_fails_on_a_served_leak(self):
        live = self.write("live-audience.json", {
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "default_classification": "restricted",
            "audiences": {f"mail:{mail_t.UPN_A}": [SVC, "staff:operations"],
                          "sharepoint:*": [SVC], "bc:production:*": [SVC]},
            "classifications": {"mail:*": "restricted"}})
        self.config = self.write_config(live_audience=live.name, case_coworker=CASE_COWORKER)
        self.run_cycle()
        full, restricted = self.served_objects(SVC), self.served_objects("staff:operations")
        self.assertGreater(full, restricted)
        self.assertGreater(restricted, 0)
        self.assertEqual((self.trimmed(SVC), self.trimmed("staff:operations")),
                         (full, restricted))
        # Revoked: the Portal relations and the mailbox taken from staff:operations.
        self.write("mirror-audience.json", {
            "schema": "plx-source-mirror-audience/v1", "tenant": "plx",
            "audiences": {"public.*": [SVC]}})
        audience = json.loads(live.read_text())
        audience["audiences"][f"mail:{mail_t.UPN_A}"] = [SVC]
        live.write_text(json.dumps(audience), "utf-8")
        revoked = self.served_objects("staff:operations")
        self.assertLess(revoked, restricted)
        self.assertEqual(self.trimmed("staff:operations"), revoked)
        # The served (live) audience grants the check's withheld principal a mailbox the
        # capture audience does not: the trim sees the leak and the live spec fails.
        audience["audiences"][f"mail:{mail_t.UPN_A}"] = [
            SVC, host_module.WITHHELD_PRINCIPAL]
        live.write_text(json.dumps(audience), "utf-8")
        self.assertGreater(self.served_objects(host_module.WITHHELD_PRINCIPAL), 0)
        spec = json.loads((ROOT / "devloop/live-checks/host.cycles.json").read_text())
        code, out, err = self.main("check", "--state-dir", str(self.root / "leaky"),
                                   "--config", str(self.config),
                                   "--interrupt-after-seconds", "5", child=HANGING_RUN)
        document = json.loads(out)
        self.assertGreater(document["trim"]["withheld_principal_objects"], 0, err)
        self.assertGreater(document["trim"]["withheld_leaks"], 0)
        failures = lv.check_expectations(spec, code, document)
        self.assertTrue(failures and all("trim." in f for f in failures), failures)


class Directory:
    """Graph directory for one employee (users/{oid} and its transitive groups)."""

    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return mail_t.j(200, {"access_token": "eyJ.t.sig", "expires_in": 3599})
        path = unquote(parts.path)
        assert method == "GET", method
        if path.lower() == f"/v1.0/users/{OID_A}":
            return mail_t.j(200, {"id": OID_A, "accountEnabled": self.enabled,
                                  "userPrincipalName": EMAIL_A, "mail": EMAIL_A,
                                  "department": "Operations"})
        if path == f"/v1.0/users/{OID_A}/transitiveMemberOf/microsoft.graph.group":
            return mail_t.j(200, {"value": []})
        return mail_t.j(404, {"error": {"code": "ResourceNotFound"}})

    def reads(self) -> int:
        return sum(1 for _m, u in self.calls if f"/users/{OID_A}" in unquote(u).lower())


class Lines:
    """MCP stdin: one JSON-RPC message per line; ``hooks[i]`` runs before line ``i``."""

    def __init__(self, messages, hooks=None) -> None:
        self.lines = [json.dumps(m).encode() + b"\n" for m in messages]
        self.hooks = hooks or {}
        self.index = 0

    def readline(self, _limit=-1) -> bytes:
        if self.index >= len(self.lines):
            return b""
        hook = self.hooks.get(self.index)
        if hook is not None:
            hook()
        self.index += 1
        return self.lines[self.index - 1]


INIT = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"}]


def call(request_id, name, arguments):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


class FreshnessBoundTests(HostCase):
    """Each captured source's freshness bound comes from
    the host config's capture cadence, and the MCP host is started with it."""

    def test_bounds_follow_the_configured_cadence_in_both_modes(self):
        from operating import ingest_forever as forever
        from operating import mcp_server
        cycle = self.write_config(interval_seconds=900,
                                  portal={"base_url": BASE, "relations": sorted(CATALOG),
                                          "budget_seconds": 500})
        host = host_module.Host(self.state, host_module.HostConfig.from_file(cycle),
                                environ=ENV, sender=self.router, clock=self.clock)
        # 2 x (interval + one capture's budget, else the default timeout).
        self.assertEqual(host_module.freshness_bounds(host.config), {
            "portal_api": 2800.0, "bc": 3000.0, "graph_mail": 3000.0, "sharepoint": 3000.0,
            "teams": 3000.0})
        argv = host_module.mcp_arguments(host, session_ticket_env="T")
        given = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--freshness-bound"]
        self.assertEqual(mcp_server.freshness_bounds(given),
                         host_module.freshness_bounds(host.config))
        ingest = self.write("ingest-bounds.json", {
            "schema": host_module.CONFIG_SCHEMA, "capture_principal": SVC,
            "capture_audience": "capture-audience.json",
            "mirror_audience": "mirror-audience.json",
            "ingest": {"mode": forever.MODE, "grace_seconds": 60},
            "portal": {"relations": sorted(CATALOG), "budget_seconds": 1500,
                       "min_interval_seconds": 300},
            "sharepoint": {"scope": "sharepoint-scope.json", "budget_seconds": 100},
            "sharepoint_lists": {"scope": "sharepoint-scope.json", "timeout_seconds": 900},
            "teams": {"scope": "teams-scope.json"}})
        # 2 x (min_interval (default 120) + budget, timeout or default 600 + grace); the
        # family two captures write takes the larger.
        self.assertEqual(host_module.freshness_bounds(host_module.HostConfig.from_file(
            ingest)), {"portal_api": 3720.0, "sharepoint": 2160.0, "teams": 1560.0})
        for refused in (["portal_api=0"], ["nowhere=60"], ["bc=60", "bc=70"], ["bc=x"],
                        ["bc=nan"], ["bc=1e10"]):
            with self.assertRaises(ValueError, msg=refused):
                mcp_server.freshness_bounds(refused)


class McpStartupTests(HostCase):
    """``host mcp``: the real start-up path resolves the employee through the Entra
    directory and consumes the start-up ticket in the durable replay guard."""

    def setUp(self) -> None:
        super().setUp()
        self.config = self.write_config(entra_directory=True)
        self.run_cycle()
        self.clock.advance(3600)
        self.portal.down = HTTP_500
        self.run_cycle()   # the last generation now serves the Portal from its copy
        self.directory = Directory()
        self.ticks = [0.0]
        self.ticket = Fixture.ticket(author=si.AUTHOR_AGENT)

    def mcp(self, lines, *, ticket=None):
        output = io.BytesIO()
        env = {"T": ticket or self.ticket, si.SESSION_TICKET_SECRET_ENV: SECRET}
        with patch.dict(os.environ, env):
            code, _out, err = self.main(
                "mcp", "--state-dir", str(self.state), "--config", str(self.config),
                "--session-ticket-env", "T", sender=self.directory,
                monotonic=lambda: self.ticks[0], input_stream=lines, output_stream=output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        return code, {r["id"]: r for r in replies if "id" in r}, err

    def test_session_resolves_through_the_directory_and_the_tools_use_it(self):
        def revoke():
            self.directory.enabled = False
            self.ticks[0] += 3600   # past the directory cache

        code, replies, err = self.mcp(Lines(
            INIT + [call(2, "plx.find", {"query": "SO100305", "object_type": "SalesOrder"}),
                    call(3, "plx.case_get", {}), call(4, "plx.case_get", {})],
            hooks={4: revoke}))
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(self.directory.reads(), 1)
        found = replies[2]["result"]
        self.assertFalse(found["isError"], found)
        (fresh,) = [f for f in found["structuredContent"]["meta"]["freshness"]
                    if f["source"] == "portal_api"]
        self.assertEqual((fresh["served_from"], fresh["stale"]), ("persisted_capture", True))
        self.assertIn("live source unavailable (500, internal)", fresh["label"])
        self.assertFalse(replies[3]["result"]["isError"], replies[3])
        # The served tools resolve the employee through the same directory provider.
        refused = replies[4]["result"]
        self.assertTrue(refused["isError"], refused)
        self.assertIn("entra_directory:account_disabled", refused["content"][0]["text"])

    def test_a_configured_portal_never_captured_is_unavailable_never_unconfigured(self):
        """No cycle ever captured the Portal: a Portal-only question names it unavailable
        on a host that configures it, and not configured only on a host that does not."""
        reasons = {}
        for configured in (True, False):
            self.state = self.root / f"never-captured-{configured}"
            self.portal.down_all = HTTP_500
            self.config = self.write_config(entra_directory=True,
                                            **({} if configured else {"portal": None}))
            self.run_cycle()
            code, replies, err = self.mcp(Lines(INIT + [call(2, "plx.find", {
                "query": "SO100305", "object_type": "SalesOrder"})]),
                ticket=Fixture.ticket(author=si.AUTHOR_AGENT))
            self.assertEqual(code, 0, err)
            found = replies[2]["result"]["structuredContent"]
            self.assertNotEqual(found["status"], "found", found)
            reasons[configured] = {(g["state"], g["reason"]) for g in found["gaps"]
                                   if g["subject"] == "portal_api"}
        self.assertIn(("inaccessible", "source_unavailable"), reasons[True])
        self.assertNotIn("source_not_configured", {r for _s, r in reasons[True]})
        self.assertEqual(reasons[False], {("not_ingested", "source_not_configured")})

    def test_the_configured_agent_definition_is_the_one_the_session_runs(self):
        self.config = self.write_config(entra_directory=True, agent="plx.agent.investigate")
        code, replies, err = self.mcp(Lines(
            INIT + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    call(3, "plx.case_get", {}),
                    call(4, "plx.act", {"mode": "status", "case_id": "c"})]))
        self.assertEqual(code, 0, err)
        self.assertNotIn("plx.act", {tool["name"] for tool in replies[2]["result"]["tools"]})
        stamp = replies[3]["result"]["_meta"]["plx/agent"]
        self.assertEqual((stamp["id"], stamp["version"]), ("plx.agent.investigate", 2))
        self.assertEqual(replies[4]["result"]["content"][0]["text"],
                         "tool_not_allowed_for_agent:plx.agent.investigate")
        with self.assertRaises(host_module.HostError):
            host_module.HostConfig({"schema": host_module.CONFIG_SCHEMA,
                                    "mirror_audience": "m.json", "agent": "plx.agent.root"},
                                   self.state)

    def test_plx_act_is_served_only_when_the_config_enables_the_action_path(self):
        status = call(2, "plx.act", {"mode": "status", "intent_id": "intent-unknown"})
        texts = {}
        for enabled in (None, True):
            if enabled:
                self.config = self.write_config(entra_directory=True, action_path=True)
            code, replies, err = self.mcp(Lines(INIT + [status]),
                                          ticket=Fixture.ticket(author=si.AUTHOR_AGENT))
            self.assertEqual(code, 0, err)
            texts[enabled] = replies[2]["result"]["content"][0]["text"]
            said = [json.loads(line)["approver_roster"] for line in err.splitlines()
                    if "approver_roster" in line]
            if not enabled:
                self.assertEqual(said, [])
                continue
            # Host start resolved the roster by UPN; the directory has no such user, so
            # the approver stays unverified and the host says so (counts only).
            self.assertEqual([(a["status"], a["reasons"]) for a in said],
                             [("unverified", {"user_not_found": len(roster_upns())})])
            for upn in roster_upns():
                self.assertNotIn(upn.lower(), err.lower())
        self.assertIn("action_path_not_configured", texts[None])   # default deny
        self.assertNotIn("action_path_not_configured", texts[True])
        for value in ("true", 1, None):
            with self.assertRaises(host_module.HostError):
                host_module.HostConfig({"schema": host_module.CONFIG_SCHEMA,
                                        "mirror_audience": "m.json", "action_path": value},
                                       self.state)

    def test_an_open_session_reads_each_new_projection_generation(self):
        """The host rebuilds the projection while a session is open: the session's next
        query reads the new generation and its labels (Portal back, then down again)."""
        def cycle(down):
            def run():
                self.clock.advance(3600)
                self.portal.down = down
                self.run_cycle()
            return run

        find = {"query": "SO100305", "object_type": "SalesOrder"}
        code, replies, err = self.mcp(Lines(
            INIT + [call(2, "plx.find", find), call(3, "plx.find", find),
                    call(4, "plx.find", find)],
            hooks={3: cycle(None), 4: cycle(HTTP_500)}))
        self.assertEqual(code, 0, err)
        fresh = []
        for request in (2, 3, 4):
            found = replies[request]["result"]
            self.assertFalse(found["isError"], found)
            (portal,) = [f for f in found["structuredContent"]["meta"]["freshness"]
                         if f["source"] == "portal_api"]
            fresh.append(portal)
        self.assertEqual([(f.get("served_from"), f["stale"]) for f in fresh],
                         [("persisted_capture", True), (None, False),
                          ("persisted_capture", True)])
        # The later outage's own label, not the first generation's.
        self.assertNotEqual(fresh[0]["label"], fresh[2]["label"])

    def test_a_replayed_ticket_is_refused_after_a_restart(self):
        code, replies, err = self.mcp(Lines(INIT))
        self.assertEqual(code, 0, err)
        self.assertIn(1, replies)
        code, _replies, err = self.mcp(Lines(INIT))
        self.assertEqual(code, 2)
        self.assertIn("session_ticket_replayed", err)
        # A fresh ticket for the same employee starts.
        code, _replies, err = self.mcp(Lines(INIT), ticket=Fixture.ticket(
            author=si.AUTHOR_AGENT))
        self.assertEqual(code, 0, err)

    def test_a_directory_that_does_not_grant_refuses_the_start(self):
        self.directory.enabled = False
        code, _replies, err = self.mcp(Lines(INIT))
        self.assertEqual(code, 2)
        self.assertIn("entra_directory:", err)
        code, _replies, err = self.mcp(Lines(INIT), ticket=Fixture.ticket(
            author=si.AUTHOR_AGENT))
        self.assertEqual(code, 2)
        # Without Graph credentials the directory is never silently dropped.
        code, _out, err = self.main(
            "mcp", "--state-dir", str(self.state), "--config", str(self.config),
            "--session-ticket-env", "T", environ={}, sender=self.directory)
        self.assertEqual(code, 2)
        self.assertIn("entra_directory_credentials_unavailable", err)
        # The host's live state is never opened by a command-line employee.
        code, _out, err = self.main(
            "mcp", "--state-dir", str(self.state), "--config", str(self.config),
            "--employee-oid", OID_A, sender=self.directory)
        self.assertEqual(code, 2)
        self.assertIn("required: --session-ticket-env", err)


    def seed_brain(self) -> dict[str, set[str]]:
        """A brain capture in the host's state directory (``knowledge_source.capture`` as
        the capture principal over the fake brain), projected by the next cycle: the
        projected object ids per audience unit."""
        from integrations.capture_store import CaptureStore
        from integrations.knowledge_source import NamespaceAuthority, capture, store_path
        from integrations.tests.test_knowledge_source import brain

        authority = NamespaceAuthority({"swarm/rollup/company": [SVC], "swarm/rag/*": [SVC]},
                                       tenant="plx")
        store_path(self.state).parent.mkdir(parents=True, exist_ok=True)
        with CaptureStore(store_path(self.state), authority) as store:
            capture(brain()[0], store, authority, Actor("plx", frozenset({SVC})),
                    ["swarm/rollup/company", "swarm/rag/*"])
        self.assertIn("brain", self.run_cycle()["projection"]["sources"])
        units: dict[str, set[str]] = {}
        with sqlite3.connect(host_module.projection_path(self.state)) as conn:
            for object_id, key in conn.execute("SELECT object_id, audience_key FROM objects "
                                               "WHERE audience_key LIKE 'brain:%'"):
                units.setdefault(key, set()).add(object_id)
        return units

    def test_a_brain_namespace_is_served_only_to_the_readers_the_authority_grants(self):
        """GOV-13: the host grants ``brain:<namespace>`` from its namespace authority, re-read
        per call: a reader sees exactly its namespaces' items, nobody without a grant, and
        nothing when the document is missing, malformed, unknown or names a personal one."""
        units = self.seed_brain()
        self.assertEqual(set(units), {"brain:swarm/rollup/company", "brain:swarm/rag/portal",
                                      "brain:swarm/rag/finance"})   # never a personal one
        namespaces = self.write("namespaces.json", {
            "schema": "plx-knowledge-namespaces/v1", "tenant": "plx", "namespaces": {
                "swarm/rag/portal": ["staff:engineering", "staff:operations"],
                "swarm/rag/*": [SVC], "swarm/rollup/company": [SVC]}})
        self.config = self.write_config(entra_directory=True,
                                        knowledge_namespaces=namespaces.name)
        unset = json.loads(self.config.read_text())
        del unset["knowledge_namespaces"]
        unset = self.write("host-no-brain.json", unset)
        with sqlite3.connect(host_module.projection_path(self.state)) as conn:
            every = [r[0] for r in conn.execute("SELECT object_id FROM objects")]

        def shown(principal: str, config: Path | None = None) -> set[str]:
            host = host_module.Host(self.state, host_module.HostConfig.from_file(
                config or self.config), environ=ENV, sender=self.router, clock=self.clock)
            actor = Actor("plx", frozenset({principal}), "restricted")
            with host.served_tools(actor) as served:
                return {i for i in every
                        if "error" not in served.call("plx.object", {"object_id": i}, actor)}

        # staff:engineering holds no other grant: the one namespace's items, nothing else.
        self.assertEqual(shown("staff:engineering"), units["brain:swarm/rag/portal"])
        self.assertEqual(shown(SVC) & set().union(*units.values()),
                         units["brain:swarm/rollup/company"] | units["brain:swarm/rag/finance"])
        self.assertEqual(shown("staff:nobody"), set())
        # A revocation applies on the next call of a session already open.
        host = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ=ENV, sender=self.router, clock=self.clock)
        actor = Actor("plx", frozenset({"staff:engineering"}), "restricted")
        granted_document = namespaces.read_text()
        revoked = json.loads(granted_document)
        revoked["namespaces"]["swarm/rag/portal"] = ["staff:operations"]
        with host.served_tools(actor) as served:
            def visible() -> set[str]:
                return {i for i in every
                        if "error" not in served.call("plx.object", {"object_id": i}, actor)}
            self.assertEqual(visible(), units["brain:swarm/rag/portal"])
            namespaces.write_text(json.dumps(revoked), "utf-8")
            self.assertEqual(visible(), set())
        namespaces.write_text(granted_document, "utf-8")
        # The served MCP session: the employee finds its namespace's item, not another's.
        query = call(2, "plx.find", {"query": "runbook", "object_type": "KnowledgeItem"})
        code, replies, err = self.mcp(Lines(INIT + [query]))
        self.assertEqual(code, 0, err)
        found = {c["object"]["object_id"] for c in
                 replies[2]["result"]["structuredContent"].get("candidates") or ()}
        self.assertEqual(found, units["brain:swarm/rag/portal"])
        # No authority configured: no brain item for anyone.
        self.assertEqual(shown(SVC, unset) & set().union(*units.values()), set())
        granted, self.config = self.config, unset
        code, replies, err = self.mcp(Lines(INIT + [query]), ticket=Fixture.ticket(
            author=si.AUTHOR_AGENT))
        self.config = granted
        self.assertEqual(code, 0, err)
        self.assertFalse(replies[2]["result"]["structuredContent"].get("candidates"))
        # Fail closed per call: a document that no longer loads (malformed, a personal
        # namespace, another tenant) or names only an unknown namespace grants nothing.
        document = json.loads(namespaces.read_text())
        eng = ["staff:engineering"]
        for broken in ("{not json", *(json.dumps({**document, **change}) for change in (
                {"namespaces": {**document["namespaces"], "swarm/personal-notes": eng}},
                {"namespaces": {"swarm/rag/portal": "staff:engineering"}},
                {"tenant": "other"}, {"namespaces": {"swarm/lessons/*": eng}}))):
            config = self.config
            namespaces.write_text(broken, "utf-8")
            if "lessons" not in broken:   # one that does not load is refused at start too
                with self.assertRaises(host_module.HostError, msg=broken) as refused:
                    host_module.HostConfig.from_file(config)
                self.assertEqual(refused.exception.code,
                                 "host_config_invalid:knowledge_namespaces")
                namespaces.write_text(json.dumps(document), "utf-8")
                host = host_module.Host(self.state, host_module.HostConfig.from_file(config),
                                        environ=ENV, sender=self.router, clock=self.clock)
                namespaces.write_text(broken, "utf-8")   # broken while served
                actor = Actor("plx", frozenset({"staff:engineering"}), "restricted")
                with host.served_tools(actor) as served:
                    self.assertEqual({i for i in every if "error" not in served.call(
                        "plx.object", {"object_id": i}, actor)}, set(), broken)
            else:
                self.assertEqual(shown("staff:engineering"), set())
        self.assertIn("--knowledge-namespaces", host_module.mcp_arguments(
            host, session_ticket_env=None))


class IntentResumeTests(ActionPathTestCase):
    """EXE-06: a ``plx.act`` intent's state and approval survive a process killed while it
    holds the action store's write lock, and a restart; nothing is dispatched until the
    approved intent is executed (fixture owner, test mode)."""

    def killed_holding_the_store(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-I", "-c",
             "import sqlite3, sys, time; db = sqlite3.connect(sys.argv[1], isolation_level=None);"
             " db.execute('BEGIN IMMEDIATE'); print('held', flush=True); time.sleep(600)",
             str(self.dir / "actions.db")], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
        finally:
            child.kill()
            child.wait(timeout=30)
            child.stdout.close()
        self.assertEqual(child.returncode, -9)

    def resumed(self, iid: str) -> dict:
        self.cases = CaseStore(self.dir / "cases.db", citation_resolver=self.resolve)
        self.path = self.new_path()
        return self.path.view(OPS, iid)

    def test_a_parked_or_approved_intent_survives_a_kill_and_resumes_undispatched(self):
        parked = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        before = dict(self.fx.rows[(REL, '{"id":3}')])
        self.killed_holding_the_store()
        view = self.resumed(parked["intent_id"])
        self.assertEqual((view["state"], host_module.approval_state(view), view["dispatches"],
                          view["outcome"], view["chain_ok"]),
                         ("proposed", "none", [], "not_dispatched", True))

        approved = self.path.register(OPS, self.case_id, self.propose(
            effect={**EFFECT, "equals": "PO-9999"}), session=SESSION)
        iid = approved["intent_id"]
        self.approve(iid, self.call_record(approved))
        self.killed_holding_the_store()
        view = self.resumed(iid)
        self.assertEqual((view["state"], host_module.approval_state(view), view["dispatches"],
                          view["outcome"], view["chain_ok"]),
                         ("call_approved", "call_approved", [], "not_dispatched", True))
        self.assertEqual((self.fx.calls, self.fx.rows[(REL, '{"id":3}')]), (0, before))
        # Approved is not authorized: no dispatch before the gate is requested and decided.
        with self.assertRaises(ap.ActionPathError):
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"])
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.killed_holding_the_store()
        view = self.resumed(iid)
        self.assertEqual((view["state"], host_module.approval_state(view), view["dispatches"]),
                         ("gate_authorized", "decided", []))
        self.assertEqual(self.fx.calls, 0)
        # Executed: the one dispatch, after the restart.
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry, session=SESSION)["state"],
                         "dispatched")
        self.assertEqual((self.fx.calls, self.fx.rows[(REL, '{"id":3}')]["po_reference"]),
                         (1, "PO-9999"))
        self.assertEqual(self.resumed(parked["intent_id"])["state"], "proposed")


if __name__ == "__main__":
    unittest.main()
