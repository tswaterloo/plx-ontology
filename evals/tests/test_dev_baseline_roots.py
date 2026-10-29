"""Root resolution and mirror completeness of the dev-set baseline.

The live baseline answered 20 of 26 items blocked: a Portal PO number is also the BC
purchase order's No. once the Portal drains it to BC, so plx.find lists both records and the
single-rooted plans abstained (``ambiguous_root``). Here the oracle's fixture world gets that
drain (``fixture_world.drain_purchase_orders_to_bc``) and the product captures BC itself, as
the live run does; the frozen dev items run end to end on the real mirror, projector, case
host and tools. A Sage PO holding the same number is another record (a ``separate_row``
carrier) and keeps its root ambiguous. The mirror tests run the live path with the fixture
world as an injected transport. Evidence: isolated-fixture / injected-transport, never live.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import quote
from typing import Callable

from evals import baseline as bl
from evals import runner
from ontology.json_contract import validate

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
PO_RELATION = "public.app_purchase_order"


def _investigation(row: dict) -> dict:
    return next(e["result"] for e in row["transcript"]["events"]
                if e.get("name") == "plx.investigate")


class TwinRootTests(unittest.TestCase):
    """F1 and F4 dev items over a world whose purchase orders were drained to BC."""

    @classmethod
    def setUpClass(cls):
        from evals.oracle.fixture_world import bc_record, drain_purchase_orders_to_bc
        from integrations.tests.test_live_source_adapters import ENV
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        from scripts import eval_unfamiliar_sample as ev
        from tests.test_eval_unfamiliar_sample import fixture_bc_sender

        from operating.ontology_tools import default_schema
        schema = default_schema()
        cls.po_identity_links = sorted(lid for lid in schema.identity_link_types()
                                       if schema.link_types[lid]["from"] == "PurchaseOrder")
        cls.tmp = tempfile.TemporaryDirectory()
        items = [i for i in runner.load_items(script.DEV_DIR, split="dev")
                 if i["family"] in ("F1", "F4")]
        world = dev.eval_world()
        drained = [i for i in items if i["root"]["relation"] == PO_RELATION]
        drain_purchase_orders_to_bc(world, [i["root"]["key"]["id"] for i in drained])
        # One F1 PO number also on a Sage PO: a separate record, never a twin.
        cls.sage_item = next(i["id"] for i in drained if i["family"] == "F1")
        number = next(i["root"]["identifier"] for i in drained if i["id"] == cls.sage_item)
        world.relations["sage.po_header"] = [{"porhseq": 990001, "ponumber": number,
                                              "audtdate": "20981231"}]
        # Copies of POs no dev item roots at (identity probes, e99f5ba164b1): the number in
        # two BC companies; an FM copy the PO's stored FM id joins (verified) beside a BC
        # copy (number only); an FM copy and a BC copy sharing only the number; an FM copy
        # alone sharing only the number.
        orders = {r["id"]: r for r in world.relations["public.app_purchase_order"]}
        fm_rows = {r["id"]: r for r in world.relations["public.generated_purchase_order"]}
        cls.probe = {name: orders[po_id]["po_number"] for name, po_id in (
            ("two_companies", "fx-po-5"), ("verified_fm", "fx-po-6"),
            ("fm_and_bc_by_number", "fx-po-9"), ("fm_alone_by_number", "fx-po-10"))}
        drain_purchase_orders_to_bc(world, ["fx-po-5", "fx-po-6", "fx-po-9"])
        copy = bc_record("purchaseOrders", 1, 790_005, number=cls.probe["two_companies"],
                         vendorNumber="FXV003", fullyReceived=False)
        world.bc["purchaseOrders"].append(copy)
        world.bc_lines[copy["id"]] = []
        fm_rows[orders["fx-po-6"]["custom_fields"]["fmGeneratedPoId"]]["ponumber"] = \
            cls.probe["verified_fm"]
        for fm_id, name in ((6997, "fm_and_bc_by_number"), (6998, "fm_alone_by_number")):
            world.relations["public.generated_purchase_order"].append({
                "id": fm_id, "ponumber": cls.probe[name],
                "audittimestampmodified": "2099-01-01T12:00:00Z"})
        fake, books = fixture_bc_sender(world)
        client = WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                        fetch=ev._fixture_fetch(world))
        relations = script.reachable_relations(load_schema(), {i["root"]["relation"]
                                                               for i in items},
                                               script.DEFAULT_LINK_DEPTH)
        cls.product = script.build_product(
            client, Path(cls.tmp.name), relations, budget_seconds=None,
            clock=script.fixture_clock, bc_environment=ENV["BC_ENVIRONMENT"],
            bc_capture=lambda db, seconds: ev.capture_bc(ENV, db, seconds, sender=fake,
                                                         books=books))
        sources, _, _, _ = dev.build_fixture(world)
        sources.portal.prefetch()
        cls.rows, _ = script.run_items(items, cls.product, sources.portal,
                                       evidence_label="isolated-fixture")
        cls.items = {i["id"]: i for i in items}
        cls.block = bl.eval_block(cls.rows, split="dev", evidence_label="isolated-fixture",
                                  run_id="t", items_digest="0" * 16)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_f4_resolves_its_root_despite_a_bc_twin_and_never_merges_it(self):
        f4 = [r for r in self.rows if r["family"] == "F4"]
        self.assertEqual(len(f4), 6)
        # The live failure reproduced: plx.find lists the Portal PO and its BC twin.
        self.assertEqual(self.block["find_by_family"]["F4"], {"ambiguous": 6})
        self.assertEqual(self.block["find_relations_by_family"]["F4"],
                         {PO_RELATION: 6, "purchaseOrders": 6})
        self.assertEqual(self.block["plan_root_by_family"]["F4"], {"found": 6})
        self.assertEqual(self.block["by_family"]["F4"]["plan_root_found"], "6/6")
        for row in f4:
            result = _investigation(row)
            self.assertNotIn("ambiguous_root", result["answerability"]["reasons"], row["item_id"])
            self.assertNotEqual(result["answerability"]["state"], "blocked", row["item_id"])
            self.assertFalse(row["abstained"], row["item_id"])
            self.assertTrue(result["root"]["object_id"].startswith("PurchaseOrder:portal."))
            twins = [g for g in result["gaps"] if g["reason"] == "root_twins_not_roots"]
            self.assertEqual(len(twins), 1, result["gaps"])
            self.assertIn("/purchaseOrders/", twins[0]["detail"])   # the BC twin, cited
            # ... and as data, within the gap contract.
            (twin,) = twins[0]["twins"]
            self.assertIn("/purchaseOrders/", twin["handle"])
            self.assertEqual(twin["tier"], "lead")   # the one BC copy shares only the number
            self.assertEqual(validate(twins[0], CONTRACTS["$defs"]["gap"], root=CONTRACTS), [])
            # The twin walk reads each declared PurchaseOrder identity link once per
            # direction, no more: the entity map (``bc_posted_as``) is a second one beside
            # ``same_record_as``, so the walk reads 2 per identity link type, never a repeat.
            self.assertLessEqual({"PurchaseOrder.same_record_as", "PurchaseOrder.bc_posted_as"},
                                 set(self.po_identity_links))
            self.assertIn({"step": "root_twins", "tool": "plx.around", "status": "ok",
                           "calls": 2 * len(self.po_identity_links), "objects": 1},
                          result["trace"])

    def test_f1_roots_resolve_where_the_record_exists_and_a_separate_record_stays_ambiguous(self):
        from ontology import identifiers
        f1 = [r for r in self.rows if r["family"] == "F1"]
        for row in f1:
            item = self.items[row["item_id"]]
            if row["item_id"] == self.sage_item:
                # The Sage PO with the same number is another record: no winner is chosen.
                self.assertEqual(row["plan_root"], "ambiguous")
                result = _investigation(row)
                self.assertEqual(result["answerability"],
                                 {"state": "blocked", "reasons": ["ambiguous_root"]})
                self.assertGreaterEqual(len(result["candidates"]), 3)
            elif identifiers.find_identifiers(item["question"]["text"]):
                # A plan rooted at the record's verified twin (an FM assembly order for the
                # Portal MO the oracle roots at) found the item's record: never wrong.
                self.assertEqual(row["plan_root"], "found", row["item_id"])
            else:   # no identifier of the grammar in the question: nothing to resolve
                self.assertEqual(row["plan_root"], "not_found", row["item_id"])
        self.assertEqual(sum(self.block["plan_root_by_family"]["F1"].values()), len(f1))
        self.assertEqual(sum(self.block["find_by_root_kind"]["po_number"].values()),
                         sum(self.items[r["item_id"]]["root"]["identifier_kind"] == "po_number"
                             for r in self.rows))

    def test_f1_receipt_and_bc_document_roots_resolve_from_prose(self):
        """find-upstream-form-roots: a receipt's lab id and a BC sales invoice's document
        No. named in the question's prose resolve to the item's root record; no F1
        identifier is left unparsed."""
        kinds: dict[str, int] = {}
        for row in (r for r in self.rows if r["family"] == "F1"):
            find = next(e["result"] for e in row["transcript"]["events"]
                        if e.get("name") == "plx.find")
            self.assertNotIn("identifier_unparsed", json.dumps(find.get("gaps")), row["item_id"])
            kind = self.items[row["item_id"]]["root"]["identifier_kind"]
            if kind not in ("receipt_number", "bc_number"):
                continue
            kinds[kind] = kinds.get(kind, 0) + 1
            self.assertEqual(find["status"], "found", row["item_id"])
            self.assertEqual(len(find["candidates"]), 1, row["item_id"])
            self.assertEqual((row["root"], row["plan_root"]), ("found", "found"), row["item_id"])
        self.assertEqual(set(kinds), {"receipt_number", "bc_number"})
        self.assertEqual(self.block["find_by_root_kind"]["bc_number"], {"found": 2})

    def investigate(self, number: str, family: str = "F4", *,
                    readable: Callable[[str], bool] = lambda key: True) -> dict:
        """plx.investigate for ``number`` as the eval reader, granted the store's audience
        keys ``readable`` accepts, at the run's pin."""
        from ontology import store_ddl
        from ontology.types import Actor
        from operating import ontology_tools as ot
        from operating import ontology_tools_investigate as oi
        from operating import ontology_tools_traverse as tt
        from scripts import eval_dev_baseline as script

        reader = ot.StoreReader.open(self.product.store)
        self.addCleanup(reader.conn.close)
        who = {script.PRINCIPAL}
        policy = ot.ReadPolicy(tenant=script.TENANT,
                               audiences={k: set(who) for k in reader.audience_keys()
                                          if readable(k)},
                               markings={m: set(who) for m in store_ddl.MARKINGS})
        base = ot.OntologyTools(reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(table, schema=base.schema))
        return table["plx.investigate"]["handler"](
            {"family": family, "root": {"identifier": number, "identifier_kind": "petra_po"},
             "as_known_at": self.rows[0]["transcript"]["run_pin"]["known_at"]},
            Actor(script.TENANT, frozenset(who)))

    @staticmethod
    def gap(result: dict, reason: str) -> dict:
        found = [g for g in result["gaps"] if g["reason"] == reason]
        return found[0] if len(found) == 1 else {}

    def test_a_reader_without_the_bc_grant_sees_one_root_and_no_twin(self):
        row = next(r for r in self.rows if r["family"] == "F4")
        result = self.investigate(self.items[row["item_id"]]["root"]["identifier"],
                                  readable=lambda key: not key.startswith("bc"))
        self.assertTrue(result["root"]["object_id"].startswith("PurchaseOrder:portal."))
        text = json.dumps(result)
        self.assertNotIn("PurchaseOrder:bc.purchase_orders:", text)   # no BC record named
        self.assertNotIn("root_twins", text)

    def test_a_verified_link_beats_a_copy_sharing_only_the_number(self):
        result = self.investigate(self.probe["verified_fm"])
        self.assertTrue(result["root"]["object_id"].startswith(
            "PurchaseOrder:portal.app_purchase_order:"), result["answerability"])
        twins = self.gap(result, "root_twins_not_roots")["detail"]
        self.assertIn("verified same-record link", twins)
        self.assertEqual([t["tier"] for t in self.gap(result, "root_twins_not_roots")["twins"]],
                         ["verified"])
        self.assertIn("api://public.generated_purchase_order/", twins)
        self.assertNotIn("bc://", twins)
        unlinked = self.gap(result, "weaker_candidates_not_followed")["detail"]
        self.assertIn("/purchaseOrders/", unlinked)   # the BC copy, cited

    def test_copies_sharing_only_the_number_resolve_only_when_unique(self):
        # One FM copy alone, like the one BC copy of every F4 root above: accepted.
        result = self.investigate(self.probe["fm_alone_by_number"])
        self.assertTrue(result["root"]["object_id"].startswith(
            "PurchaseOrder:portal.app_purchase_order:"), result["answerability"])
        lead = self.gap(result, "root_twins_not_roots")
        self.assertIn("same-number match only", lead["detail"])
        self.assertEqual([t["tier"] for t in lead["twins"]], ["lead"])
        # Two copies joined only by the number (two BC companies; an FM and a BC copy):
        # ambiguous_root, every readable candidate listed with its citation, no pick.
        for name, copies in (("two_companies", ("bc.purchase_orders", "bc.purchase_orders")),
                             ("fm_and_bc_by_number", ("portal.fm_generated_purchase_order",
                                                      "bc.purchase_orders"))):
            result = self.investigate(self.probe[name])
            self.assertEqual(result["answerability"],
                             {"state": "blocked", "reasons": ["ambiguous_root"]}, name)
            self.assertNotIn("root", result)
            listed = [c["object"]["object_id"] for c in result["candidates"]]
            self.assertEqual(len(listed), 3, name)
            gap = self.gap(result, "ambiguous_root")
            self.assertEqual(gap["state"], "unknown")
            for candidate in result["candidates"]:
                self.assertIn(candidate["matched"]["raw"]["origin"]["citation"],
                              gap["detail"])
            self.assertEqual(sorted(oid.split(":")[1] for oid in listed),
                             sorted(copies + ("portal.app_purchase_order",)), name)
            self.assertNotIn("root_twins_not_roots", json.dumps(result["gaps"]))

    def test_a_copy_the_reader_cannot_read_is_neither_picked_nor_listed(self):
        from evals.oracle.fixture_world import COMPANIES
        hidden = COMPANIES[1][0]
        both = self.investigate(self.probe["two_companies"])
        self.assertIn(hidden, json.dumps(both["candidates"]))
        result = self.investigate(self.probe["two_companies"],
                                  readable=lambda key: hidden not in key)
        # The one readable copy is the only other record this reader sees: accepted, and
        # the other company's copy is named nowhere.
        self.assertTrue(result["root"]["object_id"].startswith(
            "PurchaseOrder:portal.app_purchase_order:"), result["answerability"])
        self.assertNotIn(hidden, json.dumps(result))


