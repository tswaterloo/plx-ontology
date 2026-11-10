"""Every autopilot in the registry defaults to not live: no entry carries a live switch,
and with no owner flag or master switch the write gate reports dry_run."""
import json
import tempfile
import unittest
from pathlib import Path

from operating import autopilot_write as w

REGISTRY = Path(w.__file__).with_name("autopilot_registry.json")


class RegistryDefaultsTests(unittest.TestCase):
    def test_no_entry_ships_live(self):
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
        ids = {a["id"] for a in reg["autopilots"]}
        self.assertTrue({"invoice_entry", "shipment_posting", "cash_application",
                         "routine_update"} <= ids)
        for a in reg["autopilots"]:
            self.assertFalse({k for k in a if "live" in k.lower() or "write" in k.lower()},
                             a["id"])
            self.assertNotIn("dry_run", a)

    def test_every_workflow_is_dry_run_without_flags(self):
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(w.live_flags({}, Path(d)), set())
            for a in reg["autopilots"]:
                for passed in (True, False):
                    gate = type("G", (), {"roles_for": lambda s, o, wf, r, p=passed:
                                          ["buyer"] if p else []})()
                    rows = [{"event": "gate_passed", "role": "buyer", "workflow": a["id"]},
                            {"event": "owner_notified", "role": "buyer", "workflow": a["id"]}]
                    mode, _ = w.write_state(gate, rows, {}, Path(d), "o", a["id"], ["buyer"])
                    self.assertEqual(mode, "dry_run", a["id"])


if __name__ == "__main__":
    unittest.main()
