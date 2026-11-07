"""The graph-walk family: walks executed over every link, filters, none answers, exclusion,
valid keys and the stratified sample (made-up records)."""

from __future__ import annotations

from datetime import date
import random
import unittest

from evals import graph_walks as gw
from evals import live_ops
from evals import runner

AS_OF = date(2026, 11, 5)
SUP = 'Supplier:portal.fm_supplier:{"id":1}'
OTHER = 'Supplier:portal.fm_supplier:{"id":2}'
POS = [f'PurchaseOrder:portal.fm_generated_purchase_order:{{"id":{n}}}' for n in range(1, 6)]


def po_rows(n: int) -> list[dict]:
    day = f"2025-0{n}-10"
    return [{"property": "po_number", "value_type": "identifier", "value": f"PX{n:04d}"},
            {"property": "order_date", "value_type": "date", "value": day},
            {"property": "status", "value_type": "text", "value": "Open" if n % 2 else "Closed"},
            {"property": "total_cost", "value_type": "money", "value": str(100 * n),
             "currency": "CAD", "markings": '["finance"]'}]


class FakeRecords:
    def __init__(self, blocked: frozenset[str] = frozenset()) -> None:
        self.blocked = blocked
        self.links = [(po, SUP) for po in POS] + [(POS[0], OTHER)]

    def sample(self, link_type):
        return [(POS[0], SUP)]

    def step(self, ids, link_type, direction):
        if direction == "out":
            return [(a, b) for a, b in self.links if a in ids]
        return [(b, a) for a, b in self.links if b in ids]

    def rows(self, ids):
        out = {}
        for oid in ids:
            if oid == SUP:
                out[oid] = [{"property": "name", "value_type": "text", "value": "Acme Oils"}]
            elif oid == OTHER:
                out[oid] = [{"property": "name", "value_type": "text", "value": "Bolt Inc"}]
            else:
                out[oid] = po_rows(POS.index(oid) + 1)
        return out

    def versions(self, ids):
        return {i: (f"api://public.x/{i.rsplit(':', 1)[-1]}@v1", "[]") for i in ids}

    def lookup(self, object_type, label):
        return [i for i, rows in self.rows([SUP, OTHER, *POS]).items()
                if gw.label_of(rows) and gw.label_of(rows)[0].casefold() == label.casefold()]

    def cleared(self, ids):
        return not (set(ids) & self.blocked)


LINK = gw.Link("PurchaseOrder.ordered_from", "ordered_from", "purchase_orders",
               "PurchaseOrder", "Supplier", 6)


def walks(records, n=60, seed=3):
    composer = gw.Composer([LINK], records, AS_OF)
    rng = random.Random(seed)
    out, dropped = [], []
    for _ in range(n):
        try:
            out.append(composer.compose(LINK.id, rng))
        except gw.Dropped as why:
            dropped.append(str(why))
    return out, dropped


class GraphWalkTest(unittest.TestCase):
    def test_answers_execute_the_whole_walk_with_its_filter(self):
        made, _ = walks(FakeRecords())
        self.assertTrue(made)
        for walk in made:
            facts = {f["id"]: f for f in walk.facts}
            if walk.kind == "count" and walk.start == SUP:
                # the supplier's POs passing the filter, counted over every link
                self.assertLessEqual(int(facts["count"]["value"]), 5)
                self.assertIn("Acme Oils", walk.question)
            if walk.kind == "sum":
                self.assertEqual(facts["sum"].get("markings"), ["finance"])
            if walk.kind == "none":
                self.assertEqual(facts["none"]["kind"], "absent")
            self.assertTrue(walk.question.startswith("As of November 5, 2026, "))

    def test_a_walk_touching_an_excluded_record_is_dropped(self):
        made, dropped = walks(FakeRecords(blocked=frozenset({SUP})))
        self.assertFalse([w for w in made if w.start == SUP])
        self.assertIn("people_excluded", dropped)

    def test_items_and_keys_are_valid_and_the_sample_is_stratified(self):
        made, _ = walks(FakeRecords())
        pairs = []
        for n, walk in enumerate(made):
            item, key = gw.item_and_key(f"dev-F31-{n:04d}", walk, now="2026-11-05T00:00:00Z",
                                        as_of=AS_OF, generations={"projection": "x"},
                                        digest="0" * 64)
            self.assertEqual(runner.validate_item(item), [])
            self.assertEqual(live_ops.key_problems(key), [])
            pairs.append((item, key))
        strata = {gw.stratum(k) for _i, k in pairs}
        picked = gw.stratified(pairs, len(strata), seed=1)
        by_id = {i["id"]: gw.stratum(k) for i, k in pairs}
        self.assertEqual({by_id[i] for i in picked}, strata)
        self.assertEqual(sum(gw.coverage([k for _i, k in pairs])["hops"].values()), len(pairs))

    def test_every_linked_type_gets_its_floor(self):
        rare = gw.Link("A.b", "b", "as", "A", "B", 1)
        common = gw.Link("C.d", "d", "cs", "C", "D", 1_000_000)
        quota = gw.quotas([rare, common, gw.Link("E.f", "f", "es", "E", "F", 0)], 100)
        self.assertEqual(set(quota), {"A.b", "C.d"})
        self.assertGreaterEqual(quota["A.b"], gw.FLOOR)


if __name__ == "__main__":
    unittest.main()
