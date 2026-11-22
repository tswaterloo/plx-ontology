"""Home's Financials: the types the registry gathers into a section (``alert_kinds`` section)
are counted and their biggest few read by money impact, never listed one by one under
needs you; the same count feeds Home's needs-you total. A hold a prepared card is the same
as is named by the registry (``prepared_by``), and a source's "1 open order(s)" is plural-
correct in every sentence the card says."""
from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace
from unittest import mock

from operating import alert_kinds as ak
from operating.pg.alerts import RULES
from operator_api import page_sql as ps
from operator_api.presentation import counted_words

NOW = dt.datetime.now(dt.timezone.utc)
SINCE = NOW - dt.timedelta(days=3)


def _row(rule, kind, handle, why, **more):
    return {"rule": rule, "kind": kind, "record_table": "customers", "handle": handle,
            "topic": more.pop("topic", ""), "why": why, "since": SINCE, "first_seen": SINCE,
            "subject": more.pop("subject", handle), **more}


class Financials(unittest.TestCase):
    def briefing(self, impact, counts=None, fresh=None):
        runs = [{"rule": r, "ok_at": NOW if fresh is None or r in fresh else None}
                for r in RULES]
        counts = counts or {"price_below_cost": 67, "customer_price_drop": 21, "lot_on_hold": 4}
        rows = {"alert_runs": runs,
                "alerts_count": [{"rule": r, "open": n, "oldest": SINCE} for r, n in counts.items()],
                "alerts_open": [_row("lot_on_hold", "quality_hold", f"lot{i}", f"Batch {i} is on quality hold.")
                                for i in range(3)],
                "alerts_impact_top": impact}
        seen = []
        types = ["lot_on_hold", "price_below_cost", "customer_price_drop"]

        def fake_read(backend, reader, reads, ids=False):
            seen.append([r["name"] for r in reads])
            return rows, {}
        scope = SimpleNamespace(enabled=(), routing=None, visit=None)
        with mock.patch.object(ps, "reader_scope", lambda *a: scope), \
                mock.patch.object(ps, "alert_types", lambda *a, **k: types), \
                mock.patch.object(ps, "routed", lambda *a: True), \
                mock.patch.object(ps, "read", fake_read):
            return ps.alert_briefing(None, None, top=7), seen

    def test_the_registry_gathers_price_alerts_into_financials_and_nothing_else(self):
        self.assertEqual({t["type"] for t in ak.registry()["types"] if t.get("section")},
                         {"customer_price_drop", "price_below_cost"})
        self.assertFalse(ak.needs_you("price_below_cost"))
        self.assertTrue(ak.home("price_below_cost"))
        self.assertTrue(ak.needs_you("lot_on_hold"))
        self.assertIsNone(ak.section("no_such_type"))
        self.assertFalse(ak.needs_you("no_such_type"))

    def test_a_section_counts_in_full_lists_its_biggest_by_money_and_leaves_needs_you_alone(self):
        impact = [
            _row("price_below_cost", "margin_loss", "c1", "A paid 87% of cost for X", topic="X|EA|CAD",
                 subject="A Ltd", impact="6576.480", cur="CAD"),
            _row("price_below_cost", "margin_loss", "c2", "B paid 60% of cost for Y", topic="Y|EA|USD",
                 subject="B Inc", impact=None, cur=None),
            _row("customer_price_drop", "price_exception", "c3", "C's price dropped 38 %", topic="Z|EA|USD",
                 subject="C Co", impact="15270.00", cur="USD")]
        got, asked = self.briefing(impact)
        # one door read: the oldest-first items of needs-you types, the counts of all, the impact rows
        self.assertEqual(len(asked), 1)
        self.assertIn("alerts_impact_top", asked[0])
        groups = {g.event_type: g for g in got.groups}
        self.assertEqual((groups["price_below_cost"].count, groups["price_below_cost"].section), (67, "financials"))
        self.assertEqual((groups["customer_price_drop"].count, groups["lot_on_hold"].section), (21, None))
        by = {i.id.split(":")[2]: i for i in got.items}
        self.assertEqual(by["c1"].amount.model_dump(), {"amount": "6576.48", "currency": "CAD"})
        self.assertEqual(by["c1"].amount_basis, "below_cost")
        self.assertEqual(by["c3"].amount_basis, "price_drop")
        # no money impact worked out: no amount stated, never a zero
        self.assertIsNone(by["c2"].amount)
        self.assertEqual({i.section for i in got.items if i.event_type != "lot_on_hold"}, {"financials"})
        # needs you holds the 4 batches only; the 88 price alerts are not 88 things to act on
        self.assertEqual((got.home_count, got.count), (4, 92))
        # the three listed batches are the needs-you items, in the group's own count
        self.assertEqual(groups["lot_on_hold"].in_top, 3)

    def test_a_rule_not_current_lists_no_money_and_says_unavailable(self):
        impact = [_row("price_below_cost", "margin_loss", "c1", "x", topic="X|EA|CAD", impact="10", cur="CAD")]
        got, _ = self.briefing(impact, fresh={"lot_on_hold", "customer_price_drop"})
        self.assertEqual([i for i in got.items if i.event_type == "price_below_cost"], [])
        g = {x.event_type: x for x in got.groups}["price_below_cost"]
        self.assertTrue(g.unavailable)
        self.assertEqual(g.count, 0)

    def test_the_impact_read_orders_each_rule_by_its_own_money_with_n_rows_each(self):
        sql = " ".join(__import__("operating.page_reads", fromlist=["x"]).QUERIES["alerts_impact_top"].sql.split())
        self.assertIn("PARTITION BY rule ORDER BY impact DESC NULLS LAST", sql)
        self.assertIn("place <= %(limit)s", sql)
        for name, rule in RULES.items():
            if rule.impact:
                self.assertIn(f"WHEN '{name}' THEN {rule.impact}", sql)
        # only the two price rules state one
        self.assertEqual({n for n, r in RULES.items() if r.impact}, {"price_below_cost", "customer_price_drop"})


