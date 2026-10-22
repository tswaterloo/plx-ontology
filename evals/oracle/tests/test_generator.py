"""Dev-set generator over the synthetic fixture world (injected transport; not live)."""

from __future__ import annotations

import unittest

from evals.oracle import BcReads, PortalReads, Sources, run
from evals.oracle import keys as K
from evals.oracle.fixture_world import World
from evals.oracle.fixture_world import build_eval_world, build_world, po_number
from evals.oracle.generator import (HELD_OUT_ONLY, QUOTAS, STRATA, Candidate, audit_requested,
                                    candidate_split, closed, document_candidates, generate,
                                    item_root_digest,
                                    lineage, out_of_vocabulary, root_digest, split_of)

NOW = "2099-01-02T04:00:00Z"


def _generate(run_id: str = "fixture-run-a", **kwargs):
    world = build_eval_world()
    sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
    kwargs.setdefault("documents", world.documents())
    return generate(sources, run_id=run_id, now=NOW, **kwargs), sources, world


class GeneratorTest(unittest.TestCase):
    def test_thirty_items_across_families_and_strata(self):
        generated, _, _ = _generate()
        items = generated.items
        self.assertEqual(len(items), 30)
        families = [i["family"] for i in items]
        self.assertEqual({f: families.count(f) for f in set(families)},
                         {"F1": 12, "F2": 6, "F3": 6, "F4": 6})
        self.assertEqual(len({i["id"] for i in items}), 30)
        # Every stratum (SO/PO/AO/receipt/BC invoice/mail/SharePoint) is sampled.
        self.assertEqual(set(generated.item_strata.values()), set(STRATA))
        for stratum in STRATA:
            self.assertGreaterEqual(generated.strata[stratum]["items"], 1, stratum)
        lineages = {i["lineage_stratum"] for i in items}
        self.assertNotIn("gold_native_staging", lineages)
        self.assertTrue({"gold_etl_from_fm", "gold_etl_from_sage"} <= lineages)
        self.assertTrue(any(x.startswith("bc_") for x in lineages))

    def test_same_run_id_same_items_other_run_id_other_sample(self):
        first, _, _ = _generate("fixture-run-a")
        again, _, _ = _generate("fixture-run-a")
        other, _, _ = _generate("fixture-run-b")
        self.assertEqual(first.items, again.items)
        roots = lambda g: [(i["family"], i["root"]["identifier"]) for i in g.items]  # noqa
        self.assertNotEqual(roots(first), roots(other))

    def test_roots_are_closed_unique_and_answer_reproduces(self):
        generated, sources, world = _generate()
        for item in generated.items:
            root = item["root"]
            if root["relation"].startswith("public."):
                rows = [r for r in world.relations[root["relation"]]
                        if r["id"] == root["key"]["id"]]
                self.assertEqual(len(rows), 1, item["id"])
                self.assertTrue(closed(root["relation"], rows[0], sources.portal), item["id"])
                if item["lineage_stratum"].startswith("gold_"):
                    self.assertEqual(lineage(root["relation"], rows[0], sources.portal),
                                     item["lineage_stratum"])
            self.assertTrue(item["closed_transaction"])
            again = run(item["family"], root, item["as_known_at"], sources)
            self.assertEqual(again.result, item["known_outcome"], item["id"])
            self.assertEqual(again.generations, item["as_known_at"]["l1_generations"])
            self.assertNotIn("root_moved", {g["reason"] for g in again.result["gaps"]})

    def test_ten_labels_requested_for_human_audit_none_claimed(self):
        generated, _, _ = _generate(evidence_note="evidence: fixture, not live data.")
        marked = [i for i in generated.items if audit_requested(i)]
        self.assertEqual(len(marked), 10)
        self.assertEqual({i["family"] for i in marked}, {"F1", "F2", "F3", "F4"})
        self.assertEqual({i["label"]["status"] for i in generated.items}, {"oracle_only"})
        self.assertTrue(all("fixture, not live" in i["label"]["note"] for i in generated.items))

    def test_item_shape_follows_the_c0_contract(self):
        generated, _, _ = _generate(id_suffix="fixture")
        for item in generated.items:
            self.assertRegex(item["id"], r"^dev-F[1-4]-[0-9]{4}-fixture$")
            self.assertEqual(item["oracle"]["interface"], "plx-eval-oracle/v1")
            self.assertRegex(item["oracle"]["id"], r"^oracle:[a-z0-9_-]+@[0-9a-f]{12}$")
            self.assertTrue(item["oracle"]["derived_from"])
            self.assertTrue(all(len(c) <= 200 for c in item["oracle"]["derived_from"]))
            self.assertEqual(item["as_known_at"]["time"], "2099-01-02T03:04:05Z")
            self.assertRegex(item["as_known_at"]["l1_generations"]["portal_api"],
                             r"^[0-9a-f]{16}$")
            for value in item["known_outcome"]["values"]:
                self.assertRegex(value["name"], r"^[a-z][a-z0-9_]{0,63}$")
                self.assertLessEqual(len(value.get("citations", [])), 8)

    def test_document_strata_name_a_record_the_way_the_portal_compares_its_number(self):
        """Exact text, except a PO number, which the Portal looks up TRIM ILIKE
        (keys.PO_DOCUMENT_CITE): "po9200004" names PO9200004 and the question is asked
        from the stored number; "PO92000099" never names PO9200009."""
        generated, sources, _ = _generate()
        doc_roots = {i["root"]["identifier"] for i in generated.items
                     if generated.item_strata[i["id"]] in ("mail", "sharepoint")}
        self.assertTrue(doc_roots)
        self.assertFalse(doc_roots & {"PO92000099", "po9200004", "PO9200009"})

        mail = World({}, documents={"mail": [{
            "handle": "graph-mail://fx/1", "texts": [
                " po9200004 and fxso100005", "PO92000099 is another number"]}]}).documents()

        def pool(identifier, kind, relation, key, object_type):
            return Candidate("x", "F1", identifier, kind, "gold_etl_from_fm", relation,
                             (("id", key),), object_type)

        found = document_candidates(sources, mail, "mail", [
            pool(po_number(4), "po_number", K.PO, "fx-po-4", "PurchaseOrder"),
            pool(po_number(9), "po_number", K.PO, "fx-po-9", "PurchaseOrder"),
            pool("FXSO100005", "so_number", K.SO, "fx-so-5", "SalesOrder")], 10)
        self.assertEqual([(c.identifier, c.root_key) for c in found],
                         [(po_number(4), (("id", "fx-po-4"),))])

    def test_test_demo_and_renumbered_rows_are_closed_but_never_roots(self):
        staged_ids = {"fx-po-qa-1", "fx-po-demo-1", "fx-po-bc-1"}
        generated, sources, world = _generate()
        staged = [r for r in world.relations[K.PO] if r["id"] in staged_ids]
        self.assertEqual(len(staged), 3)
        for row in staged:
            self.assertTrue(closed(K.PO, row, sources.portal), row["id"])
            self.assertEqual(lineage(K.PO, row, sources.portal), "gold_native_staging")
        roots = {i["root"]["key"].get("id") for i in generated.items}
        self.assertFalse(roots & staged_ids)

    def test_failed_relation_is_reported_not_silent(self):
        world = build_eval_world()
        world.fail.add("public.app_receipt")
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        generated = generate(sources, run_id="fixture-run-a", now=NOW)
        self.assertEqual(generated.strata["receipt"]["status"], "unavailable")
        self.assertEqual(generated.failures, {"public.app_receipt": "RuntimeError"})
        self.assertEqual(generated.strata["mail"]["status"], "not_configured")
        self.assertEqual(len(generated.items), 30)

    def test_rejects_bad_suffix_and_split(self):
        with self.assertRaises(ValueError):
            _generate(id_suffix="Not-OK")
        with self.assertRaises(ValueError):
            _generate(split="test")