class VerifiedTwinPlanRootTests(unittest.TestCase):
    """The plan_root rule on synthetic ids: the plan's root counts as found when it is the
    item's root row or that row's verified twin, read from the gap's structured ``twins``
    (never its prose); a same-number lead or another row is wrong."""

    RELATIONS = {"fm.synthetic_ao": "public.synthetic_ao"}
    VERIFIED = ("1 record(s) holding this identifier on another declared carrier are the "
                "root's twins in other systems, joined to it by a verified same-record "
                "link; never merged into it: ")

    @staticmethod
    def handle(relation: str, row_id: str) -> str:
        return f"api://{relation}/" + json.dumps({"id": row_id}, separators=(",", ":")) + "@7"

    def plan_root(self, twin_ids=(), tier="verified", detail=VERIFIED, root_id="ao-1",
                  want_id="mo-1", reason="root_twins_not_roots"):
        item = {"root": {"relation": "public.synthetic_mo", "key": {"id": want_id}}}
        handles = [self.handle("public.synthetic_mo", t) for t in twin_ids]
        gap = {"state": "unknown", "reason": reason, "subject": "root:X",
               "detail": detail + "; ".join(handles)}
        if handles:
            gap["twins"] = [{"handle": h, "tier": tier} for h in handles]
        result = {"root": {"object_id": "Synthetic:fm.synthetic_ao:"
                           + json.dumps({"id": root_id})}, "gaps": [gap]}
        return bl._plan_root(item, [{"name": "plx.investigate", "result": result}],
                             self.RELATIONS)

    def test_the_verified_twin_of_the_plan_root_reads_found(self):
        self.assertEqual(self.plan_root(twin_ids=["mo-1"]), "found")
        self.assertEqual(self.plan_root(twin_ids=["mo-1"], tier="reviewed"), "found")

    def test_rewording_the_gap_detail_leaves_plan_root_unchanged(self):
        # The scorer reads the twins as data: prose that drops the product's phrase, or
        # says nothing at all, changes nothing; the phrase alone credits nothing.
        for detail in ("reworded: twins elsewhere ", "x "):
            self.assertEqual(self.plan_root(twin_ids=["mo-1"], detail=detail), "found")
        item = {"root": {"relation": "public.synthetic_mo", "key": {"id": "mo-1"}}}
        prose_only = {"state": "unknown", "reason": "root_twins_not_roots", "subject": "root:X",
                      "detail": self.VERIFIED + self.handle("public.synthetic_mo", "mo-1")}
        result = {"root": {"object_id": "Synthetic:fm.synthetic_ao:" + json.dumps({"id": "ao-1"})},
                  "gaps": [prose_only]}
        self.assertEqual(bl._plan_root(item, [{"name": "plx.investigate", "result": result}],
                                       self.RELATIONS), "wrong")

    def test_a_lead_or_unknown_tier_twin_reads_wrong(self):
        for tier in ("lead", "Verified", None, "same_number"):
            self.assertEqual(self.plan_root(twin_ids=["mo-1"], tier=tier), "wrong", tier)

    def test_an_unrelated_row_reads_wrong(self):
        self.assertEqual(self.plan_root(twin_ids=["mo-2"]), "wrong")
        self.assertEqual(self.plan_root(twin_ids=[]), "wrong")

    def test_a_twin_only_in_another_gap_reason_reads_wrong(self):
        self.assertEqual(self.plan_root(twin_ids=["mo-1"],
                                        reason="weaker_candidates_not_followed"), "wrong")

    def test_the_root_row_itself_still_reads_found(self):
        item = {"root": {"relation": "public.synthetic_ao", "key": {"id": "ao-1"}}}
        result = {"root": {"object_id": "Synthetic:fm.synthetic_ao:" + json.dumps({"id": "ao-1"})},
                  "gaps": []}
        self.assertEqual(bl._plan_root(item, [{"name": "plx.investigate", "result": result}],
                                       self.RELATIONS), "found")