class Wording(unittest.TestCase):
    def test_a_sources_parenthesised_plural_follows_its_count(self):
        self.assertEqual(counted_words("1 open order(s) affected"), "1 open order affected")
        self.assertEqual(counted_words("3 open order(s) affected; 0 lot(s) held"),
                         "3 open orders affected; 0 lots held")
        self.assertEqual(counted_words("1,000 line(s)"), "1,000 lines")
        self.assertEqual(counted_words("2 address(s)"), "2 addresses")
        self.assertIsNone(counted_words(None))

    def test_a_prepared_hold_card_says_its_alert_kind_and_a_plural_that_agrees(self):
        from operator_api import autopilot_area as area
        self.assertEqual(ak.kind_prepared_by("qc_hold"), "quality_hold")
        self.assertEqual(ak.kind_of("lot_on_hold"), "quality_hold")
        self.assertIsNone(ak.kind_prepared_by("teams_reply"))
        item = {"item_id": "i1", "autopilot": "qc_hold", "created_at": "2026-11-21T12:00:00Z"}
        shown = {"title": "QC hold or fail", "body": {
            "label": "QC hold or fail", "happened": {"sender": "QC"},
            "prepared": {"summary": "Lot L1 is on-hold; 1 open order(s) affected.",
                         "affected": [{"object_id": "SalesOrder:a", "order_number": "OR1",
                                       "customer": "Acme"}],
                         "notes": "", "gaps": ["Missing: 1 open order(s) affected"], "lot": "L1"}}}
        card = area.alert_of(item, shown)
        if card is None:   # the card needs a basis key to count as an alert
            shown["body"]["prepared"]["evidence"] = [{"object_id": "Batch:b"}]
            card = area.alert_of(item, shown)
        self.assertIsNotNone(card)
        self.assertEqual(card.alert_kind, "quality_hold")
        self.assertNotIn("(s)", card.summary + " ".join(card.facts))
        self.assertIn("1 open order affected", card.summary)


if __name__ == "__main__":
    unittest.main()


class Pages(unittest.TestCase):
    def test_a_gathered_section_is_never_paged_ahead(self):
        from operator_api.briefing_view import home_group_pages
        item = SimpleNamespace(event_type="lot_on_hold", home=True)
        grp = lambda t, sec: SimpleNamespace(event_type=t, count=60, in_top=3, section=sec)  # noqa: E731
        got = SimpleNamespace(items=[item], home_count=9,
                              groups=[grp("price_below_cost", "financials"), grp("lot_on_hold", None)])
        self.assertEqual(home_group_pages(got), ["lot_on_hold"])

    def test_the_records_home_opens_first_are_read_ahead_for_a_signed_in_reader(self):
        """The first item sent home of each kind (Home's pane opens the first row), each
        record once; nothing for a directory employee's round; one that fails is skipped."""
        from operator_api import backend as be
        from operator_api.briefing_view import home_first_records
        it = lambda kind, oid, home=True: SimpleNamespace(  # noqa: E731
            event_type=kind, object_id=oid, object_type=oid.split(":")[0], home=home)
        got = SimpleNamespace(status="read", items=[
            it("lot_on_hold", "Batch:b1"), it("lot_on_hold", "Batch:b2"),
            it("customer_demand_drop", "Customer:c1", home=False),
            it("customer_demand_drop", "Customer:c2"), it("price_below_typical", "Customer:c2"),
            it("needs_my_reply", ""), it("po_due", "PurchaseOrder:p1")])
        self.assertEqual(home_first_records(got), [("Batch", "Batch:b1"), ("Customer", "Customer:c2"),
                                                   ("PurchaseOrder", "PurchaseOrder:p1")])
        self.assertEqual(len(home_first_records(got, most=2)), 2)
        backend = be.WorkspaceBackend(lambda session: mock.MagicMock(), data_label="t")
        self.addCleanup(backend.stop)
        read = []

        def page(_backend, _view, otype, oid, quick=False):
            if oid == "Customer:c2":
                raise RuntimeError("cold")
            read.append((otype, oid, quick))
        backend.briefing = lambda reader, since_visit=False: got
        backend.reads.run = lambda reader, job, timeout=None, **k: job(mock.MagicMock())
        with mock.patch.object(be, "object_page", page):
            self.assertEqual(backend._home_pages_ahead(mock.Mock(key="r")), 2)
            self.assertEqual(read, [("Batch", "Batch:b1", True), ("PurchaseOrder", "PurchaseOrder:p1", True)])
            self.assertEqual(backend._home_pages_ahead(mock.Mock(key="r"), later=True), 0)
        self.assertEqual(len(read), 2)
        # the warm-up's screen round tells these two whether the reader is a directory round
        seen = {}
        for name in be.AHEAD_LATER:
            setattr(backend, name, lambda reader, n=name, **k: seen.setdefault(n, k) and 0)
        with mock.patch.object(be, "SCREENS_AHEAD", tuple(be.AHEAD_LATER)), \
                mock.patch("operator_api.page_sql.alerts_on", return_value=True):
            backend._screens_read(mock.Mock(key="r"), tuple(be.AHEAD_LATER), None, True)
        self.assertEqual(seen, {n: {"later": True} for n in be.AHEAD_LATER})
