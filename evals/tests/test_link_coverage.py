"""The link-coverage family: every link kind gets mixed easy/hard questions whose keys are
the records' own answers, the 30/70 split keeps held-out text from builders, the Postgres
schema must be the one applied, and grading reports per link kind and difficulty
(made-up records)."""

from __future__ import annotations

from collections import Counter
from datetime import date
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Sequence
import unittest
from contextlib import redirect_stdout
from unittest import mock

from evals import graph_walks as gw
from evals import link_coverage as lc
from evals import live_ops
from evals import runner

AS_OF = date(2026, 11, 5)
NOW = "2026-11-05T00:00:00Z"


def oid(kind: str, binding: str, n: int) -> str:
    return f'{kind}:{binding}:{{"id":{n}}}'


class World:
    """Made-up Portal POs and suppliers, Portal PO lines, FileMaker receiving lines, the BC
    twin of each PO; ``lines`` PO lines per PO, the odd ones received."""

    def __init__(self, pos: int = 4, lines: Sequence[int] | None = None,
                 blocked: frozenset[str] = frozenset()) -> None:
        self.blocked = blocked
        self.values: dict[str, list[dict]] = {}
        self.links: list[tuple[str, str, str]] = []
        sups = [oid("Supplier", "portal.fm_supplier", n) for n in (1, 2)]
        for n, s in enumerate(sups, 1):
            self.values[s] = [{"property": "name", "value_type": "text",
                               "value": f"Supplier {'AB'[n - 1]}lpha"}]
        line_no = 0
        for p in range(1, pos + 1):
            po = oid("PurchaseOrder", "portal.fm_generated_purchase_order", p)
            bc = oid("PurchaseOrder", "bc.purchase_orders", p)
            self.values[po] = [
                {"property": "po_number", "value_type": "identifier", "value": f"PX{p:04d}"},
                {"property": "order_date", "value_type": "date", "value": f"2025-0{p}-1{p}"}]
            self.values[bc] = [{"property": "bc_number", "value_type": "identifier",
                                "value": f"BCPO-{p:05d}"}]
            self.links.append(("PurchaseOrder.ordered_from", po, sups[p % 2]))
            self.links.append(("PurchaseOrder.same_record_as", po, bc))
            for k in range((lines or [3] * pos)[p - 1]):
                line_no += 1
                line = oid("PurchaseOrderLine", "portal.app_po_line", line_no)
                self.values[line] = [
                    {"property": "item_code", "value_type": "text", "value": f"I{line_no}"},
                    {"property": "due_date", "value_type": "date",
                     "value": f"2025-{1 + line_no % 12:02d}-0{1 + k % 9}"},
                    {"property": "quantity", "value_type": "quantity",
                     "value": str(10 * line_no), "unit": "EA"}]
                self.links.append(("PurchaseOrderLine.of_purchase_order", line, po))
                if line_no % 2:
                    rec = oid("ReceivingLine", "portal.fm_receiving_line", line_no)
                    self.values[rec] = [{"property": "lot_number", "value_type": "text",
                                         "value": f"LOT{line_no:05d}"},
                                        {"property": "received_date", "value_type": "date",
                                         "value": f"2025-08-{1 + line_no % 27:02d}"}]
                    self.links.append(("ReceivingLine.for_po_line", rec, line))

    # evals.graph_walks.Records
    def sample(self, link_type):
        return [(a, b) for t, a, b in self.links if t == link_type]

    def step(self, ids, link_type, direction):
        ids = set(ids)
        if direction == "out":
            return [(a, b) for t, a, b in self.links if t == link_type and a in ids]
        return [(b, a) for t, a, b in self.links if t == link_type and b in ids]

    def rows(self, ids):
        return {i: list(self.values.get(i, [])) for i in ids}

    def versions(self, ids):
        def handle(i):
            scheme = "bc" if ":bc." in i else "api"
            return f"{scheme}://x/{i.rsplit(':', 1)[-1]}@v1"
        return {i: (handle(i), "[]") for i in ids if i in self.values}

    def lookup(self, object_type, label):
        out = []
        for i, rows in self.values.items():
            named = gw.label_of(rows)
            if gw.object_type(i) == object_type and named and \
                    named[0].casefold() == label.casefold():
                out.append(i)
        return sorted(out)

    def cleared(self, ids):
        return not (set(ids) & self.blocked)


RAW = {
    "PurchaseOrder.ordered_from": {"from": "PurchaseOrder", "to": "Supplier",
                                   "name": "ordered_from", "reverse_name": "purchase_orders"},
    "PurchaseOrder.same_record_as": {"from": "PurchaseOrder", "to": "PurchaseOrder",
                                     "name": "same_record_as", "reverse_name": "same_record_as"},
    "PurchaseOrderLine.of_purchase_order": {"from": "PurchaseOrderLine", "to": "PurchaseOrder",
                                            "name": "of_purchase_order", "reverse_name": "lines"},
    "ReceivingLine.for_po_line": {"from": "ReceivingLine", "to": "PurchaseOrderLine",
                                  "name": "for_po_line", "reverse_name": "receiving_lines"},
    "Email.mentions_purchase_order": {"from": "Email", "to": "PurchaseOrder",
                                      "name": "mentions_purchase_order",
                                      "reverse_name": "mentioned_in_email"},
    "RelX.mirror": {"from": "RelX", "to": "PurchaseOrder", "name": "mirror"},
}