class MirrorCompletenessTests(unittest.TestCase):
    """The live mirror read (injected transport): the relations the plans read come first
    and decide eval.complete; every unfinished relation is named in eval.mirror, and one
    no plan reads is noted in eval.incomplete under its own code (review 926ee36b)."""

    # Every relation of the fixture world is one an F1-F4 plan reads (F3 states produced
    # from the FM status entries), so the unplanned case drops that relation from the plans.
    UNPLANNED = "public.assembly_order_status"

    def run_live(self, tmp: str, failing: str) -> tuple[int, dict]:
        from unittest import mock
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        from scripts import eval_unfamiliar_sample as ev

        world = dev.eval_world()
        world.fail = {failing}
        client = WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                        fetch=ev._fixture_fetch(world))

        def build(env, args, *, guard):
            sources, documents, configured, _ = dev.build_fixture(dev.eval_world())
            return sources, documents, configured
        spec = json.loads((ROOT / "devloop" / "live-checks" / "eval.dev_baseline.json")
                          .read_text())
        argv = [arg.replace("{tmp}", tmp).replace("{session_state}", tmp + "/session")
                for arg in spec["command"][3:]]
        argv = argv[:argv.index("--count")] + ["--count", "8", "--min-items", "4"] + \
            argv[argv.index("--total-budget-seconds"):] + ["--state-dir", tmp + "/state"]
        # The run id seeds the item sample: pin it, not the auto 'live-<UTC date>' (the
        # 2026-10-12 sample read no unfinished relation and the incomplete case passed).
        argv[argv.index("--run-id") + 1] = "live-2026-10-10"
        real = script.plan_relations

        def plans(schema, families):
            read = real(schema, families)
            return None if read is None else [r for r in read if r != self.UNPLANNED]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(script, "plan_relations", plans):
            code = script.main(argv, environ={}, live_build=build, live_client=client,
                               live_clock=script.fixture_clock)
        return code, json.loads(out.getvalue())

    def test_only_an_unfinished_relation_the_plans_read_makes_the_run_incomplete(self):
        from devloop import live_verify
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script

        spec = json.loads((ROOT / "devloop" / "live-checks" / "eval.dev_baseline.json")
                          .read_text())
        needed = script.plan_relations(load_schema(), ["F1", "F2", "F3", "F4"])
        self.assertIn(PO_RELATION, needed)
        self.assertIn(self.UNPLANNED, needed)
        self.assertIsNone(script.plan_relations(load_schema(), ["F1", "F5"]))
        with tempfile.TemporaryDirectory() as tmp:
            code, document = self.run_live(tmp, self.UNPLANNED)
        block = document["eval"]
        self.assertEqual((code, block["complete"], block["incomplete"]),
                         (0, True, [bl.MIRROR_UNPLANNED]), block)
        self.assertEqual(block["mirror"]["unfinished"], [self.UNPLANNED])
        self.assertEqual(block["mirror"]["needed_unfinished"], [])
        self.assertGreater(block["mirror"]["unfinished_share"], 0)
        # The spec on this output: the short run's item floor and B1's accuracy bar fail
        # (never the unfinished relation), and the recorded eval block keeps the
        # unfinished relation's name and the find counts.
        self.assertEqual(live_verify.check_expectations(spec, code, document),
                         ["eval.items < 20", "eval.answer.accuracy_rate < 0.8"])
        recorded = live_verify._recorded(spec, document)["eval"]
        self.assertEqual(recorded["mirror"], block["mirror"])
        self.assertEqual(recorded["find_by_family"], block["find_by_family"])
        self.assertEqual(recorded["find_relations_by_family"], block["find_relations_by_family"])
        with tempfile.TemporaryDirectory() as tmp:
            code, document = self.run_live(tmp, PO_RELATION)
        block = document["eval"]
        self.assertEqual((code, block["complete"], block["incomplete"]),
                         (3, False, ["mirror_incomplete"]))
        self.assertEqual(block["mirror"]["needed_unfinished"], [PO_RELATION])
        self.assertIn("eval.complete != expected",
                      live_verify.check_expectations(spec, code, document))


