"""The flagship path script (``scripts/eval_flagship_path.py``), scored honestly.

The orders and the expected answers come from a raw-capture oracle
(``evals.oracle.po_lateness``: the late lines, governing dates and receipts read from the
captured rows, never through the product). The fixture world (``operator_ui.tests.
case_fixtures``: synthetic rows over injected transports) runs every step through the
console's own path; the scoring rules (recall home, oracle-checked Ask, timing, number-only
landed cost, never-pass margin sample, withheld margin) are asserted at the step boundary
over stub workspaces. Evidence label: isolated-fixture / injected-transport; nothing is
sent and no source system is written.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from evals.oracle import po_lateness as pl
from evals.oracle.keys import FM_PO, PO, PO_LINE, RECEIPT
from ontology import store_ddl
from operating.tests import test_landed_cost_estimate as lce
from operator_ui.tests import case_fixtures as cf
from scripts import flagship_margin as fm
from scripts import eval_flagship_path as fp

AS_OF = date(2026, 10, 20)
ORDER = 'PurchaseOrder:portal.app_purchase_order:{"id":"po-synth-5447"}'
LATE = 'PurchaseOrder:portal.app_purchase_order:{"id":"po-synth-5448"}'
DUE = "2026-09-01"


def scripted_planner(object_id, text="The order's record holds {v}."):
    """Reads the order's card, then answers with one structured claim: a value the card
    returned, by reference (value, citation handle, field)."""
    def planner(request):
        if not request["transcript"]:
            return {"action": "call", "calls": [
                {"tool": "plx.object", "arguments": {"object_id": object_id}}]}, None
        card = request["transcript"][0]["observations"][0]["result"]
        cell = next(v for v in card.get("values") or []
                    if v.get("citation") and v.get("value") is not None)
        value = cell["value"]
        return {"action": "answer", "text": text,
                "claims": [{"id": "v", "citation": cell["citation"]["l1_handle"],
                            "field": cell["citation"]["field_path"],
                            "value": value["untrusted_text"] if isinstance(value, dict)
                            else value}],
                "unknowns": ["the expected date"]}, None
    return planner


class World(unittest.TestCase):
    """The fixture world seeded once with its dates cleared; ``late`` dates one SUBMITTED
    order in the past."""
    late = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.state = cls.root / "state"
        built = cf.build_world

        def dated():
            # Every date set here, never the shared fixture's: no order is late except
            # po-synth-5448 when ``late``, whatever dates the fixture carries.
            world = built()
            for row in world.fake.tables[PO]:
                row["expected_date"] = DUE if cls.late and row["id"] == "po-synth-5448" \
                    else None
                if cls.late and row["id"] == "po-synth-5448":
                    # The raw-capture oracle (evals.oracle.po_lateness, not edited here)
                    # still reads only the derived codes SUBMITTED/PARTIAL as an open
                    # header; the shared fixture holds the real word Pending, so the
                    # order this world scores carries the oracle's word.
                    row["status"] = "SUBMITTED"
            for row in world.fake.tables.get(FM_PO) or ():
                row["datearrive"] = None
            return world
        with mock.patch.object(cf, "build_world", dated):
            cf.seed(cls.state)
            cls.world = cf.build_world()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def workspace(self, name, planner=None):
        live = cf.live_readers(self.world, self.state / cf.LIVE_AUDIENCE_FILE)
        workspace, close, self.projection = fp.workspace_for(
            self.state.resolve(), self.state / cf.MIRROR_AUDIENCE_FILE, "plx",
            [cf.OPERATIONS, cf.PURCHASING], "restricted", work_dir=self.root / name,
            planner=planner, live=live)
        self.addCleanup(close)
        return workspace


class FixtureWorld(World):
    def test_an_order_the_oracle_finds_not_late_fails_home_and_ask(self):
        workspace = self.workspace("steps", scripted_planner(ORDER))
        summary = fp.run(workspace, [ORDER], count=1, as_of=AS_OF, question=fp.QUESTION,
                         identify=False)
        (row,) = summary["orders"]
        steps = row["steps"]
        self.assertEqual(set(steps), set(fp.STEPS))
        # The order is REVISED with no date: not late by the model, so home and Ask fail.
        self.assertEqual(steps["home"]["reason"], "order_not_late_by_oracle")
        self.assertEqual(steps["ask"]["outcome"], "answered")
        self.assertEqual((steps["ask"]["status"], steps["ask"]["reason"]),
                         ("fail", "order_not_late_by_oracle"))
        self.assertEqual(steps["timing"]["status"], "pass")
        # No landed-cost number on the fixture order: naming the missing inputs fails.
        self.assertEqual(steps["landed_cost"]["status"], "fail")
        self.assertEqual(steps["draft"]["status"], "pass")
        self.assertEqual(steps["draft"]["executed"], 0)
        # The fixture PO feeds no customer order on record: the impact chain says where it
        # stops, and the step fails (never an unrelated sample, never a pass).
        self.assertEqual((steps["margin"]["status"], steps["margin"]["reason"],
                          steps["margin"]["fed_by"]),
                         ("fail", "no_sales_order_fed", "impact_chain"))
        self.assertEqual(summary["passed"]["margin"], 0)
        # The oracle read the Portal rows; the fixture holds no BC capture, named unread.
        self.assertEqual(summary["oracle"]["late"], 0)
        self.assertIn("bc_capture", summary["oracle"]["unread"])
        self.assertIsNone(summary["home"]["recall"]["recall"])
        self.assertEqual(summary["margin"]["reason"], "sales_order_not_held")
        # No host serves this directory: the run builds its own projection.
        self.assertEqual((self.projection["source"], self.projection["reason"]),
                         ("private", "no_served_generation"))
        dumped = json.dumps(summary)
        for marker in ("PO5447", "po-synth", "\"claims\"", "draft_texts"):
            self.assertNotIn(marker, dumped)

    def test_a_second_record_reads_only_its_own_follow_ups(self):
        workspace = self.workspace("twice")
        first = fp.draft_step(workspace, ORDER, [], identify=False)
        second = fp.draft_step(workspace, ORDER, [], identify=False)
        self.assertEqual(first["follow_ups"], second["follow_ups"])

    def test_no_late_order_picks_nothing(self):
        summary = fp.run(self.workspace("pick"), [], count=2, as_of=AS_OF,
                         question=fp.QUESTION, identify=True, ask=False)
        self.assertEqual((summary["selected_by"], summary["orders"]), ("lateness_model", []))

    def test_a_home_that_lists_no_late_order_scores_recall_zero(self):
        # The oracle finds a late order the home lists no gap on (and the product does not
        # hold): recall 0 of 1, and every step of that order fails as not held.
        raw = FakeRaw({PO: [portal_po("po-x", "PO-X", "SUBMITTED", DUE)],
                       PO_LINE: [portal_line("pol-x", "po-x", "5", None)]})
        summary = fp.run(self.workspace("recall"), [], count=1, as_of=AS_OF,
                         question=fp.QUESTION, identify=False, ask=False, raw=raw)
        self.assertEqual(summary["home"]["recall"], {"listed": 0, "late": 1, "recall": 0.0})
        (row,) = summary["orders"]
        self.assertEqual({s["reason"] for s in row["steps"].values()}, {"order_not_held"})

    def test_main_without_a_planner_credential_blocks_ask_with_the_setting(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fp.main(["--state-dir", str(self.state),
                            "--mirror-audience", str(self.state / cf.MIRROR_AUDIENCE_FILE),
                            "--principal", cf.PURCHASING, "--order", ORDER,
                            "--as-of", AS_OF.isoformat(),
                            "--host-policy", str(fp.ROOT / "examples" /
                                                 "po_document_chain_host.live-check.json"),
                            "--work-dir", str(self.root / "main")], environ={})
        self.assertEqual(code, 0)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["schema"], fp.SCHEMA)
        self.assertEqual(summary["sent"], 0)
        self.assertEqual((summary["markings"], summary["family"]),
                         (["purchasing_prices"], fp.QUESTION_FAMILY))
        steps = summary["orders"][0]["steps"]
        self.assertEqual(steps["ask"]["status"], "blocked")
        self.assertIn("PLX_CLAUDE_CODE_OAUTH_TOKEN", steps["ask"]["missing_setting"])
        self.assertEqual(steps["timing"]["reason"], "ask_not_run")
        self.assertNotIn("PO5447", out.getvalue())
        self.assertEqual((summary["margin"]["status"], summary["margin"]["reason"]),
                         ("blocked", "sales_order_not_held"))


class LateWorld(World):
    late = True

    def test_the_late_order_is_picked_by_the_model_and_its_ask_scored_by_the_oracle(self):
        # The planner cites the order's own record but names no governing date.
        summary = fp.run(self.workspace("late", scripted_planner(LATE)), [], count=3,
                         as_of=AS_OF, question=fp.QUESTION, identify=True)
        self.assertEqual(summary["selected_by"], "lateness_model")
        self.assertEqual(summary["oracle"]["late"], 1)
        (row,) = summary["orders"]
        self.assertEqual(row["object_id"], LATE)
        steps = row["steps"]
        self.assertEqual(steps["ask"]["oracle"]["governing_dates"], [DUE])
        self.assertEqual(steps["ask"]["status"], "fail")
        self.assertIn(steps["ask"]["reason"], ("late_lines_not_cited",
                                               "governing_date_not_named"))
        recall = summary["home"]["recall"]
        self.assertEqual(recall["late"], 1)
        self.assertEqual(steps["home"]["status"] == "pass", recall["listed"] == 1)

    def test_an_ask_naming_its_family_is_the_plan_s_answer_without_a_planner_call(self):
        """The flagship's Ask names the late-order plan: code states its result whole (its
        cited claims, its gaps as unknowns) and the planner is never asked; the oracle still
        scores it (this world's Portal line holds no date of its own, so no plan entry cites
        it: the step fails on its late line, as it must)."""
        asked = []

        def planner(request):
            asked.append(1)
            return {"action": "abstain", "reason": "data_absent: not asked"}, None
        workspace = self.workspace("family", planner)
        summary = fp.run(workspace, [], count=1, as_of=AS_OF, question=fp.QUESTION,
                         identify=True, family=fp.QUESTION_FAMILY, margin=False)
        ask = summary["orders"][0]["steps"]["ask"]
        self.assertEqual(asked, [])
        self.assertEqual((ask["outcome"], ask["tool_calls"]), ("answered", 2))
        self.assertTrue(ask["late_lines"])
        self.assertIn(("expected date: " + DUE, "api"), {
            (claim["text"], fp.system_of(claim["citations"][0])) for claim in ask["claims"]})
        self.assertTrue(ask["unknown_texts"])
        self.assertEqual((ask["status"], ask["reason"]), ("fail", "late_lines_not_cited"))
        # a plan answer is asked for with its family only; a family alone keeps the planner
        for bad in ({"plan_answer": True}, {"family": fp.QUESTION_FAMILY, "plan_answer": 1}):
            with self.assertRaises(ValueError):
                workspace.ask_run({"object_id": LATE, "question": fp.QUESTION, **bad})
        self.assertEqual(asked, [])
        workspace.ask_run({"object_id": LATE, "question": fp.QUESTION,
                           "family": fp.QUESTION_FAMILY})
        self.assertTrue(asked)


def serve_generation(state: Path) -> None:
    """Build the projection a persistent host serves (``operating.host.build_projection``:
    ``<state>/projection``, a committed generation) over the state's captures."""
    from contextlib import ExitStack

    from ontology import project
    from operating import host
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE

    with ExitStack() as stack:
        sources = host.state_captures(state, stack)
        sources["portal_api"] = stack.enter_context(
            project.MirrorCaptures(state / CASE_RUNTIME_DIR / MIRROR_FILE))
        host.build_projection(state, sources, tenant="plx")


