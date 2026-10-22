"""Metamorphic held-out variants over perturbed source reads (ENG-12).

Evidence label: ``injected-transport`` fixture, never live. Every check compares a variant's
oracle answer with its parent's: the oracle recomputed both, nothing was patched by hand.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import unittest

from evals import runner, score as sc
from evals.admission import admissible
from evals.oracle import BcReads, PortalReads, Sources, run
from evals.oracle import keys as K
from evals.oracle import perturb
from evals.oracle.fixture_world import build_eval_world
from evals.oracle.generator import generate
from evals.oracle.source import canonical
from evals.tests import fixtures as fx
from ontology.json_contract import validate

NOW = "2099-01-02T04:00:00Z"
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "item.schema.json")
                    .read_text(encoding="utf-8"))
QUANTITY_PREFIXES = ("ordered", "received", "invoiced", "shipped", "produced")
VERDICTS = ("match_", "three_way_match", "short_", "shipped_short", "short_item_count")


def _sources(world):
    sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
    sources.portal.prefetch()
    return sources


def _values(item):
    return {v["name"]: v.get("value") for v in item["known_outcome"]["values"]}


def _reasons(result):
    return {g["reason"] for g in result["gaps"]}


def _chain_relations(item):
    return {c["relation"] for c in item["known_outcome"].get("chain") or []}


class PerturbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = build_eval_world()
        cls.pristine = cls.state(cls.world)
        cls.base = _sources(cls.world)
        cls.generation = cls.base.portal.generation()
        cls.generated = generate(
            cls.base, run_id="perturb-test", now=NOW, count=120, split="held_out",
            audit=0, id_suffix="fixture", documents=cls.world.documents(),
            perturb=lambda items: perturb.variants(cls.base, items, now=NOW))
        cls.items = {i["id"]: i for i in cls.generated.items}
        cls.parents = [i for i in cls.generated.items if i["variant"] == "standard"]

    @staticmethod
    def state(world):
        return canonical([world.relations, world.bc, world.bc_lines, world.generated_at,
                          sorted(world.fail)])

    def pairs(self, transform):
        out = [(self.items[i["parent_id"]], i) for i in self.generated.items
               if i["variant"] == transform]
        self.assertTrue(out, transform)
        return out

    def test_variants_are_held_out_children_and_bad_input_fails_closed(self):
        self.assertEqual(len(self.parents), 120)
        self.assertEqual(len(self.items), len(self.generated.items))  # ids unique
        variants = [i for i in self.generated.items if i["variant"] != "standard"]
        self.assertEqual({i["variant"] for i in variants}, set(perturb.TRANSFORMS))
        self.assertGreaterEqual(len(variants), 5 * len(self.parents))
        for item in self.generated.items:
            self.assertEqual(validate(item, SCHEMA, root=SCHEMA), [], item["id"])
        for item in variants:
            self.assertEqual(item["split"], "held_out")
            self.assertEqual(item["perturbation"]["transform"], item["variant"])
            self.assertEqual(self.items[item["parent_id"]]["root"], item["root"])
            self.assertEqual(self.generated.item_strata[item["id"]],
                             self.generated.item_strata[item["parent_id"]])
        # Perturbed reads only: the world and the run's own reads are never changed.
        self.assertEqual(self.state(self.world), self.pristine)
        self.assertEqual(self.base.portal.generation(), self.generation)
        # A variant never reaches the dev split, by generator or by schema.
        with self.assertRaises(ValueError):
            generate(_sources(self.world), run_id="x", now=NOW, split="dev",
                     perturb=lambda items: [])
        dev_like = dict(variants[0], split="dev", id="dev" + variants[0]["id"][2:])
        self.assertNotEqual(validate(dev_like, SCHEMA, root=SCHEMA), [])
        with self.assertRaises(ValueError):
            perturb.variants(self.base, [dict(self.parents[0], split="dev")], now=NOW)
        for transform, params in (("rename_everything", {}), ("scaled_quantity", {"factor": "0"}),
                                  ("scaled_quantity", {"factor": "x"}),
                                  ("shifted_dates", {"days": 0}),
                                  ("conflicting_units", {"mode": "guess"}),
                                  ("partial_source_failure", {"source": "public.nowhere"}),
                                  ("repeated_event", {"times": 3})):
            with self.assertRaises(ValueError, msg=transform):
                perturb.sources(self.base, transform, params)
        # No runner host serves perturbed reads: a product run never scores a variant.
        self.assertIn("perturbed_reads_not_served", admissible(variants[0]))
        self.assertNotIn("perturbed_reads_not_served", admissible(self.parents[0]))
        # One variant per parent when rotated, none lost or doubled at any cardinality.
        for n in (1, 10, 60):
            rotated = perturb.variants(self.base, self.parents[:n], now=NOW, rotate=True)
            self.assertEqual(sorted(v["parent_id"] for v in rotated),
                             sorted(p["id"] for p in self.parents[:n]), n)
            self.assertEqual(len({v["id"] for v in rotated}), n)
        # A harness rebuilds a variant's reads from the item: the same answer again.
        rebuilt = perturb.Perturbed(self.base)
        for item in variants[::40]:
            again = run(item["family"], item["root"], item["as_known_at"], rebuilt.of(item))
            self.assertEqual(again.result, item["known_outcome"], item["id"])

    def test_quantity_x2_doubles_totals_and_keeps_verdicts(self):
        doubled = 0
        for parent, variant in self.pairs("scaled_quantity"):
            before, after = _values(parent), _values(variant)
            self.assertEqual(set(before), set(after), variant["id"])
            for name, value in before.items():
                if name.startswith(VERDICTS):
                    self.assertEqual(after[name], value, (variant["id"], name))
                elif name.startswith(QUANTITY_PREFIXES) and value is not None:
                    self.assertEqual(Decimal(after[name]), 2 * Decimal(value), variant["id"])
                    doubled += 1
            self.assertEqual(_reasons(variant["known_outcome"]),
                             _reasons(parent["known_outcome"]))
        self.assertGreater(doubled, 100)
        # The quantity columns are the schema's (keys.Relation.quantities): money stays.
        scaled = perturb.portal_port(self.world.read_relation, "scaled_quantity")(
            K.PO_LINE, K.RELATIONS[K.PO_LINE].columns)["rows"]
        for row, before in zip(scaled, self.world.read_relation(
                K.PO_LINE, K.RELATIONS[K.PO_LINE].columns)["rows"]):
            self.assertEqual(row["unit_price"], before["unit_price"])
            if before["quantity"] is not None:
                self.assertEqual(Decimal(str(row["quantity"])),
                                 2 * Decimal(str(before["quantity"])))

    def test_a_repeated_event_is_not_double_counted_and_keyless_rows_both_count(self):
        for parent, variant in self.pairs("repeated_event"):
            self.assertEqual(variant["known_outcome"], parent["known_outcome"], variant["id"])
        # The perturbed reads really deliver every receipt and BC document twice.
        read = perturb.portal_port(self.world.read_relation, "repeated_event")
        self.assertEqual(len(read(K.RECEIPT, ["id"])["rows"]),
                         2 * len(self.world.relations[K.RECEIPT]))
        invoice = self.world.bc["purchaseInvoices"][0]["fields"]
        bc = perturb.bc_port(self.world.bc_port(), "repeated_event")
        self.assertEqual(len(bc.find("purchaseInvoices", "number", invoice["number"])["records"]),
                         2 * len(self.world.bc_port().find("purchaseInvoices", "number",
                                                           invoice["number"])["records"]))
        # Two rows equal in every column collapse only when they carry the source's key:
        # without one they may be two real events (keyless FM status rows), so both count.
        status = self.world.read_relation(K.FM_AO_STATUS, ["id", "idassembly", "qtymanuf"])
        twice = {**status, "rows": status["rows"] + status["rows"][:1]}
        keyless = PortalReads(lambda name, columns: {**twice, "key": []})
        self.assertEqual(len(keyless.rows(K.FM_AO_STATUS)), len(status["rows"]) + 1)
        unkeyed = PortalReads(lambda name, columns: {
            **twice, "rows": [{**r, "id": None} for r in twice["rows"]]})
        self.assertEqual(len(unkeyed.rows(K.FM_AO_STATUS)), len(status["rows"]) + 1)
        keyed = PortalReads(lambda name, columns: twice)
        self.assertEqual(len(keyed.rows(K.FM_AO_STATUS)), len(status["rows"]))
        record = self.world.bc["purchaseInvoices"][0]
        line = {"entity_set": "purchaseInvoiceLines", "fields": {"quantity": 5}}

        class Lines:  # a BC line read without the records' ids
            def lines(self, _):
                return {"records": [line, dict(line)], "complete": True}

        self.assertEqual(len(BcReads(Lines()).lines(record)[0]), 2)

    def test_twins_share_a_name_never_a_code_and_change_no_answer(self):
        for parent, variant in self.pairs("ambiguous_party"):
            # A twin is never reached: F1 walks to a supplier or customer by id, and a BC
            # vendor No. is the supplier's code, which no twin shares. Customer twins leave
            # F2 unchanged because F2 answers from the customer PO's sales orders and their
            # shipments and never reads the customer row.
            self.assertEqual(_values(variant), _values(parent), variant["id"])
            self.assertEqual(_reasons(variant["known_outcome"]), _reasons(parent["known_outcome"]))
            self.assertEqual(variant["known_outcome"]["answerability"],
                             parent["known_outcome"]["answerability"])
        world = build_eval_world()
        for relation, column in ((K.SUPPLIER, "name"), (K.CUSTOMER, "company_name")):
            for n, row in enumerate(world.relations[relation]):
                row[column] = f"FX Party {n}"
            rows = perturb.portal_port(world.read_relation, "ambiguous_party")(
                relation, ["id", "code", column])["rows"]
            self.assertEqual(len(rows), 2 * len(world.relations[relation]))
            by_name: dict[str, list[dict]] = {}
            for row in rows:
                by_name.setdefault(row[column], []).append(row)
            for twins in by_name.values():
                self.assertEqual(len(twins), 2, relation)
                self.assertEqual(len({t["id"] for t in twins}), 2)
                self.assertEqual(len({t["code"] for t in twins}), 2)
            self.assertEqual(len({r["code"] for r in rows}), len(rows))
        # Codes longer than BC's Code width: each twin's BC number (trimmed, cut, case
        # folded) is still its own, so no parent's identity turns doubtful.
        long_codes = build_eval_world()
        for n, row in enumerate(long_codes.relations[K.SUPPLIER]):
            row["code"] = f"FXV{n:03d}-LONG-SUPPLIER-CODE"   # 25 characters
        rows = perturb.portal_port(long_codes.read_relation, "ambiguous_party")(
            K.SUPPLIER, ["id", "code"])["rows"]
        self.assertTrue(all(len(r["code"]) > K.BC_PARTY_NUMBER_MAX for r in rows[:2]))
        numbers = [K.vendor_bc_number(r["code"]).casefold() for r in rows]
        self.assertEqual(len(set(numbers)), len(rows))
        # Codes unique as stored but one BC vendor No. (BC compares Code ignoring case):
        # the PO's vendor is unverifiable, never matched by code.
        for row in list(world.relations[K.SUPPLIER]):
            world.relations[K.SUPPLIER].append({**row, "id": row["id"] + "-case",
                                                "code": row["code"].lower()})
        cased = _sources(world)
        gapped = 0
        for parent in self.parents:
            if "bc.purchase_invoices" in _chain_relations(parent):
                result = run(parent["family"], parent["root"], parent["as_known_at"], cased).result
                self.assertIn("supplier_code_not_unique", _reasons(result), parent["id"])
                self.assertIn("bc_vendor_unverifiable", _reasons(result), parent["id"])
                self.assertEqual(result["answerability"], "partial")
                gapped += 1
        self.assertGreater(gapped, 10)

    def test_mixed_units_convert_or_gap(self):
        gapped = 0
        for parent, variant in self.pairs("conflicting_units"):
            before, after = _values(parent), _values(variant)
            for name, value in before.items():
                if name.startswith(VERDICTS) and value is not None and name in after:
                    # Another unit is never converted: no verdict, a unit gap.
                    self.assertIsNone(after[name], (variant["id"], name))
                    self.assertIn("unit_differs", _reasons(variant["known_outcome"]))
                    gapped += 1
            for value in variant["known_outcome"]["values"]:
                # Never a sum across units: every quantity names one unit.
                if value["name"].startswith(QUANTITY_PREFIXES) and value.get("value"):
                    self.assertIn("unit", value, (variant["id"], value["name"]))
        self.assertGreater(gapped, 10)
        # The same unit spelled another way normalises: every parent answer holds.
        alias = perturb.sources(self.base, "conflicting_units", {"mode": "alias"})
        for parent in self.parents:
            if parent["family"] in ("F4", "F6"):
                again = run(parent["family"], parent["root"], parent["as_known_at"], alias)
                verdicts = {k: v for k, v in _values(parent).items() if k.startswith(VERDICTS)}
                self.assertEqual({k: v for k, v in _values({"known_outcome": again.result})
                                  .items() if k.startswith(VERDICTS)}, verdicts, parent["id"])

    def test_a_failed_source_gives_partial_and_a_gap_and_never_a_changed_value(self):
        bc_sets = {chain: entity for entity, (_, chain) in K.BC_OBJECTS.items()}
        reads: dict[str, Sources] = {}
        cases = [(parent, variant["perturbation"]["params"]["source"], variant["known_outcome"])
                 for parent, variant in self.pairs("partial_source_failure")]
        # Beyond the variants: every other source each parent's chain reaches.
        for parent in self.parents:
            for relation in sorted(_chain_relations(parent) - {parent["root"]["relation"]}):
                source = bc_sets.get(relation, relation)
                if source not in reads:
                    reads[source] = perturb.sources(self.base, "partial_source_failure",
                                                    {"source": source})
                cases.append((parent, source, run(parent["family"], parent["root"],
                                                  parent["as_known_at"], reads[source]).result))
        self.assertGreater(len(cases), 300)
        bc_types = {object_type for object_type, _ in K.BC_OBJECTS.values()}
        kept = 0
        for parent, source, result in cases:
            label = (parent["id"], source)
            self.assertEqual(result["answerability"], "partial", label)
            self.assertTrue(any(g["state"] == "inaccessible" and source in g["subject"]
                                for g in result["gaps"]), label)
            before = _values(parent)
            # Documents found only by their own order's number lead back to it alone.
            back_to_order = source in K.BC_OBJECTS and all(
                e.source in (K.SO, K.PO) for e in K.EDGES if e.target == "bc:" + source)
            for value in result["values"]:
                # A failed read only removes knowledge: a value is the parent's, null, or a
                # count typed as a lower bound of the parent's.
                if value.get("bound") == "lower":
                    self.assertLessEqual(value["value"], before[value["name"]],
                                         (*label, value["name"]))
                elif value.get("value") is not None:
                    self.assertEqual(value["value"], before.get(value["name"]),
                                     (*label, value["name"]))
                # Such a failed BC read leaves the counts of the fully read Portal exact.
                if (parent["family"] == "F1" and back_to_order
                        and value["name"].endswith("_count")
                        and value.get("object_type") not in bc_types):
                    self.assertEqual(value["value"], before[value["name"]],
                                     (*label, value["name"]))
                    self.assertNotIn("bound", value, (*label, value["name"]))
                    kept += value["value"] is not None
        self.assertGreater(kept, 10)

    def test_a_failed_receipt_read_makes_the_bc_invoice_count_a_lower_bound_scored_undecided(self):
        reads = perturb.sources(self.base, "partial_source_failure", {"source": K.RECEIPT})
        name = "bc_purchase_invoice_count"
        bounds = []
        for parent in self.parents:
            if (parent["family"] != "F1" or parent["root"]["relation"] == K.RECEIPT
                    or not {K.RECEIPT, "bc.purchase_invoices"} <= _chain_relations(parent)):
                continue
            # The receipts lead to BC purchase invoices by their vendor invoice No., which
            # no other record of the walk reaches: with them unread, the invoices the walk
            # still found (by the PO's number) are a lower bound, never an exact count.
            result = run(parent["family"], parent["root"], parent["as_known_at"], reads).result
            value = {v["name"]: v for v in result["values"]}.get(name)
            if value is not None:
                self.assertEqual(value["bound"], "lower", parent["id"])
                self.assertLessEqual(value["value"], _values(parent)[name])
                bounds.append(value)
        self.assertGreater(len(bounds), 3)
        # The scorer leaves a lower bound undecided: neither a pass nor a failure.
        item = fx.item()
        item["known_outcome"]["values"].append(bounds[0])
        self.assertEqual(runner.validate_item(item), [])
        transcript = runner.record(item, runner.ScriptedAgent(fx.scripted(fx.good_answer())),
                                   fx.FakeHost())
        result = sc.score(item, transcript)
        undecided = [v for v in result["values"] if v["name"] == name]
        self.assertEqual([(v["passed"], v.get("undecided"), v["codes"]) for v in undecided],
                         [(None, True, [])])
        self.assertTrue(result["passed"], result)

    def test_shifted_dates_move_answers_and_stay_fresh(self):
        days = perturb.DEFAULTS["shifted_dates"]["days"]
        moved = 0
        for parent, variant in self.pairs("shifted_dates"):
            self.assertEqual(_reasons(variant["known_outcome"]),
                             _reasons(parent["known_outcome"]), variant["id"])
            self.assertEqual(date.fromisoformat(variant["as_known_at"]["time"][:10]),
                             date.fromisoformat(parent["as_known_at"]["time"][:10])
                             + timedelta(days=days))
            for name, value in _values(parent).items():
                if name.startswith("required_date") and value is not None:
                    self.assertEqual(date.fromisoformat(_values(variant)[name]),
                                     date.fromisoformat(value) + timedelta(days=days))
                    moved += 1
        self.assertGreater(moved, 10)
        # A date nested in a JSON column moves too, as an object or as JSON text.
        nested = {"shipBy": "2099-01-02", "legs": [{"eta": "2099-01-03T04:00:00Z"}], "n": 7}
        rows = perturb.portal_port(lambda name, columns: {"key": ["id"], "rows": [
            {"id": "a", "custom_fields": nested},
            {"id": "b", "custom_fields": json.dumps(nested)}]}, "shifted_dates", {"days": 3})(
            K.SO, [])["rows"]
        want = {"shipBy": "2099-01-05", "legs": [{"eta": "2099-01-06T04:00:00Z"}], "n": 7}
        self.assertEqual(rows[0]["custom_fields"], want)
        self.assertEqual(json.loads(rows[1]["custom_fields"]), want)


if __name__ == "__main__":
    unittest.main()
