"""``python3 -m operating.host``: capture cycles into one state directory, the projection
built from them, and the MCP start-up path over it.

Evidence label: ``injected-transport``. Fake Graph (mail, SharePoint, Teams, directory), BC and
Portal Workbench senders sit behind the real GET-only clients; no network, no credential.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
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

from verify import live_verify as lv
from integrations import capture_sharepoint as cs
from integrations import capture_teams
from integrations.capture_sharepoint_scope import CaptureScope
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
from tests import tmpdirs

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
        # Posted yesterday: a conversation active in the scope's active_days is due every
        # cycle (capture_teams._schedule); a fixed date would age out of that window.
        self.teams.epoch = datetime.now(timezone.utc) - timedelta(days=1)
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
        # removed again while a warm-up the host left running still writes into it
        temp = tmpdirs.TemporaryDirectory()
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
        # Each build step's start, end and seconds: in the generation, as it ran, and in
        # the cycle row (``report.build_timings``), so a cycle stopped after its build
        # still records them.
        timings = json.loads((committed.store.parent / "timings.json").read_text())
        steps = {entry["step"] for entry in timings["steps"]}
        self.assertLessEqual({"fingerprint", "project", "finish.check", "finish.digest",
                              "finish", "total"}, steps)
        self.assertIn("publish", second["projection"]["seconds"])
        with sqlite3.connect(self.state / "host" / "host.sqlite") as db:
            row = db.execute("SELECT report FROM cycle WHERE cycle_id = ?",
                             (second["cycle"],)).fetchone()
        held = {entry["step"] for entry in json.loads(row[0])["build_timings"]["steps"]}
        self.assertLessEqual(steps | {"publish"}, held)

        # GET-only: the only other method is the OAuth token POST.
        for method, url in self.router.calls:
            if urlsplit(url).hostname == "login.microsoftonline.com":
                self.assertEqual(method, "POST")
            else:
                self.assertEqual(method, "GET", url)

    def test_a_build_step_constraint_failure_is_named_and_keeps_the_served_generation(self):
        from operating import build_steps
        first = self.run_cycle()
        self.assertEqual(first["projection"]["state"], "built", first)
        served = host_module.projection_path(self.state)
        committed = inc.current_generation(served.parent)
        self.clock.advance(900)
        self.router.mail.put(mail_t.BOX_A, "m3", "ck3", received=(
            self.clock.now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        # The document links step refused by the links table, as cycle 4's was.
        with patch.object(build_steps, "store_document_links", side_effect=sqlite3.IntegrityError(
                "UNIQUE constraint failed: link_rows.link_id")):
            second = self.run_cycle()
        # The cause and the step, never the table or column the message names.
        self.assertEqual((second["projection"]["state"], second["projection"]["reasons"]),
                         ("failed", ["IntegrityError:constraint_unique:step=document_fields"]))
        self.assertNotIn("link_rows", json.dumps(second))
        after = inc.current_generation(served.parent)
        self.assertEqual((after.id, after.content_digest),
                         (committed.id, committed.content_digest))
        self.assertTrue(served.samefile(committed.store))

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
        from operating.mcp_server_host import ACTION_PATH_DIR, ACTION_PATH_FILE

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
        # ``serve`` runs the cycles only, with or without the retired ``--no-console``
        # (and ``--port``): the console it once opened is never started.
        from operator_api.workspace import case_console
        for flags in (("--no-console",), ("--port", "8765")):
            with patch.object(case_console, "main",
                                   side_effect=AssertionError("console started")):
                code, text, err = self.main("serve", *flags, "--max-cycles", "1",
                                            "--state-dir", str(self.state), "--config",
                                            str(self.config))
            self.assertEqual(code, 0, err)
            waiting = json.loads(text)
            self.assertEqual({k: waiting[k] for k in ("schema", "state", "reason", "cycle")},
                             {"schema": host_module.CYCLE_SCHEMA, "state": "waiting",
                              "reason": "host_cycle_running", "cycle": None}, flags)
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










if __name__ == "__main__":
    unittest.main()