def private_files(state: Path) -> list[tuple]:
    """What a private projection build writes in the case workspace directory (each
    file's name, inode and modification time: a build replaces the file)."""
    folder = state / "case-workspace-live"
    return sorted((p.name, p.stat().st_ino, p.stat().st_mtime_ns) for p in folder.iterdir()
                  if p.name.startswith(("ontology-", ".ontology-", ".plx-projection-"))
                  ) if folder.is_dir() else []


class ServedWorld(World):
    """A state directory a host serves: the flagship reads the served generation,
    read-only and pinned, never a private build; the scoring is the private build's."""
    late = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        serve_generation(cls.state)

    def flagship(self, name):
        summary = fp.run(self.workspace(name, scripted_planner(LATE)), [], count=3,
                         as_of=AS_OF, question=fp.QUESTION, identify=False)
        return [{k: (v["status"], v["reason"]) for k, v in row["steps"].items()
                 if k != "timing"} for row in summary["orders"]], summary

    def test_the_flagship_reads_the_served_generation_and_scores_as_a_private_build(self):
        from ontology.project_incremental import current_generation

        served = current_generation(self.state / "projection")
        before = private_files(self.state)
        steps, summary = self.flagship("served")
        self.assertEqual(self.projection, {"source": "served", "reason": "served_generation",
                                           "generation": served.id, "pinned": True})
        self.assertEqual(private_files(self.state), before)   # nothing projected for it
        self.assertEqual(summary["oracle"]["late"], 1)
        # The same captures built privately (the served generation set aside) score the
        # same per step.
        current = self.state / "projection" / "CURRENT"
        aside = current.with_name("CURRENT.aside")
        current.rename(aside)
        try:
            private, _ = self.flagship("private")
            self.assertEqual(self.projection["source"], "private")
            self.assertEqual(self.projection["reason"], "no_served_generation")
        finally:
            aside.rename(current)
        self.assertEqual(steps, private)

    def test_the_pin_keeps_the_generation_while_the_host_prunes_it(self):
        import shutil

        from operating import case_workspace_live as live_ws

        chosen = live_ws.run_projection(self.state, "plx")
        self.addCleanup(chosen.close)
        generation = self.state / "projection" / "generations" / chosen.generation
        copy = self.root / "generation-copy"
        shutil.copytree(generation, copy)   # restored after: other tests read it
        self.addCleanup(lambda: generation.exists() or shutil.copytree(copy, generation))
        shutil.rmtree(generation)   # the host committed twice and pruned this one
        import sqlite3
        with contextlib.closing(sqlite3.connect(f"file:{chosen.path}?mode=ro", uri=True)) as db:
            digest = db.execute("SELECT value FROM store_meta WHERE key = "
                                "'content_digest'").fetchone()[0]
        self.assertEqual(digest, chosen.content_digest)
        pin = chosen.path
        chosen.close()
        self.assertFalse(pin.exists())

    def test_a_foreign_tenants_decision_is_never_served_and_a_low_disk_builds_nothing(self):
        import sqlite3

        from ontology import reviews
        from operating import case_workspace_live as live_ws

        import types

        from ontology.project_incremental import current_generation

        store = reviews.store_path(self.state)
        with contextlib.closing(sqlite3.connect(store)) as db, db:
            db.execute("CREATE TABLE reviews (tenant TEXT, recorded_at TEXT)")
            db.execute("INSERT INTO reviews VALUES ('plx', '2999-01-01T00:00:00Z')")
        self.addCleanup(store.unlink)
        # The tenant's own decision the served build read none of (the host may serve
        # another tenant), or one recorded after the build started: never served.
        self.assertEqual(live_ws.served_generation(self.state, "plx"),
                         (None, "served_tenant_unproven"))
        served = current_generation(self.state / "projection")
        read = types.SimpleNamespace(store=served.store, report={
            "post_project": {"reviews": {"counts": {"decisions": 1}}}})
        self.assertEqual(live_ws._foreign_or_unproven(self.state, "plx", read),
                         "served_older_than_decisions")
        with contextlib.closing(sqlite3.connect(store)) as db, db:
            db.execute("UPDATE reviews SET recorded_at = '2000-01-01T00:00:00Z'")
        self.assertIsNone(live_ws._foreign_or_unproven(self.state, "plx", read))
        with contextlib.closing(sqlite3.connect(store)) as db, db:
            db.execute("INSERT INTO reviews VALUES ('another-tenant', '2000-01-01T00:00:00Z')")
        before = private_files(self.state)
        self.assertEqual(live_ws.served_generation(self.state, "plx"),
                         (None, "served_tenant_unproven"))
        # It falls back to a private build, which the disk check refuses before it starts.
        with self.assertRaises(live_ws.ProjectionDiskLow):
            live_ws.run_projection(self.state, "plx", disk_free=lambda _path: 1)
        self.assertEqual(private_files(self.state), before)
        # Room at the start, none once it runs: the build stops and leaves no part behind.
        calls = []

        def filling(_path):
            calls.append(1)
            return 10 ** 15 if len(calls) == 1 else 1
        with self.assertRaises(live_ws.ProjectionDiskLow):
            live_ws.run_projection(self.state, "plx", disk_free=filling)
        self.assertGreater(len(calls), 1)
        self.assertEqual(private_files(self.state), before)

    def test_a_stopped_run_leaves_no_pin_behind(self):
        from operating import case_workspace_live as live_ws

        folder = self.state / "case-workspace-live"
        folder.mkdir(exist_ok=True)
        dead = folder / f"{live_ws.PIN_PREFIX}00000001-999999999-deadbeef.sqlite"
        dead.write_bytes(b"")
        chosen = live_ws.run_projection(self.state, "plx")
        self.addCleanup(chosen.close)
        self.assertFalse(dead.exists())
        self.assertTrue(chosen.pinned)


