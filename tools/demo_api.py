"""The demo API for tools/demo.sh: the mock backend (fixture data only, no credentials, no
business system) signed in as the demo owner, with an ops-alarm state, a week of actions, a
prepared write, approvals waiting, a work log and view-as roles. The drafting model is
scripted, the people exclusion is a placeholder and no live-send switch is set, so a
confirm only logs a dry run. argv: port state_dir. ``DEMO_ALARMS=1`` keeps two alarms open.
Kept from the 11-11 preview and video harness (ui-preview-1111, rishi-video)."""
import sys, os, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, os.getcwd())
import uvicorn
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend
try:
    from operator_api import mock_studio
except ImportError:
    mock_studio = None
from operating import session_identity as si

port, state = int(sys.argv[1]), Path(sys.argv[2])
state.mkdir(parents=True, exist_ok=True)
now = datetime.now(timezone.utc)
z = lambda d: d.isoformat().replace("+00:00", "Z")
(state / "ops-alarms").mkdir(exist_ok=True)
checks = [
 {"name": "delta_last_cycle", "state": "ok", "value": 9, "target": 15, "unit": "min", "detail": "", "line": "Data refresh (delta) last finished 9 min ago (target 15 min)"},
 {"name": "capture:mail", "state": "ok", "value": 31, "target": 360, "unit": "min", "detail": "", "line": "mail capture last refreshed 31 min ago (target 6 h)"},
 {"name": "capture:sharepoint_lists", "state": "breach", "value": 400, "target": 160, "unit": "min", "detail": "", "line": "sharepoint_lists capture last refreshed 7 h ago (target 3 h)"},
 {"name": "capture:bc", "state": "ok", "value": 40, "target": 120, "unit": "min", "detail": "", "line": "bc capture last refreshed 40 min ago (target 2 h)"},
 {"name": "unit:plx-proactive.service", "state": "breach", "value": None, "target": 0, "unit": "", "detail": "killed: out of memory", "line": "plx-proactive.service (system): killed: out of memory"},
 {"name": "units:system", "state": "ok", "value": 41, "target": 0, "unit": "", "detail": "41 units", "line": "System plx units checked: 41 units"},
 {"name": "units:user", "state": "ok", "value": 6, "target": 0, "unit": "", "detail": "6 units", "line": "User plx units checked: 6 units"},
 {"name": "pg_connections", "state": "ok", "value": 40, "target": 160, "unit": "", "detail": "40 of 200 connections", "line": "Postgres: 40 of 200 connections"},
 {"name": "pg_idle_in_transaction", "state": "ok", "value": 0, "target": 0, "unit": "", "detail": "none idle in a transaction", "line": "Postgres: none idle in a transaction"},
 {"name": "disk_free", "state": "ok", "value": 412, "target": 100, "unit": "GB", "detail": "412 GB free on /data", "line": "Disk: 412 GB free on /data"},
 {"name": "memory_available", "state": "ok", "value": 14, "target": 6, "unit": "GB", "detail": "14 GB available", "line": "Memory: 14 GB available"},
 {"name": "backup", "state": "ok", "value": 9, "target": 30, "unit": "h", "detail": "last ran 9 h ago, success", "line": "Nightly backup: last ran 9 h ago, success"},
 {"name": "restore_test", "state": "unknown", "value": None, "target": 0, "unit": "", "detail": "none since boot", "line": "Backup restore test: none since boot"}]
ALARMS = os.environ.get("DEMO_ALARMS") == "1"
opened = [{"key": "unit:plx-proactive.service", "line": checks[4]["line"], "opened_at": z(now - timedelta(minutes=48)), "last_seen_at": z(now - timedelta(minutes=2))},
          {"key": "capture:sharepoint_lists", "line": checks[2]["line"], "opened_at": z(now - timedelta(hours=3, minutes=10)), "last_seen_at": z(now - timedelta(minutes=2))}]
if not ALARMS:
    opened = []
    for c in checks:
        c["state"] = "ok" if c["state"] == "breach" else c["state"]
    checks[2]["line"] = "sharepoint_lists capture last refreshed 52 min ago (target 3 h)"; checks[2]["value"] = 52
    checks[4]["line"] = "plx-proactive.service (system): running"; checks[4]["detail"] = "running"
status = {"schema": "plx-ops-alarms/v1", "checked_at": z(now - timedelta(minutes=2)), "every_minutes": 5,
 "open": opened,
 "recovered": [{"key": "disk_free", "line": "Disk: 61 GB free on /data (target 100 GB)", "opened_at": z(now - timedelta(hours=20)), "last_seen_at": z(now - timedelta(hours=18)), "closed_at": z(now - timedelta(hours=17, minutes=50))}],
 "checks": checks}
(state / "ops-alarms" / "status.json").write_text(json.dumps(status))

from operator_api import models as m

