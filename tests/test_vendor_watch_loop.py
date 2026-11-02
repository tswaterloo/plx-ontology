"""scripts/vendor_watch_loop.sh: the vendor-mail pass's runner. One pass (VENDOR_WATCH_ONCE)
with a stub interpreter: the pass gets its state, config and a budget inside the cap, and no
credential-shaped variable reaches it; a new generation served starts the next pass. The pass
itself (``operating.vendor_watch.run_pass``) runs the whole engine first on a new generation.
SYNTHETIC stores only. Evidence label: isolated-fixture."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vendor_watch_loop.sh"


class VendorWatchLoopTest(unittest.TestCase):

    def test_one_pass_gets_its_arguments_and_no_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            seen = tmp / "seen.txt"
            stub = tmp / "python-stub"
            stub.write_text(f"#!/bin/bash\n{{ echo \"$@\"; env; }} > {seen}\n", "utf-8")
            stub.chmod(0o755)
            envfile = tmp / "plx.env"
            envfile.write_text("PLX_EXCLUDED_PEOPLE_OIDS=synthetic-oid\n"
                               "PLX_WORKBENCH_API_KEY=synthetic-secret\n"
                               "MICROSOFT_GRAPH_CLIENT_SECRET=synthetic-secret\n", "utf-8")
            environ = {"PATH": os.environ["PATH"], "VENDOR_WATCH_ONCE": "1",
                       "VENDOR_WATCH_PYTHON": str(stub), "VENDOR_WATCH_CAP": "5400",
                       "SOME_TOKEN": "synthetic-secret"}
            done = subprocess.run(["bash", str(SCRIPT), str(tmp / "state"),
                                   str(tmp / "host.json"), str(envfile)],
                                  env=environ, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("vendor pass exit 0", done.stdout)
            text = seen.read_text("utf-8")
            self.assertIn(f"-m operating.vendor_watch --state-dir {tmp / 'state'} --config "
                          f"{tmp / 'host.json'} --budget-seconds 5100", text)
            self.assertIn("PLX_EXCLUDED_PEOPLE_OIDS=synthetic-oid", text)
            self.assertNotIn("synthetic-secret", text)


    def test_a_stop_file_ends_the_wait_between_passes_within_a_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "state").mkdir()
            stub = tmp / "python-stub"
            # the pass: drops the stop file, as an operator would during the wait
            stub.write_text(f"#!/bin/bash\ntouch {tmp / 'state' / 'vendor-watch.stop'}.later\n"
                            "exit 0\n", "utf-8")
            stub.chmod(0o755)
            environ = {"PATH": os.environ["PATH"], "VENDOR_WATCH_PYTHON": str(stub),
                       "VENDOR_WATCH_INTERVAL": "7200", "VENDOR_WATCH_STEP": "1"}
            loop = subprocess.Popen(["bash", str(SCRIPT), str(tmp / "state"),
                                     str(tmp / "host.json")], env=environ,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 30
                while not (tmp / "state" / "vendor-watch.stop.later").exists():
                    self.assertLess(time.monotonic(), deadline, "the pass never ran")
                    time.sleep(0.2)
                (tmp / "state" / "vendor-watch.stop").touch()
                out, _ = loop.communicate(timeout=20)   # well before the 2 h interval
            finally:
                loop.kill()
            self.assertEqual(loop.returncode, 0)
            self.assertIn("stopped by", out)

    def test_a_new_generation_served_starts_the_next_pass_without_the_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "state" / "projection").mkdir(parents=True)
            current = tmp / "state" / "projection" / "CURRENT"
            current.write_text('{"generation":"00000006"}', "utf-8")
            passes = tmp / "passes.txt"
            stub = tmp / "python-stub"
            stub.write_text(f"#!/bin/bash\necho pass >> {passes}\nexit 0\n", "utf-8")
            stub.chmod(0o755)
            environ = {"PATH": os.environ["PATH"], "VENDOR_WATCH_PYTHON": str(stub),
                       "VENDOR_WATCH_INTERVAL": "7200", "VENDOR_WATCH_STEP": "1"}
            loop = subprocess.Popen(["bash", str(SCRIPT), str(tmp / "state"),
                                     str(tmp / "host.json")], env=environ,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            def count() -> int:
                return len(passes.read_text("utf-8").split()) if passes.exists() else 0

            try:
                deadline = time.monotonic() + 30
                while count() < 1:
                    self.assertLess(time.monotonic(), deadline, "the first pass never ran")
                    time.sleep(0.2)
                time.sleep(2)
                self.assertEqual(count(), 1)            # waiting out the 2 h interval
                current.write_text('{"generation":"00000007"}', "utf-8")   # published
                deadline = time.monotonic() + 20
                while count() < 2:
                    self.assertLess(time.monotonic(), deadline, "no pass on the new generation")
                    time.sleep(0.2)
                (tmp / "state" / "vendor-watch.stop").touch()
                out, _ = loop.communicate(timeout=20)
            finally:
                loop.kill()
            self.assertIn("new projection generation served", out)


class VendorPassRefreshTest(unittest.TestCase):
    """``operating.vendor_watch.run_pass`` over a real synthetic store and inbox: the whole
    engine runs first when the generation served is newer than the inbox's last full run,
    and only then. The case session and runtime are stubs (no source is read)."""

    def test_a_new_generation_is_recomputed_once_and_older_findings_never_listed(self):
        from datetime import date, datetime, timezone
        from unittest import mock

        from operating import proactive_engine as pe
        from operating import vendor_watch
        from operating.tests import test_proactive_engine as tpe

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            from operating.host import projection_path
            store = projection_path(state)
            store.parent.mkdir(parents=True)
            inbox = pe.inbox_path(store)
            world, ids = tpe.seeded()
            world.build(store)
            moment = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
            real_run = pe.run
            pe.run(store, inbox, tpe.AS_OF, coverage=ids["coverage"],
                   qc_link_types=(tpe.QC_LINK,), clock=lambda: moment)
            # the next generation is served: the order is now confirmed on time
            world.link("SupplierConfirmation.confirms",
                       world.obj("SupplierConfirmation", {"delivery_date": "2026-10-09"}),
                       ids["unconfirmed"])
            world.build(store)

            def read() -> dict:
                return pe.read_inbox(inbox, store, reader="plx:ops", may_read=tpe.everyone,
                                     held=tpe.ALL_MARKINGS, decide=tpe.admit)

            self.assertTrue(read()["stale"])
            refreshed = []

            def refresh(runtime, actor, audience, projection, as_of, **options):
                refreshed.append((projection, as_of, options["vendor_bound"]))
                return real_run(projection, pe.inbox_path(projection), as_of,
                                coverage=ids["coverage"], qc_link_types=(tpe.QC_LINK,),
                                clock=lambda: moment.replace(hour=8))

            def run(*args, **options):
                self.assertEqual(options.get("only"), pe.INCREMENTAL)
                return {"detectors": {name: "complete" for name in pe.INCREMENTAL}}

            config = mock.Mock(tenant="plx", principal="svc:workbench-host",
                               mirror_audience=state / "audience.json",
                               sources={"portal": {}})
            with mock.patch("operating.case_workspace_live.offline_runtime"), \
                    mock.patch("operating.case_workspace_live._Session"), \
                    mock.patch("integrations.source_mirror.AudienceMap.from_file"), \
                    mock.patch("operating.host.engine_readers", return_value=None), \
                    mock.patch.object(pe, "refresh", side_effect=refresh), \
                    mock.patch.object(pe, "run", side_effect=run):
                first = vendor_watch.run_pass(state, config, seconds=600, bound=5,
                                              as_of=date(2026, 10, 19))
                second = vendor_watch.run_pass(state, config, seconds=600, bound=5,
                                               as_of=date(2026, 10, 19))
            self.assertEqual(refreshed, [(store, date(2026, 10, 19), 0)])   # once
            self.assertEqual(first["full_refresh"]["state"], "ran")
            self.assertIsNone(second["full_refresh"])
            self.assertEqual(first["detectors"], {n: "complete" for n in pe.INCREMENTAL})
            fresh = read()
            self.assertFalse(fresh["stale"])
            late = {g["object_id"] for g in fresh["gaps"] if g["kind"] == "unconfirmed_po"}
            self.assertNotIn(ids["unconfirmed"], late)

    def test_a_failed_refresh_still_runs_the_vendor_checks_and_is_said(self):
        from unittest import mock

        from operating import proactive_engine as pe
        from operating import vendor_watch

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            config = mock.Mock(tenant="plx", principal="svc:x",
                               mirror_audience=state / "a.json", sources={})
            with mock.patch("operating.case_workspace_live.offline_runtime"), \
                    mock.patch("operating.case_workspace_live._Session"), \
                    mock.patch("integrations.source_mirror.AudienceMap.from_file"), \
                    mock.patch("operating.host.engine_readers", return_value=None), \
                    mock.patch.object(pe, "served_behind", return_value=True), \
                    mock.patch.object(pe, "refresh", side_effect=OSError("SYNTH-detail")), \
                    mock.patch.object(pe, "run", return_value={"detectors": {}}) as run:
                found = vendor_watch.run_pass(state, config, seconds=600, bound=5)
            self.assertEqual(found["full_refresh"], {"state": "failed", "error": "OSError"})
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("SYNTH-detail", str(found))


if __name__ == "__main__":
    unittest.main()