class LandedEstimate(unittest.TestCase):
    """The open, unreceived purchase order of the landed-cost estimate world, answered by
    the real ``plx.investigate`` (``operating.tests.test_landed_cost_estimate``; its world
    and reader are borrowed, not its tests): the actual landed cost is null by design and
    the number is the labelled estimate."""
    PRINCIPAL = lce.InvestigateEstimateTests.PRINCIPAL
    setUpClass = classmethod(lce.InvestigateEstimateTests.setUpClass.__func__)
    tearDownClass = classmethod(lce.InvestigateEstimateTests.tearDownClass.__func__)
    investigate = lce.InvestigateEstimateTests.investigate

    def landed(self, markings):
        result = self.investigate(markings)
        return fp.landed_step(StubWorkspace(investigated={"investigation": result}), "o",
                              identify=False)

    def test_the_labelled_estimate_passes_and_a_withheld_estimate_fails(self):
        out = self.landed(store_ddl.MARKINGS)
        self.assertEqual((out["status"], out["reason"], out["source"], out["estimate"]),
                         ("pass", "landed_cost_shown", "landed_cost_estimate", True))
        blind = self.landed(())
        self.assertEqual(blind["status"], "fail")
        self.assertEqual(blind["reason"], "landed_cost_names_only_missing_inputs")


class FakeRaw:
    def __init__(self, portal, bc=(), confirmed=()):
        self._portal, self._bc, self.unread = portal, list(bc), []
        self.confirmed = set(confirmed)

    def portal(self):
        return copy.deepcopy(self._portal)

    def bc(self):
        return copy.deepcopy(self._bc)

    def confirms(self, handle, numbers):
        return handle in self.confirmed


def portal_po(pid, number, status, expected=None, **custom):
    return {"row": {"id": pid, "po_number": number, "status": status,
                    "expected_date": expected, "custom_fields": custom},
            "citation": f"api://po/{pid}@1"}


def portal_line(lid, pid, quantity, received):
    return {"row": {"id": lid, "purchase_order_id": pid, "quantity": quantity,
                    "quantity_received": received}, "citation": f"api://pol/{lid}@1"}


def bc_po(bid, number, *, received=False, requested=None, status="Open", company="c1"):
    return {"company_id": company, "entity_set": pl.BC_ORDERS, "citation": f"bc://po/{bid}@1",
            "fields": {"id": bid, "number": number, "fullyReceived": received,
                       "status": status, "requestedReceiptDate": requested}}


def bc_line(lid, bid, quantity, received, expected=None, company="c1"):
    return {"company_id": company, "entity_set": pl.BC_LINES, "citation": f"bc://pol/{lid}@1",
            "fields": {"id": lid, "documentId": bid, "quantity": quantity,
                       "receivedQuantity": received, "expectedReceiptDate": expected}}


