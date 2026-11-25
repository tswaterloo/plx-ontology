"""Home's unified Needs-you read (/api/home/needs): every source merged server-side, ranked by
typed urgency, 90-day items folded rather than dropped, Mission Control approvals excluded,
Genie item 9 gap alerts delivered to the owner only, and honest partial/failure reporting.
Evidence label: isolated-fixture (the demo backend and synthetic matters)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from operator_api import home_needs as hn
from operator_api import models as m
from operator_api.mock import MockBackend
from operator_api.backend_common import PREWARM_SCREENS, SCREENS_AHEAD
from operator_api.tests.test_app_api import mock_client, signed_in

NOW = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)


def reader():
    return SimpleNamespace(
        key="reader",
        display_name="Demo Reader",
        session=SimpleNamespace(access=None, view_as=False),
    )


def owner_reader(*, view_as: bool = False):
    return SimpleNamespace(
        key="owner",
        display_name="Owner",
        session=SimpleNamespace(access=None, view_as=view_as),
    )


def matter(n, *, kind="conversation", channel="mail", act="reply", due=None, days=0, detail=None):
    detail = {"records": []} if detail is None else detail
    if kind == "approval" and detail == {"records": []}:
        detail = {"approval": f"MCApproval:task:{n}", "records": [f"MCApproval:task:{n}"]}
    return {
        "item_id": f"{n:024x}",
        "kind_of_matter": kind,
        "state": "open",
        "channel": channel,
        "sender": f"person{n}@example.com",
        "subject": f"Subject {n}",
        "summary": f"Do {n}",
        "message": "quote",
        "note": f"Do {n}",
        "ask": "",
        "body": "draft" if act == "reply" else "",
        "next_act": act,
        "basis_at": NOW - timedelta(days=days),
        "due_at": due,
        "still_open": False,
        "sources_detail": detail,
        "verified_at": NOW,
        "plan_status": "ready",
        "plan_reason": "",
    }


class Build(unittest.TestCase):
    def test_sources_merge_into_one_counted_list_excluding_mc_approvals_and_late_po_dupes(self):
        store = {
            "status": "ok",
            "reason": "",
            "as_of": NOW,
            "items": [
                matter(1, kind="approval", act="system_write", due=NOW - timedelta(days=3)),
                matter(2, channel="teams", due=NOW + timedelta(days=1)),
            ],
        }
        with patch("operator_api.open_work_tool.store", return_value=store):
            got = hn.build(MockBackend(), reader(), None)
        ids = {i.id for i in got.items}
        self.assertIn("matter:" + f"{2:024x}", ids)
        self.assertIn("late:PurchaseOrder:demo:90412", ids)
        self.assertIn("late:PurchaseOrder:demo:90388", ids)
        self.assertTrue(any(i.startswith("alert:") for i in ids))
        self.assertFalse(any(i.startswith("alert:alert:po_late:") for i in ids))
        self.assertEqual(got.total, len(got.items))
        self.assertEqual(sum(f.count for f in got.sources), got.total)
        self.assertEqual(sum(f.count for f in got.kinds), got.total)
        self.assertTrue({"pos", "alerts", "teams"}.issubset({f.value for f in got.sources}))
        # Mission Control approvals stay out of Workbench Home lists.
        self.assertNotIn("matter:" + f"{1:024x}", ids)
        self.assertEqual(got.items[0].id, "late:PurchaseOrder:demo:90412")
        self.assertTrue(all(i.rank == n for n, i in enumerate(got.items, 1)))

    def test_mc_approvals_excluded_from_both_matters_and_alerts_while_other_approvals_stay(self):
        mc_by_ref = matter(
            10,
            kind="approval",
            act="system_write",
            detail={"approval": "MCApproval:gate:10", "records": ["MCApproval:gate:10"]},
        )
        mc_by_event = matter(
            11,
            kind="conversation",
            act="reply",
            detail={"alert_kind": "mc_approval_pending", "records": []},
        )
        non_mc_approval = matter(
            12,
            kind="approval",
            act="system_write",
            detail={"approval": "PortalApproval:12", "records": ["PortalApproval:12"]},
        )
        self.assertTrue(hn._mc_approval_matter(mc_by_ref))
        self.assertTrue(hn._mc_approval_matter(mc_by_event))
        self.assertFalse(hn._mc_approval_matter(non_mc_approval))

        mc_alert_event = SimpleNamespace(
            id="a1", event_type="mc_approval_pending", object_type="Task", object_id="Task:1"
        )
        mc_alert_type = SimpleNamespace(
            id="a2", event_type="approval_waiting", object_type="MCApproval", object_id="MCApproval:2"
        )
        mc_alert_kind = SimpleNamespace(
            id="a3", event_type="approval_waiting", alert_kind="mc_approval_pending",
            object_type="Task", object_id="Task:3",
        )
        normal_alert = SimpleNamespace(
            id="a4", event_type="lot_on_hold", object_type="Batch", object_id="Batch:1"
        )
        self.assertTrue(hn._mc_approval_alert(mc_alert_event))
        self.assertTrue(hn._mc_approval_alert(mc_alert_type))
        self.assertTrue(hn._mc_approval_alert(mc_alert_kind))
        self.assertFalse(hn._mc_approval_alert(normal_alert))

    def test_two_reply_matters_on_one_record_both_survive(self):
        oid = "PurchaseOrder:demo:90412"

        def reply(n):
            return m.HomeNeedItem(
                id=f"matter:m{n}",
                kind="reply",
                source="mail",
                source_label="Mail",
                title=f"Re: 90412 {n}",
                line="Answer the supplier.",
                person="supplier@example.com",
                action="reply",
                action_label="Reply",
                href="/po/90412",
                object_id=oid,
                object_type="PurchaseOrder",
                matter_id=f"m{n}",
                body=f"answer {n}",
                draft_ready=True,
                rank=1,
            )

        got = hn._dedupe([reply(1), reply(2)])
        self.assertEqual([r.id for r in got], ["matter:m1", "matter:m2"])

    def test_reply_about_late_po_stays_beside_the_late_po(self):
        oid = "PurchaseOrder:demo:90412"
        late = m.HomeNeedItem(
            id=f"late:{oid}",
            kind="late_po",
            source="pos",
            source_label="POs",
            title="PO 90412",
            line="Late purchase order.",
            person="Acme",
            due_at="2026-11-01",
            amount=m.Money(amount="1200.00", currency="CAD"),
            days_late=23,
            action="check",
            action_label="Check",
            href="/po/90412",
            object_id=oid,
            object_type="PurchaseOrder",
            rank=1,
        )
        reply = m.HomeNeedItem(
            id="matter:m1",
            kind="reply",
            source="mail",
            source_label="Mail",
            title="Re: 90412",
            line="Confirm delivery date.",
            person="supplier@example.com",
            action="reply",
            action_label="Reply",
            href="/po/90412",
            object_id=oid,
            object_type="PurchaseOrder",
            matter_id="m1",
            body="Can you confirm?",
            draft_ready=True,
            rank=1,
        )
        got = hn._dedupe([late, reply])
        self.assertEqual([r.id for r in got], [f"late:{oid}", "matter:m1"])

    def test_older_than_90_days_folds_cleanly_without_dropping_any_item(self):
        def item(key, *, due_days=None, basis_days=None, days_late=None):
            return m.HomeNeedItem(
                id=key,
                kind="late_po" if days_late else "task",
                source="pos" if days_late else "planner",
                source_label="POs" if days_late else "Planner",
                title=key,
                line="Check item.",
                basis_at=(NOW - timedelta(days=basis_days)).isoformat() if basis_days is not None else None,
                due_at=(NOW - timedelta(days=due_days)).isoformat() if due_days is not None else None,
                days_late=days_late,
                action="check",
                action_label="Check",
                href="/",
                rank=1,
            )

        rows = [
            item("recent-due", due_days=30),
            item("boundary-90", due_days=90),
            item("old-due-91", due_days=91),
            item("old-basis-120", basis_days=120),
            item("old-undated-po", days_late=140),
            item("recent-basis", basis_days=5),
        ]
        current, older = hn.split_older(rows, now=NOW)
        self.assertEqual({r.id for r in current}, {"recent-due", "boundary-90", "recent-basis"})
        self.assertEqual({r.id for r in older}, {"old-due-91", "old-basis-120", "old-undated-po"})
        self.assertEqual(len(current) + len(older), len(rows))

    def test_ranking_puts_overdue_then_today_then_quiet_then_newest(self):
        def row(key, *, due=None, basis_days=0, quiet=False):
            return m.HomeNeedItem(
                id=key,
                kind="task",
                source="planner",
                source_label="Planner",
                title=key,
                line="Do it.",
                basis_at=(NOW - timedelta(days=basis_days)).isoformat(),
                due_at=due.isoformat() if due else None,
                still_open=quiet,
                action="do",
                action_label="Do it",
                href="/",
                rank=1,
            )

        old = row("old-overdue", due=NOW - timedelta(days=5))
        newer_overdue = row("newer-overdue", due=NOW - timedelta(days=2))
        today = row("due-today", due=NOW)
        quiet_old = row("quiet-old", basis_days=9, quiet=True)
        quiet_new = row("quiet-new", basis_days=2, quiet=True)
        newest = row("newest", basis_days=0)
        older = row("older", basis_days=4)
        got = hn.rank([newest, quiet_new, today, newer_overdue, older, quiet_old, old], now=NOW)
        self.assertEqual(
            [r.id for r in got],
            ["old-overdue", "newer-overdue", "due-today", "quiet-old", "quiet-new", "newest", "older"],
        )
        self.assertTrue(any(r.startswith("overdue") for r in got[0].rank_reasons))
        self.assertIn("waiting on a follow-up", got[3].rank_reasons)
        self.assertEqual(got[5].rank_reasons, [])
        # Never emit robotic filler phrases or em dashes.
        for r in got:
            joined = " ".join(r.rank_reasons)
            self.assertNotIn("newest open work", joined)
            self.assertNotIn("waiting and gone quiet", joined)
            self.assertNotIn("\u2014", joined)

    def test_gap_alerts_reach_owners_needs_you_list_and_never_other_readers(self):
        from operating import gap_list
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "gaps.jsonl")
            with patch.dict(os.environ, {gap_list.ENV: p}):
                gap_list.record("measure", "fill_rate", now=NOW - timedelta(hours=2))
                gap_list.record("measure", "fill_rate", now=NOW - timedelta(hours=1))
                gap_list.record("measure", "fill_rate", now=NOW)
                gap_list.record("measure", "revenue", now=NOW)   # defined measure: never alerted
                gap_list.record("view", "receipts_missing", now=NOW - timedelta(hours=3))
                with patch("operator_api.feedback.owner_open", return_value=True), \
                        patch("operator_api.open_work_tool.store",
                              return_value={"status": "ok", "reason": "", "as_of": NOW, "items": []}):
                    got = hn.build(MockBackend(), owner_reader(), None)
                gap_items = [i for i in got.items if i.kind == "gap"]
                self.assertEqual(
                    [i.id for i in gap_items],
                    ["gap:measure:fill_rate", "gap:view:receipts_missing"],
                )
                self.assertEqual(gap_items[0].title, "fill_rate (measure)")
                self.assertEqual(gap_items[0].line, "Asked 3 times, no definition.")
                self.assertEqual(gap_items[0].href, "/data-health#answers-gaps")
                self.assertEqual(gap_items[1].line, "Asked once, no definition.")
                self.assertTrue(any(f.value == "gaps" and f.count == 2 for f in got.sources))

                # view-as owner sees no gap alerts
                with patch("operator_api.feedback.owner_open", return_value=True):
                    self.assertEqual(hn._gaps(owner_reader(view_as=True))[0], [])

                # non-owner sees no gap alerts
                with patch("operator_api.feedback.owner_open", return_value=False):
                    rows, part = hn._gaps(reader())
                self.assertEqual((rows, part.status), ([], "off"))

            # unset PLX_GAP_LIST reports status=off cleanly without failing Home
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(gap_list.ENV, None)
                with patch("operator_api.feedback.owner_open", return_value=True):
                    rows, part = hn._gaps(owner_reader())
                self.assertEqual((rows, part.status), ([], "off"))


    def test_alert_item_leads_with_specific_summary_and_keeps_action_in_ask(self):
        bi = m.BriefingItem(
            id="price-drop-1",
            rank=1,
            new_since_visit=False,
            new_this_generation=False,
            event_type="customer_price_drop",
            label="Price drop",
            subject_label="PHILLIP ADAM INC (NA)",
            summary="PHILLIP ADAM INC (NA)'s price for Bottle 500ml dropped 28% (CAD per EA) against the 180 days before.",
            object_id="Customer:bc:CUST-1",
            object_type="Customer",
            open_url="/account?kind=customer&h=CUST-1",
            next_action=m.NextAction(
                rule="customer_price_drop",
                text="Check the customer's agreed price for the product and raise it at the next quote, or record why it is lower.",
            ),
        )
        got = hn._alert_item(bi)
        self.assertEqual(got.line, bi.summary)
        self.assertEqual(got.ask, bi.next_action.text)
        self.assertEqual(hn.SOURCE_WORDS["mc"], "Mission Control")

    def test_price_alert_sql_has_no_space_before_percent_sign(self):
        from operating.pg import alerts as pg_alerts
        self.assertIn("|| '% below the usual price for'", pg_alerts.PRICE_BELOW_TYPICAL.select)
        self.assertNotIn(", '% below the usual price for'", pg_alerts.PRICE_BELOW_TYPICAL.select)
        self.assertIn("|| '% ('", pg_alerts.CUSTOMER_PRICE_DROP.select)
        self.assertNotIn(", '% ('", pg_alerts.CUSTOMER_PRICE_DROP.select)
        self.assertIn("Check unit cost before treating as margin loss.", pg_alerts.PRICE_BELOW_COST.select)
        self.assertIn(pg_alerts.COST_LOOKS_WRONG_SQL, pg_alerts._LINES)

    def test_financials_and_holds_tag_section_and_preserve_specific_badges(self):
        def brief(eid, event_type, alert_kind, label, summary, *, section=None, amount=None):
            return m.BriefingItem(
                id=eid,
                rank=1,
                new_since_visit=False,
                new_this_generation=False,
                event_type=event_type,
                alert_kind=alert_kind,
                section=section,
                label=label,
                subject_label=label,
                summary=summary,
                amount=amount,
                object_id=f"Customer:bc:{eid}",
                object_type="Customer",
                open_url=f"/object/Customer/{eid}",
                next_action=m.NextAction(rule=event_type, text="Review this alert."),
            )

        drop = hn._alert_item(
            brief("pd-1", "customer_price_drop", "price_exception", "Acme", "Acme's price dropped 18%.", section="financials")
        )
        below_ok = hn._alert_item(
            brief(
                "bc-1", "price_below_cost", "margin_loss", "Beta",
                "Beta paid 85% of cost for Soap 100g (CAD per EA) over the last 180 days.",
                section="financials",
                amount=m.Money(amount="1200.00", currency="CAD"),
            )
        )
        lot_hold = hn._alert_item(
            brief("h-1", "lot_on_hold", "quality_hold", "Batch L-100", "Batch L-100 is on quality hold.")
        )
        aging_hold = hn._alert_item(
            brief("h-2", "quality_hold_aging", "quality_hold", "Lot L-200", "Quality hold open too long.")
        )
        qc_matter = hn._matter_item(
            matter(
                50,
                kind="alert",
                channel="system",
                act="decide",
                detail={"alert": "qc-1", "autopilot": "qc_hold", "records": ["Batch:bc:B-50"]},
            )
        )
        order_hold_matter = hn._matter_item(
            matter(
                51,
                kind="alert",
                channel="system",
                act="decide",
                detail={"alert": "oh-1", "event_type": "order_hold", "records": ["SalesOrder:bc:SO-51"]},
            )
        )

        self.assertEqual((drop.section, drop.event_type, drop.badge), ("financials", "customer_price_drop", "Price drop"))
        self.assertEqual((below_ok.section, below_ok.event_type, below_ok.badge), ("financials", "price_below_cost", "Below cost"))
        self.assertEqual(below_ok.amount.amount, "1200.00")
        self.assertEqual((lot_hold.section, lot_hold.badge), ("holds", "Batch hold"))
        self.assertEqual((aging_hold.section, aging_hold.badge), ("holds", "Quality hold"))
        self.assertEqual((qc_matter.section, qc_matter.alert_kind, qc_matter.badge), ("holds", "quality_hold", "QC hold"))
        self.assertEqual((order_hold_matter.section, order_hold_matter.badge), ("holds", "Order hold"))

    def test_cost_sanity_guard_flags_extreme_below_cost_without_misleading_margin_loss(self):
        def below(eid, pct, amount_val="4500.00"):
            return hn._alert_item(
                m.BriefingItem(
                    id=eid,
                    rank=1,
                    new_since_visit=False,
                    new_this_generation=False,
                    event_type="price_below_cost",
                    alert_kind="margin_loss",
                    section="financials",
                    label="Customer price below cost",
                    subject_label="BE FIT NOW",
                    summary=f"BE FIT NOW paid {pct}% of cost for Serum 30ml (CAD per EA) over the last 180 days.",
                    amount=m.Money(amount=amount_val, currency="CAD"),
                    object_id=f"Customer:bc:{eid}",
                    object_type="Customer",
                    open_url="/account?kind=customer&h=C1",
                    next_action=m.NextAction(rule="price_below_cost", text="Check unit cost and selling price."),
                )
            )

        # 23% of cost (>2x cost-to-revenue): flagged to verify unit cost before treating as margin loss
        low = below("bc-23", 23)
        self.assertEqual(low.section, "financials")
        self.assertEqual(low.badge, "Verify unit cost")
        self.assertIn("unit cost", (low.ask or "").lower())
        self.assertEqual(low.state, "open")

        # 12% of cost (>5x cost-to-revenue, exceeds COST_WRONG_TIMES): suspect amount suppressed + uncertain state
        extreme = below("bc-12", 12)
        self.assertEqual(extreme.badge, "Verify unit cost")
        self.assertIsNone(extreme.amount)
        self.assertEqual(extreme.state, "uncertain")
        self.assertIn("5x revenue", extreme.ask or "")
        self.assertNotIn("—", (extreme.line or "") + (extreme.ask or ""))

        # 82% of cost: genuine below-cost margin loss, keeps amount and Below cost badge
        normal = below("bc-82", 82)
        self.assertEqual(normal.badge, "Below cost")
        self.assertIsNotNone(normal.amount)
        self.assertEqual(normal.state, "open")


    def test_home_needs_is_the_home_screen_read_ahead_and_its_kept_check_prewarmed(self):
        self.assertIn("home_needs", SCREENS_AHEAD)
        self.assertIn(("home_needs",), PREWARM_SCREENS)

    def test_customer_demand_drop_folds_into_financials_section_without_dropping(self):
        demand = hn._alert_item(
            m.BriefingItem(
                id="cd-1",
                rank=1,
                new_since_visit=False,
                new_this_generation=False,
                event_type="customer_demand_drop",
                alert_kind="demand_drop",
                label="Customer demand drop",
                subject_label="THE KANAWAKI GOLF CLUB",
                summary="THE KANAWAKI GOLF CLUB has no orders in the last 90 days.",
                object_id="Customer:bc:KAN-1",
                object_type="Customer",
                open_url="/account?kind=customer&h=KAN-1",
                next_action=m.NextAction(rule="customer_demand_drop", text="Check in with the account owner."),
            )
        )
        self.assertEqual(demand.section, "financials")
        self.assertEqual(demand.badge, "Demand drop")
        self.assertEqual(demand.object_id, "Customer:bc:KAN-1")
        self.assertEqual(demand.href, "/account?kind=customer&h=KAN-1")
        self.assertTrue(any(l.href == "/account?kind=customer&h=KAN-1" for l in demand.links))

    def test_qc_hold_matter_enriches_from_queue_with_record_links_and_affected_orders(self):
        qitem = {
            "item_id": "ap_qc_1",
            "autopilot": "qc_hold",
            "body": {
                "label": "QC hold or fail",
                "happened": {
                    "object_id": "Batch:bc:BC-26313C",
                    "object_type": "Batch",
                    "sender": "QC",
                    "fields": {"lot": "BC-26313C", "product": "G-LS-15-206"},
                },
                "prepared": {
                    "summary": "Lot BC-26313C (G-LS-15-206) is on-hold; it holds up 1 open order(s).",
                    "evidence": [{"object_id": "Batch:bc:BC-26313C"}],
                    "affected": [
                        {
                            "object_id": "SalesOrder:bc:SO-10042",
                            "order_number": "SO-10042",
                            "customer": "Acme Cosmetics",
                        }
                    ],
                    "note": "Review QC hold and release or substitute lot.",
                },
            },
        }
        raw_matter = matter(
            77,
            kind="record",
            channel="system",
            act="ask_person",
            due=NOW,
            detail={
                "queue_item": "ap_qc_1",
                "autopilot": "qc_hold",
                "records": ["bc://prod/lots/BC-26313C"],
            },
        )
        raw_matter.update(
            subject="QC hold or fail: Lot BC-26313C (G-LS-15-206) is on-hold; it holds up 1 open order.",
            summary="QC hold or fail: Lot BC-26313C (G-LS-15-206) is on-hold; it holds up 1 open order.",
            note="Still open? Nothing has happened on it since it was due 2026-11-05. QC hold or fail.",
            sender="",
            message="",
            still_open=True,
        )
        got = hn._matter_item(raw_matter, queue_item=qitem)
        # Canonical /object/Batch/... link and object_id instead of bare /assist fallback.
        self.assertEqual(got.object_id, "Batch:bc:BC-26313C")
        self.assertEqual(got.object_type, "Batch")
        self.assertTrue(got.href.startswith("/object/Batch/"))
        self.assertEqual((got.section, got.badge), ("holds", "QC hold"))
        # Rich line replaces the empty "QC hold or fail." stub with concrete summary + affected order.
        self.assertIn("Lot BC-26313C (G-LS-15-206) is on-hold", got.line)
        self.assertIn("SO-10042", got.line)
        self.assertIn("Acme Cosmetics", got.line)
        self.assertNotIn("(s)", got.line)
        self.assertNotIn("\u2014", got.line)
        self.assertEqual(got.person, "Acme Cosmetics")
        self.assertEqual(got.ask, "Review QC hold and release or substitute lot.")
        # Both Lot and SalesOrder links are populated, plus structured facts.
        hrefs = [l.href for l in got.links]
        self.assertTrue(any(h.startswith("/object/Batch/") for h in hrefs))
        self.assertTrue(any(h.startswith("/object/SalesOrder/") for h in hrefs))
        self.assertTrue(any("SO-10042" in f for f in got.facts))

        # Ranking a due-today + still_open item never produces "due today, waiting and gone quiet".
        ranked = hn.rank([got], now=NOW)[0]
        self.assertEqual(ranked.rank_reasons, ["due today"])
        self.assertNotIn("waiting and gone quiet", " ".join(ranked.rank_reasons))

    def test_record_ref_accepts_short_and_canonical_ids_and_object_hrefs(self):
        oid, otype, url = hn._record_ref(["bc://prod/item/1", "Batch:BC-26313C"])
        self.assertEqual((oid, otype), ("Batch:BC-26313C", "Batch"))
        self.assertTrue(url.startswith("/object/Batch/"))

        oid2, otype2, url2 = hn._record_ref(["/object/SalesOrder/SalesOrder%3Abc%3ASO-9"])
        self.assertEqual((oid2, otype2), ("SalesOrder:bc:SO-9", "SalesOrder"))
        self.assertTrue(url2.startswith("/object/SalesOrder/"))

        # URI schemes and lowercase tokens are rejected.
        self.assertEqual(hn._record_ref(["https://example.com", "graph-mail://m/1@1", "mail:a@b.com"]), (None, None, None))

    def test_price_below_typical_folds_into_financials_with_badge(self):
        self.assertIn("price_below_typical", hn.FINANCIALS_EVENTS)
        self.assertEqual(hn.FINANCIALS_BADGES["price_below_typical"], "Below typical price")


class Route(unittest.TestCase):
    def test_route_serves_one_whole_payload(self):
        got = signed_in().get("/api/home/needs")
        self.assertEqual(got.status_code, 200, got.text)
        body = got.json()
        self.assertEqual(body["total"], len(body["items"]))
        self.assertEqual(body["older_after_days"], 90)
        self.assertIn(body["status"], ("ok", "partial", "stale"))
        self.assertTrue(body["items"])
        self.assertEqual(sum(f["count"] for f in body["sources"]), body["total"])
        self.assertEqual(sum(f["count"] for f in body["kinds"]), body["total"])
        self.assertTrue(
            any(p["source"] == "work" and p["status"] != "ok" for p in body["parts"]),
            "the demo has no Assist store; that is said, not an empty list",
        )

    def test_signed_out_gets_nothing(self):
        self.assertEqual(mock_client().get("/api/home/needs").status_code, 401)


if __name__ == "__main__":
    unittest.main()
