"""The demo's autopilot studio (``serve --mock`` only): a SYNTHETIC queue, replay scores and
write-gate state in a temp directory, read through the real autopilot area and studio
code. The demo reader is the owner and decides the demo person's items. Suggest and Watch
modes, a kept draft and a replay are answered from fixed synthetic counts here;
nothing reaches a system: no adapter is installed, so an accept
is a recorded dry run. Evidence label: ``isolated-fixture``.

A ``MockBackend`` without ``install`` (every test) keeps the real, empty behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Optional

from operating import autopilot as ap
from operating import autopilot_write as aw

from . import models as m

DOMAIN = "plx-demo.example"
ME = "0d3e0000-0000-4000-8000-00000000d3e0"          # the demo reader's synthetic owner id
PEOPLE = {
    ME: ("Ben Okafor", "ben.okafor"),
    "0d3e0000-0000-4000-8000-0000000000c1": ("Claire Dubois", "claire.dubois"),
    "0d3e0000-0000-4000-8000-0000000000d1": ("Dev Raman", "dev.raman"),
}
CLAIRE, DEV = "0d3e0000-0000-4000-8000-0000000000c1", "0d3e0000-0000-4000-8000-0000000000d1"


def _role(oid: str) -> str:
    return f"person:{PEOPLE[oid][1]}@{DOMAIN}"


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(n: int, autopilot: str, owner: str, title: str, happened: dict, prepared: dict,
          now: datetime, due_h: float, cites: list[str]) -> dict[str, Any]:
    created = now - timedelta(hours=2 + n)
    return {"item_id": f"demo_ap_{n}", "event_id": f"demo_ev_{n}", "autopilot": autopilot,
            "owner": owner, "title": title, "citations": cites,
            "audiences": ["demo"], "markings": [],
            "due_at": _iso(now + timedelta(hours=due_h)), "created_at": _iso(created),
            "body": {"label": title, "happened": happened, "prepared": prepared}}


def _items(now: datetime) -> list[dict[str, Any]]:
    mail = "demo://Email/demo/"
    return [
        _item(1, "customer_po", ME, "Customer PO received",
              {"event_type": "message.received", "object_type": "Email",
               "sender": "nadia.haddad@glowbotanicals.example",
               "subject": "PO 4471: 2,400 eucalyptus bars, ship by 2 Dec",
               "object_id": "Customer:demo:glow"},
              {"summary": "New PO 4471 from Glow Botanicals: 2 of 2 lines matched to items; "
                          "price matches the October quote.", "gaps": [],
               "write": {"system": "bc", "operation": "sales_order_create",
                         "reason": "Customer PO 4471 received by mail; 2 lines read."},
               "draft": {"channel": "email", "subject": "RE: PO 4471",
                         "to": [{"address": "nadia.haddad@glowbotanicals.example"}],
                         "body": "Hi Nadia,\n\nThanks, we have PO 4471 for 2,400 eucalyptus "
                                 "bars and will confirm the ship date by tomorrow noon.\n\nBen"}},
              now, 1.5, [mail + "po-4471"]),
        _item(2, "supplier_delay", ME, "Supplier delay or shortage",
              {"event_type": "message.received", "object_type": "Email",
               "sender": "jordan.lee@mapleleaf.example",
               "subject": "PO 90412 now ships 18 Nov", "object_id": "PurchaseOrder:demo:90412"},
              {"summary": "Maple Leaf moved PO 90412 to 18 Nov. SO 77120 for Glow Botanicals "
                          "ships 13 Nov and needs these bars.",
               "write": {"system": "bc", "operation": "purchase_line_expected_date",
                         "reason": "Supplier's mail of today states 18 Nov."},
               "draft": {"channel": "email", "subject": "SO 77120: new ship date",
                         "to": [{"address": "nadia.haddad@glowbotanicals.example"}],
                         "body": "Hi Nadia,\n\nThe lavender bars for SO 77120 arrive 18 Nov. "
                                 "We can ship on 20 Nov, or split: 1,200 now and the rest on "
                                 "20 Nov. Which do you prefer?\n\nBen"}},
              now, 3, [mail + "maple-1811", "demo://PurchaseOrder/demo/90412"]),
        _item(3, "po_followup", ME, "Supplier PO without confirmation",
              {"event_type": "record.due", "object_type": "PurchaseOrder",
               "subject": "PO 90455 Ridgeway Labels: no confirmation after 3 days",
               "object_id": "PurchaseOrder:demo:90455"},
              {"summary": "PO 90455 was sent 7 Nov; Ridgeway has not confirmed.",
               "draft": {"channel": "email", "subject": "PO 90455: please confirm",
                         "to": [{"address": "orders@ridgeway.example"}],
                         "body": "Hello,\n\nCould you confirm PO 90455 and its ship date?"
                                 "\n\nThanks,\nBen"}},
              now, 6, ["demo://PurchaseOrder/demo/90455"]),
        _item(4, "meeting_followup", ME, "Meeting ended",
              {"event_type": "meeting.ended", "object_type": "CalendarEvent",
               "subject": "Ops stand-up"},
              {"summary": "3 action items from the stand-up; 2 are yours.",
               "draft": {"channel": "teams", "subject": "Stand-up follow-ups",
                         "body": "Dev: chase Northwind on PO 90388.\nFrank: book line 2 "
                                 "changeover for the holiday SKU."}},
              now, 20, ["demo://MeetingTranscript/demo/ops-daily"]),
        _item(5, "approval_request", CLAIRE, "Approval requested",
              {"event_type": "approval.requested", "object_type": "Approval",
               "subject": "Credit hold release: Harbor Skin Co", "sender": "Mission Control"},
              {"summary": "Harbor Skin Co is 12 days past terms on CAD 4,180.00; the last two "
                          "payments came 9 and 11 days late.",
               "write": {"system": "mc", "operation": "approval_decision",
                         "reason": "Recommend hold until the open invoice is paid."}},
              now, 2, ["demo://Approval/demo/harbor-credit"]),
        _item(6, "payment_question", CLAIRE, "Invoice or payment question",
              {"event_type": "message.received", "object_type": "Email",
               "sender": "ap@harborskin.example", "subject": "Invoice 55012 copy?"},
              {"summary": "Harbor asks for a copy of invoice 55012 (CAD 4,180.00, due 30 Oct).",
               "draft": {"channel": "email", "subject": "RE: Invoice 55012 copy?",
                         "to": [{"address": "ap@harborskin.example"}],
                         "body": "Hello,\n\nAttached is invoice 55012. It was due 30 Oct."
                                 "\n\nClaire"}},
              now, 4, [mail + "harbor-55012"]),
        _item(7, "qc_hold", DEV, "QC hold or fail",
              {"event_type": "qc.result", "object_type": "Lot",
               "subject": "Lot L-24-0917 failed fragrance", "object_id": "Lot:demo:L-24-0917"},
              {"summary": "Lot L-24-0917 failed the fragrance check; it feeds SO 77131.",
               "draft": {"channel": "teams", "subject": "Lot L-24-0917 hold",
                         "body": "Lot L-24-0917 is on hold. Retest booked for Thursday."}},
              now, 8, ["demo://Lot/demo/L-24-0917"]),
        _item(8, "supplier_eta", DEV, "Supplier states a ship date",
              {"event_type": "message.received", "object_type": "Email",
               "sender": "dispatch@northwind.example", "subject": "PO 90388 ships Friday",
               "object_id": "PurchaseOrder:demo:90388"},
              {"summary": "Northwind says PO 90388 ships Friday 14 Nov.",
               "write": {"system": "bc", "operation": "purchase_line_expected_date",
                         "reason": "Supplier's mail states 14 Nov."}},
              now, 5, [mail + "northwind-90388"]),
    ]


DEV_SCORES = {
    _role(ME): {"customer_order_intake": (40, 37), "supplier_eta_receiving": (31, 26),
                "purchase_order_to_supplier": (22, 21), "meeting_admin": (18, 13)},
    _role(CLAIRE): {"internal_approval": (26, 25), "ar_collections": (34, 29)},
    _role(DEV): {"qc_hold_release_internal": (12, 9), "supplier_eta_receiving": (19, 15)},
}
FRESH_SCORES = {_role(ME): {"purchase_order_to_supplier": (17, 16),
                            "customer_order_intake": (16, 13)},
                _role(CLAIRE): {"internal_approval": (19, 18)}}


class StudioDemo:
    """The seeded temp state and the mock-only answers (modes, drafts, replays)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.modes: dict[str, str] = {}
        self.drafts: dict[str, dict[str, Any]] = {}

    def visible(self, reader: Any) -> list[dict[str, Any]]:
        q = ap.Queue(self.root / "queue" / ap.QUEUE_FILE)
        try:
            items = q.items(None)
        finally:
            q.close()
        for item in items:
            item["mine"] = item["owner"] == ME
        return items

    def replay(self, workflow: str, days: int) -> m.AutopilotReplay:
        """Fixed synthetic counts, scaled by the days asked (a fixture, never a measurement)."""
        seed = int(hashlib.sha256(workflow.encode()).hexdigest()[:6], 16)
        judged = max(1, days * (2 + seed % 3))
        missed = max(0, judged // (8 + seed % 5))
        false = max(0, judged // (15 + seed % 7))
        today = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)
        words = [("caught", "Prepared what the person sent"),
                 ("missed", "The person replied before it was prepared"),
                 ("caught", "Prepared what the person sent"),
                 ("false_alarm", "Prepared a reply nobody sent: the thread was closed"),
                 ("caught", "Prepared what the person sent")]
        items = [m.AutopilotReplayItem(at=_iso(today - timedelta(days=i + 1)),
                                       title=f"Day {i + 1}: {workflow.replace('_', ' ')}",
                                       outcome=o, why=w)  # type: ignore[arg-type]
                 for i, (o, w) in enumerate(words[:min(days, 5)])]
        return m.AutopilotReplay(workflow=workflow, days=days, judged=judged,
                                 caught=judged - missed - false, missed=missed,
                                 false_alarms=false, items=items)


def seed(root: Path, now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    qpath = root / "queue" / ap.QUEUE_FILE
    q = ap.Queue(qpath)
    with q.conn:
        for item in _items(now):
            q.add_item(item)
    q.close()

    def doc(scores: dict) -> dict:
        return {"schema": ap.GATE_SCHEMA, "roles": {
            role: {"members": [oid for oid in PEOPLE if _role(oid) == role],
                   "workflows": {f: {"judged": j, "matched": k} for f, (j, k) in flows.items()}}
            for role, flows in scores.items()}}
    (qpath.parent / ap.GATE_FILE).write_text(json.dumps(doc(DEV_SCORES)))
    state = root / "autopilot-write"
    run = root / "fresh-run"
    run.mkdir(parents=True)
    (run / ap.GATE_FILE).write_text(json.dumps(doc(FRESH_SCORES)))
    for n, role in enumerate(FRESH_SCORES):
        (run / f"r{n}").mkdir()
        (run / f"r{n}" / "result.json").write_text(json.dumps(
            {"role": role, "status": "complete",
             "window": ["2026-11-06T07:00:00Z", "2026-11-10T07:00:00Z"]}))
    state.mkdir(parents=True, exist_ok=True)
    rules = aw.load_rules()
    aw.record_scores(run, state / aw.WRITE_GATE_FILE, rules)
    aw.sync_gate(aw.Journal(state / aw.AUDIT_FILE),
                 aw.WriteGate(state / aw.WRITE_GATE_FILE, rules), now)


def install(backend: Any) -> StudioDemo:
    """Seed a temp state and point ``backend``'s autopilot hooks at it (the demo reader is
    the owner; no adapter, so nothing is performed)."""
    root = Path(tempfile.mkdtemp(prefix="plx-demo-studio-"))
    seed(root)
    demo = StudioDemo(root)
    backend.studio_demo = demo
    backend.writes_environ = {}
    backend.autopilot_queue = root / "queue" / ap.QUEUE_FILE
    backend.autopilot_write_state = root / "autopilot-write"
    backend.autopilot_grant = (lambda aud: True, frozenset(), lambda hs: {h: None for h in hs})
    backend.autopilot_owner = lambda reader: True
    backend.autopilot_adapters = {}
    from integrations.people_exclusion import PeopleExclusion
    guard = PeopleExclusion(["99999999-9999-4999-8999-999999999999"])
    guard.attach_directory([(oid, f"{local}@{DOMAIN}", []) for oid, (_, local) in PEOPLE.items()])
    backend.autopilot_guard = guard
    return demo