def build(world=None, seed=7):
    return lc.build(lc.link_types(RAW), world or World(), seed=seed, as_of=AS_OF, now=NOW,
                    generations={"postgres": "x"})


class LinkCoverageTest(unittest.TestCase):
    def test_every_link_kind_gets_mixed_items_keyed_by_the_records(self):
        world = World()
        items, keys, stats = build(world)
        per = Counter(k["strata"]["link_kind"] for k in keys)
        linked = {"PurchaseOrder.ordered_from", "PurchaseOrder.same_record_as",
                  "PurchaseOrderLine.of_purchase_order", "ReceivingLine.for_po_line"}
        self.assertNotIn("RelX.mirror", stats["per_link_kind"])
        self.assertEqual(stats["link_kinds_without_firm_links"], ["Email.mentions_purchase_order"])
        for link in linked:
            self.assertGreaterEqual(per[link], lc.TARGET_PER_LINK, (link, stats["dropped"]))
            got = {k["strata"]["difficulty"] for k in keys if k["strata"]["link_kind"] == link}
            self.assertIn("easy", got, link)
            self.assertIn("hard", got, link)
        self.assertEqual(stats["link_kinds_at_target"], len(linked))
        for item, key in zip(items, keys):
            self.assertEqual(runner.validate_item(item), [])
            self.assertEqual(live_ops.key_problems(key), [])
            self.assertEqual(item["family"], lc.FAMILY)
            self.assertTrue(item["question"]["text"].startswith("As of November 5, 2026, "))
        # keys recomputed from the made-up records, independently of the generator
        received = {b for t, _a, b in world.links if t == "ReceivingLine.for_po_line"}
        lines_of = {}
        for t, a, b in world.links:
            if t == "PurchaseOrderLine.of_purchase_order":
                lines_of.setdefault(b, []).append(a)
        by_text = {i["question"]["text"]: k for i, k in zip(items, keys)}
        checked = Counter()
        for text, key in by_text.items():
            s, facts = key["strata"], {f["id"]: f for f in key["facts"]}
            if s["template"] == "twin" and s["link_kind"] == "PurchaseOrder.same_record_as":
                named = text.split(" as ")[-1].split("?")[0].split()[-1]
                p = int(named.removeprefix("PX").removeprefix("BCPO-"))
                twin = f"BCPO-{p:05d}" if named.startswith("PX") else f"PX{p:04d}"
                self.assertEqual(facts["list"]["members"], [[twin]])
                self.assertEqual(facts["list"]["allowed"], ["bc" if "BC" in twin else "api"])
                checked["twin"] += 1
            if s["template"] == "without" and s["link_types"] == [
                    "PurchaseOrderLine.of_purchase_order", "ReceivingLine.for_po_line"]:
                po = next(p for p in lines_of if world.values[p][0]["value"] in text)
                lacking = sorted(world.values[l][0]["value"] for l in lines_of[po]
                                 if l not in received)
                got = [m[0] for m in facts["list"]["members"]] if "list" in facts else None
                self.assertEqual(got, lacking) if got is not None else \
                    self.assertEqual(int(facts["count"]["value"]), len(lacking))
                checked["without"] += 1
        self.assertTrue(checked["twin"] and checked["without"], checked)

    def test_link_kinds_dealt_over_workers_give_the_same_questions(self):
        links, world = lc.link_types(RAW), World()
        whole = lc.compose(links, world, seed=7, as_of=AS_OF)
        ids = sorted(l.id for l in links)
        parts = [lc.compose(links, World(), seed=7, as_of=AS_OF, only=ids[k::3])
                 for k in range(3)]
        self.assertEqual(sorted(q.text for q in whole[0]),
                         sorted(q.text for p in parts for q in p[0]))

    def test_counts_hold_every_record_at_one_ten_and_sixty(self):
        for n in (1, 10, 60):
            world = World(pos=2, lines=[n, n])
            _items, keys, _stats = build(world, seed=n)
            counts = [k for k in keys if k["strata"]["template"] == "count"
                      and k["strata"]["link_types"] == ["PurchaseOrderLine.of_purchase_order"]]
            for key in counts:
                fact = key["facts"][0]
                if fact["kind"] == "number":   # from a PO: its lines; from a line: its PO
                    self.assertIn(int(fact["value"]), (n, 1), n)
                    if int(fact["value"]) == n:
                        self.assertEqual(len(set(fact["records"])), min(n, 60))

    def test_an_excluded_person_never_reaches_a_question(self):
        blocked = oid("Supplier", "portal.fm_supplier", 1)
        world = World(blocked=frozenset({blocked}))
        items, keys, _stats = build(world)
        self.assertTrue(keys)
        for item, key in zip(items, keys):
            self.assertNotIn("Supplier Alpha", item["question"]["text"])
            for fact in key["facts"]:
                self.assertNotIn(["Supplier Alpha"], fact.get("members") or [])

    def test_split_is_thirty_seventy_by_start_and_held_out_text_stays_with_graders(self):
        items, keys, stats = build()
        self.assertEqual(stats["dev"] + stats["held_out"], len(items))
        self.assertTrue(0.2 <= stats["dev"] / len(items) <= 0.4, stats)
        starts = {}
        for item in items:
            starts.setdefault(item["root"]["identifier"], set()).add(item["split"])
        self.assertTrue(all(len(s) == 1 for s in starts.values()))
        from scripts import eval_live_ops as lo
        from scripts import eval_link_coverage as cli
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PLX_WORKBENCH_API_KEY": "marker", "PLX_EVAL_HELD_OUT_DIR": f"{tmp}/held"}
            dev_dir, runs = Path(tmp) / "dev", Path(tmp) / "runs"
            with mock.patch.dict(os.environ, env):
                held = lo.held_out_dir(dev_dir)
                for split, directory in (("dev", dev_dir), ("held_out", held)):
                    chosen = [(i, k) for i, k in zip(items, keys) if i["split"] == split]
                    lo.write_split(directory, [i for i, _ in chosen], [k for _, k in chosen])
                self.assertEqual(len(list((dev_dir / "items").iterdir())), stats["dev"])
                self.assertFalse(str(held).startswith(str(dev_dir)))
                ho = [(i, k) for i, k in zip(items, keys) if i["split"] == "held_out"]
                for item, key in [p for p in zip(items, keys) if p[0]["split"] == "dev"][:2] + ho[:2]:
                    (runs / item["id"]).mkdir(parents=True)
                    say = "; ".join(str(f.get("value") or f.get("members"))
                                    for f in key["facts"])
                    (runs / item["id"] / "answer.sse").write_text(
                        "event: x\ndata: {\"stage\": 1}\n"
                        f"data: {json.dumps({'direct_answer': say, 'outcome': 'answered'})}\n")
                (Path(tmp) / "times.txt").write_text(
                    "".join(f"{i['id']} 0 9 9 attempt=1\n" for i in items))
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(["grade", "--dev-dir", str(dev_dir), "--runs", str(runs),
                                     "--times", str(Path(tmp) / "times.txt"), "--per-item"])
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["items"], 4)
            self.assertEqual(sum(c["items"] for c in report["by_difficulty"].values()), 4)
            self.assertEqual(sum(c["seconds"] for c in report["by_link_kind"].values()), 36)
            for item, _k in ho:
                self.assertNotIn(item["question"]["text"], out.getvalue())

    def test_grades_correct_incomplete_wrong_abstain(self):
        key = {"facts": [{"id": "count", "kind": "number", "value": "12"},
                         {"id": "list", "kind": "set", "members": [["PX1"], ["PX2"]]}]}
        self.assertEqual(lc.grade({"direct_answer": "Twelve: PX1 and PX2"}, key)[0], "correct")
        self.assertEqual(lc.grade({"direct_answer": "12 lines, PX1"}, key)[0], "incomplete")
        self.assertEqual(lc.grade({"direct_answer": "There are 7 lines."}, key)[0], "wrong")
        self.assertEqual(lc.grade({"direct_answer": "I could not find that PO."}, key)[0],
                         "abstain")
        self.assertEqual(lc.grade(None, key)[0], "abstain")
        none = {"facts": [{"id": "none", "kind": "absent", "value": "no lines",
                           "accept": ["no lines", *gw.NONE_FORMS]}]}
        self.assertEqual(lc.grade({"direct_answer": "There are no lines."}, none)[0], "correct")
        with self.assertRaises(ValueError):
            lc.grade({"direct_answer": "x"}, {"facts": [{"id": "q", "kind": "new"}]})
        with self.assertRaises(ValueError):
            lc.grade_report([{"key": {"strata": {}}, "grade": "maybe"}])

    def test_keys_need_the_applied_schema(self):
        from scripts import eval_link_coverage as cli
        from scripts.eval_graph_walks import Refused
        held = {"schema_digest": "sha256:19ec2a62aa"}
        cli.check_schema(held, "sha256:19ec2a62aa", "19ec2a62")
        for compiled, expect in (("sha256:5f8d6cbf", None), ("sha256:19ec2a62aa", "5f8d")):
            with self.assertRaises(Refused):
                cli.check_schema(held, compiled, expect)
        with self.assertRaises(Refused):
            cli.check_schema({}, "sha256:19ec2a62aa", None)


if __name__ == "__main__":
    unittest.main()
