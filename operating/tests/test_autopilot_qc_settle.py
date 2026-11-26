"""A QC-hold item closes itself once every order it says it holds up has closed
(``autopilot_engine.settle``, rule ``batch_released``); one still open, or one that cannot
be read, keeps it open. Synthetic records only."""
from __future__ import annotations

import json

from operating import autopilot as ap
from operating import autopilot_queue
from operating import proactive_engine as pe
from operating import autopilot_engine as ae
from operating.tests.test_autopilot import NOW, Fixture
from operating.tests.test_proactive_engine import OURS_OID
from operating.tests.test_proactive_threads import people


class QcSettle(Fixture):
    def test_item_closes_when_all_its_orders_closed_and_not_before(self):
        w = self.w
        closed = w.obj("SalesOrder", {"order_number": "SO-7101", "status": "Closed"})
        closed2 = w.obj("SalesOrder", {"order_number": "SO-7102", "status": "Cancelled"})
        live = w.obj("SalesOrder", {"order_number": "SO-7103", "status": "Open",
                                    "required_date": "2026-10-30"})
        w.link("SalesOrder.ordered_by", live, self.customer)
        part = w.obj("SalesOrder", {"order_number": "SO-7104", "status": "Partially Shipped",
                                    "required_date": "2026-10-30"})
        full = w.obj("SalesOrder", {"order_number": "SO-7105", "status": "Fully Shipped"})

        lot = w.obj("Batch", {"lot_number": "L-9", "quality_status": "On-Hold",
                              "order_list": "SO-7103", "tested_by": "QA"})
        self.env[autopilot_queue.ROLES_ENV] = str(self.dir / "roles.json")
        (self.dir / "roles.json").write_text(json.dumps({"roles": {"quality": [OURS_OID]}}))
        self.cycle([lot])
        q = self.queue()
        (held,) = [i for i in q.items() if i["autopilot"] == "qc_hold"]

        self.assertEqual(held["owner"], autopilot_queue.POOL_PREFIX + "quality")   # one item, not one per member

        def copy(item_id: str, orders: list[str], **extra) -> None:
            body = {**held["body"], "prepared": {**held["body"]["prepared"], **extra,
                                                 "affected": [{"object_id": o} for o in orders],
                                                 "subject": "lot:" + item_id}}
            with q.conn:
                q.add_item({**held, "item_id": item_id, "event_id": "ev-" + item_id, "body": body})

        copy("ap_all_closed", [closed, closed2])
        copy("ap_mixed", [closed, live])
        copy("ap_unread", [closed, "SalesOrder:missing:1"])
        copy("ap_partial", [closed, part])            # a partial shipment is still open work
        copy("ap_full", [closed, full])
        copy("ap_cut", [closed, closed2], requests_not_followed=3)   # saw only part of its orders
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        ae.settle(st, q, ap.load_registry(), people(), now=NOW)
        states = {i["item_id"]: i["state"] for i in q.items(states=("open", "done"))
                  if i["autopilot"] == "qc_hold"}
        self.assertEqual(states["ap_all_closed"], "done")
        self.assertEqual(states["ap_mixed"], "open")
        self.assertEqual(states["ap_partial"], "open")
        self.assertEqual(states["ap_full"], "done")
        self.assertEqual(states["ap_cut"], "open")
        self.assertEqual(states["ap_unread"], "open")
        self.assertEqual(states[held["item_id"]], "open")

    def test_a_lot_holding_twelve_orders_keeps_all_twelve(self):
        w = self.w
        nos = [f"SO-72{n:02d}" for n in range(12)]
        for no in nos:
            w.obj("SalesOrder", {"order_number": no, "status": "Open", "required_date": "2026-10-30"})
        lot = w.obj("Batch", {"lot_number": "L-12", "quality_status": "On-Hold",
                              "order_list": " ".join(nos), "tested_by": "QA"})
        self.env[autopilot_queue.ROLES_ENV] = str(self.dir / "roles.json")
        (self.dir / "roles.json").write_text(json.dumps({"roles": {"quality": [OURS_OID]}}))
        self.cycle([lot])
        (held,) = [i for i in self.queue().items() if i["autopilot"] == "qc_hold"]
        self.assertEqual(len(held["body"]["prepared"]["affected"]), 12)

    def test_misrouted_non_qa_per_person_qc_hold_rerouted_to_quality_pool(self):
        w = self.w
        live = w.obj("SalesOrder", {"order_number": "SO-7301", "status": "Open",
                                    "required_date": "2026-10-30"})
        lot = w.obj("Batch", {"lot_number": "BC-26313C", "quality_status": "On-Hold",
                              "order_list": "SO-7301", "tested_by": "QA"})
        self.env[autopilot_queue.ROLES_ENV] = str(self.dir / "roles.json")
        (self.dir / "roles.json").write_text(json.dumps({"roles": {"quality": [OURS_OID]}}))
        self.cycle([lot])
        q = self.queue()
        # Simulate legacy misrouted per-person rows owned by IT/executive staff (no pool row)
        (pool_item,) = [i for i in q.items() if i["autopilot"] == "qc_hold"]
        with q.conn:
            q.settle_item(pool_item["item_id"], "done", "test_cleanup", "2026-10-20T00:00:00Z")
            q.add_item({
                **pool_item, "item_id": "ap_it_qc", "owner": "it-user-1",
                "body": {**pool_item["body"], "prepared": {
                    **pool_item["body"]["prepared"], "subject": "lot:BC26313C",
                    "affected": [{"object_id": live}],
                }},
            })
            q.add_item({
                **pool_item, "item_id": "ap_exec_qc", "owner": "exec-user-1",
                "body": {**pool_item["body"], "prepared": {
                    **pool_item["body"]["prepared"], "subject": "lot:BC26313C",
                    "affected": [{"object_id": live}],
                }},
            })
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        staff_rows = [
            {"person_id": "it-user-1", "department": "IT", "title": "Software Engineer"},
            {"person_id": "exec-user-1", "department": "Executive", "title": "CEO"},
            {"person_id": OURS_OID, "department": "Quality Assurance", "title": "QA Manager"},
        ]
        done = ae.settle(st, q, ap.load_registry(), people(), now=NOW, staff_rows=staff_rows)
        self.assertEqual(done["rerouted_to_role_pool"], 2)
        all_qc = {i["item_id"]: (i["state"], i["done_reason"], i["owner"])
                  for i in q.items(states=("open", "done", "superseded"))
                  if i["autopilot"] == "qc_hold"}
        self.assertEqual(all_qc["ap_it_qc"][:2], ("superseded", "rerouted_to_role_pool"))
        self.assertEqual(all_qc["ap_exec_qc"][:2], ("superseded", "rerouted_to_role_pool"))
        open_qc = [i for i in q.items() if i["autopilot"] == "qc_hold"]
        self.assertEqual(len(open_qc), 1)
        self.assertEqual(open_qc[0]["owner"], autopilot_queue.POOL_PREFIX + "quality")
