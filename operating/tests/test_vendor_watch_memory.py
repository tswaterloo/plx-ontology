"""The vendor-mail pass keeps the whole engine's result and reads out of its vendor stage.

AL-06: the 4-hourly whole pass peaked at the sum of the whole engine's reads and the vendor
checks' own, because one runtime and the engine's result lived through both. Now the engine's
result is reduced to counts and its runtime closed before the vendor stage. Synthetic
objects stand in for the engine and the runtime (no store); evidence label: isolated-fixture.
"""

from __future__ import annotations

import gc
import tracemalloc
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operating import vendor_watch as vw


class Big(dict):
    """A weak-referenceable stand-in for the engine's whole-store result."""


class Runtime:
    made: list["Runtime"] = []

    def __init__(self):
        self.closed = False
        self.cache = bytearray(8_000_000)   # what a runtime's reads hold while it lives
        Runtime.made.append(self)

    def close(self):
        self.closed = True
        self.cache = bytearray()


class Session:
    def __init__(self, runtime, *a, **k):
        self.runtime = runtime

    def close(self):
        pass


def _run(make_result, vendor_probe):
    Runtime.made = []
    config = SimpleNamespace(tenant="t", principal="p", mirror_audience="aud", sources=())

    def fake_run(*a, **k):
        vendor_probe()
        return {"detectors": {}, "gaps_by_kind": {}}

    from operating import case_workspace_live as live
    from operating import host, proactive_engine as pe, proactive_pool

    with mock.patch.object(live, "offline_runtime", lambda *a, **k: Runtime()), \
            mock.patch.object(live, "_Session", Session), \
            mock.patch.object(host, "engine_readers", lambda c: None), \
            mock.patch.object(host, "projection_path", lambda d: Path("store")), \
            mock.patch.object(proactive_pool, "from_host_config", lambda *a, **k: None), \
            mock.patch("integrations.source_mirror.AudienceMap.from_file", lambda p: object()), \
            mock.patch.object(pe, "served_behind", lambda s: False), \
            mock.patch.object(pe, "engine_marking_grants", lambda a: {}), \
            mock.patch.object(pe, "inbox_path", lambda s: Path("inbox")), \
            mock.patch.object(pe, "refresh", lambda *a, **k: make_result()), \
            mock.patch.object(pe, "run", fake_run), \
            mock.patch("operating.meeting_followup.cycle", lambda *a, **k: {}):
        return vw.run_pass(Path("state"), config, seconds=100, bound=1,
                           as_of=__import__("datetime").date(2026, 11, 14), refresh="whole")


class VendorStageRetention(unittest.TestCase):

    def test_whole_engine_result_and_runtime_are_gone_before_the_vendor_stage(self):
        refs = []

        def make():
            big = Big(detectors={"a": 1}, gaps_found=3, seconds=1.0, gaps=list(range(100_000)))
            refs.append(weakref.ref(big))
            return big

        seen = {}

        def probe():
            gc.collect()
            seen["result_alive"] = refs[0]() is not None
            seen["first_closed"] = Runtime.made[0].closed
            seen["live_runtimes"] = sum(not r.closed for r in Runtime.made)

        out = _run(make, probe)
        self.assertEqual(out["full_refresh"]["gaps_found"], 3)       # the counts are kept
        self.assertNotIn("gaps", out["full_refresh"])
        self.assertFalse(seen["result_alive"])
        self.assertTrue(seen["first_closed"])
        self.assertEqual(seen["live_runtimes"], 1)
        self.assertTrue(all(r.closed for r in Runtime.made))          # and all closed at the end

    def test_vendor_stage_peak_does_not_carry_the_engines_read(self):
        peaks = {}
        _run(lambda: Big(gaps_found=0), lambda: None)   # imports settle before measuring

        def probe():
            gc.collect()
            peaks["held"] = tracemalloc.get_traced_memory()[0]

        tracemalloc.start()
        try:
            _run(lambda: Big(detectors={}, gaps_found=0, seconds=0.0,
                             rows=list(range(500_000))), probe)
        finally:
            tracemalloc.stop()
        # one 8 MB runtime cache is live in the vendor stage, not that and the engine's
        # 8 MB runtime plus its 500k-row result (> 20 MB together)
        self.assertLess(peaks["held"], 12_000_000)


if __name__ == "__main__":
    unittest.main()
