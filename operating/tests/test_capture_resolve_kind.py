"""A capture handle of a kind other than BC never resolves through the BC reader.

Evidence label: ``isolated-fixture`` (temp SQLite, no source I/O).
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from integrations.capture_store import (CaptureStore, CapturedRecord, SourceKind,
                                        make_handle, register_source_kind)
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AudienceMap
from ontology.core import Actor
from operating.case_runtime import CaseRuntime, CitationUnresolvable, LiveReaders
from operating.host import BC_DIR, BC_FILE

ZZ = register_source_kind(SourceKind(
    name="zz_test", scheme="zztest", path_parts=1, canonical_path=tuple,
    unit=lambda path: "bc:zz", resources=lambda path: ("bc:zz",),
    key=lambda path: {"id": path[0]}))
SVC = Actor("plx", frozenset({"svc"}), "restricted")


class CaptureResolveKindTests(unittest.TestCase):
    def test_a_non_bc_handle_in_the_bc_store_is_unresolvable(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        state = Path(scratch.name)
        (state / BC_DIR).mkdir()
        audience = LiveSourceAudience({"bc:*": ["svc"]}, tenant="plx",
                                      default_classification="restricted")
        store = CaptureStore(state / BC_DIR / BC_FILE, audience)
        run = store.begin_run("zz_test", "bc:zz", ["bc:zz"])
        store.save_page(run["run_id"], [CapturedRecord(
            path=("r1",), payload={"row": {"id": "r1"}}, source_version="v1",
            lineage="bc_manufacturing")], None)
        store.finish_run(run["run_id"], "complete")
        handle = store.records(SVC, "zz_test", "bc:zz")["items"][0]["citation"]
        self.assertEqual(store.resolve(handle, SVC)["kind"], "zz_test")  # store would serve it
        store.close()
        runtime = CaseRuntime(state, client=None, audience=AudienceMap({}, tenant="plx"),
                              tenant="plx", live=LiveReaders(audience))
        self.addCleanup(runtime.close)
        with self.assertRaises(CitationUnresolvable):
            runtime._capture_resolve(handle, SVC)


if __name__ == "__main__":
    unittest.main()
