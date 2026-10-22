"""find-po repair: a PO's same-number twin never unseats its exact match.

The synthetic case fixture holds one gold PO and its FM row, which stores the bare number
and is joined to the gold PO by the verified ``PurchaseOrder.same_record_as`` link (the
gold row's stored FM id). plx.find answers ``found`` with the FM row as a related lead,
and plx.investigate roots the PO instead of blocking on ``ambiguous_root``.
Evidence label: isolated-fixture.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from operating import session_identity
from operating.case_runtime import CaseRuntime
from operator_ui import case_console
from operator_ui import object_workspace as objects
from operator_ui.tests import case_fixtures


class PoTwinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="find-po-"))
        state = cls.tmp / "state"
        case_fixtures.seed(state, "plx")
        store = state / case_console.ONTOLOGY_FILE
        objects.project_mirror(state, store)
        cls.runtime = CaseRuntime(state, client=None,
                                  audience=state / case_fixtures.MIRROR_AUDIENCE_FILE,
                                  tenant="plx")
        session = session_identity.fixture_session(
            "plx", frozenset({case_fixtures.OPERATIONS, case_fixtures.QUALITY,
                              case_fixtures.PURCHASING}),
            channel=session_identity.CHANNEL_CONSOLE)
        cls.view = case_console.object_workspace_for(cls.runtime, store, session.actor,
                                                     state_dir=state)
        cls.addClassCleanup(cls.view.close)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.runtime.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_exact_po_is_found_and_its_linked_fm_twin_is_a_related_lead(self):
        out = self.view._call("plx.find", {"query": case_fixtures.PO_NUMBER})
        self.assertEqual(out["status"], "found", out)
        (candidate,) = out["candidates"]
        self.assertEqual(candidate["reason"], "exact_identifier")
        related = [f for f in candidate["why_not"] if f["feature"].startswith("related_")]
        self.assertEqual([(f["feature"], f["agrees"]) for f in related],
                         [("related_same_record", True)])
        self.assertIn("generated_purchase_order", related[0]["citations"][0]["l1_handle"])

    def test_investigate_roots_the_po_instead_of_blocking_on_ambiguity(self):
        for family in ("F4", "F1"):
            with self.subTest(family=family):
                out = self.view._call("plx.investigate", {
                    "family": family, "root": {"identifier": case_fixtures.PO_NUMBER}})
                self.assertNotIn("error", out)
                self.assertNotEqual(out["answerability"]["state"], "blocked", out)
                self.assertNotIn("ambiguous_root", out["answerability"]["reasons"])


if __name__ == "__main__":
    unittest.main()
