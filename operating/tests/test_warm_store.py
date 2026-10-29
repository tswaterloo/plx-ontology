"""The live verifier's warm store (``operating.warm_store``): two verifier sessions over one
``PLX_WORKBENCH_STATE_DIR`` carry the session mirror and projection, so the second
session's producer applies a changed source row incrementally (digest equal to a cold
build) and its consumer opens that generation instead of building; without the setting the
store is refused, naming it. Fixture Workbench; nothing live."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from devloop import live_verify
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.tests.test_projection import BASE, KEY, FakeWorkbench
from operating import host, warm_store
from operating.warm_store import STATE_ENV, WarmStore
from scripts import ontology_projection_probe as probe
from scripts.mirror_measure import ordered_full_coverage

ROOT = Path(__file__).resolve().parents[2]
SPECS = {name: json.loads((ROOT / "devloop" / "live-checks" / f"{name}.json").read_text())
         for name in ("mirror.full_coverage", "ontology.session_projection")}
MIRROR, PRODUCER = SPECS["mirror.full_coverage"], SPECS["ontology.session_projection"]
FILLED = {"result": "pass", "exit_code": 0, "detail": {"fields": {}}}


def produce(projection: str, mirror: str) -> dict:
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = probe.main(["--produce-session", "--session-projection", projection,
                           "--db", mirror], environ={})
    assert code == 0, out.getvalue()
    return json.loads(out.getvalue())


class WarmSessionTest(unittest.TestCase):

    def session(self, environ, fake, cold_dir=None, clock=None, exhausted=False):
        """One verifier session: the mirror spec syncs (``exhausted``: its budget is spent
        before it reads anything, and it still exits 0), the session producer builds, a
        consumer opens; returns (warm detail, producer report, opened, cold digest)."""
        warm, detail = WarmStore.open(environ, **({"clock": clock} if clock else {}))
        self.assertIsNotNone(warm, detail)
        session = live_verify.SessionState(ROOT, warm=warm, environ=environ)
        try:
            plan = session.plan(MIRROR, SPECS)
            mirror = Path(session.bases(MIRROR, plan)["/mirror.sqlite"]) / "mirror.sqlite"
            spent = itertools.count(0, 1000.0)
            ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=fake), str(mirror),
                                  resolve_sample=0,
                                  **({"budget_seconds": 1, "monotonic": lambda: next(spent)}
                                     if exhausted else {}))
            session.record(MIRROR, plan, {"id": "mirror.full_coverage", **FILLED})
            if exhausted:
                return detail, None, None, None, None
            plan = session.plan(PRODUCER, SPECS)
            self.assertEqual(plan["/mirror.sqlite"], "shared")
            projection = Path(session.bases(PRODUCER, plan)["/projection"]) / "projection"
            # A carried generation is only the producer's starting point, never read.
            self.assertIsNone(probe.open_session(str(projection), session.started_at))
            report = produce(str(projection), str(mirror))
            session.record(PRODUCER, plan, {"id": "ontology.session_projection", **FILLED})
            opened = probe.open_session(str(projection), session.started_at)
            cold = None
            if cold_dir:   # a full rebuild of the same mirror in a directory of its own
                produce(cold_dir, str(mirror))
                cold = json.loads(Path(cold_dir, probe.SESSION_MANIFEST).read_text())
            manifest = json.loads((projection / probe.SESSION_MANIFEST).read_text())
        finally:
            session.close()
        return (detail, report, opened, manifest["content_digest"],
                cold and cold["content_digest"])

    def test_second_session_applies_a_changed_row_incrementally_and_its_consumer_opens_it(self):
        fake = FakeWorkbench()
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as cold:
            environ = {STATE_ENV: state, "PLX_WORKBENCH_API_URL": BASE,
                       "PLX_WORKBENCH_API_KEY": "fixture-key-a"}
            first, report, opened, _, _ = self.session(environ, fake)
            self.assertEqual((first["runs_seen"], first["persists_between_runs"]), (1, False))
            self.assertEqual(report["session_projection"]["mode"], "cold")
            self.assertIsNotNone(opened)
            kept = Path(state) / warm_store.WARM_DIR / "kept"
            self.assertEqual(len(list(kept.iterdir())), 2)   # the mirror and the projection
            # only the committed generation is carried
            self.assertEqual(len(list(kept.glob("*/projection/generations/*"))), 1)

            fake.set("public.app_purchase_order", "po-b", status="CLOSED")
            second, report, opened, digest, cold_digest = self.session(environ, fake,
                                                                       cold_dir=cold)
            self.assertEqual((second["runs_seen"], second["persists_between_runs"]), (2, True))
            self.assertEqual(sorted(second["restored"].values()), ["warm", "warm"])
            built = report["session_projection"]
            self.assertEqual(built["mode"], "incremental")
            self.assertLess(0, built["units_rebuilt"])
            self.assertLess(built["units_rebuilt"], built["units"])
            self.assertEqual(digest, cold_digest)        # the same store a rebuild makes
            self.assertEqual(opened["generation"].content_digest, digest)   # build skipped

            # Another credential value is another audience: nothing carried is restored.
            third, _, _, _, _ = self.session({**environ, "PLX_WORKBENCH_API_KEY": "key-b"}, fake)
            self.assertEqual(sorted(third["restored"].values()),
                             ["cold_audience", "cold_audience"])

    def test_a_mirror_re_kept_without_a_refresh_ages_from_its_last_real_refresh(self):
        """A run that re-read nothing still exits 0 (filled), so its mirror is kept again,
        but under the age of its oldest relation refresh: 26 h after that it is cold."""
        fake, now = FakeWorkbench(), datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as state:
            environ = {STATE_ENV: state, "PLX_WORKBENCH_API_URL": BASE,
                       "PLX_WORKBENCH_API_KEY": "fixture-key-a"}
            first, _, _, _, _ = self.session(environ, fake)
            self.assertEqual(sorted(first["not_refreshed"].values()), [0])
            fake.set("public.app_purchase_order", "po-b", status="CLOSED")
            second, _, _, _, _ = self.session(environ, fake, exhausted=True,
                                              clock=lambda: now + timedelta(hours=20))
            key = live_verify.session_key(MIRROR, "/mirror.sqlite")
            self.assertEqual((second["restored"][key], second["kept"][key]), ("warm", "kept"))
            self.assertGreater(second["not_refreshed"][key], 0)   # all counted, none read
            third, _, _, _, _ = self.session(environ, fake, exhausted=True,
                                             clock=lambda: now + timedelta(hours=27))
            self.assertEqual(third["restored"][key], "cold_stale")

    def test_a_store_its_writer_did_not_fill_is_not_carried(self):
        with tempfile.TemporaryDirectory() as state:
            environ = {STATE_ENV: state}
            warm, _ = WarmStore.open(environ)
            session = live_verify.SessionState(ROOT, warm=warm, environ=environ)
            plan = session.plan(MIRROR, SPECS)
            base = Path(session.bases(MIRROR, plan)["/mirror.sqlite"])
            (base / "mirror.sqlite").write_bytes(b"part")
            session.record(MIRROR, plan, {"id": "mirror.full_coverage", "result": "fail",
                                          "exit_code": None, "detail": {"timed_out": True}})
            session.close()
            self.assertFalse((Path(state) / warm_store.WARM_DIR / "kept").exists())
            self.assertEqual(list((Path(state) / warm_store.WARM_DIR).glob("session-*")), [])


def killed(environ, fake, clock, projection_check=FILLED):
    """One verifier session killed after its producers ran (no ``close``: the kernel only
    drops the lock): the mirror spec syncs and is recorded filled, the session producer
    builds and is recorded with ``projection_check`` (None: killed while it ran)."""
    warm, detail = WarmStore.open(environ, clock=clock)
    session = live_verify.SessionState(ROOT, warm=warm, environ=environ)
    plan = session.plan(MIRROR, SPECS)
    mirror = Path(session.bases(MIRROR, plan)["/mirror.sqlite"]) / "mirror.sqlite"
    session.begin(MIRROR, plan)
    ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=fake), str(mirror),
                          resolve_sample=0)
    session.record(MIRROR, plan, {"id": "mirror.full_coverage", **FILLED})
    plan = session.plan(PRODUCER, SPECS)
    projection = Path(session.bases(PRODUCER, plan)["/projection"]) / "projection"
    session.begin(PRODUCER, plan)
    produce(str(projection), str(mirror))
    if projection_check is not None:
        session.record(PRODUCER, plan, {"id": "ontology.session_projection",
                                        **projection_check})
    warm.handle.close()   # SIGKILL: nothing of close runs
    return session


class KilledRunTest(unittest.TestCase):
    """A session killed before ``close`` keeps every store its journal names as filled."""

    KEYS = {"mirror": live_verify.session_key(MIRROR, "/mirror.sqlite"),
            "projection": live_verify.session_key(PRODUCER, "/projection")}

    def environ(self, state, key="fixture-key-a"):
        return {STATE_ENV: state, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": key}

    def stamps(self, state):
        kept = Path(state) / warm_store.WARM_DIR / "kept"
        return {name: json.loads((kept / key / "stamp.json").read_text())
                for name, key in self.KEYS.items() if (kept / key / "stamp.json").is_file()}

    def test_a_killed_runs_filled_stores_are_kept_with_their_writer_and_restored_warm(self):
        fake, now = FakeWorkbench(), datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as state:
            environ = self.environ(state)
            first = killed(environ, fake, clock=lambda: now)
            later = now + timedelta(hours=1)
            warm, detail = WarmStore.open(environ, clock=lambda: later)
            self.assertEqual(detail["salvaged"], {key: "kept" for key in self.KEYS.values()})
            self.assertGreater(detail["kept_bytes"], 0)
            self.assertEqual(list(warm.root.glob("session-*")), [])   # the rest is removed
            warm.close()
            stamps = self.stamps(state)
            for name, producer in (("mirror", "mirror.full_coverage"),
                                   ("projection", "ontology.session_projection")):
                writer = stamps[name]["writer"]
                self.assertEqual((writer["producer"], writer["session_started_at"]),
                                 (producer, first.started_at))
                # kept when the killed run last recorded it, never when found
                self.assertEqual(stamps[name]["kept_at"], warm_store._iso(now))
            self.assertEqual(len(list((Path(state) / warm_store.WARM_DIR).glob(
                "kept/*/projection/generations/*"))), 1)   # its committed generation only

            # The next run restores both warm; a consumer adopts them, no producer runs.
            warm, detail = WarmStore.open(environ, clock=lambda: later)
            session = live_verify.SessionState(ROOT, warm=warm, environ=environ)
            consumer = json.loads((ROOT / "devloop" / "live-checks" /
                                   "ontology.projection.json").read_text())
            specs = {**SPECS, "ontology.projection": consumer}
            plan = session.plan(consumer, specs)
            self.assertEqual(set(plan.values()), {"warm"})
            self.assertEqual(session.needs_producers(consumer, specs),
                             {path: [] for path in plan})
            session.bases(consumer, plan)
            self.assertEqual(sorted(detail["restored"].values()), ["warm", "warm"])
            self.assertEqual({session.adopted[key]["producer"] for key in self.KEYS.values()},
                             {"mirror.full_coverage", "ontology.session_projection"})
            session.close()

    def test_a_store_its_writer_did_not_fill_or_still_wrote_is_not_kept(self):
        fake, now = FakeWorkbench(), datetime.now(timezone.utc)
        for check in ({"result": "fail", "exit_code": None, "detail": {"timed_out": True}},
                      {"result": "error", "exit_code": 1, "detail": {"fields": {"error": "x"}}},
                      None):
            with self.subTest(check=check), tempfile.TemporaryDirectory() as state:
                killed(self.environ(state), fake, clock=lambda: now, projection_check=check)
                warm, detail = WarmStore.open(self.environ(state))
                self.assertEqual(detail["salvaged"], {self.KEYS["mirror"]: "kept"})
                warm.close()
                self.assertEqual(sorted(self.stamps(state)), ["mirror"])

    def test_a_foreign_or_unreadable_journal_or_another_audience_keeps_nothing(self):
        fake, now = FakeWorkbench(), datetime.now(timezone.utc)
        for spoil in ("unreadable", "foreign", "audience"):
            with self.subTest(spoil=spoil), tempfile.TemporaryDirectory() as state:
                killed(self.environ(state), fake, clock=lambda: now)
                journal, = (Path(state) / warm_store.WARM_DIR).glob("session-*/journal.json")
                if spoil == "unreadable":
                    journal.write_text("{")
                elif spoil == "foreign":   # sealed under another directory's key
                    (Path(state) / warm_store.WARM_DIR / ".salt").write_bytes(b"k" * 32)
                environ = self.environ(state, "key-b" if spoil == "audience"
                                       else "fixture-key-a")
                warm, detail = WarmStore.open(environ)
                session = live_verify.SessionState(ROOT, warm=warm, environ=environ)
                session.bases(MIRROR, session.plan(MIRROR, SPECS))
                session.bases(PRODUCER, session.plan(PRODUCER, SPECS))
                restored = sorted(detail["restored"].values())
                session.close()
                if spoil == "audience":   # kept under its stamp, never restored under another
                    self.assertEqual(restored, ["cold_audience", "cold_audience"])
                else:
                    self.assertEqual((detail["salvaged"], restored),
                                     ({}, ["cold_none", "cold_none"]))
                self.assertEqual(self.stamps(state), {})

    def test_the_disk_line_resets_an_oversized_salvage(self):
        fake, now = FakeWorkbench(), datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as state:
            killed(self.environ(state), fake, clock=lambda: now)
            with mock.patch.object(warm_store, "_free_bytes", return_value=4096):
                warm, detail = WarmStore.open(self.environ(state))
            self.assertEqual((sorted(detail["salvaged"].values()), detail["reset"],
                              detail["kept_bytes"]), (["kept", "kept"], "disk_over_half", 0))
            warm.close()
            self.assertEqual(self.stamps(state), {})


class RefusalTest(unittest.TestCase):

    def test_unset_state_dir_is_refused_naming_it(self):
        self.assertEqual(WarmStore.open({}),
                         (None, {"status": "state_dir_unset", "setting": STATE_ENV}))
        self.assertEqual(WarmStore.open({STATE_ENV: "relative/dir"})[1]["status"],
                         "state_dir_not_absolute")
        self.assertEqual(WarmStore.open({STATE_ENV: str(ROOT / "devloop")})[1]["status"],
                         "state_dir_inside_git_tree")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = host.main(["status"], environ={})
        self.assertEqual((code, json.loads(err.getvalue())),
                         (2, {"error": "state_dir_unset", "setting": STATE_ENV}))
        # The verifier then runs a temporary session, as before.
        session = live_verify.SessionState(ROOT, warm=None)
        made = session.directory()
        self.assertFalse(made.resolve().is_relative_to(ROOT))
        session.close()
        self.assertFalse(made.exists())

    def test_one_verifier_at_a_time_and_the_disk_line(self):
        with tempfile.TemporaryDirectory() as state:
            environ = {STATE_ENV: state}
            held, _ = WarmStore.open(environ)
            self.assertEqual(WarmStore.open(environ), (None, {"status": "in_use"}))
            session = held.session_dir()
            (session / "store.bin").write_bytes(b"x" * 65536)
            self.assertEqual(held.keep("k", session, "store.bin", held.stamp({"a": 1})), "kept")
            held.close()
            with mock.patch.object(warm_store, "_free_bytes", return_value=4096):
                again, detail = WarmStore.open(environ)
            self.assertEqual((detail["reset"], detail["kept_bytes"]), ("disk_over_half", 0))
            self.assertEqual(again.restore("k", again.session_dir(), "store.bin",
                                           again.stamp({"a": 1})), "cold_none")
            again.close()


if __name__ == "__main__":
    unittest.main()
