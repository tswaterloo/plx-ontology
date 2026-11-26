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
        # Clean hold title without 3x "QC hold" repetition, and non-duplicated affected order line.
        self.assertEqual(got.title, "Lot BC-26313C on hold (G-LS-15-206)")
        self.assertNotIn("QC hold or fail", got.title)
        self.assertNotIn("QC hold or fail", got.line)
        self.assertNotIn("Lot BC-26313C (G-LS-15-206) is on-hold", got.line)
        self.assertIn("1 open order affected", got.line)
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

    def test_waiting_commitment_and_qc_hold_copy_cleans_clutter_and_duplication(self):
        # 1. Waiting item: promote real topic to headline, surface "Waiting on Ricardo" in action_label.
        wait_raw = matter(
            81,
            kind="wait",
            channel="teams",
            act="check",
            detail={"records": []},
        )
        wait_raw.update(
            sender="Ricardo",
            subject="No answer yet from Ricardo",
            summary="No answer yet from Ricardo",
            note="Ricardo: Portal write endpoints and key in staging (no typical reply time on record yet)\nOnly you can see this.",
        )
        wait_item = hn._matter_item(wait_raw)
        self.assertEqual(wait_item.kind, "waiting")
        self.assertEqual(wait_item.person, "Ricardo")
        self.assertEqual(wait_item.title, "Portal write endpoints and key in staging")
        self.assertEqual(wait_item.action_label, "Waiting on Ricardo")
        self.assertEqual(wait_item.line, "Only you can see this.")
        self.assertNotIn("no typical reply time", wait_item.line.lower())
        self.assertNotIn("Ricardo:", wait_item.line)

        # 2. Commitment ("You said: ..."): crisp headline, Commitment label, zero echoed subtitle.
        promise_raw = matter(
            82,
            kind="promise",
            channel="teams",
            act="do",
            due=datetime(2026, 12, 14, 15, 0, tzinfo=timezone.utc),
            detail={"records": []},
        )
        promise_raw.update(
            sender="",
            subject="You said: renew the workbench-host-vince Portal access before Dec 14, 2026",
            summary="You said: renew the workbench-host-vince Portal access before Dec 14, 2026",
            note=(
                "renew the workbench-host-vince Portal access before Dec 14, 2026 "
                "(nothing in your mail says how long this usually takes: when should it be looked for?)"
            ),
        )
        promise_item = hn._matter_item(promise_raw)
        self.assertEqual(promise_item.kind, "task")
        self.assertEqual(promise_item.action_label, "Commitment")
        self.assertEqual(promise_item.title, "Renew workbench-host-vince Portal access")
        self.assertEqual(promise_item.line, "")

        # 3. QC hold rejected lot without queue enrichment + trailing privacy line.
        qc_raw = matter(
            83,
            kind="record",
            channel="system",
            act="ask_person",
            due=datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc),
            detail={"autopilot": "qc_hold", "records": ["Batch:bc:BC-25295D"]},
        )
        qc_raw.update(
            sender="Tiger Hospitality LLC",
            subject="QC hold or fail: Lot BC-25295D (AA-NA650) is rejected; it holds up 1 open order.",
            summary="QC hold or fail: Lot BC-25295D (AA-NA650) is rejected; it holds up 1 open order.",
            note="9 Nov. QC hold or fail.\nOnly you can see this.",
        )
        qc_item = hn._matter_item(qc_raw)
        self.assertEqual(qc_item.title, "Lot BC-25295D rejected (AA-NA650)")
        self.assertEqual(qc_item.badge, "QC hold")
        self.assertEqual(qc_item.person, "Tiger Hospitality LLC")
        self.assertEqual(qc_item.line, "1 open order affected\nOnly you can see this.")
        self.assertNotIn("QC hold or fail", qc_item.title + qc_item.line)
        self.assertNotIn("\u2014", qc_item.title + qc_item.line)

    def test_dedupe_collapses_duplicate_waiting_and_hold_notifications_with_update_count(self):
        # Two overlapping Teams waiting items for Ricardo collapse into the newer one with (+1 update).
        w1 = m.HomeNeedItem(
            id="matter:w1",
            matter_id="w1",
            kind="waiting",
            source="teams",
            source_label="Teams",
            title="Portal write endpoints and key in staging",
            line="",
            person="Ricardo",
            basis_at=(NOW - timedelta(days=7)).isoformat(),
            action="check",
            action_label="Waiting on Ricardo",
            href="/assist?item=w1",
            rank=1,
        )
        w2 = m.HomeNeedItem(
            id="matter:w2",
            matter_id="w2",
            kind="waiting",
            source="teams",
            source_label="Teams",
            title="Portal write endpoints and key in staging environment",
            line="",
            person="Ricardo",
            basis_at=(NOW - timedelta(days=2)).isoformat(),
            action="check",
            action_label="Waiting on Ricardo",
            href="/assist?item=w2",
            rank=2,
        )
        # Distinct topic for Ricardo stays separate.
        w3 = m.HomeNeedItem(
            id="matter:w3",
            matter_id="w3",
            kind="waiting",
            source="teams",
            source_label="Teams",
            title="Approve firewall egress rule for warehouse scanner",
            line="",
            person="Ricardo",
            basis_at=(NOW - timedelta(days=1)).isoformat(),
            action="check",
            action_label="Waiting on Ricardo",
            href="/assist?item=w3",
            rank=3,
        )
        # Two duplicate QC holds on the same Batch object_id collapse into one.
        h1 = m.HomeNeedItem(
            id="matter:h1",
            matter_id="h1",
            kind="alert",
            source="alerts",
            source_label="Alerts",
            title="Lot BC-25295D rejected (AA-NA650)",
            line="1 open order affected",
            person="Tiger Hospitality LLC",
            object_id="Batch:bc:BC-25295D",
            object_type="Batch",
            section="holds",
            badge="QC hold",
            basis_at=(NOW - timedelta(days=3)).isoformat(),
            action="decide",
            action_label="Decide",
            href="/object/Batch/Batch%3Abc%3ABC-25295D",
            facts=["Affected order: SO-55101 (Tiger Hospitality LLC)"],
            rank=4,
        )
        h2 = m.HomeNeedItem(
            id="alert:h2",
            kind="alert",
            source="alerts",
            source_label="Alerts",
            title="Lot BC-25295D rejected (AA-NA650)",
            line="1 open order affected",
            object_id="Batch:bc:BC-25295D",
            object_type="Batch",
            section="holds",
            badge="QC hold",
            basis_at=(NOW - timedelta(days=5)).isoformat(),
            action="decide",
            action_label="Decide",
            href="/object/Batch/Batch%3Abc%3ABC-25295D",
            rank=5,
        )
        got = hn._dedupe([w1, w2, w3, h1, h2])
        self.assertEqual([r.id for r in got], ["matter:w2", "matter:w3", "matter:h1"])
        self.assertIn("+1 update collapsed", got[0].facts)
        self.assertEqual(got[1].facts, [])
        self.assertIn("+1 update collapsed", got[2].facts)

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

    def test_stale_and_automated_reply_alerts_filtered_from_home_needs_and_page_sql(self):
        from operator_api import page_sql as ps

        stale_reply_matter = matter(
            81,
            kind="record",
            channel="mail",
            act="reply",
            days=25,
            detail={"alert": "needs_my_reply|h1||me", "event_type": "needs_my_reply", "records": []},
        )
        planner_matter = matter(
            82,
            kind="conversation",
            channel="mail",
            act="reply",
            days=2,
            detail={"records": []},
        )
        planner_matter["sender"] = "stefanie@petrasoap.com"
        planner_matter["subject"] = "You were mentioned in a chat about a task in Microsoft Planner"
        maccount_matter = matter(
            83,
            kind="conversation",
            channel="mail",
            act="reply",
            days=1,
            detail={"records": []},
        )
        maccount_matter["sender"] = "maccount@microsoft.com"
        maccount_matter["subject"] = "Audio Conferencing for Microsoft Teams has been turned off"
        shared_chat_matter = matter(
            84,
            kind="conversation",
            channel="mail",
            act="reply",
            days=1,
            detail={"records": []},
        )
        shared_chat_matter["sender"] = "giulia@petrasoap.com"
        shared_chat_matter["subject"] = "RE: Giulia shared a chat with you"
        real_reply_matter = matter(85, kind="conversation", channel="mail", act="reply", days=2)
        real_reply_matter["sender"] = "buyer@customer.example"
        real_reply_matter["subject"] = "Question on PO 10442"

        self.assertTrue(hn._filtered_reply_matter(stale_reply_matter, now=NOW))
        self.assertTrue(hn._filtered_reply_matter(planner_matter, now=NOW))
        self.assertTrue(hn._filtered_reply_matter(maccount_matter, now=NOW))
        self.assertTrue(hn._filtered_reply_matter(shared_chat_matter, now=NOW))
        self.assertFalse(hn._filtered_reply_matter(real_reply_matter, now=NOW))

        # page_sql filters stale (>14d) and automated needs_my_reply / reply_owed rows and adjusts counts
        counts = {"needs_my_reply": 5, "lot_on_hold": 1}
        rows = [
            {"rule": "needs_my_reply", "kind": "reply_owed", "since": NOW - timedelta(days=45),
             "why": 'alice@client.example wrote "Old thread" on Oct 11 with no reply from you in the thread yet.'},
            {"rule": "needs_my_reply", "kind": "reply_owed", "since": NOW - timedelta(days=2),
             "why": 'stefanie@petrasoap.com wrote "You were mentioned in a chat about a task in Microsoft Planner" on Nov 23.'},
            {"rule": "needs_my_reply", "kind": "reply_owed", "since": NOW - timedelta(days=2),
             "why": 'maccount@microsoft.com wrote "Audio Conferencing for Microsoft Teams has been turned off" on Nov 23.'},
            {"rule": "needs_my_reply", "kind": "reply_owed", "since": NOW - timedelta(days=2),
             "why": 'quarantine@messaging.microsoft.com wrote "Quarantine digest" on Nov 23.'},
            {"rule": "needs_my_reply", "kind": "reply_owed", "since": NOW - timedelta(days=3),
             "why": 'buyer@client.example wrote "Shipment timing" on Nov 22 with no reply from you in the thread yet.'},
            {"rule": "lot_on_hold", "kind": "quality_hold", "since": NOW - timedelta(days=30),
             "why": "Batch B-1 is on quality hold."},
        ]
        kept = ps._filter_reply_rows(rows, counts, now=NOW)
        self.assertEqual(len(kept), 2)
        self.assertEqual([r["rule"] for r in kept], ["needs_my_reply", "lot_on_hold"])
        self.assertEqual(counts, {"needs_my_reply": 1, "lot_on_hold": 1})

    def test_automated_it_monitoring_spam_filtered_for_ricardo_and_theo_while_real_work_stays(self):
        spam_cases = [
            # Ricardo's live automated staging/UAT monitoring spam
            ("cos@petrasoap.com", "Automated alert: the email-relay job failed on staging", "", "mail"),
            ("", "Automated alert: the UAT Business Central sync has not run for 48 hours", "", "system"),
            ("", "UAT BC sync has not run for two days on staging", "", "mail"),
            ("", "Portal-to-BC sync has not run for two days", "", "mail"),
            # Theo's live automated daily health checks and cos PR merge-class gates
            ("cos@petrasoap.com", "Automated daily health alert: eight Workbench API staging relations unavailable", "", "mail"),
            ("", "Daily health check flags eight unavailable Workbench API relations", "", "system"),
            ("", "Daily Workbench API health alert reporting 8 stale views", "", "mail"),
            ("cos@petrasoap.com", "Mission Control PR #412 merge-class gate blocked", "PR merge-class gate check failed", "mail"),
        ]
        for idx, (sender, subject, summary, channel) in enumerate(spam_cases, 1):
            row = matter(200 + idx, kind="conversation", channel=channel, act="reply", days=1)
            row["sender"] = sender
            row["subject"] = subject
            row["summary"] = summary or subject
            self.assertTrue(
                hn._filtered_reply_matter(row, now=NOW),
                f"Expected automated IT spam to be filtered: {subject!r}",
            )

        # Real staff/customer/sourcing items MUST stay visible (including Ricardo's real Teams staging wait items)
        legit_cases = [
            ("Ricardo", "Portal write endpoints and key in staging", "Ricardo: Portal write endpoints and key in staging", "teams"),
            ("Ricardo", "Portal write endpoints, Bridge 0.9.23.0 publish, Warehouse Setup fix", "", "teams"),
            ("cos@petrasoap.com", "[ACTION REQUIRED] New Sourcing Request: Bluzen", "New supplier sourcing request", "mail"),
            ("buyer@customer.example", "PO 106001 revised delivery date", "Can we ship Friday?", "mail"),
        ]
        for idx, (sender, subject, summary, channel) in enumerate(legit_cases, 1):
            row = matter(250 + idx, kind="wait" if channel == "teams" else "conversation", channel=channel, act="reply", days=1)
            row["sender"] = sender
            row["subject"] = subject
            row["summary"] = summary or subject
            self.assertFalse(
                hn._filtered_reply_matter(row, now=NOW),
                f"Expected genuine business item to stay visible: {subject!r}",
            )

    def test_vince_cross_kind_same_timestamp_promise_and_conversation_pairs_collapse_to_planned(self):
        t1 = datetime(2026, 11, 25, 15, 18, 33, tzinfo=timezone.utc)
        t2 = datetime(2026, 11, 25, 12, 37, 6, tzinfo=timezone.utc)

        # Pair 1: 8oz template to Greg (exact same second, promise + planned conversation)
        p1_raw = matter(301, kind="promise", channel="teams", act="do", days=0)
        p1_raw.update(
            basis_at=t1,
            sender="Vince",
            subject="You said: send the 8oz template to Greg",
            summary="You said: send the 8oz template to Greg",
            plan_status="none",
        )
        c1_raw = matter(302, kind="conversation", channel="teams", act="reply", days=0)
        c1_raw.update(
            basis_at=t1,
            sender="Greg",
            subject="Vince has the 8oz template and will send it to Greg.",
            summary="Vince has the 8oz template and will send it to Greg.",
            plan_status="planned",
            body="Hey Greg, sending over the 8oz template now.",
        )

        # Pair 2: lunch order with Giulia and Greg (exact same second, promise + planned conversation)
        p2_raw = matter(303, kind="promise", channel="teams", act="do", days=0)
        p2_raw.update(
            basis_at=t2,
            sender="Vince",
            subject="You said: send his and Greg's lunch order",
            summary="You said: send his and Greg's lunch order",
            plan_status="none",
        )
        c2_raw = matter(304, kind="conversation", channel="teams", act="do", days=0)
        c2_raw.update(
            basis_at=t2,
            sender="Giulia",
            subject="Marco says he is trying to send Giulia his and Greg's lunch orders",
            summary="Marco says he is trying to send Giulia his and Greg's lunch orders",
            plan_status="planned",
        )

        # Distinct Ricardo wait items 7 days apart stay separate.
        r1 = m.HomeNeedItem(
            id="matter:ric1",
            matter_id="ric1",
            kind="waiting",
            source="teams",
            source_label="Teams",
            title="Portal write endpoints, Bridge 0.9.23.0 publish, Warehouse Setup fix",
            line="",
            person="Ricardo",
            basis_at=(NOW - timedelta(days=1)).isoformat(),
            action="check",
            action_label="Waiting on Ricardo",
            href="/assist?item=ric1",
            rank=1,
        )
        r2 = m.HomeNeedItem(
            id="matter:ric2",
            matter_id="ric2",
            kind="waiting",
            source="teams",
            source_label="Teams",
            title="Portal write endpoints and key in staging",
            line="",
            person="Ricardo",
            basis_at=(NOW - timedelta(days=8)).isoformat(),
            action="check",
            action_label="Waiting on Ricardo",
            href="/assist?item=ric2",
            rank=2,
        )

        # Same-second items with different PO numbers stay separate.
        po_a = m.HomeNeedItem(
            id="matter:poa",
            matter_id="poa",
            kind="task",
            source="mail",
            source_label="Mail",
            title="Send confirmation for PO 101",
            line="",
            basis_at=t1.isoformat(),
            action="do",
            action_label="Commitment",
            href="/assist?item=poa",
            rank=3,
        )
        po_b = m.HomeNeedItem(
            id="matter:pob",
            matter_id="pob",
            kind="reply",
            source="mail",
            source_label="Mail",
            title="Marco says he will send confirmation for PO 102",
            line="",
            basis_at=t1.isoformat(),
            plan_status="planned",
            action="reply",
            action_label="Reply",
            href="/assist?item=pob",
            rank=4,
        )

        rows = [
            hn._matter_item(p1_raw),
            hn._matter_item(c1_raw),
            hn._matter_item(p2_raw),
            hn._matter_item(c2_raw),
            r1,
            r2,
            po_a,
            po_b,
        ]
        got = hn._dedupe(rows)
        got_ids = [r.id for r in got]
        self.assertIn(f"matter:{c1_raw['item_id']}", got_ids)
        self.assertNotIn(f"matter:{p1_raw['item_id']}", got_ids)
        self.assertIn(f"matter:{c2_raw['item_id']}", got_ids)
        self.assertNotIn(f"matter:{p2_raw['item_id']}", got_ids)
        self.assertIn("matter:ric1", got_ids)
        self.assertIn("matter:ric2", got_ids)
        self.assertIn("matter:poa", got_ids)
        self.assertIn("matter:pob", got_ids)
        self.assertEqual(len(got), 6)

    def test_mc_task_stays_in_tasks_never_reply_and_zombie_pos_are_filtered(self):
        # TASK-3601 (kind_of_matter='task') must NEVER be classified as 'reply' even with a
        # drafted body or reply next_act, while MC questions still classify as 'question'.
        mc_task = matter(
            3601,
            kind="task",
            channel="system",
            act="reply",
            due=NOW - timedelta(days=27),
            days=28,
            detail={"records": ["MCTask:3601"]},
        )
        mc_task["subject"] = "TASK-3601: Complete ERP sync cleanup"
        mc_task["body"] = "Drafted status update"
        got_task = hn._matter_item(mc_task)
        self.assertEqual(got_task.kind, "task")
        self.assertEqual(got_task.source, "mc")

        mc_question = matter(
            3602,
            kind="task",
            channel="system",
            act="ask_person",
            detail={"records": ["MCTask:3602"], "question": "q-1"},
        )
        self.assertEqual(hn._matter_item(mc_question).kind, "question")

        # Ancient 2018 zombie PO (> 365 days late and requested > 365 days ago) is filtered out
        # of active Home Needs-you while a recent late PO stays.
        zombie_po = SimpleNamespace(
            days_late=2900,
            due_date="2018-05-01",
            promised_date=None,
            order_date="2018-04-01",
        )
        recent_po = SimpleNamespace(
            days_late=18,
            due_date="2026-11-01",
            promised_date=None,
            order_date="2026-10-15",
        )
        self.assertTrue(hn._is_legacy_zombie_po(zombie_po, now=NOW))
        self.assertFalse(hn._is_legacy_zombie_po(recent_po, now=NOW))

        # Legacy > 365d PO/sales order alert is also dropped by the Home alert filter.
        zombie_alert = SimpleNamespace(
            event_type="customer_order_late",
            alert_kind="customer_order_late",
            summary="Order SO-2020 is 2,100 days past its required date.",
            subject_label="SO-2020",
            label="Late order",
            since="2020-06-01T00:00:00Z",
        )
        self.assertTrue(hn._filtered_reply_alert_item(zombie_alert))

        # Stale legacy Planner zombies (>180d undated), >365d old tasks, and >120d undated waits
        old_planner = {
            "kind_of_matter": "task",
            "source": "planner",
            "note": "Planner task in Marketing",
            "basis_at": (NOW - timedelta(days=240)).isoformat(),
            "due_at": None,
        }
        future_planner = {
            **old_planner,
            "due_at": (NOW + timedelta(days=10)).isoformat(),
        }
        old_wait = {
            "kind_of_matter": "wait",
            "basis_at": (NOW - timedelta(days=150)).isoformat(),
            "due_at": None,
        }
        agent_msg = {
            "kind_of_matter": "conversation",
            "summary": "Agent reports sync complete",
            "basis_at": (NOW - timedelta(days=1)).isoformat(),
        }
        self.assertTrue(hn._filtered_reply_matter(old_planner, now=NOW))
        self.assertFalse(hn._filtered_reply_matter(future_planner, now=NOW))
        self.assertTrue(hn._filtered_reply_matter(old_wait, now=NOW))
        self.assertTrue(hn._filtered_reply_matter(agent_msg, now=NOW))

    def test_qc_holds_never_route_to_production_non_qa_staff_and_route_to_qa(self):
        from operating import briefing as bf
        from operator_api import page_sql as ps

        self.assertFalse(ps.routed("lot_on_hold", bf.Routing(frozenset({"PROD"}))))
        self.assertTrue(ps.routed("lot_on_hold", bf.Routing(frozenset({"QUALITY"}))))
        self.assertTrue(ps.routed("lot_on_hold", bf.Routing(frozenset({"EXEC"}))))

        staff = [
            {"person_id": "oid-theo", "email": "theo@petrasoap.com", "department": "Production", "title": ""},
            {"person_id": "oid-ricardo", "email": "ricardo@petrasoap.com", "department": "Production", "title": "Software Engineer"},
            {"person_id": "oid-qa", "email": "qa@petrasoap.com", "department": "Quality Assurance", "title": "QA Specialist"},
            {"person_id": "oid-lab", "email": "lab@petrasoap.com", "department": "Laboratory", "title": "Lab Technician"},
        ]
        theo = SimpleNamespace(key="theo", email="theo@petrasoap.com", session=SimpleNamespace(access=None, view_as=False, subject_oid="oid-theo", identities=()))
        ricardo = SimpleNamespace(key="ricardo", email="ricardo@petrasoap.com", session=SimpleNamespace(access=None, view_as=False, subject_oid="oid-ricardo", identities=()))
        qa = SimpleNamespace(key="qa", email="qa@petrasoap.com", session=SimpleNamespace(access=None, view_as=False, subject_oid="oid-qa", identities=()))
        lab = SimpleNamespace(key="lab", email="lab@petrasoap.com", session=SimpleNamespace(access=None, view_as=False, subject_oid="oid-lab", identities=()))

        with patch.object(hn, "_load_staff_rows", return_value=staff):
            self.assertFalse(hn._reader_receives_qc_holds(None, theo))
            self.assertFalse(hn._reader_receives_qc_holds(None, ricardo))
            self.assertTrue(hn._reader_receives_qc_holds(None, qa))
            self.assertTrue(hn._reader_receives_qc_holds(None, lab))

            qc_alert = m.BriefingItem(
                id="qc-26280B", rank=1, new_since_visit=False, new_this_generation=False,
                event_type="lot_on_hold", alert_kind="quality_hold", label="Batch on quality hold",
                subject_label="Batch BC-26280B", summary="Batch BC-26280B is on quality hold.",
                object_id="Batch:bc:BC-26280B", object_type="Batch", open_url="/object/Batch/BC-26280B",
                next_action=m.NextAction(rule="lot_on_hold", text="Review hold."),
            )
            fake_brief = SimpleNamespace(status="read", items=[qc_alert], groups=[], refreshing=False, reading=False, as_of=NOW, checked_on=None)
            fake_backend = SimpleNamespace(briefing=lambda r, since_visit=False: fake_brief)
            rows_t, _ = hn._alerts(fake_backend, theo)
            rows_q, _ = hn._alerts(fake_backend, qa)
            self.assertEqual(rows_t, [])
            self.assertEqual(len(rows_q), 1)

    def test_stale_extracted_due_noreply_senders_and_stale_alerts_mc_tasks(self):
        # Forwarded conversation from Nov 24 with stale extracted due=Nov 11 (>7d earlier) drops bogus due_at
        fwd = matter(401, kind="conversation", channel="mail", act="reply", due=datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc), days=1)
        fwd["opened_at"] = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
        got_fwd = hn._matter_item(fwd)
        self.assertIsNone(got_fwd.due_at)

        # Automated noreply/updates/microsoftstore senders filtered out of reply matters
        for addr in ("noreply@vendor.example", "updates@saas.example", "microsoftstore@email.microsoft.com"):
            row = matter(402, kind="conversation", channel="mail", act="reply", days=1)
            row["sender"] = addr
            self.assertTrue(hn._filtered_reply_matter(row, now=NOW), addr)

        # Stale undated alerts (>21d) and old MC tasks (>21d overdue, no activity in 14d) fold into older_items
        stale_alert = m.HomeNeedItem(
            id="alert:old-1", kind="alert", source="alerts", source_label="Alerts",
            title="Old alert", line="Stale.", basis_at=(NOW - timedelta(days=36)).isoformat(),
            action="decide", action_label="Decide", href="/", rank=1,
        )
        old_mc = m.HomeNeedItem(
            id="matter:mc-old", kind="task", source="mc", source_label="Mission Control",
            title="Old MC task", line="Overdue.", basis_at=(NOW - timedelta(days=28)).isoformat(),
            due_at=(NOW - timedelta(days=28)).isoformat(),
            action="do", action_label="Do it", href="/", rank=1,
        )
        fresh_mc = m.HomeNeedItem(
            id="matter:mc-fresh", kind="task", source="mc", source_label="Mission Control",
            title="Active MC task", line="Active.", basis_at=(NOW - timedelta(days=3)).isoformat(),
            due_at=(NOW - timedelta(days=28)).isoformat(),
            action="do", action_label="Do it", href="/", rank=1,
        )
        cur, old = hn.split_older([stale_alert, old_mc, fresh_mc], now=NOW)
        self.assertEqual([r.id for r in cur], ["matter:mc-fresh"])
        self.assertEqual({r.id for r in old}, {"alert:old-1", "matter:mc-old"})

    def test_zombie_sales_order_and_po_days_late_suppressed_in_page_sql(self):
        from operator_api import page_sql as ps

        self.assertIsNone(ps._sales_order_live_days_late({"status": "open", "open_state": "nothing_outstanding", "days_late": 3166}))
        self.assertIsNone(ps._sales_order_live_days_late({"status": "open", "open_state": "stale", "days_late": 300}))
        self.assertIsNone(ps._sales_order_live_days_late({"status": "closed", "days_late": 45}))
        self.assertIsNone(ps._sales_order_live_days_late({"status": "open", "handle": "h-zombie", "days_late": 900}, {"h-zombie"}))
        self.assertEqual(ps._sales_order_live_days_late({"status": "open", "open_state": "live", "days_late": 12}), 12)
        self.assertIsNone(ps._purchase_order_live_days_late({"status": "closed", "verdict": "late", "days_late": 50}))
        self.assertIsNone(ps._purchase_order_live_days_late({"status": "open", "verdict": "on_time", "days_late": 5}))
        self.assertEqual(ps._purchase_order_live_days_late({"status": "open", "verdict": "late", "days_late": 14}), 14)

    def test_universal_home_item_state_close_snooze_and_restore_route(self):
        client = signed_in()
        before = client.get("/api/home/needs").json()
        target_id = next(i["id"] for i in before["items"] if i["id"].startswith("alert:"))

        closed = client.post("/api/home/item-state", json={"item_id": target_id, "state": "closed"})
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["state"], "closed")
        after_close = client.get("/api/home/needs").json()
        self.assertNotIn(target_id, [i["id"] for i in after_close["items"]])

        restored = client.post("/api/home/item-state", json={"item_id": target_id, "state": "open"})
        self.assertEqual(restored.status_code, 200, restored.text)
        after_restore = client.get("/api/home/needs").json()
        self.assertIn(target_id, [i["id"] for i in after_restore["items"]])

        # Snooze hides the item too
        snoozed = client.post("/api/home/item-state", json={"item_id": target_id, "state": "snoozed"})
        self.assertEqual(snoozed.status_code, 200, snoozed.text)
        self.assertIsNotNone(snoozed.json()["until"])
        after_snooze = client.get("/api/home/needs").json()
        self.assertNotIn(target_id, [i["id"] for i in after_snooze["items"]])

    def test_home_item_state_propagates_to_assist_autopilot_and_watcher_with_reopen_and_clamp(self):
        from operator_api import assist_routes
        from operator_api.app import WRITE_SCREENS
        from operator_api.autopilot_area import SNOOZE_DAYS

        self.assertTrue({"home_needs", "home", "briefing", "briefing_items"} <= WRITE_SCREENS["/api/home/item-state"])
        self.assertTrue(callable(getattr(assist_routes, "decide_matter", None)))
        self.assertTrue(callable(getattr(assist_routes, "reopen_matter", None)))

        with tempfile.TemporaryDirectory() as tmp:
            dropped = []
            wrote_calls = []
            watcher_calls = []
            ap_calls = []
            ap_reopens = []
            backend = SimpleNamespace(
                state_dir=tmp,
                _drop_good=lambda key: dropped.append(key),
                reader_wrote=lambda r, screens: wrote_calls.append(tuple(screens)),
                watcher_item_state=lambda r, wid, body: watcher_calls.append((wid, body)),
                autopilot_decide=lambda r, iid, body: ap_calls.append((iid, body)),
                autopilot_reopen=lambda r, iid, reason=None: ap_reopens.append((iid, reason)) or True,
            )
            who = SimpleNamespace(key="r1", mode="dev", session=SimpleNamespace(access=None, view_as=False))

            # 1. File permissions on HomeItemStateStore are 0600
            store = hn.item_state_store(backend)
            self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)

            # 2. 24-hex Assist matter: stripped ID, clamped 60d snooze -> <= 30d, and reopen
            m_hex = "a1" * 12
            far_until = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
            decided = []
            reopened = []
            with patch.object(assist_routes, "decide_matter", side_effect=lambda b, r, mid, body: decided.append((mid, body))), \
                    patch.object(assist_routes, "reopen_matter", side_effect=lambda b, r, mid, reason=None: reopened.append((mid, reason)) or True):
                res_s = hn.set_home_item_state(
                    backend, who,
                    m.HomeItemStateRequest(item_id=f"  matter:{m_hex}  ", state="snoozed", until=far_until, reason="later"),
                )
                self.assertTrue(res_s.propagated)
                self.assertEqual(res_s.item_id, f"matter:{m_hex}")
                self.assertEqual(decided[0][0], m_hex)
                self.assertEqual(decided[0][1].decision, "snooze")
                clamped = hn._moment(decided[0][1].until)
                self.assertIsNotNone(clamped)
                self.assertLessEqual(clamped, datetime.now(timezone.utc) + timedelta(days=SNOOZE_DAYS))

                res_o = hn.set_home_item_state(
                    backend, who,
                    m.HomeItemStateRequest(item_id=f" matter:{m_hex} ", state="open", reason="undo"),
                )
                self.assertTrue(res_o.propagated)
                self.assertEqual(reopened, [(m_hex, "undo")])

            # 3. Cache drops both home_needs and home, and reader_wrote includes briefing screens
            dropped_screens = {k[2] for k in dropped}
            self.assertIn(("home_needs",), dropped_screens)
            self.assertIn(("home",), dropped_screens)
            self.assertTrue(any("briefing" in s and "briefing_items" in s for s in wrote_calls))

            # 4. Watcher IDs with either watcher: or alert:watcher: prefix, YYYY-MM-DD until, reason, and reopen
            for raw_id in ("watcher:w-1", "  alert:watcher:w-2  "):
                res_w = hn.set_home_item_state(
                    backend, who,
                    m.HomeItemStateRequest(item_id=raw_id, state="snoozed", until=far_until, reason="check monday"),
                )
                self.assertTrue(res_w.propagated)
            self.assertEqual(watcher_calls[0][0], "watcher:w-1")
            self.assertEqual(watcher_calls[1][0], "watcher:w-2")
            self.assertEqual(len(watcher_calls[0][1].until), 10)
            self.assertEqual(watcher_calls[0][1].reason, "check monday")

            res_wo = hn.set_home_item_state(
                backend, who,
                m.HomeItemStateRequest(item_id="alert:watcher:w-2", state="open", reason="undo watcher"),
            )
            self.assertTrue(res_wo.propagated)
            self.assertEqual(watcher_calls[-1][0], "watcher:w-2")
            self.assertEqual(watcher_calls[-1][1].state, "open")
            self.assertEqual(watcher_calls[-1][1].reason, "undo watcher")

            # 5. Autopilot queue item propagation + reopen
            res_ap = hn.set_home_item_state(
                backend, who,
                m.HomeItemStateRequest(item_id="matter:ap_123", state="closed", reason="done"),
            )
            self.assertTrue(res_ap.propagated)
            self.assertEqual(ap_calls[0][0], "ap_123")
            res_apo = hn.set_home_item_state(
                backend, who,
                m.HomeItemStateRequest(item_id="matter:ap_123", state="open", reason="undo"),
            )
            self.assertTrue(res_apo.propagated)
            self.assertEqual(ap_reopens, [("ap_123", "undo")])

    def test_qc_hold_routing_both_directions_owner_exec_vs_super_admin_non_qa(self):
        from operating import briefing as bf
        from operator_api import page_sql as ps

        staff = [
            {"person_id": "oid-vince", "email": "vince@petrasoap.com", "department": "IT", "title": "", "reports_count": 27},
            {"person_id": "oid-theo", "email": "theo@petrasoap.com", "department": "Production", "title": "", "reports_count": 0},
            {"person_id": "oid-ricardo", "email": "ricardo@petrasoap.com", "department": "Production", "title": "", "reports_count": 0},
            {"person_id": "oid-qa", "email": "qa@petrasoap.com", "department": "QA/QC", "title": "QA/QC Manager", "reports_count": 8},
        ]
        vince = SimpleNamespace(key="vince", email="vince@petrasoap.com", session=SimpleNamespace(access=None, view_as=False, subject_oid="oid-vince", identities=()))
        theo = SimpleNamespace(key="theo", email="theo@petrasoap.com", session=SimpleNamespace(access=None, view_as=False, subject_oid="oid-theo", identities=()))
        ricardo = SimpleNamespace(key="ricardo", email="ricardo@petrasoap.com", session=SimpleNamespace(access=None, view_as=False, subject_oid="oid-ricardo", identities=()))

        with patch.object(hn, "_load_staff_rows", return_value=staff):
            # Even without owner_open, reports_count >= 5 routes QC holds to Vince (CEO)
            self.assertTrue(hn._reader_receives_qc_holds(None, vince))
            self.assertFalse(hn._reader_receives_qc_holds(None, theo))
            self.assertFalse(hn._reader_receives_qc_holds(None, ricardo))
            # With owner_open=True, AD's legacy department="IT" NEVER blocks owner
            with patch("operator_api.feedback.owner_open", return_value=True):
                vince_0 = SimpleNamespace(key="vince", email="vince@petrasoap.com", session=SimpleNamespace(access="full", view_as=False, subject_oid="oid-vince", identities=()))
                with patch.object(hn, "_load_staff_rows", return_value=[{"person_id": "oid-vince", "email": "vince@petrasoap.com", "department": "IT", "title": "", "reports_count": 0}]):
                    self.assertTrue(hn._reader_receives_qc_holds(None, vince_0))

        # page_sql.routed: SUPER_ADMIN on Production/IT does NOT bypass QC hold routing; owner/EXEC/QUALITY do
        self.assertFalse(ps.routed("lot_on_hold", bf.Routing(frozenset({"PROD"}), role="SUPER_ADMIN", is_owner=False)))
        self.assertFalse(ps.routed("quality_hold_aging", bf.Routing(frozenset({"IT"}), role="SUPER_ADMIN", is_owner=False)))
        self.assertTrue(ps.routed("lot_on_hold", bf.Routing(frozenset({"IT"}), role="SUPER_ADMIN", is_owner=True)))
        self.assertTrue(ps.routed("lot_on_hold", bf.Routing(frozenset({"EXEC"}), role="USER")))
        self.assertTrue(ps.routed("lot_on_hold", bf.Routing(frozenset({"QUALITY"}), role="USER")))

    def test_stale_alerts_over_30d_fold_with_recent_first_seen_and_conversation_due_before_basis_cleared(self):
        # Alert with recent first_seen (8d ago) but days_late=45 folds into older
        stale_with_recent_seen = m.HomeNeedItem(
            id="alert:so-late-1", kind="alert", source="alerts", source_label="Alerts",
            title="SO 1001", line="45 days past required date.",
            basis_at=(NOW - timedelta(days=8)).isoformat(), days_late=45,
            action="decide", action_label="Decide", href="/", rank=1,
        )
        # Alert with since 36d ago and first_seen 8d ago uses since as basis_at and folds into older
        oct21_brief = SimpleNamespace(
            id="lot-oct21", event_type="lot_on_hold", alert_kind="quality_hold", section=None,
            label="Batch on quality hold", subject_label="Batch BC-25",
            summary="Batch BC-25 is on quality hold.",
            next_action=m.NextAction(rule="lot_on_hold", text="Check quality hold."),
            counterpart=None, amount=None, amount_suspect=None, amount_disagree=False,
            carried=None, days=None, open_url="/object/Batch/BC-25", object_id="Batch:bc:BC-25",
            object_type="Batch",
            first_seen=(NOW - timedelta(days=8)).isoformat(),
            since=(NOW - timedelta(days=36)).isoformat(),
        )
        oct21_item = hn._alert_item(oct21_brief)
        self.assertEqual(oct21_item.basis_at, oct21_brief.since)

        cur, old = hn.split_older([stale_with_recent_seen, oct21_item], now=NOW)
        self.assertEqual(cur, [])
        self.assertEqual({r.id for r in old}, {"alert:so-late-1", "alert:lot-oct21"})

        # Vince's 1d, 3d, 6d earlier due_at < basis_at conversations are all cleared; valid future/same-day & tasks stay
        for delta_days in (1, 3, 6):
            conv = matter(500 + delta_days, kind="conversation", channel="mail", act="reply",
                          due=NOW - timedelta(days=delta_days), days=0)
            self.assertIsNone(hn._matter_item(conv).due_at, f"delta={delta_days}d")

        future_conv = matter(510, kind="conversation", channel="mail", act="reply",
                             due=NOW + timedelta(days=2), days=0)
        self.assertIsNotNone(hn._matter_item(future_conv).due_at)

        overdue_task = matter(511, kind="task", channel="system", act="do",
                              due=NOW - timedelta(days=3), days=0)
        self.assertIsNotNone(hn._matter_item(overdue_task).due_at)


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
