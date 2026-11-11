"""Novel cross-system find and connection questions: the truth is every linked record of
another system (or every record joining two), over every link type; any draw that cannot
be settled whole is dropped (made-up records)."""

from __future__ import annotations

from datetime import date
import random
import unittest

from evals import generative as gen
from evals import graph_walks as gw
from evals import novel_walks as nw

AS_OF = date(2026, 11, 11)
BC = 'Customer:bc.customers:{"id":"c1"}'
FM = 'Customer:portal.fm_customer:{"id":10}'
MAIL = 'Email:graph_mail.messages:{"id":"m1"}'
SAGE = 'SalesOrder:sage.sales_orders:{"id":77}'
LINE = 'SalesOrderLine:portal.app_order_line:{"id":1}'   # the Portal's
NAMES = {BC: "Acme Oils", FM: "ACME OILS LTD", MAIL: "Re: Acme reorder", SAGE: "SO-77"}
HANDLES = {BC: "bc://production/c/customers/c1@v", FM: "api://public.customer/10@v",
           MAIL: "graph-mail://box/m1@v", SAGE: "api://public.sage_so/77@v",
           LINE: "api://public.sage_sol/1@v"}
LINKS = [gw.Link("Customer.same_record_as", "same_record_as", "same_record_as",
                 "Customer", "Customer", 10),
         gw.Link("Email.mentions_customer", "mentions_customer", "emails", "Email",
                 "Customer", 10),
         gw.Link("SalesOrder.placed_by", "placed_by", "sales_orders", "SalesOrder",
                 "Customer", 10),
         gw.Link("Email.mentions_sales_order", "mentions_sales_order", "emails", "Email",
                 "SalesOrder", 10),
         gw.Link("SalesOrderLine.line_of", "line_of", "lines", "SalesOrderLine",
                 "SalesOrder", 10)]


class Records:
    def __init__(self, blocked=frozenset(), extra=(), names=None, props=None) -> None:
        self.blocked = blocked
        self.props = props or {}   # oid -> [(property, value)] beside its name
        self.names = dict(NAMES if names is None else names)
        self.edges = {("Customer.same_record_as", BC, FM), ("Email.mentions_customer", MAIL, BC),
                      ("SalesOrder.placed_by", SAGE, FM),
                      ("Email.mentions_sales_order", MAIL, SAGE), *extra}

    def sample(self, link_type):
        return [(a, b) for t, a, b in sorted(self.edges) if t == link_type][:1]

    def step(self, ids, link_type, direction):
        if direction == "out":
            return [(a, b) for t, a, b in self.edges if t == link_type and a in ids]
        return [(b, a) for t, a, b in self.edges if t == link_type and b in ids]

    def rows(self, ids):
        return {i: ([{"property": "name", "value_type": "text", "value": self.names[i]}]
                    if i in self.names else [])
                + [{"property": p, "value_type": "text", "value": v}
                   for p, v in self.props.get(i, ())] for i in ids}

    def versions(self, ids):
        return {i: (HANDLES[i], "[]") for i in ids if i in HANDLES}

    def lookup(self, object_type, label):
        return [i for i, n in self.names.items()
                if gw.object_type(i) == object_type and n.casefold() == label.casefold()]

    def cleared(self, ids):
        return not set(ids) & self.blocked


def draw(records, card, start=BC):
    composer = nw.NovelComposer(LINKS, records, AS_OF)
    return getattr(composer, card)(start, records.names[start], random.Random(1))


def members(item):
    return sorted(m[0] for m in item["facts"][0]["members"])


