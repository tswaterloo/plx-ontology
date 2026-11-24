"""One due-event source failing never ends the cycle (``operating.autopilot_due_sources``)."""
from __future__ import annotations

import unittest
from unittest import mock

from operating.tests.test_autopilot import Fixture


class DueSources(Fixture):
    def test_a_failing_due_source_is_counted_and_the_cycle_goes_on(self):
        """Any error in one due-event source (receivables, QC entries, payables) is logged
        and counted; the sources after it and the cycle's own work still run."""
        from operating import autopilot_payables as pay
        from operating import autopilot_qc_entry as qce
        from operating import autopilot_receivables as ar
        ran = []
        with mock.patch.object(ar, "due_events", side_effect=RuntimeError("boom")), \
                mock.patch.object(qce, "due_events", side_effect=ValueError("boom")), \
                mock.patch.object(pay, "due_events",
                                  side_effect=lambda *a, **k: ran.append("payables") or []):
            out = self.cycle([])
        self.assertEqual(ran, ["payables"])
        self.assertEqual(out["skipped"]["receivables_due_failed"], 1)
        self.assertEqual(out["skipped"]["qc_entry_due_failed"], 1)
        self.assertNotIn("payables_due_failed", out["skipped"])


if __name__ == "__main__":
    unittest.main()
