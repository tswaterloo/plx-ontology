"""The autopilot write path (``operating.autopilot_write``): gate, routine rules, people
exclusion, kill switches, rate limits, idempotency, audit log and undo.

SYNTHETIC items and addresses; fake transports and recorded answers only: nothing here
reaches a network. Evidence label: injected-transport.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from integrations.people_exclusion import PeopleExclusion
from operating import autopilot as ap
from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa

OWNER = "owner@plx.example"
OWNER_OID = "11111111-1111-4111-8111-111111111111"
HIDDEN = "hidden@plx.example"
HIDDEN_OID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
ROLE = f"person:{OWNER}"
BC_COMPANY = "33333333-3333-4333-8333-333333333333"
NEW_ID = "44444444-4444-4444-8444-444444444444"


def guard(unavailable: bool = False) -> PeopleExclusion:
    found = PeopleExclusion([HIDDEN_OID])
    if unavailable:
        return PeopleExclusion([])           # no list: unavailable (fail closed)
    found.attach_directory([(OWNER_OID, OWNER, []), (HIDDEN_OID, HIDDEN, [])])
    return found


def mc_item(n: int = 1, *, reason: str = "Launch the nightly lint agent",
            scope: dict | None = None, rec: str = "approve") -> dict:
    scope = {"kind": "launch", "lane": "lint", "promptHash": "ab12"} if scope is None else scope
    return {"item_id": f"it_mc_{n}", "autopilot": "approval_request", "owner": OWNER,
            "title": f"Approval requested for TASK-{n}", "citations": [f"mc://ops/approvals/g{n}@1"],
            "body": {"label": "Approval requested",
                     "happened": {"event_type": "approval.requested", "object_type": "MCApproval",
                                  "fields": {"task_id": f"TASK-{n}", "reason": reason,
                                             "requested_by": "sp_mcp_agent"}},
                     "prepared": {"summary": f"Approval requested for TASK-{n}: {reason}",
                                  "recommendation": rec, "gaps": [],
                                  "action": {"kind": "mc_approval", "gate": f"gate_{n}",
                                             "task_id": f"TASK-{n}",
                                             "requested_by": "sp_mcp_agent", "scope": scope}}}}


def po_item(*, price: bool = False, draft: bool = True) -> dict:
    line = {"lineType": "Item", "lineObjectNumber": "ITEM-1", "quantity": 4}
    if price:
        line["unitPrice"] = "12.50"
    prepared = {"summary": "New PO P-1 from a customer: 1 of 1 lines resolved", "gaps": [],
                "write": {"system": "bc", "operation": "sales_order_create",
                          "payload": {"customerNumber": "C-1", "externalDocumentNumber": "P-1",
                                      "orderDate": "2026-11-12", "salesOrderLines": [line]},
                          "reason": "Customer PO P-1 received by mail; 1 lines read.",
                          "precondition": {"party": "C-1"}}}
    if draft:
        prepared["draft"] = {"channel": "email", "to": [{"address": "buyer@customer.example"}],
                             "subject": "RE: PO P-1", "body": "Thanks, we have PO P-1 with 1 line."}
    return {"item_id": "it_po_1", "autopilot": "customer_po", "owner": OWNER,
            "title": "Customer PO received", "citations": ["graph-mail://x/m1@1"],
            "body": {"label": "Customer PO received",
                     "happened": {"event_type": "message.received", "object_type": "Email",
                                  "sender": "buyer@customer.example", "subject": "PO P-1"},
                     "prepared": prepared}}


class Transport:
    """A fake system: records every request; answers from a script (default 201)."""

    def __init__(self, *answers: tuple[int, dict]) -> None:
        self.calls: list[tuple[dict, str]] = []
        self.answers = list(answers)

    def __call__(self, request, key):
        self.calls.append((dict(request), key))
        return self.answers.pop(0) if self.answers else (201, {})


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.mc, self.bc = Transport(), Transport()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def executor(self, env: dict | None = None, *, g=None, rules: Path | None = None,
                 **adapters) -> aw.Executor:
        env = {"PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY, **(env or {})}
        found = {**wa.default_adapters(env),
                 "mc_approval": wa.MCApprovalAdapter(self.mc, environ=env),
                 "bc_write": wa.BCAdapter(self.bc, environ=env)}
        found.pop("send_mail")   # a test never reaches the real send path
        found.update(adapters)
        return aw.Executor(self.state, environ=env, adapters=found,
                           guard=guard() if g is None else g, rules_path=rules)

    def fresh_run(self, judged: int = 15, matched: int = 14, *,
                  start: str = "2026-11-06T07:00:00Z", status: str = "complete",
                  workflows=("internal_approval", "customer_order_intake")) -> Path:
        run = Path(self.tmp.name) / f"gen1-until-x{time.monotonic_ns()}"
        (run / "slug").mkdir(parents=True)
        (run / ap.GATE_FILE).write_text(json.dumps({"schema": ap.GATE_SCHEMA, "roles": {
            ROLE: {"members": [OWNER], "workflows": {
                w: {"judged": judged, "matched": matched} for w in workflows}}}}))
        (run / "slug" / "result.json").write_text(json.dumps(
            {"role": ROLE, "status": status, "window": [start, "2026-11-10T07:00:00Z"]}))
        return run

    def pass_gate(self, **kw) -> None:
        rules = aw.load_rules()
        out = aw.record_scores(self.fresh_run(**kw), self.state / aw.WRITE_GATE_FILE, rules)
        self.assertIn(ROLE, out["admitted"])
        aw.sync_gate(aw.Journal(self.state / aw.AUDIT_FILE),
                     aw.WriteGate(self.state / aw.WRITE_GATE_FILE, rules), NOW)

    def go_live(self, workflows=("internal_approval",)) -> dict:
        self.pass_gate()
        journal = aw.Journal(self.state / aw.AUDIT_FILE)
        for w in workflows:
            aw.ack_notice(journal, ROLE, w, NOW, "operator")
        return {aw.MASTER_ENV: "1", aw.LIVE_ENV: ",".join(f"{ROLE}:{w}" for w in workflows)}


class GateAndFlags(Base):
    def test_ships_off_dry_run_until_gate_notice_and_owner_flag(self):
        # no gate, no flags: a routine MC launch approval is a dry run with the exact request
        ex = self.executor()
        r = ex.run(mc_item(), NOW)[0]
        self.assertEqual((r["status"], r["reasons"]), ("dry_run", ["gate_not_passed"]))
        self.assertEqual(r["request"]["body"]["decision"], "approved")
        self.assertEqual(r["request"]["body"]["renderedScope"]["kind"], "launch")
        # passed on fresh days: recorded once, waits for the owner notice
        self.pass_gate()
        self.pass_gate()                   # a re-run of the same scores: no second notice
        rows = ex.journal.rows()
        self.assertEqual([n["workflow"] for n in aw.notices(rows)],
                         ["customer_order_intake", "internal_approval"])
        self.assertEqual(sum(r.get("event") == "gate_passed" for r in rows), 2)
        self.assertEqual(ex.run(mc_item(), NOW)[0]["reasons"], ["passed_awaiting_owner_notice"])
        aw.ack_notice(ex.journal, ROLE, "internal_approval", NOW, "operator")
        self.assertEqual(ex.run(mc_item(), NOW)[0]["reasons"], ["owner_flag_off"])
        # the owner's flag without the master switch, then both
        flag = {aw.LIVE_ENV: f"{ROLE}:internal_approval"}
        self.assertEqual(self.executor(flag).run(mc_item(), NOW)[0]["reasons"],
                         [f"master_switch_off:{aw.MASTER_ENV}"])
        self.assertEqual(self.mc.calls, [])
        r = self.executor({**flag, aw.MASTER_ENV: "1"}).run(mc_item(), NOW)[0]
        self.assertEqual((r["status"], r["mode"]), ("done", "live"))
        self.assertEqual(len(self.mc.calls), 1)
        # the audit row: what, where, why, citations, result, key; the chain holds
        self.assertEqual((r["kind"], r["system"], r["target"]["gate_id"], r["why"]),
                         ("mc_approval", "mc", "gate_1", "Approval requested"))
        self.assertEqual(r["citations"], ["mc://ops/approvals/g1@1"])
        self.assertIsNone(r["inverse"])
        self.assertIn("4 h", r["undo_note"])
        aw.Journal(self.state / aw.AUDIT_FILE).rows()

    def test_gate_needs_fresh_complete_and_90_on_15(self):
        rules = aw.load_rules()
        path = self.state / aw.WRITE_GATE_FILE
        out = aw.record_scores(self.fresh_run(start="2026-10-27T07:00:00Z"), path, rules)
        self.assertEqual(out["refused"], {ROLE: "not_fresh"})
        out = aw.record_scores(self.fresh_run(status="partial"), path, rules)
        self.assertEqual(out["refused"], {ROLE: "status:partial"})
        for judged, matched, passes in ((14, 14, False), (15, 13, False), (15, 14, True),
                                        (20, 18, True)):
            aw.record_scores(self.fresh_run(judged, matched), path, rules)
            self.assertEqual(bool(aw.WriteGate(path, rules).passing()), passes, (judged, matched))
        # a gate file edited to an old window is not fresh at read time either
        doc = json.loads(path.read_text())
        doc["roles"][ROLE]["window"][0] = "2026-10-20T07:00:00Z"
        path.write_text(json.dumps(doc))
        self.assertEqual(aw.WriteGate(path, rules).passing(), [])

    def test_lost_gate_stops_live_at_once(self):
        env = self.go_live()
        aw.record_scores(self.fresh_run(15, 10), self.state / aw.WRITE_GATE_FILE,
                         aw.load_rules())
        aw.sync_gate(aw.Journal(self.state / aw.AUDIT_FILE),
                     aw.WriteGate(self.state / aw.WRITE_GATE_FILE, aw.load_rules()), NOW)
        r = self.executor(env).run(mc_item(), NOW)[0]
        self.assertEqual((r["status"], r["reasons"]), ("dry_run", ["gate_not_passed"]))
        # passing again needs a fresh owner notice before live
        self.pass_gate()
        self.assertEqual(self.executor(env).run(mc_item(), NOW)[0]["reasons"],
                         ["passed_awaiting_owner_notice"])
        self.assertEqual(self.mc.calls, [])

    def test_invalid_or_looser_rules_refuse_everything(self):
        env = self.go_live()
        loose = json.loads(aw.RULES_FILE.read_text())
        loose["gate"]["min_judged"] = 5
        path = Path(self.tmp.name) / "rules.json"
        path.write_text(json.dumps(loose))
        r = self.executor(env, rules=path).run(mc_item(), NOW)[0]
        self.assertEqual(r["status"], "refused")
        self.assertTrue(r["reasons"][0].startswith("rules_invalid"))
        loose["gate"]["min_judged"] = 15
        loose["actions"]["mc_approval"]["deny_term_classes"].append("nonexistent")
        path.write_text(json.dumps(loose))
        self.assertEqual(self.executor(env, rules=path).run(mc_item(), NOW)[0]["status"],
                         "refused")
        self.assertEqual(self.mc.calls, [])


class Routine(Base):
    def status_of(self, item, env=None, **kw):
        return [(r["status"], r.get("reasons")) for r in self.executor(env, **kw).run(item, NOW)]

    def test_mc_only_launch_with_precedent_and_no_sensitive_terms(self):
        env = self.go_live()
        cases = {
            "money": mc_item(2, reason="Approve the vendor payment run"),
            "permission": mc_item(3, reason="Grant admin access to the agent"),
            "migration": mc_item(4, reason="Run the schema migration"),
            "production": mc_item(5, reason="Deploy the fix"),
            "merge_scope": mc_item(6, scope={"kind": "merge", "repo": "a/b", "pr": 1}),
            "no_scope": mc_item(7, scope={}),
            "no_precedent": mc_item(8, rec="review"),
            "new_vendor": mc_item(9, reason="Set up a new vendor in the books"),
        }
        for name, item in cases.items():
            (status, reasons), = self.status_of(item, env)
            self.assertEqual(status, "not_routine", name)
        self.assertEqual(self.mc.calls, [])
        self.assertEqual(self.status_of(mc_item(10), env)[0][0], "done")

    def test_money_price_unknown_ops_and_contested_never_routine(self):
        env = self.go_live(("customer_order_intake",))
        (status, reasons), = self.status_of(po_item(price=True, draft=False), env)
        self.assertEqual(status, "not_routine")
        self.assertIn("field:unitPrice", reasons)
        sage = po_item(draft=False)
        sage["body"]["prepared"]["write"] = {"system": "sage", "operation": "invoice_entry",
                                             "payload": {"batch": "B1"}, "reason": "x"}
        self.assertEqual(self.status_of(sage, env)[0][1][0], "operation_not_routine:invoice_entry")
        odd = po_item(draft=False)
        odd["body"]["prepared"]["write"]["system"] = "ledger"
        self.assertEqual(self.status_of(odd, env)[0][0], "refused")
        gaps = po_item(draft=False)
        gaps["body"]["prepared"]["gaps"] = ["lines_from_po_document"]
        self.assertIn("incomplete:gaps", self.status_of(gaps, env)[0][1])
        rules = json.loads(aw.RULES_FILE.read_text())
        rules["contested_workflows"] = ["customer_order_intake"]
        path = Path(self.tmp.name) / "rules.json"
        path.write_text(json.dumps(rules))
        self.assertIn("contested_workflow",
                      self.status_of(po_item(draft=False), env, rules=path)[0][1])
        self.assertEqual(self.bc.calls, [])

    def test_mail_to_a_new_domain_or_too_many_is_not_routine(self):
        env = self.go_live(("customer_order_intake",))
        item = po_item()
        item["body"]["prepared"]["write"] = None
        item["body"]["prepared"]["draft"]["to"] = [{"address": "someone@elsewhere.example"}]
        self.assertEqual(self.status_of(item, env)[0], ("not_routine", ["unknown_domain"]))
        item["body"]["prepared"]["draft"]["to"] = [
            {"address": f"p{n}@customer.example"} for n in range(4)]
        self.assertEqual(self.status_of(item, env)[0], ("not_routine", ["recipients:4"]))

    def test_unknown_prepared_shape_is_refused(self):
        env = self.go_live()
        item = mc_item()
        item["body"]["prepared"]["action"]["kind"] = "wire_funds"
        self.assertEqual(self.status_of(item, env)[0],
                         ("refused", ["unknown_action:wire_funds"]))
        stray = mc_item()
        stray["autopilot"] = "not_in_registry"
        self.assertEqual(self.status_of(stray, env)[0],
                         ("refused", ["unknown_autopilot"]))
        empty = mc_item()
        empty["body"]["prepared"] = {}
        self.assertEqual(self.status_of(empty)[0], ("refused", ["no_action"]))


class Safety(Base):
    def test_excluded_people_and_unavailable_guard(self):
        env = self.go_live(("customer_order_intake",))
        item = po_item(draft=False)
        item["body"]["prepared"]["write"]["reason"] = f"Asked by {HIDDEN}"
        self.assertEqual(self.executor(env).run(item, NOW)[0]["reasons"], ["person_excluded"])
        hidden_owner = mc_item()
        hidden_owner["owner"] = HIDDEN
        self.assertEqual(self.executor(env).run(hidden_owner, NOW)[0]["reasons"],
                         ["owner_not_cleared"])
        unknown_colleague = mc_item()
        unknown_colleague["body"]["prepared"]["action"]["requested_by"] = "nobody@plx.example"
        self.assertEqual(self.executor(env).run(unknown_colleague, NOW)[0]["reasons"],
                         ["person_undecided"])
        self.assertEqual(self.executor(env, g=guard(unavailable=True)).run(mc_item(), NOW)[0]
                         ["reasons"], ["exclusion_unavailable"])
        self.assertEqual((self.mc.calls, self.bc.calls), ([], []))

    def test_global_and_workflow_kill_switches(self):
        env = self.go_live()
        (self.state / aw.KILL_FILE).touch()
        self.assertEqual(self.executor(env).run(mc_item(), NOW)[0]["status"], "killed")
        (self.state / aw.KILL_FILE).unlink()
        self.assertEqual(self.executor({**env, ap.KILL_ENV: "1"}).run(mc_item(), NOW)[0]
                         ["status"], "killed")
        off = {**env, aw.WORKFLOW_KILL_ENV: "internal_approval"}
        self.assertEqual(self.executor(off).run(mc_item(), NOW)[0]["reasons"],
                         ["workflow_kill_switch"])
        (self.state / aw.WORKFLOW_KILL_FILE).write_text("{not json")
        r = self.executor(env).run(mc_item(), NOW)[0]
        self.assertEqual((r["status"], r["reasons"]), ("killed", ["workflow_kill_file_unreadable"]))
        self.assertEqual(self.mc.calls, [])

    def test_rate_limit_per_workflow_and_cardinality(self):
        env = self.go_live()
        ex = self.executor(env)
        statuses = [ex.run(mc_item(n), NOW)[0]["status"] for n in range(1, 8)]
        self.assertEqual(statuses, ["done"] * 5 + ["rate_limited"] * 2)
        # an hour later the window moved (5 in any rolling hour); the day cap (20) holds
        later = [ex.run(mc_item(n), NOW + timedelta(hours=1, minutes=13 * (n - 8)))[0]
                 ["status"] for n in range(8, 30)]
        self.assertEqual(later.count("done"), 15)
        self.assertEqual(len(self.mc.calls), 20)
        # N = 1 / 10 / 60 with a high cap: every item once, none lost or doubled
        rules = json.loads(aw.RULES_FILE.read_text())
        rules["rate_limits"] = {"per_hour": 1000, "per_day": 1000, "workflows": {}}
        for n_items in (1, 10, 60):
            self.tearDown()
            self.setUp()
            path = Path(self.tmp.name) / "rules.json"
            path.write_text(json.dumps(rules))
            env = self.go_live()
            ex = self.executor(env, rules=path)
            began = time.monotonic()
            first = [ex.run(mc_item(n), NOW)[0] for n in range(n_items)]
            self.assertEqual({r["status"] for r in first}, {"done"}, first[0].get("reasons"))
            done = [r["status"] for r in first]
            done += [ex.run(mc_item(n), NOW)[0]["status"] for n in range(n_items)]
            self.assertLess(time.monotonic() - began, 20)
            self.assertEqual(done, ["done"] * n_items + ["duplicate"] * n_items)
            self.assertEqual(sorted(c[0]["body"]["gateId"] for c in self.mc.calls),
                             sorted(f"gate_{n}" for n in range(n_items)))

    def test_unknown_outcome_blocks_repeat_until_reconciled(self):
        env = self.go_live()
        self.mc.answers = [(503, {})]
        ex = self.executor(env)
        self.assertEqual(ex.run(mc_item(), NOW)[0]["status"], "unknown")
        again = ex.run(mc_item(), NOW)[0]
        self.assertEqual((again["status"], again["reasons"]), ("duplicate", ["already:unknown"]))
        ex.reconcile(again["key"], "done", "a person", NOW)
        self.assertEqual(ex.run(mc_item(), NOW)[0]["reasons"], ["already:done"])
        self.assertEqual(len(self.mc.calls), 1)
        # a definite refusal is settled too (a changed payload is a new key)
        self.mc.answers = [(403, {"code": "separation_of_duties"})]
        self.assertEqual(ex.run(mc_item(2), NOW)[0]["status"], "failed")
        self.assertEqual(ex.run(mc_item(2), NOW)[0]["status"], "duplicate")

    def test_entry_then_message_stops_when_entry_fails_and_undo(self):
        env = self.go_live(("customer_order_intake",))
        mail = Transport()
        sender = _Sender(mail)
        adapters = {"send_mail": wa.MailAdapter(sender, environ=env)}
        self.bc.answers = [(400, {"code": "BadRequest"})]
        out = self.executor(env, **adapters).run(po_item(), NOW)
        self.assertEqual([r["kind"] for r in out], ["bc_write"])
        self.assertEqual(out[0]["status"], "failed")
        self.assertEqual(mail.calls, [])
        # a created order: done, the inverse recorded, undone once
        item = po_item()
        item["item_id"] = "it_po_2"
        self.bc.answers = [(201, {"id": NEW_ID, "@odata.etag": "W/\"1\""}), (204, {})]
        out = self.executor(env, **adapters).run(item, NOW)
        self.assertEqual([(r["kind"], r["status"]) for r in out],
                         [("bc_write", "done"), ("send_mail", "done")])
        self.assertEqual(out[0]["request"]["method"], "POST")
        self.assertEqual(out[0]["request"]["body"]["salesOrderLines"][0]["quantity"], "4")
        self.assertEqual(out[0]["inverse"]["method"], "DELETE")
        ex = self.executor(env, **adapters)
        undone = ex.undo(out[0]["key"], NOW)
        self.assertEqual(undone["status"], "done")
        self.assertEqual(self.bc.calls[-1][0]["method"], "DELETE")
        self.assertTrue(self.bc.calls[-1][0]["url"].endswith(f"({NEW_ID})"))
        with self.assertRaises(ValueError):
            ex.undo(out[0]["key"], NOW)
        with self.assertRaises(ValueError):         # a sent email has no undo
            ex.undo(out[1]["key"], NOW)
        self.assertEqual(len(mail.calls), 1)

    def test_unavailable_adapters_never_attempt(self):
        env = self.go_live()
        ex = self.executor(env, mc_approval=wa.MCApprovalAdapter(None, environ=env))
        r = ex.run(mc_item(), NOW)[0]
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("approval.decide", r["reasons"][0])
        self.assertFalse(any(row.get("event") == "attempt" for row in ex.journal.rows()))
        status = aw.status(self.state, {})
        states = {a["kind"]: a["state"] for a in status["adapters"]}
        self.assertEqual(states["send_mail"], "ready")
        for kind in ("mc_approval", "bc_write", "sage_write", "portal_write",
                     "filemaker_write", "send_teams"):
            self.assertTrue(states[kind].startswith("unavailable: "), kind)
        self.assertFalse(status["master_switch"])
        self.assertEqual(status["writes"], 0)
        # the live check's spec holds on the real status output
        from tests.spec_verdict import spec_of, verdict
        doc = json.loads(json.dumps(aw.status(Path(self.tmp.name) / "empty", {})))
        self.assertEqual(verdict(spec_of("autopilot.write_path"), 0, doc), ("pass", []))


class _Sender:
    """Stands in for ``graph_send.ActionSender``: the fake Graph answers 202."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def dispatch(self, kind, sender, payload):
        self.transport({"kind": kind, "sender": sender, **payload}, "")
        return {"status": "sent", "http_status": 202, "missing": []}


if __name__ == "__main__":
    unittest.main()
