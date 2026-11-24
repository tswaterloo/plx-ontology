"""A plan run over the console's fixture world records the handles it asked the door about, through the
real view (``view.investigate``, ``HostAccess.decide``, the item gate of plx.read / plx.object /
plx.find), not through a hand call: the runner line carries a non-empty ``opened`` set that holds the
records the run read, and a line that read something yet states an empty set is a typed failure, never
an answer. Evidence label: isolated-fixture (the console's fixture world; no network, no model)."""
from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from operator_api import backend as be
from operator_api import plan_batch
from operator_api.server import build_parser
from operating import session_identity
from operating.pg import plan_sections as ps


class RealViewOpened(unittest.TestCase):
    lines: dict = {}

    @classmethod
    def setUpClass(cls):
        from operator_api.tests import case_fixtures
        from operator_api.workspace import case_console
        from operator_api.workspace import object_workspace as objects
        cls.tmp = Path(tempfile.mkdtemp(prefix="plan-opened-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        state = cls.tmp / "state"
        case_fixtures.seed(state, "plx")
        store = state / case_console.ONTOLOGY_FILE
        objects.project_mirror(state, store)
        ops = [case_fixtures.OPERATIONS, case_fixtures.PURCHASING, case_fixtures.QUALITY]
        args = build_parser().parse_args(
            ["--dev", "--state-dir", str(state), "--pg-dsn", "",
             "--console-arg=--source-mirror-audience",
             f"--console-arg={state / case_fixtures.MIRROR_AUDIENCE_FILE}"]
            + [a for p in ops for a in ("--dev-fixture-principal", p)])
        argv = be.console_argv(build_parser(), args, "dev")

        def serve(ctx):
            session = session_identity.fixture_session(
                "plx", ops, channel=session_identity.CHANNEL_CONSOLE)
            view = ctx["objects_for"](session)
            with be.page_scope(), view._request():
                oid = be.page_oid(be.WorkspaceBackend(ctx["objects_for"], data_label="fixture",
                                                      state_dir=state, store_path=store, pg_dsn="",
                                                      ask_served=False, data_dir=cls.tmp / "app"),
                                  view, "PurchaseOrder", case_fixtures.PO_NUMBER)
            cls.oid = oid
            for family in ("late_po", "people_on_record", "landed_cost", "po_trail"):
                cls.lines[family] = plan_batch.answer(view, oid, family)
            return 0
        with contextlib.redirect_stderr(io.StringIO()):
            rc = case_console.main(argv, serve_with=serve, fixtures=case_fixtures)
        assert rc == 0, rc

    def test_every_family_run_through_the_real_view_opened_something(self):
        for family, line in self.lines.items():
            self.assertIsNotNone(line, family)
            self.assertNotIn("failed", line, (family, line.get("failed")))
            self.assertTrue(line["opened"], f"{family}: the run read records yet opened is empty")
            self.assertTrue(all(isinstance(h, str) and h for h in line["opened"]), family)
            self.assertEqual(len(line["opened"]), len(set(line["opened"])), family)
        late = self.lines["late_po"]["opened"]
        self.assertTrue(any(self.oid in h or h == self.oid for h in late) or len(late) > 1, late)

    def test_a_line_that_read_something_but_states_an_empty_set_is_a_typed_failure(self):
        for family, line in self.lines.items():
            kind, why = ps.outcome(dict(line, opened=[]))
            self.assertEqual(kind, "failed", (family, kind, why))
            self.assertIn("opened set", why)
            kind, _ = ps.outcome({k: v for k, v in line.items() if k != "opened"})
            self.assertEqual(kind, "failed", family)
            self.assertNotEqual(ps.outcome(line)[0], "failed", (family, ps.outcome(line)))


if __name__ == "__main__":
    unittest.main()