class Owner(MockBackend):
    def _is_owner(self, reader):
        return True

    # preview-only fixture rows: a week of actions, one prepared write, approvals waiting
    def actions_log(self, reader, limit=200):
        rows = []
        plan = [("send_email", "sent", 2, ["jordan.lee@mapleleaf.example"], "PO 90412"),
                ("reply_in_thread", "sent", 5, ["dispatch@northwind.example", "ap@northwind.example"], "PO 90388"),
                ("send_email", "dry_run", 20, ["nadia.haddad@glowbotanicals.example"], "SO 77120"),
                ("create_follow_up", "follow_up", 26, [], "PO 90455"),
                ("dismiss", "dismissed", 30, [], "Lot L-24-0917"),
                ("send_email", "failed", 50, ["orders@ridgeway.example"], "PO 90455"),
                ("send_email", "sent", 75, ["ap@harborskin.example"], "Invoice 55012")]
        for n, (kind, status, hours, to, card) in enumerate(plan, 1):
            rows.append(m.ActionLogRow(seq=n, kind=kind, status=status, at=z(now - timedelta(hours=hours)), to=to, cc=[],
                                       card_id=f"c{n}", card_label=card, text=f"{card}: preview row", actor="Demo reader",
                                       payload_hash=f"h{n}", replayed=False, missing=[], citations=[], subject=f"About {card}"))
        return m.ActionsLog(rows=list(reversed(rows)))

    def write_list(self, reader):
        v = m.WriteIntentView(intent={"intent_id": "wi-1", "record_id": "PurchaseOrder:demo:90412", "operation": "update_expected_receipt_date"}, status="prepared",
                              text="Set PO 90412 expected receipt date to 18 Nov 2026", at=z(now - timedelta(hours=3)), replayed=False)
        return m.WriteIntentList(items=[v])

    def approvals_queue(self, reader):
        item = m.ApprovalItem(id="ap-1", kind="credit_hold", summary="Release Harbor Skin Co's credit hold (CAD 4,180.00)",
                              days=2, aging="2 days", channel="Mission Control", asked_by="A. Buyer",
                              nudge=m.ApprovalNudge(channel="email", subject="Credit hold", body="x", status="draft_not_sent", sent=False))
        other = m.ApprovalItem(id="ap-2", kind="po_approval", summary="Approve PO 90470", days=5, aging="5 days",
                               channel="Mail", nudge=m.ApprovalNudge(channel="email", subject="PO 90470", body="x", status="draft_not_sent", sent=False))
        return m.ApprovalsQueue(as_of=z(now), total=2, sent=0, writes=0, groups=[
            m.ApprovalGroup(waiting_on="Demo reader", count=1, oldest_days=2, items=[item]),
            m.ApprovalGroup(waiting_on="Finance lead", count=1, oldest_days=5, items=[other])])

from operator_api import mock as _mock
_MC = _mock.src("MC", "mc://demo/requests/R-4471", "Mission Control request R-4471",
                fields={"Request": "Expedite PO 90412", "State": "Open", "Raised by": "Buyer"}, hours=3)
_page = Owner.object_page
def _object_page(self, reader, object_type, object_id, **kw):
    page = _page(self, reader, object_type, object_id, **kw)
    if page.object_id == "PurchaseOrder:demo:90412":
        page.timeline = sorted(page.timeline + [
            m.TimelineEvent(at="2026-10-24T14:05:00Z", system="Portal", title="Promise date entered as 24 Oct", source=_mock.PORTAL),
            m.TimelineEvent(at="2026-10-28T15:30:00Z", system="MC", title="Expedite request opened", source=_MC)], key=lambda e: e.at)
        page.linked = page.linked + [m.LinkedItem(kind="document", label="Mission Control expedite request", at="2026-10-28T15:30:00Z", source=_MC)]
    return page
Owner.object_page = _object_page
_source = Owner.source
def _source_view(self, reader, handle, text_offset):
    if handle == _MC.handle:
        return m.SourceView(handle=_MC.handle, system=_MC.system, label=_MC.label, kind="record", title=_MC.label, text=None, fields=_MC.fields, as_of=_MC.as_of, text_offset=text_offset)
    return _source(self, reader, handle, text_offset)
Owner.source = _source_view

def _workload(self, reader):
    T = [("PURCHASING", 9, 14, 6), ("SALES", 12, 8, 3), ("QUALITY", 2, 5, 4)]
    P = [("Demo reader", "PURCHASING", True, 4, 6, 2), ("Dana Ortiz", "PURCHASING", False, 3, 5, 3), ("Jordan Kim", "PURCHASING", False, 2, 3, 1)]
    return m.Workload(teams=[m.WorkloadTeam(team=t, awaiting_reply=a, tasks=k, alerts=l) for t, a, k, l in T],
                      people=[m.WorkloadPerson(person=n, team=t, self=s_, awaiting_reply=a, tasks=k, alerts=l) for n, t, s_, a, k, l in P],
                      granted_teams=["PURCHASING"])
