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
        self.assertIn("overdue", got[0].rank_reasons)
        self.assertIn("waiting and gone quiet", got[3].rank_reasons)

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