class Oracle(unittest.TestCase):
    def late(self, portal, bc=()):
        return pl.late_orders(portal, list(bc), AS_OF)

    def test_precedence_fm_then_portal_then_bc_line_then_header(self):
        portal = {PO: [portal_po("p1", "PO#1", "SUBMITTED", "2026-12-01",
                                 fmGeneratedPoId=11, renumberedTo="PO1")],
                  FM_PO: [{"row": {"id": 11, "ponumber": "1", "datearrive": "2026-08-01"},
                           "citation": "api://fm/11@1"}],
                  PO_LINE: [portal_line("l1", "p1", "10", "4")]}
        bc = [bc_po("b1", "PO1", requested="2026-12-31"),
              bc_line("bl1", "b1", "10", "0", expected="2026-12-15")]
        (group,) = self.late(portal, bc)["late"]
        self.assertEqual(group["numbers"], ["1", "PO#1", "PO1"])
        self.assertEqual({(u["citation"], u["due"], u["date_source"])
                          for u in group["late_lines"]},
                         {("api://pol/l1@1", "2026-08-01", "api://fm/11@1"),
                          ("bc://pol/bl1@1", "2026-08-01", "api://fm/11@1")})
        # Without FileMaker's date the Portal date governs (here: not late) ...
        portal[FM_PO] = []
        self.assertEqual(self.late(portal, bc)["late"], [])
        # ... and without both, BC's line date beats its header date.
        portal[PO][0]["row"]["expected_date"] = None
        bc[1]["fields"]["expectedReceiptDate"] = "2026-09-15"
        (group,) = self.late(portal, bc)["late"]
        self.assertEqual({u["due"] for u in group["late_lines"]}, {"2026-09-15"})
        bc[1]["fields"]["expectedReceiptDate"] = "0001-01-01"   # BC's empty date
        self.assertEqual(self.late(portal, bc)["late"], [])

    def test_late_is_judged_on_the_oracles_own_et_day_across_both_dst_changes(self):
        # (due day, the moment in UTC, its ET wall time, late?): the oracle reads the
        # clock itself (pl.business_today), never through the Workbench
        cases = (("2026-10-19", (2026, 10, 20, 1, 0), "21:00 EDT Oct 19", False),
                 ("2026-10-19", (2026, 10, 20, 4, 30), "00:30 EDT Oct 20", True),
                 ("2026-03-07", (2026, 3, 8, 2, 0), "21:00 EST Mar 7", False),
                 ("2026-03-07", (2026, 3, 8, 5, 30), "00:30 EST Mar 8", True),
                 ("2026-03-08", (2026, 3, 9, 1, 0), "21:00 EDT Mar 8", False),
                 ("2026-03-08", (2026, 3, 9, 4, 30), "00:30 EDT Mar 9", True),
                 ("2026-10-31", (2026, 11, 1, 1, 0), "21:00 EDT Oct 31", False),
                 ("2026-10-31", (2026, 11, 1, 4, 30), "00:30 EDT Nov 1", True),
                 ("2026-11-01", (2026, 11, 2, 2, 0), "21:00 EST Nov 1", False),
                 ("2026-11-01", (2026, 11, 2, 5, 30), "00:30 EST Nov 2", True))
        for due, moment, wall, late in cases:
            with self.subTest(due=due, at=wall):
                now = datetime(*moment, tzinfo=timezone.utc)
                as_of = pl.business_today(now)
                self.assertEqual(as_of, date.fromisoformat(due) + timedelta(days=int(late)))
                portal = {PO: [portal_po("p1", "1", "SUBMITTED", due)],
                          PO_LINE: [portal_line("l1", "p1", "10", "0")]}
                found = pl.late_orders(portal, [], as_of)["late"]
                self.assertEqual([u["due"] for g in found for u in g["late_lines"]],
                                 [due] if late else [])
        with self.assertRaisesRegex(ValueError, "aware"):
            pl.business_today(datetime(2026, 10, 20, 1, 0))
        with self.assertRaisesRegex(ValueError, "dst_rule"):   # its rule is 2007's
            pl.business_today(datetime(2006, 7, 1, tzinfo=timezone.utc))

    def test_open_received_and_closed(self):
        lines = [portal_line("l1", "p1", "10", "10")]
        self.assertEqual(self.late({PO: [portal_po("p1", "1", "SUBMITTED", DUE)],
                                    PO_LINE: lines}, [])["late"], [])   # all received
        for status in ("DRAFT", "RECEIVED", "CANCELLED", "REVISED"):
            self.assertEqual(self.late({PO: [portal_po("p1", "1", status, DUE)],
                                        PO_LINE: [portal_line("l1", "p1", "10", None)]},
                                       [])["late"], [], status)
        # A BC twin that says fully received closes the order the Portal says is open.
        portal = {PO: [portal_po("p1", "1", "SUBMITTED", DUE, renumberedTo="B1")],
                  PO_LINE: [portal_line("l1", "p1", "10", None)]}
        self.assertEqual(self.late(portal, [bc_po("b1", "B1", received=True)])["late"], [])
        self.assertEqual(len(self.late(portal, [bc_po("b1", "B1")])["late"]), 1)

    def test_a_number_text_twin_is_never_joined(self):
        # Same number, no renumberedTo: two orders, each judged alone (a lead, not a join).
        portal = {PO: [portal_po("p1", "PO7", "SUBMITTED", DUE)],
                  PO_LINE: [portal_line("l1", "p1", "1", None)]}
        out = self.late(portal, [bc_po("b1", "PO7", requested="2026-12-01")])
        self.assertEqual(out["counts"]["groups"], 2)
        (group,) = out["late"]
        self.assertFalse(group["bc"])
        # Two Portal orders claiming one BC number: ambiguous, no join.
        portal[PO].append(portal_po("p2", "PO8", "SUBMITTED", DUE, renumberedTo="PO7"))
        portal[PO][0]["row"]["custom_fields"] = {"renumberedTo": "PO7"}
        out = self.late(portal, [bc_po("b1", "PO7")])
        self.assertEqual(out["counts"]["ambiguous"], 2)
        self.assertTrue(all(not g["bc"] for g in out["late"]))

    def test_cardinality_no_order_lost_or_doubled(self):
        for n in (1, 10, 60):
            portal = {PO: [portal_po(f"p{i}", f"N{i}", "SUBMITTED", DUE) for i in range(n)],
                      PO_LINE: [portal_line(f"l{i}", f"p{i}", "2", None) for i in range(n)],
                      RECEIPT: []}
            out = self.late(portal)
            self.assertEqual(len(out["late"]), n)
            self.assertEqual(len({tuple(g["numbers"]) for g in out["late"]}), n)


def bc_reference(bid, number, your_reference, company="c1"):
    return {"company_id": company, "entity_set": pl.BC_REFERENCES,
            "citation": f"bc://production/{company}/{pl.BC_REFERENCES}/{bid}@1",
            "fields": {"No": number, "Your_Reference": your_reference}}


def vendor_map(supplier, bc_vendor, company="c1"):
    return {"row": {"entity_type": "vendor", "source_system": "portal", "status": "LINKED",
                    "source_id": supplier, "bc_number": bc_vendor, "bc_company_id": company},
            "citation": f"api://map/{supplier}@1"}


def po_map(portal_id, bc_id, *, company="c1", status="LINKED", environment="Production",
           entity_type="purchaseOrder"):
    return {"row": {"entity_type": entity_type, "source_system": "portal", "status": status,
                    "source_id": portal_id, "bc_system_id": bc_id, "bc_company_id": company,
                    "bc_environment": environment},
            "citation": f"api://map/{portal_id}-{bc_id}@1"}


