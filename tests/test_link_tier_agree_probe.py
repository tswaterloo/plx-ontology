"""scripts.link_tier_agree_probe on a synthetic store: plx.around, plx.aggregate and
plx.process read one tier for one link, and a tool that reads another fails the probe.

Evidence label: isolated-fixture (the real compiled schema and stage graphs, synthetic
records; the live run is verify/live-checks/ontology.link_tier_agree.json)."""

from __future__ import annotations

import unittest
from unittest import mock

from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_traverse as tt
from operating.tests import test_link_tier_rule as base
from scripts import link_tier_agree_probe as probe

STAGING = base.STAGING


def setUpModule():
    base.setUpModule()


class Served:
    """The served host's call surface over the hand-filled store."""

    def __init__(self, test, store):
        self.tools_set = base.Tools(test, store)
        self.tools = self.tools_set.base

    def call(self, name, arguments, actor):
        t = self.tools_set
        if name == "plx.around":
            return t.around_tools.call(name, arguments, actor)
        if name == "plx.aggregate":
            return t.aggregate_tools.call(name, arguments, actor)
        return t.process_tools.process(arguments, actor).result


def world():
    s = base.Store()
    so_s = base.order(s, "so-s1", STAGING)            # same-source FK between staging rows
    base.line(s, "so-s1-l1", STAGING, so_s)
    base.assembly(s, "mo-s1", s_line(s, so_s))
    so_g = base.order(s, "so-g1", "gold_etl_from_fm")  # text join to a staging line: a lead
    ln = base.line(s, "so-g1-l1", STAGING, so_g, tier="stored_by_text_join", kind="text_match")
    base.assembly(s, "mo-g1", ln)
    so_a = base.order(s, "so-a1", "gold_etl_from_fm")  # authority order, authority line
    base.line(s, "so-a1-l1", "gold_etl_from_fm", so_a)
    s.conn.commit()
    return s


def s_line(s, order):
    return base.line(s, "so-s1-l2", STAGING, order)


class LinkTierAgreeTests(unittest.TestCase):
    def run_probe(self, store):
        served = Served(self, store)
        return probe.check_links(served, base.STAFF, store.conn, sample=20,
                                 budget_seconds=None), served

    def test_same_source_fk_link_reads_one_tier_in_all_three_tools(self):
        report, _ = self.run_probe(world())
        self.assertEqual(report["contract_failures"], 0, report)
        self.assertEqual(report["tool_errors"], 0, report)
        self.assertEqual(report["disagreements"], 0, report)
        self.assertEqual(report["orders_asked"], 3)
        self.assertEqual(report["links_asked"], 4)
        self.assertGreaterEqual(report["staging_links_asked"], 1)
        # the text-join line is a lead everywhere: three of four lines count in all three
        self.assertEqual(report["counted"], {"around": 3, "aggregate": 3, "process": 3})
        self.assertEqual(report["process_lead_orders"], 1)

    def test_a_tool_reading_another_tier_fails_the_probe(self):
        store = world()
        # plx.aggregate alone stops applying the shared rule: every link counts at its
        # stored tier, so the text-join staging line is counted there and a lead elsewhere.
        stored_tier = lambda row: row["tier"]   # noqa: E731
        with mock.patch.object(oa, "_effective_tier", stored_tier):
            report, _ = self.run_probe(store)
        self.assertGreaterEqual(report["disagreements"], 1, report)
        self.assertIn("aggregate_counted", report["disagreement_kinds"])

    def test_process_naming_no_lead_when_a_line_is_a_lead_fails_the_probe(self):
        store = world()
        with mock.patch.object(probe, "process_names", lambda out: (False, False)):
            report, _ = self.run_probe(store)
        self.assertGreaterEqual(report["disagreements"], 1, report)
        self.assertIn("process_lead", report["disagreement_kinds"])

    def test_around_reading_another_tier_fails_the_probe(self):
        store = world()
        stored_tier = lambda row: row["tier"]   # noqa: E731
        with mock.patch.object(tt, "_effective_tier", stored_tier):
            report, _ = self.run_probe(store)
        self.assertGreaterEqual(report["disagreements"], 1, report)

    def test_unreadable_store_orders_fail_closed(self):
        # a reader shown none of the lines asks nothing and reports it, never a pass
        store = world()
        served = Served(self, store)
        nobody = base.Actor("plx", frozenset({"nobody"}))
        report = probe.check_links(served, nobody, store.conn, sample=20, budget_seconds=None)
        self.assertEqual(report["links_asked"], 0, report)
        self.assertGreaterEqual(report["orders_partial"] + report["tool_errors"], 1, report)

    def test_cardinality_sample_caps_orders_without_losing_links(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                s = base.Store()
                for k in range(n):
                    so = base.order(s, f"so-{k}", "gold_etl_from_fm")
                    base.line(s, f"so-{k}-l1", "gold_etl_from_fm", so)
                s.conn.commit()
                report, _ = self.run_probe(s)
                self.assertEqual(report["orders_asked"], min(n, 20))
                self.assertEqual(report["links_asked"], min(n, 20))
                self.assertEqual(report["disagreements"], 0, report)


if __name__ == "__main__":
    unittest.main()