class SplitTest(unittest.TestCase):
    """Dev and held-out are drawn from one world and never share a root record."""

    @classmethod
    def setUpClass(cls):
        cls.dev, _, _ = _generate("split-run", split="dev", count=30)
        cls.held, cls.sources, cls.world = _generate("split-run", split="held_out", count=120)

    def test_held_out_is_120_items_with_two_families_without_a_plan(self):
        families = [i["family"] for i in self.held.items]
        self.assertEqual(len(families), 120)
        self.assertEqual({f: families.count(f) for f in set(families)}, QUOTAS["held_out"])
        self.assertEqual(HELD_OUT_ONLY, {"F5", "F6"})
        for item in self.held.items:
            self.assertRegex(item["id"], r"^ho-F[1-6]-[0-9]{4}$")
            self.assertEqual(item["split"], "held_out")

    def test_dev_never_asks_a_held_out_only_family(self):
        big, _, _ = _generate("split-run", split="dev", count=90)
        self.assertFalse({i["family"] for i in big.items} & HELD_OUT_ONLY)

    def test_roots_are_disjoint_by_partition(self):
        dev_roots = {item_root_digest(i) for i in self.dev.items}
        held_roots = {item_root_digest(i) for i in self.held.items}
        self.assertTrue(dev_roots and held_roots)
        self.assertEqual(dev_roots & held_roots, set())
        for item in self.dev.items:
            self.assertEqual(split_of(item["root"]["relation"], item["root"]["key"]), "dev")
        for item in self.held.items:
            self.assertEqual(split_of(item["root"]["relation"], item["root"]["key"]),
                             "held_out")
        # Disjoint across run ids too: the partition does not depend on the seed.
        other, _, _ = _generate("another-run", split="dev", count=30)
        self.assertEqual({item_root_digest(i) for i in other.items} & held_roots, set())
        self.assertGreater(self.held.dropped["other_split"], 0)

    def test_customer_po_questions_need_every_order_in_one_split(self):
        def candidate(*keys):
            members = tuple(("public.app_sales_order", '{"id":"%s"}' % k) for k in keys)
            return Candidate("sales_order", "F2", "X", "customer_po", "gold_etl_from_fm",
                             "public.app_sales_order", (("id", keys[0]),), "SalesOrder",
                             members)
        ids = [f"fx-so-{i}" for i in range(1, 40)]
        dev_id = next(i for i in ids if split_of("public.app_sales_order", {"id": i}) == "dev")
        held_id = next(i for i in ids
                       if split_of("public.app_sales_order", {"id": i}) == "held_out")
        self.assertIsNone(candidate_split(candidate(dev_id, held_id)))
        self.assertEqual(candidate_split(candidate(dev_id)), "dev")
        for item in self.held.items:
            if item["family"] != "F2":
                continue
            for row in self.world.relations["public.app_sales_order"]:
                if row["po_reference"] == item["root"]["identifier"]:
                    self.assertEqual(split_of("public.app_sales_order", {"id": row["id"]}),
                                     "held_out", item["id"])

    def test_excluded_roots_are_never_sampled(self):
        banned = {item_root_digest(i) for i in self.held.items[:40]}
        again, _, _ = _generate("split-run", split="held_out", count=120,
                                exclude_roots=banned)
        self.assertEqual({item_root_digest(i) for i in again.items} & banned, set())
        self.assertGreater(again.dropped["excluded_root"], 0)

    def test_root_digest_names_relation_and_key_only(self):
        self.assertEqual(root_digest("public.app_purchase_order", {"id": "a"}),
                         root_digest("public.app_purchase_order", '{"id":"a"}'))
        self.assertNotEqual(root_digest("public.app_purchase_order", {"id": "a"}),
                            root_digest("public.app_sales_order", {"id": "a"}))

    def test_held_out_answers_reproduce(self):
        for item in self.held.items:
            if item["family"] in HELD_OUT_ONLY:
                again = run(item["family"], item["root"], item["as_known_at"], self.sources)
                self.assertEqual(again.result, item["known_outcome"], item["id"])

    def test_a_fixed_run_id_keeps_its_items_when_the_source_grows(self):
        small = build_world(sales=100, purchases=80)
        large = build_world(sales=120, purchases=96)
        picked = []
        for world in (small, large):
            sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
            generated = generate(sources, run_id="stable", now=NOW, split="held_out",
                                 count=120, documents=world.documents())
            picked.append({(i["family"], i["root"]["identifier"]) for i in generated.items})
        # Ranking (not shuffling) keeps most of the set; only better-ranked newcomers enter.
        self.assertGreater(len(picked[0] & picked[1]), 80)


