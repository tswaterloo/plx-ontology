"""The host's Mission Control capture is stamped for the host, never for the live check.

Reproduced 2026-10-26 live: the ingest-mode host (tenant ``plx``) ran
``integrations.mc_sources capture``, which stamped its store with the live check's own
tenant (``live-check``), so every host ``mc://`` read and claim was refused
(``binding_mismatch``). The host now passes its capture audience and principal; a store
stamped for another tenant is moved aside and captured again, never relabelled; every task's
details are read (no 25-read cap) under a time budget that resumes. Injected transport
(the fake MC of ``test_mc_sources``); no network, no credentials.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from integrations import mc_sources
from integrations.capture_store import CaptureError, CaptureStore
from integrations.live_source_guard import LiveSourceAudience
from integrations.mc_sources import KIND, CaptureSpec, McSourcesCapture, unit_of
from integrations.tests.test_mc_sources import ENV, FakeMc, hub, mc_client
from ontology.types import Actor
from operating import ingest_forever as forever

ROOT = Path(__file__).resolve().parents[2]
HOST_PRINCIPAL = "svc:workbench-host"
OIDS = "00000000-0000-4000-8000-0000000000aa"
HOST_ENV = {**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": OIDS}
# The host's served audience: one granted reader of the MC scope, nobody else.
SERVED = LiveSourceAudience({"mc:live:*": ["staff:ops"]}, tenant="plx")
OPS = Actor("plx", frozenset({"staff:ops"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:outsider"}), "restricted")


def tasks(fake: FakeMc, n: int) -> None:
    fake.tasks = {f"TASK-{i}": {"id": f"TASK-{i}", "title": f"t{i}", "stage": "progress",
                                "bucket": "BKT-OPS", "approvalGates": [{
                                    "id": f"apg_{i}", "status": "pending", "reason": "r",
                                    "requestedBy": "x", "requestedAt": "2026-10-01T11:00:00Z"}]}
                  for i in range(1, n + 1)}
    fake.events = {}


class HostTenantTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = mc_sources.store_path(self.dir)
        self.store.parent.mkdir(parents=True)
        self.audience = ROOT / "examples" / "ingest_all.capture-audience.json"
        self.fake = FakeMc()

    def cli(self, *extra: str, env=HOST_ENV, host: bool = True,
            principal: str = HOST_PRINCIPAL) -> tuple[int, dict]:
        argv = ["capture", "--store", str(self.store), "--scope", "live",
                "--max-task-reads", "50000"]
        if host:
            argv += ["--live-audience", str(self.audience), "--principal", principal]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mc_sources.main(argv + list(extra), environ=env, hub=hub(self.fake))
        return code, json.loads(out.getvalue())

    def first_task(self, store: CaptureStore, actor: Actor) -> str:
        return store.records(actor, KIND, unit_of("live", "tasks"), limit=1)["items"][0][
            "citation"]

    def host_resolve(self, actor: Actor, handle: str) -> dict:
        with CaptureStore.open_read_only(self.store, SERVED) as store:
            return store.resolve(handle, actor)

    def test_host_capture_resolves_for_granted_readers_only(self) -> None:
        code, report = self.cli()
        self.assertEqual(code, 0, report)
        summary = report["_summary"]
        self.assertTrue(summary["resolve_all_ok"])
        self.assertTrue(summary["audience_trim_ok"])
        self.assertIsNone(report["store_reset"])
        with CaptureStore.open_read_only(self.store, SERVED) as store:
            self.assertEqual(store.stamped_tenant(), "plx")
            handle = self.first_task(store, OPS)
            with self.assertRaises(CaptureError) as refused:
                store.records(OUTSIDER, KIND, unit_of("live", "tasks"), limit=1)
            self.assertEqual(refused.exception.code, "not_visible")
        self.assertTrue(self.host_resolve(OPS, handle)["ok"])
        self.assertFalse(self.host_resolve(OUTSIDER, handle).get("ok"))

    def test_a_live_check_store_is_set_aside_and_recaptured_never_relabelled(self) -> None:
        code, report = self.cli(host=False)   # the old behaviour: stamped "live-check"
        self.assertEqual(code, 0, report)
        with CaptureStore.open_read_only(self.store, SERVED) as store:
            self.assertEqual(store.stamped_tenant(), "live-check")
            with self.assertRaises(CaptureError) as refused:
                store.records(OPS, KIND, unit_of("live", "tasks"), limit=1)
            self.assertEqual(refused.exception.code, "binding_mismatch")
        code, report = self.cli()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["store_reset"], "tenant_mismatch")
        self.assertTrue(report["_summary"]["resolve_all_ok"])
        with CaptureStore.open_read_only(self.store, SERVED) as store:
            self.assertEqual(store.stamped_tenant(), "plx")
            handle = self.first_task(store, OPS)
        self.assertTrue(self.host_resolve(OPS, handle)["ok"])
        moved = self.store.with_name(self.store.name + mc_sources.TENANT_MISMATCH_SUFFIX)
        with CaptureStore.open_read_only(moved, LiveSourceAudience({}, tenant="x")) as old:
            self.assertEqual(old.stamped_tenant(), "live-check")   # kept, not relabelled
        # the next cycle captures into the host's store again: nothing more to clear
        code, report = self.cli()
        self.assertEqual((code, report["store_reset"]), (0, None))

    def test_unusable_people_exclusion_or_missing_principal_reads_nothing(self) -> None:
        for env, principal, error in (
                ({**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": "not-an-oid"}, HOST_PRINCIPAL,
                 "excluded_people_oids_invalid"),
                (HOST_ENV, "", "mc_principal_missing")):
            self.fake.calls.clear()
            code, report = self.cli(env=env, principal=principal)
            self.assertEqual((code, report.get("error")), (1, error))
            self.assertEqual(self.fake.calls, [])
            self.assertFalse(self.store.exists())

    def test_a_reader_the_audience_does_not_grant_is_no_vacuous_pass(self) -> None:
        code, report = self.cli(principal="staff:nobody")
        self.assertEqual(report["_summary"]["handles_resolved"], 0)
        self.assertGreater(report["_summary"]["records_current"], 0)
        self.assertFalse(report["_summary"]["resolve_all_ok"])

    def test_every_tasks_details_are_read_at_1_10_and_60_tasks(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                for path in self.store.parent.iterdir():
                    path.unlink()
                tasks(self.fake, n)
                code, report = self.cli()
                self.assertEqual(code, 0, report)
                units = {u["collection"]: u for u in report["passes"][-1]["units"]}
                self.assertEqual(units["tasks"]["records_current"], n)
                self.assertEqual(units["approvals"]["records_current"], n)
                self.assertEqual(units["approvals"]["state"], "complete")
                self.assertTrue(report["_summary"]["resolve_all_ok"])
                self.assertEqual(report["_summary"]["handles_resolved"],
                                 report["_summary"]["records_current"])

    def test_a_budget_pauses_task_details_and_the_next_cycle_resumes(self) -> None:
        tasks(self.fake, 60)
        audience = LiveSourceAudience({"mc:live:*": [HOST_PRINCIPAL]}, tenant="plx")
        ticks = iter(range(10_000))
        spec = CaptureSpec("live", max_task_reads=50_000, page_size=25)
        with CaptureStore(self.store, audience) as store:
            first = McSourcesCapture(store, mc_client(self.fake), deadline=2,
                                     clock=lambda: next(ticks)).capture(spec)
            units = {u["collection"]: u for u in first["units"]}
            self.assertEqual((units["approvals"]["state"], units["approvals"]["reason"]),
                             ("in_progress", "cap:budget_seconds"))
            self.assertEqual(units["approvals"]["records_current"], 50)
            self.fake.calls.clear()
            second = McSourcesCapture(store, mc_client(self.fake)).capture(spec)
            units = {u["collection"]: u for u in second["units"]}
            self.assertEqual(units["approvals"]["state"], "complete")
            self.assertEqual(units["approvals"]["records_current"], 60)
            self.assertEqual(self.fake.tool_names().count("mc_get_task"), 10)
        code, report = self.cli("--budget-seconds", "0")
        self.assertEqual((code, report.get("error")), (1, "mc_budget_seconds_invalid"))


class IngestModeTests(unittest.TestCase):
    def test_the_host_runs_mc_as_its_capture_principal_under_its_audience(self) -> None:
        from operating import host as host_module

        path = ROOT / "examples" / "ingest_all_sources.json"
        config = host_module.HostConfig.from_file(path, capture_sources=forever.SOURCES)
        options = config.sources["mc"]
        audience = str((path.parent / "ingest_all.capture-audience.json").resolve())
        self.assertEqual((options["live_audience"], options["principal"]),
                         (audience, HOST_PRINCIPAL))
        self.assertGreaterEqual(options["max_task_reads"], 10_000)
        argv = forever.SOURCES["mc"].argv(Path("/state"), options)
        for flag, value in (("--live-audience", audience), ("--principal", HOST_PRINCIPAL),
                            ("--budget-seconds", str(options["budget_seconds"]))):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        granted = LiveSourceAudience.from_file(audience)
        actor = Actor(granted.tenant, frozenset({HOST_PRINCIPAL}), "restricted")
        self.assertEqual(granted.tenant, config.tenant)
        self.assertTrue(granted.allows(actor, "mc:live:tasks"))
        self.assertTrue(granted.allows(actor, "mc:live:project:PRJ-OPS"))
        # without the host's audience the section is refused, never run as the live check
        with self.assertRaises(forever.IngestError):
            forever.section_options(forever.SOURCES["mc"], {"scope": "live"}, Path("/"), {})


if __name__ == "__main__":
    unittest.main()
