"""A linked source the host's budget cut blocks the pick on a typed capture_gap.

Evidence label: injected-transport (the spec fixture over the fake Portal, BC and Graph,
and a host ledger written by the test; no network, no real PLX data).
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from integrations.source_mirror import AudienceMap
from operating import case_workspace_live as live_ws
from operating import host

PICK = "PurchaseOrder+SupplierConfirmation"


def ledger(state: Path, mail_complete: int | None, reasons: tuple[str, ...] = ("time_budget",)
           ) -> None:
    """A host ledger whose mail source is configured with ``mail_complete`` (None: no row),
    the latest cycle reporting it partial for ``reasons``."""
    path = state / host.HOST_DIR / host.LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        for ddl in host._LEDGER_DDL:
            db.execute(ddl)
        if mail_complete is not None:
            db.execute("INSERT OR REPLACE INTO source_state VALUES ('mail',1,1,'2026-10-20T00:00:00Z',"
                       "'live',?,'2026-10-20T00:00:00Z',NULL,NULL,NULL)", (mail_complete,))
        report = {"sources": {"mail": {"state": "partial", "complete": False,
                                       "reasons": list(reasons)}}}
        db.execute("DELETE FROM cycle")
        db.execute("INSERT INTO cycle VALUES (1,'2026-10-20T00:00:00Z','2026-10-20T00:01:00Z',"
                   "'complete',?)", (json.dumps(report),))


class CaptureGapTest(unittest.TestCase):
    def opener(self, complete: int | None, reasons: tuple[str, ...] = ("time_budget",)):
        from tests import test_investigate_chain_documents as chain

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        unheld = (("A1", "pi.txt", chain.CONFIRMATION.replace("PO100777", "PO100555")),)
        chain.run_spec(temp.name, documents=unheld)
        state = Path(temp.name) / "state"
        ledger(state, complete, reasons)
        runtime = live_ws.offline_runtime(state, chain.MIRROR_AUDIENCE, "plx")
        self.addCleanup(runtime.close)
        opener = live_ws.CaseOpener(runtime, chain.PURCHASING, state,
                                    audience=AudienceMap.from_file(chain.MIRROR_AUDIENCE))
        opener.open_case("pick", PICK)   # build the projection
        with contextlib.closing(sqlite3.connect(opener.projection())) as db, db:
            [(value,)] = db.execute(
                "SELECT value FROM store_meta WHERE key = 'source_freshness'").fetchall()
            freshness = json.loads(value)
            for facts in freshness["graph_mail"].values():
                facts["last_full_traversal_at"] = None
            db.execute("UPDATE store_meta SET value = ? WHERE key = 'source_freshness'",
                       (json.dumps(freshness),))
        return opener

    def test_budget_cut_linked_source_blocks_capture_gap(self):
        opener = self.opener(0)
        opened = opener.open_case("pick", PICK)
        self.assertEqual((opened["status"], opened["missing"]),
                         ("blocked", "blocked_upstream:graph_mail"), opened)
        self.assertNotIn("case_id", opened)
        self.assertEqual(list(opener.gap_blocked.values()), [{
            "part": "mail", "source": "microsoft-graph",
            "error_code": "capture_budget_cut", "cause": "capture_gap"}])
        # The live verifier keeps the declared cause (blocked, never failed).
        from devloop import live_verify
        [part] = live_verify.blocked_parts(
            {"blocked_upstream": list(opener.gap_blocked.values())})
        self.assertEqual((part["cause"], part["error_code"]),
                         ("capture_gap", "capture_budget_cut"))

    def test_a_finished_unrecorded_or_not_budget_cut_capture_is_no_gap(self):
        for complete, reasons in ((1, ("time_budget",)), (None, ()), (0, ()),
                                  (0, ("unit_failed",))):
            opened = self.opener(complete, reasons).open_case("pick", PICK)
            self.assertEqual((opened["status"], opened["missing"]),
                             ("unlinked", "pick_source_not_captured:graph_mail"), opened)


if __name__ == "__main__":
    unittest.main()