Owner.workload = _workload
if os.environ.get('PREVIEW_STAFF'):
    Owner._is_owner = lambda self, reader: False
backend = Owner(step_delay=0.05)
if mock_studio is not None:
    mock_studio.install(backend)
backend.state_dir = state
# the demo's drafting model: one scripted reply (the recording never calls a real model)
class _Scripted:
    def __call__(self, text, **_kw):
        return ("Drafted a reply asking the supplier to confirm the new date in writing.\n"
                "[[DRAFT email]]\nSubject: RE: Delay on lavender bars\nHi Jordan,\n\n"
                "Thanks for the update on the lavender bars. Can you confirm in writing that "
                "the 12,000 bars will ship on 4 November? A customer order depends on them.\n\nDana\n[[END]]")
backend.instruct_call = _Scripted()
# the action desk's checks in the demo: a placeholder exclusion list (no real IDs) and the
# demo reader's own mailbox; no live-send switch is set, so a confirm only logs a dry run
backend.actions_environ = {"PLX_EXCLUDED_PEOPLE_OIDS": "00000000-0000-4000-8000-000000000000"}
Owner._mailbox = staticmethod(lambda reader: "demo.reader@plx.example")
import dataclasses as _dc
_ctx = Owner._action_context
Owner._action_context = lambda self, reader: _dc.replace(_ctx(self, reader), sender_oid="11111111-2222-4333-8444-000000000009")
try:
    from operator_api import work_log as wl, view_as as va
    class G:
        unavailable = None
        def is_excluded(self, v): return False
        def decides(self, a): return True
        def known_domains(self): return {"plx.example"}
    store = wl.WorkLog(None, guard=lambda: G())
    def R(key, name):
        return type("R", (), {"key": key, "display_name": name, "email": None,
            "session": type("S", (), {"identities": frozenset(), "subject_oid": None})()})()
    plan = [("plx:staff:demo", "Demo Reader", {("ask","asked"): [14, 9, 12, 18], ("autopilot","sent"): [3, 6, 8, 11], ("autopilot","accepted"): [2, 3, 5, 6], ("action","sent"): [1, 2, 2, 4]}),
            ("plx:staff:opslead", "Operations lead", {("ask","asked"): [20, 25, 22, 30], ("autopilot","sent"): [8, 12, 15, 21], ("write","executed"): [0, 2, 3, 4]}),
            ("plx:staff:finance", "Finance lead", {("ask","asked"): [4, 6, 9, 7], ("autopilot","accepted"): [1, 4, 6, 9]})]
    n = 0
    local = now - timedelta(hours=5)
    monday = (local - timedelta(days=local.weekday())).replace(hour=15)
    for key, name, kinds in plan:
        for (kind, verb), per in kinds.items():
            for w, count in enumerate(per):
                for i in range(count):
                    n += 1
                    at = monday - timedelta(days=7 * (3 - w)) + timedelta(minutes=i)
                    if at > now: at = now - timedelta(minutes=n)
                    store.clock = lambda at=at: at
                    store.record(R(key, name), kind=kind, verb=verb, title="t",
                                 thread=f"autopilot:i{n}" if kind == "autopilot" else f"ask:i{n}" if kind == "ask" else f"write:i{n}" if kind == "write" else f"card:i{n}",
                                 source="x", source_id=f"i{n}", awaiting={"due": "x"} if kind == "action" else None)
    store.clock = lambda: datetime.now(timezone.utc)
    backend.work_log = store
    from operating.employee_permissions import EmployeePrincipal
    def ident(oid, exp):
        p = EmployeePrincipal("11111111-2222-4333-8444-555555555555", oid)
        return si.SessionIdentity(tenant="plx", channel=si.CHANNEL_CONSOLE, author_kind=si.AUTHOR_HUMAN, proof=si.PROOF_VIEW_AS, employee=p, principals=frozenset({p.principal}), expires_at=exp, identities=frozenset({oid}))
    backend.view_as = va.ViewAs(candidates=lambda: [("aaaaaaaa-bbbb-4ccc-8ddd-000000000001", "ops.lead@plx.example"), ("aaaaaaaa-bbbb-4ccc-8ddd-000000000002", "finance.lead@plx.example"), ("aaaaaaaa-bbbb-4ccc-8ddd-000000000003", "buyer@plx.example")],
                                excluded=lambda: (lambda o: False), identity=ident, audit=va.Audit(state))
except ImportError:
    pass
import os as _o
_o.environ["PLX_TEAM_VIEW_READERS"] = ""
if not _o.environ.get("PREVIEW_STAFF"):
    _o.environ["PLX_FEEDBACK_ADMINS"] = "staff:demo"
auth = DevAuth(lambda: si.fixture_session("plx", ["staff:demo"], channel=si.CHANNEL_CONSOLE), mode="mock")
app = create_app(backend, auth, secure_cookie=False, auth_config={"sign_in_available": True, "message": "Demo data"})
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