class ClosedByPortalDerivationTest(unittest.TestCase):
    """A PO is closed when derivePoFulfillmentCode over its header and summed lines is
    RECEIVED, or closed_at is set (decision oracle-closed-vocabulary); the header alone
    never decides."""

    PO = "public.app_purchase_order"
    LINE = "public.app_purchase_order_line"

    def portal(self, lines):
        return PortalReads(World({self.LINE: lines}).read_relation)

    def line(self, n, qty, received):
        return {"id": f"l{n}", "purchase_order_id": "po", "quantity": qty,
                "quantity_received": received}

    def po(self, status, closed_at=None):
        return {"id": "po", "status": status, "closed_at": closed_at}

    def test_lines_decide_over_the_header(self):
        full = [self.line(1, "10", "10"), self.line(2, "4.5", "4.5")]
        short = [self.line(1, "10", "10"), self.line(2, "4.5", "4.4")]
        self.assertTrue(closed(self.PO, self.po("SUBMITTED"), self.portal(full)))
        self.assertFalse(closed(self.PO, self.po("RECEIVED"), self.portal(short)))
        # Received within Portal's 0.0001 and an over-receipt are both RECEIVED.
        near = [self.line(1, "10", "9.99995")]
        over = [self.line(1, "10", "12")]
        self.assertTrue(closed(self.PO, self.po("PARTIAL"), self.portal(near)))
        self.assertTrue(closed(self.PO, self.po("ORDERED"), self.portal(over)))

    def test_cancelled_draft_and_lineless_orders_are_not_closed(self):
        full = [self.line(1, "10", "10")]
        for header in ("CANCELLED", " canceled ", "Cancel", "DRAFT"):
            self.assertFalse(closed(self.PO, self.po(header), self.portal(full)), header)
        self.assertFalse(closed(self.PO, self.po("RECEIVED"), self.portal([])))
        # An empty received quantity counts as 0 (COALESCE), an empty ordered one is skipped.
        self.assertFalse(closed(self.PO, self.po("RECEIVED"),
                                self.portal([self.line(1, "10", None)])))
        self.assertTrue(closed(self.PO, self.po("RECEIVED"),
                               self.portal([self.line(1, None, "3"), self.line(2, "3", "3")])))

    def test_closed_at_closes_whatever_the_lines_say(self):
        self.assertTrue(closed(self.PO, self.po("SUBMITTED", "2098-01-01T00:00:00Z"),
                               self.portal([self.line(1, "10", "0")])))

    def test_partially_received_fixture_orders_are_never_sampled(self):
        generated, sources, world = _generate()
        partial = {r["po_number"] for r in world.relations[self.PO]
                   if r["status"] == "RECEIVED" and not closed(self.PO, r, sources.portal)}
        self.assertTrue(partial)  # the fixture has header-RECEIVED, short-received POs
        self.assertFalse({i["root"]["identifier"] for i in generated.items} & partial)

    def test_status_words_outside_the_portal_vocabulary_are_counted(self):
        rows = [{"status": "Open"}, {"status": "SHIPPED"}, {"status": "Shipped"},
                {"status": None}]
        self.assertEqual(out_of_vocabulary("public.app_sales_order", rows), 2)
        # the FM labels the Portal's PO writers copy are its words; another word is not
        self.assertEqual(out_of_vocabulary(self.PO, [{"status": "RECEIVED"},
                                                     {"status": "Complete"},
                                                     {"status": "B.Order"},
                                                     {"status": "Shipped"}]), 1)
        self.assertIsNone(out_of_vocabulary("public.app_manufacturing_order", rows))
        generated, _, _ = _generate()
        self.assertEqual(generated.strata["sales_order"]["status_out_of_vocabulary"], 0)
        # The demo replica's header is OPEN, which the demo script writes raw
        # (demo-july-p2p-replicas.ts:273-298) and PurchaseOrderStatus lacks.
        self.assertEqual(generated.strata["purchase_order"]["status_out_of_vocabulary"], 1)