class FixtureIdentifierFormTests(unittest.TestCase):
    """find-upstream-form-roots: the eval world writes identifiers in upstream forms, so no
    question root is unparsed; one declared malformed value stays for the object view."""

    def test_question_root_identifiers_parse_and_one_malformed_value_stays(self):
        from evals.oracle import keys as K
        from evals.oracle.fixture_world import build_eval_world
        from ontology import identifiers
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script

        schema, world = load_schema(), build_eval_world()

        def rows(spec):
            source = world.relations if spec["source"] == "portal_api" else (
                world.bc if spec["source"] == "bc" else {})
            return source.get(spec["relation"], [])

        def value(row, path):
            for part in path.split("."):
                row = row.get(part) if isinstance(row, dict) else None
            return row

        kind_of, unparsed = {}, []   # (relation, field) -> kind; values no kind parses
        for object_type, spec in schema.object_types.items():
            for prop in spec["properties"]:
                kind = prop.get("identifier_kind")
                for source in prop.get("sources", ()) if kind else ():
                    if source["binding"] not in schema.bindings:
                        continue
                    binding = schema.bindings[source["binding"]][1]
                    prefix = "bc:" if binding["source"] == "bc" else ""
                    kind_of[(prefix + binding["relation"], source["path"])] = kind
                    kind_of[(source["binding"], source["path"])] = kind   # dev items' BC form
                    for row in rows(binding):
                        raw = value(row, source["path"])
                        if raw not in (None, "") and identifiers.parse(kind, raw) is None:
                            unparsed.append((object_type, prop["name"], raw, row))
        carriers = {pair for pairs in K.ROOT_KEYS.values() for pair in pairs}
        self.assertEqual(carriers - set(kind_of), set(), "a root carrier the schema leaves untyped")
        # Every frozen dev item's root identifier parses as its carrier's kind.
        for item in runner.load_items(script.DEV_DIR, split="dev"):
            root = item["root"]
            field, = {f for r, f in K.ROOT_KEYS[root["identifier_kind"]]
                      if r == root["relation"] or r.startswith("bc:") and
                      root["relation"].startswith("bc.")}
            kind = kind_of[(root["relation"], field)]
            self.assertIsNotNone(identifiers.parse(kind, root["identifier"]), item["id"])
        by_carrier = {(t, name): (raw, row) for t, name, raw, row in unparsed}
        self.assertEqual(len(by_carrier), len(unparsed), unparsed)
        self.assertEqual(set(by_carrier), {("Customer", "erp_code"),
                                           ("PurchaseOrder", "po_number")}, unparsed)
        # The object_view unparsed-denominator case: no identifier kind of its type parses
        # it, and the oracle never roots at a customer.
        raw, row = by_carrier[("Customer", "erp_code")]
        own = {p["identifier_kind"] for p in schema.object_types["Customer"]["properties"]
               if p.get("identifier_kind")}
        self.assertFalse([k for k in own if identifiers.parse(k, raw)], raw)
        self.assertNotIn(K.CUSTOMER, {relation for relation, _ in carriers})
        # The PO renumbered to a BC document No. (decision 2026-10-04 po-fixture-roots) is
        # well formed as that kind and staging lineage keeps it out of every draw.
        raw, row = by_carrier[("PurchaseOrder", "po_number")]
        self.assertEqual(row["custom_fields"]["renumberedTo"], raw)
        self.assertIsNotNone(identifiers.parse("bc_document_no", raw))


if __name__ == "__main__":
    unittest.main()
