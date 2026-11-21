"""The host's background facts keeper (``ontology_tools_traverse.prime_lazily`` on
Postgres): PLX_PG_FACTS_KEEPER=0 starts no whole read, the simple Ask's hosts start with
it off, and when it does run it reads on at most two Postgres connections (11-18: every
bench host started one and ~139 connections ran whole reads during a live rebuild)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operating import ontology_tools_traverse as t


class KeeperSwitchTests(unittest.TestCase):
    def tools(self):
        tools = object.__new__(t.TraverseTools)
        tools.store = SimpleNamespace(lock=threading.Lock(), format_problem=lambda: None,
                                      conn=SimpleNamespace(_pg=object()), facts=None)
        tools._pending = None
        return tools

    def prime(self, env):
        tools = self.tools()
        started = []
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(t, "_store_identity", return_value=("p", {"data_version": 1})), \
                mock.patch.object(t.TraverseTools, "_kept_facts", return_value=False), \
                mock.patch.object(t, "_delta_folder", return_value=None), \
                mock.patch.object(t, "_background_prime",
                                  side_effect=lambda *a, **k: started.append(a) or "done"):
            loaded = tools.prime_lazily(("cells",))
        return loaded, started, tools._pending

    def test_off_starts_no_background_read_and_on_still_does(self):
        loaded, started, pending = self.prime({t.FACTS_KEEPER_ENV: "0"})
        self.assertEqual((loaded, started, pending), (False, [], None))
        for value in ("1", "", "yes"):   # anything but "0" keeps it as it was
            with self.subTest(value=value):
                loaded, started, pending = self.prime({t.FACTS_KEEPER_ENV: value})
                self.assertFalse(loaded)
                self.assertEqual(len(started), 1)
                self.assertIsNotNone(pending)

    def test_the_keeper_reads_on_two_connections_at_most(self):
        seen = []

        class Child:
            returncode = 0

            def communicate(self, data, timeout=None):
                return b"", b""

        def popen(argv, **kw):
            seen.append(kw["env"]["PLX_FACTS_WORKERS"])
            return Child()
        # no setting widens it: an old worker override and a deploy's own count are ignored
        for value, want in ((None, "1"), ("3", "1"), ("8", "1"), ("0", "1"), ("lots", "1"),
                            ("99", "1")):
            env = {} if value is None else {"PLX_PG_FACTS_KEEPER_WORKERS": value}
            with self.subTest(value=value), mock.patch.dict(os.environ, env), \
                    mock.patch.object(t, "keep_pg_facts", return_value=False), \
                    mock.patch("subprocess.Popen", side_effect=popen):
                if value is None:
                    os.environ.pop("PLX_PG_FACTS_KEEPER_WORKERS", None)
                    os.environ["PLX_FACTS_WORKERS"] = "8"   # a deploy's own setting
                self.assertTrue(t._keep_pg_facts_apart("postgresql:///x"))
                self.assertEqual(seen[-1], want)
        self.assertEqual(t.KEEPER_CONNECTIONS, 2)   # the leader and one worker

    def test_the_simple_asks_host_starts_with_the_keeper_off(self):
        from operator_api import simple_ask as sa
        with tempfile.TemporaryDirectory() as work:
            config = Path(work) / "host.json"
            config.write_text(json.dumps({"tenant": "plx"}), encoding="utf-8")
            asker = SimpleNamespace(state_dir=work, host_config=str(config),
                                    secret="s" * 32)
            session = SimpleNamespace(
                employee=SimpleNamespace(kind="employee", object_id="oid", tenant_id="tid"),
                email="ann@example.com", portal_user_id="7")
            host = sa.host_for(asker, session)
            self.assertEqual(host.mcp_server()["env"][t.FACTS_KEEPER_ENV], "0")


if __name__ == "__main__":
    unittest.main()


class WorkingDayCap(unittest.TestCase):
    """The delta follower's whole facts read in the working day (PLX_FACTS_DAY_WORKERS, its
    unit): at most that many readers and 1 parallel worker per statement, 7 AM-7 PM ET; at
    night, or for any process that does not set it, as before (11-21 10:15 AM ET: 8 readers
    x 4 parallel workers held the box at load 43-54 for 10+ minutes)."""

    def test_the_day_cap_holds_in_working_hours_only_and_only_when_set(self):
        import datetime as dt
        at = lambda h: dt.datetime(2026, 11, 21, 5, 0, tzinfo=dt.timezone.utc) + dt.timedelta(hours=h)  # h ET
        env = {"PLX_FACTS_DAY_WORKERS": "3", "PLX_FACTS_WORKERS": "8"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(t._facts_day_cap(at(10)), (3, 1))
            self.assertEqual(t._facts_day_cap(at(7)), (3, 1))
            self.assertIsNone(t._facts_day_cap(at(19)))
            self.assertIsNone(t._facts_day_cap(at(2)))
            with mock.patch.object(t, "_facts_day_cap", return_value=(3, 1)):
                self.assertEqual(t._facts_workers(), 3)
            with mock.patch.object(t, "_facts_day_cap", return_value=None):
                self.assertEqual(t._facts_workers(), 8)
        with mock.patch.dict(os.environ, {"PLX_FACTS_WORKERS": "8"}):
            os.environ.pop("PLX_FACTS_DAY_WORKERS", None)
            self.assertIsNone(t._facts_day_cap(at(10)))   # not set: no cap (hosts, the API)
            self.assertEqual(t._facts_workers(), 8)
        with mock.patch.dict(os.environ, {**env, "PLX_FACTS_DAY_HOURS": "8-17",
                                          "PLX_FACTS_DAY_PARALLEL": "0"}):
            self.assertEqual(t._facts_day_cap(at(8)), (3, 0))
            self.assertIsNone(t._facts_day_cap(at(17)))