class NovelWalkTest(unittest.TestCase):
    def test_elsewhere_is_every_linked_record_of_another_system_over_any_link(self):
        item = draw(Records(), "elsewhere")
        # the FileMaker copy (identity link) and the mail (a mention), not asked by name
        self.assertEqual(members(item), ["ACME OILS LTD", "Re: Acme reorder"])
        self.assertIn("Business Central", item["question"])
        self.assertNotIn("mention", item["question"])
        self.assertEqual(item["facts"][0]["allowed"], ["api", "graph-mail"])

    def test_bridge_is_every_record_linked_to_both_ends(self):
        item = draw(Records(), "bridge")
        self.assertIn("SO-77", item["question"])
        self.assertEqual(members(item), ["ACME OILS LTD", "Re: Acme reorder"])

    def test_a_member_is_named_by_its_own_identifier_too(self):
        """dev 11-11: an answer naming the linked Sage item by its item code graded as
        missing it. A member's forms hold its own number or code (``ID_PROPS``), never a
        key of another system's record (``erp_code``) nor a form the question writes."""
        props = {FM: [("code", "ACME-01"), ("erp_code", "SAGE-ACME")],
                 MAIL: [("number", "Acme Oils")]}
        item = draw(Records(props=props), "elsewhere")
        self.assertEqual(sorted(item["facts"][0]["members"]),
                         [["ACME OILS LTD", "ACME-01"], ["Re: Acme reorder"]])
        for said, outcome in (("ACME-01 and Re: Acme reorder", "correct"),
                              ("SAGE-ACME and Re: Acme reorder", "wrong"),
                              ("Acme Oils and Re: Acme reorder", "wrong")):
            with self.subTest(said=said):
                graded = gen.grade({**item, "id": "nv-1", "category": item["card"],
                                    "difficulty": "hard", "style": "plain"},
                                   {"direct_answer": f"It is tied to {said}."}, [])
                self.assertEqual(graded["outcome"] == "correct", outcome == "correct",
                                 graded)

    def test_a_copy_under_the_asked_name_is_no_finding(self):
        # the FileMaker copy named as the start is written in the question already
        same = dict(NAMES, **{FM: "Acme Oils"})
        self.assertEqual(members(draw(Records(names=same), "elsewhere")),
                         ["Re: Acme reorder"])
        # a bridge through the far end's own copy alone is no bridge
        far_copy = dict(NAMES, **{FM: "SO-77", MAIL: "acme oils"})
        with self.assertRaises(gw.Dropped):
            draw(Records(names=far_copy), "bridge")

    def test_a_draw_that_cannot_be_settled_whole_is_dropped(self):
        # a person's record touched: dropped, never a truth without it
        with self.assertRaisesRegex(gw.Dropped, "people_excluded"):
            draw(Records(blocked=frozenset({MAIL})), "elsewhere")
        # a member without a name (a line): no truth that cannot be said; the question
        # narrows to another system whose records are all named, never the line's
        line = ("SalesOrderLine.line_of", LINE, SAGE)
        item = draw(Records(extra=[line]), "elsewhere", start=SAGE)
        self.assertEqual(item["card"], "novel-elsewhere-in")
        self.assertNotIn("Portal", item["question"])
        self.assertNotIn("Portal", item["systems"])
        with self.assertRaisesRegex(gw.Dropped, "member_unnamed"):
            draw(Records(extra=[line], names={SAGE: "SO-77"}), "elsewhere", start=SAGE)
        # two members of one name: the answer could not tell them apart
        twin = dict(NAMES, **{MAIL: "ACME OILS LTD"})
        with self.assertRaisesRegex(gw.Dropped, "member_ambiguous"):
            draw(Records(names=twin), "elsewhere")
        # a link type reaching more records than a walk lists whole
        many = [("Email.mentions_customer", f'Email:graph_mail.messages:{{"id":"x{n}"}}', BC)
                for n in range(gw.MAX_SET + 1)]
        with self.assertRaisesRegex(gw.Dropped, "fanout"):
            draw(Records(extra=many), "elsewhere")

    def test_a_wide_record_is_asked_about_one_other_system(self):
        # the Business Central customer reaches 7 mails (too many to list) and one FileMaker
        # copy: asked what FileMaker holds, the truth is that copy alone
        mails = {f'Email:graph_mail.messages:{{"id":"w{n}"}}': f"Order {n}" for n in range(7)}
        extra = [("Email.mentions_customer", m, BC) for m in mails]
        item = draw(Records(extra=extra, names={**NAMES, **mails}), "elsewhere")
        self.assertEqual(item["card"], "novel-elsewhere-in")
        self.assertEqual(members(item), ["ACME OILS LTD"])
        self.assertIn("FileMaker", item["question"])
        # no other system narrow enough (the FileMaker side unnamed): dropped, never a
        # partial list
        names = {k: v for k, v in {**NAMES, **mails}.items() if k != FM}
        with self.assertRaises(gw.Dropped):
            draw(Records(extra=extra, names=names), "elsewhere")

    def test_no_start_type_fills_a_batch(self):
        items, stats = nw.build(LINKS, Records(), items=40, seed="q", as_of=AS_OF, now="n")
        kinds = [i["start_type"] for i in items]
        self.assertTrue(items)
        self.assertLessEqual(max(kinds.count(k) for k in kinds), round(40 * nw.TYPE_SHARE))

    def test_a_built_batch_is_held_out_and_graded_by_its_members(self):
        items, stats = nw.build(LINKS, Records(), items=4, seed="2026-11-11", as_of=AS_OF,
                                now="2026-11-11T00:00:00Z")
        self.assertTrue(items)
        self.assertEqual({i["split"] for i in items}, {"held"})
        self.assertEqual(stats["items"], len(items))
        self.assertEqual(len({i["question"] for i in items}), len(items))   # no repeats
        item = items[0]
        names = [m[0] for m in item["facts"][0]["members"]]
        whole = gen.grade(item, {"direct_answer": "It is tied to " + " and ".join(names)}, [])
        self.assertEqual(whole["outcome"], "correct")
        half = gen.grade(item, {"direct_answer": "It is tied to " + names[0]}, [])
        self.assertNotEqual(half["outcome"], "correct")

    def test_a_held_batch_starts_nowhere_a_dev_batch_started_and_splits_are_closed(self):
        dev, _ = nw.build(LINKS, Records(), items=2, seed="s", as_of=AS_OF, now="n",
                          split="dev")
        self.assertTrue(dev)
        self.assertEqual({i["split"] for i in dev}, {"dev"})
        held, _ = nw.build(LINKS, Records(), items=4, seed="s", as_of=AS_OF, now="n",
                           exclude=[i["start"] for i in dev])
        self.assertFalse({i["start"] for i in held} & {i["start"] for i in dev})
        with self.assertRaises(ValueError):
            nw.build(LINKS, Records(), items=1, seed="s", as_of=AS_OF, now="n",
                     split="train")

    def test_a_type_whose_records_carry_no_name_stops_being_drawn(self):
        class Lines(Records):
            def sample(self, link_type):
                return [(LINE, LINE)]

        composer = nw.NovelComposer(LINKS, Lines(), AS_OF)
        read, named = [], composer.named
        composer.named = lambda oid: read.append(oid) or named(oid)
        for _ in range(nw.UNNAMED_AFTER + 5):
            with self.assertRaisesRegex(gw.Dropped, "start_unnamed"):
                composer.start("SalesOrderLine.line_of", random.Random(0))
        self.assertEqual(len(read), nw.UNNAMED_AFTER)


if __name__ == "__main__":
    unittest.main()
