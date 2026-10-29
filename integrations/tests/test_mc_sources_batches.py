"""The MC capture CLI's reader check over more records than one resolve batch.

Reproduced 2026-10-26 on the shared state: once the ``live`` scope held 8,262 current
records, every ingest-mode cycle failed MC with a bare ``CaptureError`` after a good
capture: the CLI's reader check resolved every handle in one ``resolve_many`` call, which
refuses more than ``MAX_BATCH_HANDLES`` (``invalid_handles``). The bound is lowered here
so the fake's few records cross it. Injected transport; no network, no credentials.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from integrations import capture_store, mc_sources
from integrations.tests.test_mc_sources import ENV, FakeMc, hub


class ResolveBatchTests(unittest.TestCase):
    def run_cli(self, bound: int) -> tuple[int, dict]:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), \
                mock.patch.object(capture_store, "MAX_BATCH_HANDLES", bound), \
                mock.patch.object(mc_sources, "MAX_BATCH_HANDLES", bound):
            code = mc_sources.main(["capture", "--store", str(Path(tmp) / "mc.sqlite"),
                                    "--scope", "ops", "--query", "landed cost"],
                                   environ=ENV, hub=hub(FakeMc()))
        return code, json.loads(out.getvalue())

    def test_every_record_resolves_across_batches(self) -> None:
        _, whole = self.run_cli(5000)
        held = whole["_summary"]["handles_resolved"]
        self.assertGreater(held, 2)
        code, batched = self.run_cli(2)
        self.assertNotIn("error", batched)
        self.assertEqual(code, 0)
        self.assertEqual(batched["_summary"]["handles_resolved"], held)
        self.assertTrue(batched["_summary"]["resolve_all_ok"])
        self.assertTrue(batched["_summary"]["audience_trim_ok"])


if __name__ == "__main__":
    unittest.main()