class FixtureCollisionTest(unittest.TestCase):
    """Hand-derived from fixture_world: PO 2's vendor invoice No. is reused by another
    vendor in the second BC company; PO 10 is invoiced in both companies (58 + 1)."""

    def test_bc_collisions_leave_gaps(self):
        world = build_world()
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        known = {"time": "2099-01-02T03:04:05Z"}
        two = run("F4", {"identifier": po_number(2), "identifier_kind": "po_number"}, known,
                  sources).result
        values = {v["name"]: v["value"] for v in two["values"]}
        self.assertEqual((values["invoiced_1"], values["three_way_match"]), ("52", True))
        self.assertEqual([g["reason"] for g in two["gaps"]], ["bc_vendor_differs"])
        self.assertEqual(two["answerability"], "partial")
        ten = run("F4", {"identifier": po_number(10), "identifier_kind": "po_number"}, known,
                  sources).result
        values = {v["name"]: v["value"] for v in ten["values"]}
        self.assertEqual((values["ordered_1"], values["received_1"], values["invoiced_1"]),
                         ("60", "58", "59"))
        self.assertEqual([g["reason"] for g in ten["gaps"]], ["bc_match_spans_companies"])

    def test_document_items_keep_the_root_record_lineage(self):
        generated, _, _ = _generate()
        for item in generated.items:
            self.assertNotIn(item["lineage_stratum"], ("mail", "sharepoint"))
        self.assertTrue({"mail", "sharepoint"} <= set(generated.item_strata.values()))


if __name__ == "__main__":
    unittest.main()
