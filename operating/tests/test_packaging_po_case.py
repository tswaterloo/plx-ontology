"""The packaging purchase order (PO5447) opened through the generic case tools.

The per-kind ``operating/packaging_case_script.py`` is retired (task allowlist-3). This
module keeps its gated path and now drives the replacement: ``operating.ingestion`` fills a
state directory's source mirror from the in-process ``FakeWorkbench`` (the SYNTHETIC
``fixtures/packaging_case_fixture.json`` rows: PO5447 with four lines, a decoy PO5448 line
and product, its FM twin), then ``operating.case_workspace_live.CaseOpener`` opens
``petra_po=`` offline through ``plx.find``, ``plx.object``, ``plx.around`` and
``plx.investigate``, and ``scripts/eval_packaging_oracle_check.py`` checks that case against the
evaluation oracle's own reading. Behaviour that lived only in the script (its draft
playbook, landed-cost arithmetic, original-document roles) is covered where it lives now.

Evidence label: injected-transport (no network, no real PLX data).
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from integrations.source_mirror import AudienceMap
from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor
from operating import case_workspace_live as live_ws
from operating.case_runtime import CaseRuntime
from operating.ingestion import Ingestor, load_sources

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "operating" / "tests" / "fixtures" /
                      "packaging_case_fixture.json").read_text("utf-8"))
AUDIENCE = json.loads((ROOT / "examples" / "packaging_case_audience.example.json")
                      .read_text("utf-8"))
SOURCES = json.loads((ROOT / "examples" / "packaging_case_sources.live-check.json")
                     .read_text("utf-8"))
OPS = Actor("plx", frozenset({"staff:operations"}))
ROOT_ROW = {"id": "po-synth-5447"}
LINES = {"pol-synth-0", "pol-synth-1", "pol-synth-2", "pol-synth-3"}


class PackagingWorld:
    """One ingested state directory over the fixture (``variant`` rows replace theirs)."""

    def __init__(self, test: unittest.TestCase, *, variant: str | None = None,
                 tables: dict | None = None, audiences: dict | None = None,
                 unavailable: tuple[str, ...] = ()) -> None:
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.state = self.dir / "state"
        self.fake = FakeWorkbench()
        self.fake.catalog = copy.deepcopy(FIXTURE["catalog"])
        self.fake.tables = copy.deepcopy(FIXTURE["tables"])
        for relation, rows in (FIXTURE["variants"][variant] if variant else {}).items():
            self.fake.tables[relation] = copy.deepcopy(rows)
        for relation, rows in (tables or {}).items():
            self.fake.tables[relation] = copy.deepcopy(rows)
        for relation in unavailable:  # listed but not served, as after a Sage full load
            self.fake.catalog[relation].update(
                key=None, unavailable="12 approved columns are missing or not granted")
        self.audience = self.dir / "audience.json"
        self.audience.write_text(json.dumps({**AUDIENCE, "audiences": {
            **AUDIENCE["audiences"], **(audiences or {})}}), "utf-8")
        _tenant, actor, sources = load_sources(SOURCES, {})
        filler = CaseRuntime(self.state, client=WorkbenchReadApiClient(BASE, KEY, fetch=self.fake),
                             audience=self.audience, tenant="plx",
                             mirror_options={"retry_delay_seconds": 0})
        ingestor = Ingestor(self.state, sources, actor=actor, mirror=filler.mirror)
        try:
            ingestor.run()
        finally:
            ingestor.close()
            filler.close()
        self.requests = len(self.fake.paths)
        self.runtime = live_ws.offline_runtime(self.state, self.audience, "plx")
        test.addCleanup(self.runtime.close)
        self.opener = live_ws.CaseOpener(self.runtime, OPS, self.state,
                                         audience=AudienceMap.from_file(self.audience))

    def open(self, number: str = "PO5447") -> dict:
        return self.opener.open_case("petra_po", number, reinvestigate=True)

    def record(self, case_id: str, actor: Actor = OPS) -> dict:
        return self.runtime.cases.get(actor, case_id)


def cited(record: dict) -> set[tuple[str, str]]:
    """(relation, row id) of every mirror row a case record cites."""
    out = set()
    for item in [*(i for bucket in record["findings"].values() for i in bucket),
                 *record["gaps"]]:
        for cite in item.get("citations") or []:
            found = live_ws._mirror_relation(cite.get("handle"))
            if found is not None:
                out.add((found[0], str(found[1].get("id"))))
    return out


def oracle_main(world: PackagingWorld, argv: list[str], *, oracle_fake=None, fetch=None):
    """``scripts.eval_packaging_oracle_check.main`` with the ingestion client and the oracle's
    Portal port over in-process fakes (injected transport)."""
    from integrations import workbench_read_api
    from integrations.source_mirror import SourceMirror
    from scripts import eval_packaging_oracle_check as check
    from scripts.eval_oracle_dev import portal_port

    real = workbench_read_api.WorkbenchReadApiClient
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.object(
            workbench_read_api, "WorkbenchReadApiClient",
            lambda url, key: real(BASE, KEY, fetch=fetch or world.fake)), \
            mock.patch.dict(SourceMirror.__init__.__kwdefaults__, {"sleep": lambda _: None}):
        code = check.main(argv, environ={"PLX_WORKBENCH_API_URL": BASE,
                                         "PLX_WORKBENCH_API_KEY": "fixture-key"},
                          read_relation=portal_port(real(BASE, KEY, fetch=oracle_fake
                                                         or world.fake)))
    return code, json.loads(out.getvalue())


class PackagingCaseGenericTest(unittest.TestCase):
    def test_po5447_roots_on_its_row_and_cites_every_fact(self):
        world = PackagingWorld(self)
        opened = world.open()
        self.assertEqual(opened["status"], "opened", opened)
        self.assertEqual((world.opener.root["relation"], world.opener.root["key"]),
                         ("public.app_purchase_order", ROOT_ROW))
        record = world.record(opened["case_id"])
        rows = cited(record)
        self.assertIn(("public.app_purchase_order", "po-synth-5447"), rows)
        self.assertEqual({row for relation, row in rows
                          if relation == "public.app_purchase_order_line"}, LINES)
        self.assertTrue({"public.app_supplier", "public.app_product",
                         "public.generated_purchase_order"} <= {r for r, _ in rows})
        # Rows of another purchase order (its line and product) are never cited.
        self.assertFalse({("public.app_purchase_order_line", "pol-synth-other"),
                          ("public.app_product", "prod-synth-9001"),
                          ("public.app_purchase_order", "po-synth-5448")} & rows)
        findings = [item for bucket in record["findings"].values() for item in bucket]
        self.assertTrue(findings)
        for item in findings:
            handles = {cite["handle"] for cite in item["citations"]}
            self.assertTrue(handles, item["statement"])
            for claim in item.get("value_claims") or []:
                self.assertIn(claim["citation"], handles)
        # The case tools refuse a fact with no citation.
        from operating.mcp_server import MCPServer, local_client
        call = local_client(MCPServer(case_runtime=world.runtime, actor=OPS), "probe")
        with self.assertRaises(RuntimeError):
            call("plx.case_add_finding", {"case_id": opened["case_id"], "kind": "fact",
                                          "status": "supported", "statement": "PO total 9."})
        # Offline after ingestion: the Portal saw no request, only reads before it.
        self.assertEqual(len(world.fake.paths), world.requests)

    def test_the_generic_path_reproduces_the_oracle_root_and_its_four_lines(self):
        """Acceptance (allowlist-3): before case.packaging_case_script retires, the generic
        path reproduces the oracle's root and its 4 lines, or reports a typed gap; the
        committed spec's own command meets its expectations on the real summary."""
        from verify import live_verify as lv

        spec = json.loads((ROOT / "devloop" / "live-checks" / "case.packaging_oracle.json")
                          .read_text("utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem="case.packaging_oracle"), [])
        # The fixture's own spelling (PO#5447): the oracle compares the number in the
        # normal form plx.find searches by, so it finds the row plx.find rooted the case on
        # and reaches a verdict.
        world = PackagingWorld(self)
        command = list(spec["command"])
        self.assertEqual(command[:3], ["python3", "-m", "scripts.eval_packaging_oracle_check"])
        with tempfile.TemporaryDirectory() as temp:
            argv = [str(ROOT / arg) if arg.startswith("examples/") else
                    arg.replace("{tmp}", temp) for arg in command[3:]]
            code, document = oracle_main(world, argv)
        self.assertEqual(lv.check_expectations(spec, code, document), [], document)
        self.assertEqual((document["oracle_check"]["status"], document["case"]["lines"],
                          document["oracle_check"]["oracle_lines"]), ("agree", 4, 4))
        self.assertEqual((document["oracle_check"]["requested_via"],
                          document["oracle_check"]["root_agrees"]), ("number", True))
        self.assertEqual(document["case"]["root_relation"], "public.app_purchase_order")
        recorded = json.dumps(lv._recorded(spec, document))
        for value in ("PO5447", "po-synth", "pol-synth", "api://", "0.802"):
            self.assertNotIn(value, recorded)
        # An oracle that reads a fifth line of the root disagrees: the case's own flags
        # never decide.
        other = FakeWorkbench()
        other.catalog = copy.deepcopy(world.fake.catalog)
        other.tables = copy.deepcopy(world.fake.tables)
        other.tables["public.app_purchase_order_line"].append(
            {**other.tables["public.app_purchase_order_line"][0], "id": "pol-synth-extra"})
        with tempfile.TemporaryDirectory() as temp:
            code, document = oracle_main(world, [arg.replace("{tmp}", temp) if "{tmp}" in arg
                                                 else str(ROOT / arg) if arg.startswith(
                                                     "examples/") else arg
                                                 for arg in command[3:]], oracle_fake=other)
        self.assertEqual(code, 3)
        self.assertEqual(document["oracle_check"]["reasons"], ["line_count_differs"])
        # Only the FM twin carries the number, bare (5447): plx.find roots the case there,
        # but no served row carries PO5447 in its normal form, directly or over the FM link,
        # so the check reports the typed gap, never a pass: the spec's blocked_when fires
        # (exit code met, a plain reason string) and its expect still fails.
        twin_only = PackagingWorld(self, variant="portal_number_absent")
        with tempfile.TemporaryDirectory() as temp:
            code, document = oracle_main(twin_only, [
                str(ROOT / arg) if arg.startswith("examples/") else arg.replace("{tmp}", temp)
                for arg in command[3:]])
        self.assertEqual((code, document["case"]["lines"], document["oracle_check"]["reasons"]),
                         (spec["expect"]["exit_code"], 4, ["requested_po_not_in_portal"]))
        self.assertEqual((document["oracle_check"]["status"],
                          document["oracle_check"]["root_agrees"]), ("oracle_gap", None))
        self.assertEqual(lv.resolve(document, spec["blocked_when"]),
                         ["requested_po_not_in_portal"])
        self.assertNotEqual(lv.check_expectations(spec, code, document), [])
        self.assertEqual(lv._recorded(spec, document)["oracle_gap"],
                         "requested_po_not_in_portal")

    def test_a_portal_outage_at_ingestion_is_blocked_upstream_never_absence(self):
        from verify import live_verify as lv
        from integrations.http import SourceHttpError

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        spec = json.loads((ROOT / "devloop" / "live-checks" / "case.packaging_oracle.json")
                          .read_text("utf-8"))
        world = PackagingWorld(self)
        with tempfile.TemporaryDirectory() as temp:
            argv = [str(ROOT / arg) if arg.startswith("examples/") else
                    arg.replace("{tmp}", temp) for arg in spec["command"][3:]]
            code, document = oracle_main(world, argv, fetch=portal_down)
        self.assertEqual(document["case"]["missing"], live_ws.BLOCKED_UPSTREAM)
        self.assertTrue(lv.blocked_parts(document))
        self.assertNotEqual(lv.check_expectations(spec, code, document), [])
        # Accepted trade-off (gap-050f0faa80e2): expect_blocked checks what still ran, so an
        # outage that stops the case from opening fails, never passes.
        self.assertEqual(code, 3)
        self.assertIn("exit_code 3 != 0",
                      lv.check_expectations(spec, code, document, spec["expect_blocked"]))

    def test_an_unavailable_sage_relation_blocks_while_the_case_and_oracle_still_agree(self):
        """gap-050f0faa80e2: sage.po_header / sage.po_receipt_header unavailable at
        ingestion are blocked_upstream parts, the case still opens and the oracle agrees:
        the real summary meets expect_blocked (the verifier records blocked, never pass)."""
        from verify import live_verify as lv

        spec = json.loads((ROOT / "devloop" / "live-checks" / "case.packaging_oracle.json")
                          .read_text("utf-8"))
        world = PackagingWorld(self, unavailable=("sage.po_header", "sage.po_receipt_header"))
        with tempfile.TemporaryDirectory() as temp:
            argv = [str(ROOT / arg) if arg.startswith("examples/") else
                    arg.replace("{tmp}", temp) for arg in spec["command"][3:]]
            code, document = oracle_main(world, argv)
        self.assertEqual([(p["part"], p.get("error_code")) for p in lv.blocked_parts(document)],
                         [("ingestion:portal_relation", "mirror.relation_unavailable")])
        self.assertEqual((code, document["oracle_check"]["status"], document["case"]["lines"],
                          document["oracle_check"]["oracle_lines"]), (0, "agree", 4, 4))
        self.assertEqual(lv.check_expectations(spec, code, document, spec["expect_blocked"]),
                         [])

    def test_unknown_or_shared_numbers_are_named_never_guessed(self):
        world = PackagingWorld(self, tables={"public.app_purchase_order": [
            *FIXTURE["tables"]["public.app_purchase_order"],
            {**FIXTURE["tables"]["public.app_purchase_order"][1], "id": "po-synth-dup",
             "po_number": "PO5448"}]})
        self.assertEqual(world.open("PO9999")["missing"], "missing_data:not_found")
        self.assertEqual(world.open("PO5448")["missing"], "ambiguous_identifier")
        self.assertIsNone(world.opener.root)
        # The number in its FM twin's bare-digit form roots the same record: both systems'
        # rows are cited, never merged.
        opened = world.open("5447")
        self.assertEqual(opened["status"], "opened", opened)
        rows = cited(world.record(opened["case_id"]))
        self.assertTrue({("public.app_purchase_order", "po-synth-5447"),
                         ("public.generated_purchase_order", "5447")} <= rows)

    def test_a_number_held_only_by_the_fm_row_roots_there(self):
        # Live 2026-10-01 shape: no Portal po_number matches, the FM row does.
        world = PackagingWorld(self, variant="portal_number_absent")
        opened = world.open("PO5447")
        self.assertEqual(opened["status"], "opened", opened)
        self.assertEqual(world.opener.root["relation"], "public.generated_purchase_order")
        self.assertNotIn("public.app_purchase_order",
                         {relation for relation, _ in cited(world.record(opened["case_id"]))})

    def test_a_receipt_joined_by_po_text_only_is_a_lead_never_a_fact(self):
        world = PackagingWorld(self, variant="text_lead")
        opened = world.open()
        record = world.record(opened["case_id"])
        about = [item for bucket, items in record["findings"].items() for item in items
                 if any("app_receipt/" in cite["handle"] for cite in item["citations"])]
        self.assertTrue(about)
        for item in about:
            self.assertEqual(item["status"], "unknown", item["statement"])
            self.assertIn("lead-tier", item["statement"])
        # The root's own values stay supported facts.
        self.assertTrue(any("app_purchase_order/" in cite["handle"]
                            for item in record["findings"]["supported"]
                            for cite in item["citations"]))

    def test_readers_and_a_fresh_runtime_see_only_what_they_may(self):
        world = PackagingWorld(self)
        opened = world.open()
        outsider = Actor("plx", frozenset({"staff:sales"}))
        self.assertIsNone(live_ws.workspace(world.runtime, outsider, opened["case_id"]))
        self.assertEqual(world.runtime.cases.list(outsider), [])
        fresh = live_ws.offline_runtime(world.state, world.audience, "plx")
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.cases.get(OPS, opened["case_id"])["findings"],
                         world.record(opened["case_id"])["findings"])
        fresh.cases.verify_chain(OPS, opened["case_id"])
        view = live_ws.structure(live_ws.workspace(fresh, OPS, opened["case_id"]))
        self.assertEqual((view["read_only"], view["business_system_writes"]), (True, 0))

    def test_currency_and_unit_come_from_the_cited_rows_never_a_constant(self):
        world = PackagingWorld(self)
        record = world.record(world.open()["case_id"])
        units = [item for item in record["findings"]["unknown"] + record["findings"]["supported"]
                 if " EA (quantity role" in item["statement"]]
        self.assertTrue(units)
        self.assertTrue(all(any("app_purchase_order_line/" in c["handle"]
                                for c in item["citations"]) for item in units))
        self.assertTrue(any(item["statement"].startswith("PurchaseOrder.currency")
                            for item in record["findings"]["supported"]))
        import ast
        tree = ast.parse((ROOT / "operating" / "case_workspace_live.py").read_text("utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                      and node.body and isinstance(node.body[0], ast.Expr)}
        literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                    and isinstance(node.value, str) and id(node) not in docstrings]
        for constant in ("USD", "CAD", "EA", "5447"):
            self.assertFalse([text for text in literals if re.search(
                rf"(?<![A-Za-z0-9]){constant}(?![A-Za-z0-9])", text)], constant)

    def test_nothing_unread_is_recorded_absent_and_landed_cost_stays_unknown(self):
        # The PO's receipt (joined by purchase_order_id) is cited when the investigator
        # may read receipts ...
        receipts = {"public.app_receipt", "public.app_receipt_line"}
        granted = PackagingWorld(self, variant="receipt")
        self.assertTrue(receipts <= {relation for relation, _ in cited(
            granted.record(granted.open()["case_id"]))})
        # ... and with the receipt relations withheld it is never read, never absent.
        world = PackagingWorld(self, variant="receipt", audiences={
            name: ["svc:ingest"] for name in ("public.app_receipt", "public.app_receipt_line",
                                              "public.receipt_packaging", "silver.receipts",
                                              "sage.po_receipt_header")})
        record = world.record(world.open()["case_id"])
        self.assertNotIn("absent_in_source", {gap["state"] for gap in record["gaps"]})
        self.assertFalse({relation for relation, _ in cited(record)} & receipts)
        # Landed cost: its question family is answered partial with its unknowns named as
        # gaps; no landed-cost total is stated anywhere on the case.
        landed = [gap for gap in record["gaps"]
                  if gap["missing"].startswith("Landed cost") and "partial" in gap["missing"]]
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0]["state"], "unknown")
        statements = " ".join(item["statement"] for bucket in record["findings"].values()
                              for item in bucket)
        self.assertIsNone(re.search(r"landed_cost\w* = \d", statements))


if __name__ == "__main__":
    unittest.main()
