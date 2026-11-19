"""API start preloads the deployed code's fingerprint, so a first briefing does not pay it."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from operating import derived_status, proactive_engine_inbox
from operator_api import preload


class PreloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache.name})
        env.start()
        self.addCleanup(env.stop)
        saved = dict(derived_status._VERSIONS)
        derived_status._VERSIONS.clear()
        self.addCleanup(lambda: (derived_status._VERSIONS.clear(),
                                 derived_status._VERSIONS.update(saved)))

    def test_the_first_briefing_finds_the_engine_code_preloaded(self) -> None:
        computed: list[str] = []

        def compute_all(artifacts, root):
            computed.extend(a.name for a in artifacts)
            return {a.name: f"v-{a.name}" for a in artifacts}

        with mock.patch.object(derived_status, "_compute_all", compute_all):
            preload.run()
            self.assertIn("proactive-inbox", computed)
            computed.clear()
            # what the briefing's inbox read asks: from memory, nothing parsed again
            self.assertEqual(proactive_engine_inbox.engine_code(), "v-proactive-inbox")
        self.assertEqual(computed, [])

    def test_the_schema_is_compiled_at_start_not_on_a_first_read(self) -> None:
        from ontology import schema as schema_loader
        from operating import ontology_tools
        saved = dict(schema_loader._LOADED)
        schema_loader._LOADED.clear()
        ontology_tools.shared_schema.cache_clear()
        self.addCleanup(lambda: (schema_loader._LOADED.update(saved),
                                 ontology_tools.shared_schema.cache_clear()))
        compiled = mock.Mock(wraps=schema_loader.compile_documents)
        with mock.patch.object(derived_status, "_compute_all",
                               lambda arts, root: {a.name: "v" for a in arts}), \
                mock.patch.object(schema_loader, "compile_documents", compiled):
            preload.run()
            self.assertEqual(compiled.call_count, 1)
            # a host's first tool table loads it from the process cache
            self.assertIsNotNone(ontology_tools.default_schema())
            self.assertEqual(compiled.call_count, 1)

    def test_a_failing_preload_leaves_the_work_to_the_first_read(self) -> None:
        def broken(artifacts, root):
            raise RuntimeError("no children")

        with mock.patch.object(derived_status, "_compute_all", broken), \
                self.assertLogs("operator_api.preload", "WARNING"):
            preload.run()   # never raises: the API serves on
        with mock.patch.object(derived_status, "_compute_all",
                               lambda arts, root: {a.name: "late" for a in arts}):
            self.assertEqual(proactive_engine_inbox.engine_code(), "late")


if __name__ == "__main__":
    unittest.main()