class EntityMapJoin(unittest.TestCase):
    """The oracle groups a Portal order and the BC order the Portal's own entity map says
    it was posted as into one order, by its own reading of the raw map rows (decision
    2026-10-28-oracle-entity-map-po-groups): one late order is never counted twice."""

    def world(self, maps, *, n=1, extra_bc=()):
        portal = {PO: [portal_po(f"p{i}", f"PO#{4600 + i}", "SUBMITTED", None)
                       for i in range(n)],
                  PO_LINE: [portal_line(f"l{i}", f"p{i}", "5", None) for i in range(n)],
                  pl.ENTITY_MAP: list(maps)}
        bc = [bc_po(f"B-{i}", f"PO1000{i}", requested="2026-09-01") for i in range(n)]
        bc += [bc_line(f"bl{i}", f"B-{i}", "5", "0") for i in range(n)]
        return pl.late_orders(portal, bc + list(extra_bc), AS_OF)

    def test_a_posted_order_is_one_late_group_with_both_numbers(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                out = self.world([po_map(f"p{i}", f"b-{i}") for i in range(n)], n=n)
                self.assertEqual((out["counts"]["groups"], len(out["late"])), (n, n))
                self.assertEqual(out["counts"]["entity_map_joined"], n)
                for group in out["late"]:
                    self.assertEqual({h["system"] for h in group["headers"]}, {"portal", "bc"})
                    self.assertEqual(len(group["numbers"]), 2)
        # without the map: the same order is two groups (the Portal one undated here)
        out = self.world([])
        self.assertEqual((out["counts"]["groups"], len(out["late"])), (2, 1))

    def test_a_row_that_is_not_a_linked_production_po_mapping_joins_nothing(self):
        for kw in ({"status": "ARCHIVED"}, {"environment": "UAT"},
                   {"entity_type": "item"}, {"company": "c2"}):
            with self.subTest(**kw):
                out = self.world([po_map("p0", "b-0", **kw)])
                self.assertEqual(out["counts"]["groups"], 2)
                self.assertEqual(out["counts"]["entity_map_joined"], 0)

    def test_two_rows_for_one_end_are_a_conflict_never_a_guess(self):
        # one Portal order mapped to two BC orders, and two Portal orders to one BC order
        for maps in ([po_map("p0", "b-0"), po_map("p0", "b-1")],
                     [po_map("p0", "b-0"), po_map("p1", "b-0")]):
            with self.subTest(maps=[m["citation"] for m in maps]):
                out = self.world(maps, n=2)
                self.assertEqual((out["counts"]["entity_map_joined"],
                                  out["counts"]["entity_map_conflict"]), (0, 2))
                self.assertEqual(out["counts"]["groups"], 4)


class YourReferenceJoin(unittest.TestCase):
    """The oracle joins a BC order to the Portal order its Your_Reference names only when
    the product's own rule (``evals.oracle.po_reference.decide``) finds the link firm."""

    def world(self, *, portal_date="2026-03-02", order_date="2026-03-02", supplier=None,
              bc_vendor=None, mapped=None, extra_portal=(), renumbered=None, extra_bc=(), n=1):
        pos, lines, refs, orders, bc_lines = [], [], [], [], []
        for i in range(n):
            row = portal_po(f"p{i}", f"PO10600{i}", "SUBMITTED", None,
                            fmPoNumber=f"PO#700{i}", source="etl-silver-to-gold",
                            **({"renumberedTo": renumbered} if renumbered else {}))
            row["row"].update(date_generated=portal_date, supplier_id=supplier)
            pos.append(row)
            lines.append(portal_line(f"l{i}", f"p{i}", "5", None))
            order = bc_po(f"b{i}", f"BC{i}", requested="2026-09-01")
            order["fields"].update(orderDate=order_date, vendorNumber=bc_vendor)
            orders.append(order)
            bc_lines.append(bc_line(f"bl{i}", f"b{i}", "5", "0"))
            refs.append(bc_reference(f"b{i}", f"BC{i}", f" po#700{i}"))
        portal = {PO: pos + list(extra_portal), PO_LINE: lines,
                  pl.ENTITY_MAP: [vendor_map(supplier, m) for m in mapped or ()]}
        return pl.late_orders(portal, orders + bc_lines + refs + list(extra_bc), AS_OF)

    def joined(self, out):
        return [g for g in out["groups"] if g["bc"] and any(
            h["system"] == "portal" for h in g["headers"])]

    def test_a_firm_reference_joins_and_its_fm_number_is_the_group_s(self):
        out = self.world()
        (group,) = self.joined(out)
        self.assertEqual(out["counts"]["groups"], 1)
        self.assertIn("PO#7000", group["numbers"])   # what a document names
        self.assertTrue(fp.names_number("Re: PO#7000 delivery", group["numbers"]))
        self.assertEqual((out["counts"]["your_reference_firm"],
                          out["counts"]["your_reference_firm_by_order_date"],
                          out["counts"]["your_reference_lead"]), (1, 1, 0))
        # the Portal's supplier mapped to the order's BC vendor corroborates on its own
        out = self.world(order_date="2026-06-01", supplier="s1", bc_vendor="V1", mapped=["v1"])
        self.assertEqual(len(self.joined(out)), 1)
        self.assertEqual(out["counts"]["your_reference_firm_by_supplier"], 1)

    def test_a_lead_never_joins(self):
        cases = {
            # dates 91 days apart, no supplier map: uncorroborated
            "uncorroborated": dict(order_date="2026-06-01"),
            # the map names another BC vendor: a conflict, though the dates agree
            "supplier_conflict": dict(supplier="s1", bc_vendor="V1", mapped=["V2"]),
            # a second Portal order holds the same FM number
            "ambiguous_fm": dict(extra_portal=[portal_po("px", "PO9", "SUBMITTED", None,
                                                         fmPoNumber="PO#7000",
                                                         source="etl-silver-to-gold")]),
            # the Portal order is renumbered to another BC order already
            "conflict": dict(renumbered="BCX", extra_bc=[bc_po("bx", "BCX")]),
        }
        for code, world in cases.items():
            with self.subTest(code=code):
                out = self.world(**world)
                self.assertFalse(any(h["citation"] == "bc://po/b0@1" for g in self.joined(out)
                                     for h in g["headers"]))
                self.assertEqual(out["counts"]["your_reference_firm"], 0)
                self.assertEqual(out["counts"][f"your_reference_lead_{code}"],
                                 2 if code == "ambiguous_fm" else 1)
        # a Portal-minted order never qualifies, not even as a lead
        portal = {PO: [portal_po("p0", "PO1", "SUBMITTED", None, fmPoNumber="PO#7000",
                                 source="portal-generate-po")]}
        out = pl.late_orders(portal, [bc_po("b0", "BC0"), bc_reference("b0", "BC0", "PO#7000")],
                             AS_OF)
        self.assertEqual(self.joined(out), [])
        self.assertEqual((out["counts"]["your_reference_firm"],
                          out["counts"]["your_reference_lead"]), (0, 0))

    def test_a_reference_that_is_not_the_order_s_number_or_blank_joins_nothing(self):
        portal = {PO: [portal_po("p0", "PO1", "SUBMITTED", None, fmPoNumber="PO#7000",
                                 source="etl-silver-to-gold")]}
        for ref in (bc_reference("b0", "OTHER", "PO#7000"), bc_reference("b0", "BC0", "  "),
                    bc_reference("b0", "BC0", "7000")):
            with self.subTest(ref=ref["fields"]):
                out = pl.late_orders(portal, [bc_po("b0", "BC0"), ref], AS_OF)
                self.assertEqual(self.joined(out), [])

    def test_the_oracle_s_rule_fails_closed(self):
        # the product holds the same rule (ontology.tests.test_bc_po_your_reference
        # RuleParityTests runs both copies over every input combination)
        from evals.oracle import po_reference
        self.assertIs(pl.po_reference, po_reference)
        # unknown or unexpected inputs never make a link firm
        firm = dict(portal_matches=1, bc_claimants=1, authority=True, other_partner=False,
                    supplier=True, order_days_apart=0)
        self.assertEqual(po_reference.decide(**firm).tier, po_reference.FIRM)
        for name, value in (("portal_matches", "1"), ("portal_matches", 0),
                            ("bc_claimants", None), ("bc_claimants", 2), ("authority", "yes"),
                            ("other_partner", None), ("supplier", False)):
            with self.subTest(**{name: value}):
                self.assertNotEqual(po_reference.decide(**{**firm, name: value}).tier,
                                    po_reference.FIRM)
        for supplier, days in ((None, None), ("yes", 0.5), (None, True), (None, -1), (None, 8)):
            with self.subTest(supplier=supplier, days=days):
                verdict = po_reference.decide(**{**firm, "supplier": supplier,
                                                 "order_days_apart": days})
                self.assertEqual((verdict.tier, verdict.code),
                                 (po_reference.LEAD, "uncorroborated"))

    def test_cardinality_no_order_lost_doubled_or_joined_twice(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                out = self.world(n=n)
                self.assertEqual(out["counts"]["groups"], n)
                self.assertEqual(len(self.joined(out)), n)
                self.assertEqual(len(out["late"]), n)
                self.assertEqual(len({h["citation"] for g in out["groups"]
                                      for h in g["headers"]}), 2 * n)


def group(*, bc=True):
    lines = [{"system": "bc", "citation": "bc://pol/l1@1", "due": DUE,
              "date_source": "bc://pol/l1@1", "line": True}]
    records = ["bc://po/b1@1", "bc://pol/l1@1", "api://po/p1@1"]
    return {"numbers": ["PO5"], "headers": [{"system": "bc", "citation": "bc://po/b1@1"}],
            "late_lines": lines, "records": records if bc else records[2:],
            "governing_dates": [DUE], "receipts": [], "bc": bc}


def answered(*claims):
    return {"outcome": "answered", "reason": None, "withheld": 0,
            "claims": [{"text": text, "citations": list(handles)} for text, handles in claims],
            "loop": {"unsupported_claims": [], "unknowns": [], "tool_calls": [{}]}}


class StubWorkspace:
    """The workspace calls the steps make, with answers set per test."""

    def __init__(self, *, ask=None, proposal=None, follow_ups=(), investigated=None,
                 around=(), families=None):
        self.asked, self.proposal, self.recorded_follow_ups = ask, proposal, follow_ups
        self.investigated, self.around = investigated, list(around)
        self.families = dict(families or {})   # family -> its answer (else ``investigated``)
        self.follow_ups = True
        self.asked_families: list[tuple[str, str]] = []

    def ask_run(self, request):
        if self.asked is None:
            raise LookupError("ask_not_served")
        return self.asked

    def investigate(self, family, root):
        self.family = family
        self.asked_families.append((family, root))
        return self.families.get(family, self.investigated)

    def record_on_case(self, request):
        return {"recorded": [{"record_id": "G1", "outcome": "recorded"}],
                "case": {"follow_ups": list(self.recorded_follow_ups), "drafts": []}}

    def case_records(self, case_id):
        if case_id != "C1":
            raise LookupError("case_not_found")
        return {"case": {"drafts": list(getattr(self, "stored", ()))}}

    def follow_up(self, request):
        return {"proposal": self.proposal}

    def _call(self, tool, arguments):
        if tool == "plx.around":
            return {"objects": self.around}
        if tool == "plx.find":
            return {"candidates": [{"object": item} for item in self.around]}
        return {}


def ask(workspace, late, *, others=(), confirmed=("graph-mail://m@2",)):
    return fp.ask_step(workspace, "o", fp.QUESTION, late=late, others=set(others),
                       confirm=lambda handle, numbers: handle in confirmed, identify=False)


class Steps(unittest.TestCase):
    MAIL = "graph-mail://m@2"
    LINE = "bc://pol/l1@3"

    def test_an_answer_citing_two_systems_with_the_wrong_date_fails(self):
        wrong = StubWorkspace(ask=answered(
            ("Line 1 was due 2026-09-02.", [self.LINE]),
            ("The supplier mail names PO5.", [self.MAIL])))
        out = ask(wrong, group())
        self.assertEqual((out["status"], out["reason"]), ("fail", "governing_date_not_named"))
        self.assertEqual(out["systems_cited"], {"bc": 1, "graph-mail": 1})
        right = StubWorkspace(ask=answered(
            ("Line 1 was due September 1, 2026.", [self.LINE]),
            ("The supplier mail names PO5.", [self.MAIL])))
        self.assertEqual(ask(right, group())["reason"],
                         "names_late_lines_and_date_cross_system")

    def test_the_second_source_must_name_the_number_and_the_lines_must_be_cited(self):
        claims = (("Line 1 was due 2026-09-01.", [self.LINE]), ("A mail.", [self.MAIL]))
        unconfirmed = ask(StubWorkspace(ask=answered(*claims)), group(), confirmed=())
        self.assertEqual(unconfirmed["reason"], "lead_not_worded_as_lead")
        worded = ask(StubWorkspace(ask=answered(claims[0], ("A lead mail.", [self.MAIL]))),
                     group(), confirmed=())
        self.assertEqual(worded["reason"], "no_second_source_cited")
        header_only = ask(StubWorkspace(ask=answered(
            ("Due 2026-09-01.", ["bc://po/b1@1"]), ("Mail names PO5.", [self.MAIL]))), group())
        self.assertEqual(header_only["reason"], "late_lines_not_cited")
        # A record of another order (the number-text twin is its own order) is a lead.
        twin = ask(StubWorkspace(ask=answered(
            claims[0], ("Mail names PO5.", [self.MAIL]), ("BC also holds it.", ["bc://po/t@1"]))),
            group(), others=["bc://po/t"])
        self.assertEqual(twin["reason"], "lead_not_worded_as_lead")
        portal_only = ask(StubWorkspace(ask=answered(
            ("Due 2026-09-01.", ["bc://pol/l1@1"]), ("Mail names PO5.", [self.MAIL]))),
            group(bc=False))
        self.assertEqual(portal_only["reason"], "no_bc_record_cited")
        self.assertEqual(ask(StubWorkspace(ask=answered(*claims)), None)["reason"],
                         "order_not_late_by_oracle")
        abstained = dict(answered(), outcome="abstained", reason="no_evidence")
        self.assertEqual(ask(StubWorkspace(ask=abstained), group())["reason"],
                         "ask_abstained:no_evidence")

    def test_a_document_confirms_only_an_exact_order_number(self):
        self.assertTrue(fp.names_number("Re: PO5 ships Friday", ["PO5"]))
        # One order whatever its written form, either way round, in any case.
        for form in ("PO#4643", "PO 4643", "PO4643", "PO-4643", "po # 4643", "PO#04643"):
            self.assertTrue(fp.names_number(f"Re: {form}, update please", ["PO#4643"]), form)
            self.assertTrue(fp.names_number("Re: PO#4643.", [form]), form)
        self.assertTrue(fp.names_number("DPO100001 shipped", ["DPO100001"]))
        for text, numbers in (("Re: PO55 ships", ["PO5"]), ("2 of line 1", ["1", "2"]),
                              ("XPO5", ["PO5"]), ("PO#46431 ships", ["PO#4643"]),
                              ("PO#4643-FM12 ships", ["PO#4643"]),
                              ("DPO4643 ships", ["PO#4643"]),
                              # a bare number never names an order, whatever the order holds
                              ("4643 units of line 2", ["PO#4643"]),
                              ("order 40017 is late", ["40017"]),
                              ("PO 40017 is late", ["40017"])):
            self.assertFalse(fp.names_number(text, numbers), text)

    TEAMS = "teams-chat://19%3Achat/m1@v1"

    def test_a_teams_message_naming_the_order_is_a_second_source(self):
        line = ("Line 1 was due 2026-09-01.", [self.LINE])
        out = ask(StubWorkspace(ask=answered(line, ("A chat asks about PO5.", [self.TEAMS]))),
                  group(), confirmed=(self.TEAMS,))
        self.assertEqual((out["reason"], out["second_sources"]),
                         ("names_late_lines_and_date_cross_system", 1))
        self.assertEqual(out["systems_cited"], {"bc": 1, "teams-chat": 1})
        # one that does not name the number is a lead, never a second source
        out = ask(StubWorkspace(ask=answered(line, ("A lead chat.", [self.TEAMS]))), group(),
                  confirmed=())
        self.assertEqual((out["reason"], out["second_sources"]), ("no_second_source_cited", 0))

    def test_a_lead_the_answer_marks_counts_only_when_worded_as_a_lead(self):
        line = ("Line 1 was due 2026-09-01.", [self.LINE])

        def marked(text):
            reply = answered(line, (text, [self.MAIL]))
            reply["claims"][1]["value_claims"] = [{"citation": self.MAIL, "lead": True}]
            return ask(StubWorkspace(ask=reply), group())
        self.assertEqual(marked("The supplier mail names PO5.")["reason"],
                         "lead_not_worded_as_lead")
        worded = marked("The supplier mail names PO5 (lead: link not verified).")
        self.assertEqual((worded["reason"], worded["second_sources"]),
                         ("names_late_lines_and_date_cross_system", 1))
        # a firm one counts plainly
        plain = ask(StubWorkspace(ask=answered(line, ("The mail names PO5.", [self.MAIL]))),
                    group())
        self.assertEqual(plain["reason"], "names_late_lines_and_date_cross_system")

    def test_a_teams_message_is_read_through_the_runtime_s_gates_and_fails_closed(self):
        """``RawCaptures.confirms`` reads a cited Teams message only for a reader the live
        audience grants Teams and through the runtime's read (its membership check and the
        people exclusion); a refusal, a moved revision or no grant is unconfirmed."""
        class Audience:
            def __init__(self, teams):
                self.teams = teams

            def may_read_any(self, actor, prefix):
                return self.teams and prefix == "teams:"

        class Runtime:
            def __init__(self, teams, view):
                self.live = mock.Mock(audience=Audience(teams))
                self.view, self.reads = view, []

            def employee_gate(self, actor):
                return actor

            def claim_row(self, handle, actor):
                self.reads.append(handle)
                if isinstance(self.view, Exception):
                    raise self.view
                return self.view

        row = {"row": {"body_text": "Can you chase PO 5 for an update?"},
               "changed_since": False}
        for teams, view, expect, reads in (
                (True, row, True, 1), (False, row, False, 0),
                (True, PermissionError("capture_citation_unresolvable"), False, 1),
                (True, {**row, "changed_since": True}, False, 1),
                (True, {"row": {"body_text": "Can you chase 5?"}}, False, 1)):
            runtime = Runtime(teams, view)
            raw = fp.RawCaptures(runtime, actor=object())
            self.assertIs(raw.confirms(self.TEAMS, ["PO5"]), expect, (teams, view))
            self.assertEqual(len(runtime.reads), reads, (teams, view))

    def test_the_flagship_reader_holds_the_markings_its_host_grants_and_no_more(self):
        """The flagship host config grants purchasing_prices as the real host does
        (examples/host.live-check.json): to staff:purchasing only, finance to nobody. The
        reader gets exactly what its principals are granted; an invalid grant refuses the
        run."""
        flagship = fp.ROOT / "examples/po_document_chain_host.live-check.json"
        real = json.loads((fp.ROOT / "examples/host.live-check.json").read_text("utf-8"))
        self.assertEqual(json.loads(flagship.read_text("utf-8"))["marking_grants"],
                         real["marking_grants"])
        self.assertEqual(fp.granted_markings(flagship, ["staff:purchasing"]),
                         {"purchasing_prices"})
        for principals in (["staff:operations"], ["staff:finance"], []):
            self.assertEqual(fp.granted_markings(flagship, principals), set(), principals)
        with tempfile.TemporaryDirectory() as tmp:
            for grants in ({"purchasing_prices": ["*"]}, {"no_such_marking": ["staff:x"]},
                           ["purchasing_prices"]):
                path = Path(tmp) / "host.json"
                path.write_text(json.dumps({"marking_grants": grants}), "utf-8")
                with self.assertRaises(ValueError):
                    fp.granted_markings(path, ["staff:purchasing"])
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit):
                    fp.main(["--state-dir", tmp, "--mirror-audience", str(path),
                             "--principal", "staff:purchasing", "--host-policy", str(path)],
                            environ={})

    def test_a_25_second_ask_fails_timing(self):
        self.assertEqual(fp.timing_step({"status": "pass", "seconds": 25.0})["reason"],
                         "ask_over_20_seconds")
        self.assertEqual(fp.timing_step({"status": "fail", "seconds": 25.0})["status"], "fail")
        self.assertEqual(fp.timing_step({"status": "fail", "seconds": 20.0})["status"], "pass")
        # The scorer's own document reads never count against the Ask.
        self.assertEqual(fp.timing_step({"status": "fail", "seconds": 25.0,
                                         "ask_seconds": 3.0})["status"], "pass")
        self.assertEqual(fp.timing_step({"status": "fail", "seconds": 25.0,
                                         "ask_seconds": 25.0})["status"], "fail")
        self.assertEqual(fp.timing_step({"status": "blocked", "reason": "x"})["status"],
                         "blocked")

    def test_landed_cost_passes_only_with_a_number(self):
        gaps = {"investigation": {"answer": [], "gaps": [{"reason": "no_freight_cost"}]}}
        out = fp.landed_step(StubWorkspace(investigated=gaps), "o", identify=False)
        self.assertEqual((out["status"], out["reason"]),
                         ("fail", "landed_cost_names_only_missing_inputs"))
        for value, verdict in (("1250.40", "pass"), ("unknown", "fail"), (True, "fail")):
            shown = {"investigation": {"answer": [
                {"kind": "computed", "name": "landed_cost", "value": value}], "gaps": []}}
            out = fp.landed_step(StubWorkspace(investigated=shown), "o", identify=False)
            self.assertEqual(out["status"], verdict, value)
        actual = fp.landed_step(StubWorkspace(investigated={"investigation": {
            "answer": [{"kind": "computed", "name": "landed_cost", "value": "3"}]}}),
            "o", identify=False)
        self.assertEqual((actual["status"], actual["estimate"]), ("pass", False))
        unlabelled = fp.landed_step(StubWorkspace(investigated={"investigation": {"answer": [
            {"kind": "computed", "name": "landed_cost", "value": None},
            {"kind": "computed", "name": "landed_cost_estimate", "value": "9"}]}}),
            "o", identify=False)
        self.assertEqual(unlabelled["reason"], "landed_cost_estimate_not_labelled")
        # The walkthrough shows the amount in cents; the entry keeps its precision.
        long_tail = fp.landed_step(StubWorkspace(investigated={"investigation": {"answer": [
            {"kind": "computed", "name": "landed_cost_estimate", "currency": "CAD",
             "value": "371.199420010000000000000000101", "steps": ["ESTIMATE x"]}]}}),
            "o", identify=True)
        self.assertEqual((long_tail["status"], long_tail["landed_cost_shown"],
                          long_tail["landed_cost_estimate"]),
                         ("pass", "371.20 CAD", "371.199420010000000000000000101"))

    def test_the_margin_is_the_first_order_the_impact_chain_says_the_po_feeds(self):
        """No direct link type: the margin step asks the impact chain (firm links only,
        the entity map among them) and takes the order it ranks first; the margin is the
        order_margin answer for that order, finance-gated. No order fed fails with where
        the walk stopped, never a pass and never an unrelated sample."""
        self.assertEqual(fp.feeds_links(), [])   # no direct purchase-order -> sales-order link
        shown = {"investigation": {"answer": [
            {"kind": "computed", "name": "order_margin", "value": "10", "basis": ["x"]},
            {"kind": "property", "name": "n", "citation": {"l1_handle": "bc://so/1@1"}}],
            "gaps": []}}
        impact = {"investigation": {"answer": [
            {"kind": "computed", "name": "impact_2", "steps": ["chain from o to SalesOrder:b"]},
            {"kind": "computed", "name": "impact_1", "steps": ["chain from o to SalesOrder:a"]},
            {"kind": "computed", "name": "impact", "value": 2, "steps": []}], "gaps": []}}
        ws = StubWorkspace(families={"impact_chain": impact, fm.FAMILY: shown})
        fed = fp.margin_step(ws, "o", feeds=[], identify=True)
        self.assertEqual((fed["status"], fed["reason"], fed["fed_by"], fed["orders_fed"]),
                         ("pass", "margin_shown", "impact_chain", 2))
        self.assertEqual(ws.asked_families, [("impact_chain", "o"),
                                             (fm.FAMILY, "SalesOrder:a")])
        self.assertEqual(fed["object_id"], "SalesOrder:a")
        # A reader without the finance marking: blocked with the typed gap, no figure.
        withheld = {"investigation": {"answer": [
            {"kind": "computed", "name": "order_margin", "value": None}],
            "gaps": [{"reason": "marking_withheld", "subject": "order_margin"}]}}
        gated = fp.margin_step(StubWorkspace(families={"impact_chain": impact,
                                                       fm.FAMILY: withheld}),
                               "o", feeds=[], identify=False)
        self.assertEqual((gated["status"], gated["reason"]), ("blocked", "margin_withheld"))
        # The walk reaches no order (a lead-only item twin): a fail naming the stop.
        stopped = {"investigation": {"answer": [
            {"kind": "computed", "name": "impact", "value": 0, "steps": []}],
            "gaps": [{"reason": "link_lead_only", "subject": "Product:x"}]}}
        out = fp.margin_step(StubWorkspace(families={"impact_chain": stopped}), "o",
                             feeds=[], identify=False)
        self.assertEqual((out["status"], out["reason"], out["impact_stops"]),
                         ("fail", "no_sales_order_fed", ["link_lead_only"]))
        # A walk the step budget cut (live PO100035 shape, 128 of 128 steps): still a
        # fail, and it says the walk was cut, so no order fed is never read as a data fact
        cut = {"investigation": dict(stopped["investigation"], completeness={
            "complete": False, "truncated": True, "reason": "step_budget"})}
        out = fp.margin_step(StubWorkspace(families={"impact_chain": cut}), "o",
                             feeds=[], identify=False)
        self.assertEqual((out["status"], out["reason"], out["impact_cut"]),
                         ("fail", "no_sales_order_fed", "step_budget"))
        fitted = {"investigation": dict(stopped["investigation"], completeness={
            "complete": False, "truncated": True, "reason": "result_too_large"})}
        out = fp.margin_step(StubWorkspace(families={"impact_chain": fitted}), "o",
                             feeds=[], identify=False)
        self.assertNotIn("impact_cut", out)
        # No impact answer at all (not served): a fail, never a pass.
        out = fp.margin_step(StubWorkspace(), "o", feeds=[], identify=False)
        self.assertEqual((out["status"], out["reason"]), ("fail", "no_sales_order_fed"))

    def test_an_unrelated_margin_sample_is_not_a_pass(self):
        shown = {"investigation": {"answer": [
            {"kind": "computed", "name": "order_margin", "value": "10", "basis": ["x"]},
            {"kind": "property", "name": "n", "citation": {"l1_handle": "bc://so/1@1"}}],
            "gaps": []}}
        so = {"object_id": "SalesOrder:1", "object_type": "SalesOrder"}
        sample = fm.margin_sample(StubWorkspace(investigated=shown, around=[so]))
        self.assertEqual(sample["status"], "unrelated_sample")
        self.assertEqual((sample["status"], sample["sample"]["reason"]),
                         ("unrelated_sample", "margin_shown"))
        self.assertEqual(fm.margin_sample(StubWorkspace())["reason"], "sales_order_not_held")
        # The purchase order's own fed customer order, once a link says so, can pass.
        fed = fp.margin_step(StubWorkspace(investigated=shown, around=[so]), "o",
                             feeds=["PurchaseOrder.feeds"], identify=False)
        self.assertEqual((fed["status"], fed["reason"]), ("pass", "margin_shown"))
        two = fp.margin_step(StubWorkspace(around=[so, dict(so, object_id="SalesOrder:2")]),
                             "o", feeds=["PurchaseOrder.feeds"], identify=False)
        self.assertEqual(two["reason"], "sales_order_fed_ambiguous")

    def test_a_finance_withheld_reader_gets_a_typed_gap_never_a_figure(self):
        withheld = {"investigation": {"answer": [
            {"kind": "computed", "name": "order_margin", "value": None}],
            "gaps": [{"reason": "marking_withheld", "subject": "order_margin"}]}}
        out = fm.answer_step(withheld, identify=True)
        self.assertEqual((out["status"], out["reason"], out["gap"]),
                         ("blocked", "margin_withheld", "marking_withheld"))
        self.assertEqual(set(out["figures"].values()), {None})
        leaked = copy.deepcopy(withheld)
        leaked["investigation"]["answer"][0]["value"] = "12.00"
        self.assertEqual(fm.answer_step(leaked, identify=False)["status"], "fail")
        gaps_only = {"investigation": {"answer": [], "gaps": [{"reason": "no_cost"}]}}
        self.assertEqual(fm.answer_step(gaps_only, identify=False)["reason"],
                         "margin_names_only_missing_inputs")

    def test_home_step_and_recall(self):
        late = [group()]
        self.assertEqual(fp.home_recall(late, {}), {"listed": 0, "late": 1, "recall": 0.0})
        self.assertEqual(fp.home_recall(late, {"bc://po/b1": "o"})["recall"], 1.0)
        self.assertEqual(fp.home_step(late[0], {}, {"gaps": []})["reason"],
                         "late_order_not_on_home")
        self.assertEqual(fp.home_step(late[0], {"bc://po/b1": "o"}, {})["reason"],
                         "late_order_listed")

    def test_an_executed_or_approved_follow_up_fails_the_draft_step(self):
        gap = {"follow_up": {"who_can_answer": ["PROC"]}}
        cited = [{"record_id": "G1", "who_can_answer": ["PROC"],
                  "citations": [{"status": "resolved"}]}]
        for flag in ("executed", "approved"):
            stub = StubWorkspace(proposal={flag: True, "cited": True}, follow_ups=cited)
            self.assertEqual(fp.draft_step(stub, "o", [gap], identify=False)["reason"],
                             "follow_up_executed")
        stub = StubWorkspace(proposal={"cited": True}, follow_ups=cited)
        self.assertEqual(fp.draft_step(stub, "o", [gap], identify=False)["reason"],
                         "follow_up_recorded_not_sent")
        draft = {"status": "draft_not_sent", "sent": False, "citations": ["bc://l@1"],
                 "facts": [{"fact": "expected_receipt"}], "text": "Draft supplier message",
                 "record_id": "F1"}
        stub = StubWorkspace(proposal={"cited": True, "draft": draft, "case_id": "C1"},
                             follow_ups=cited)
        # The supplier draft passes only when the case store reads it back as recorded.
        for stored, unread in (([], {"draft_not_on_case": 1}),
                               ([{"record_id": "F1", "statement": "Other text"}],
                                {"draft_text_differs": 1}),
                               ([{"record_id": "F2", "statement": draft["text"]}],
                                {"draft_not_on_case": 1})):
            stub.stored = stored
            result = fp.draft_step(stub, "o", [gap], identify=False)
            self.assertEqual((result["reason"], result["supplier_drafts"],
                              result["supplier_drafts_not_read_back"]),
                             ("follow_up_recorded_not_sent", 0, unread))
        stub.stored = [{"record_id": "F1", "statement": draft["text"]}]
        result = fp.draft_step(stub, "o", [gap], identify=False)
        self.assertEqual((result["reason"], result["supplier_draft_facts"],
                          result["supplier_drafts_not_read_back"]),
                         ("supplier_draft_recorded_not_sent", 1, {}))
        lost = StubWorkspace(proposal={"cited": True, "draft": draft, "case_id": "C9"},
                             follow_ups=cited)
        self.assertEqual(fp.draft_step(lost, "o", [gap], identify=False)[
            "supplier_drafts_not_read_back"], {"case_not_read": 1})
        stub = StubWorkspace(proposal={"cited": True, "draft": dict(draft, sent=True)},
                             follow_ups=cited)
        self.assertEqual(fp.draft_step(stub, "o", [gap], identify=False)["reason"],
                         "follow_up_executed")


if __name__ == "__main__":
    unittest.main()
