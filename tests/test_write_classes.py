"""The write classes (``operating.write_classes``, ASSIST specs 1.3 / 2.10, contract C8).

Checked at the boundary: (a) every operation any write adapter exposes, every
``write_intents`` operation and every Sage sheet kind has a class row (and no row names an
operation nothing exposes); (b) nothing but a class A write goes by itself: through the
executor with every switch on and no person's approval, each class B operation, and each
class A operation whose rule fires on a planted payload, stays a dry run saying why and
reaches no system, while the same executor performs a clean class A write; with a person's
approval a class B write goes; (c) class C has no transport (nothing performs it); (d) the
three journal CHECKs reject the planted rows; the migration's seed equals the module's table.

SYNTHETIC items and stub adapters; nothing reaches a network. Evidence label:
``injected-transport``."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating import day_coverage
from operating import write_classes as wc
from operating import write_intents as wi
from operating.prepared_writes import Refused
from operating.tests import pg_testdb
from operating.tests.test_autopilot_write import NOW, OWNER, Base, mc_item
from operating.tests.test_autopilot_write_guards import OTHER, people_guard

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

ROOT = Path(__file__).resolve().parents[1]
COMPANY = ["plx.example"]


def exposed() -> set[tuple[str, str]]:
    found = {(c.system, op) for c in wa.ADAPTER_TYPES for op in c.operations}
    found |= {("bc", op) for op in wi.OPERATIONS}
    found |= {("sage", k[len("sage_"):]) for k in day_coverage.MINUTES if k.startswith("sage_")}
    return found


class Stub(wa.Adapter):
    """A system that accepts anything and records what it was asked to perform."""
    TARGET_KEYS = frozenset({"sender", "user", "chat_id", "team_id", "channel_id", "acting_for"})

    def __init__(self, system: str, kind: str, operations) -> None:
        super().__init__(None, environ={})
        self.system, self.kind = system, kind
        self.operations = {op: "stub" for op in operations}
        self.performed: list[str] = []

    def unavailable(self):
        return None

    def request(self, action):
        self._operation(action)
        return {"method": "POST", "url": "/v2.0/tenant/Sandbox2/api/v2.0/stub", "body": {}}

    def perform(self, request, key):
        self.performed.append(key)
        return {"status": "done", "http_status": 200, "response": {}}


def write_item(n: int, system: str, operation: str, payload=None, target=None) -> dict:
    return {"item_id": f"it_class_{n}", "autopilot": "customer_po", "owner": OWNER,
            "title": "Routine follow-up", "citations": ["graph-mail://x/m1@1"],
            "body": {"label": "Routine follow-up",
                     "happened": {"event_type": "message.received", "object_type": "Email",
                                  "sender": "buyer@customer.example", "subject": "Follow-up"},
                     "prepared": {"summary": "Routine follow-up", "gaps": [], "write": {
                         "system": system, "operation": operation, "target": target or {},
                         "payload": payload or {}, "precondition": {},
                         "reason": "Routine follow-up from the thread"}}}}


def mail_item(n: int, body: str, to: str) -> dict:
    item = write_item(n, "bc", "x")
    item["body"]["prepared"]["write"] = None
    item["body"]["prepared"]["draft"] = {"channel": "email", "to": [{"address": to}],
                                         "subject": "Re: docs", "body": body}
    return item


class Classes(Base):
    def live(self):
        env = {**self.go_live(("customer_order_intake",)),
               "PLX_BC_SANDBOX_ENVIRONMENT": "Sandbox2"}   # BC Production has its own approval rule
        stubs = {}
        for cls in wa.ADAPTER_TYPES:
            if cls.operations and cls.kind not in ("mc_approval",):
                stubs[cls.kind] = Stub(cls.system, cls.kind, cls.operations)
        self.stubs = stubs
        return aw.Executor(self.state, environ=env, adapters=stubs, guard=people_guard(),
                           internal_domains=COMPANY, commits=lambda text: [])

    # ---- (a) every operation has a class, none is unclassed
    def test_every_exposed_operation_has_exactly_one_class_and_no_row_is_orphaned(self):
        table = {(r.system, r.operation) for r in wc.SEED}
        self.assertEqual(len(table), len(wc.SEED))                      # one row each
        self.assertEqual(exposed() - table, set(), "operations with no class")
        self.assertEqual(table - exposed(), set(), "classes naming no operation")
        for system, op in exposed():
            self.assertIn(wc.write_class({"system": system, "operation": op}), "ABC")

    def test_an_operation_with_no_class_is_refused_never_guessed(self):
        for action in ({"system": "bc", "operation": "invoice_post"}, {"system": "x"}, {}):
            with self.assertRaises(Refused) as got:
                wc.write_class(action)
            self.assertEqual(got.exception.problems, ["unclassed operation"])

    # ---- (b) only class A goes by itself
    def test_no_class_b_or_c_write_goes_by_itself_and_a_clean_class_a_write_does(self):
        ex = self.live()
        # the rules layer (preconditions, deny terms) is taken out: only the class stands
        # between the item and the system, so a missing class check shows as a performed write
        with mock.patch.object(aw, "routine", lambda *a, **k: []):
            n = 0
            want = [r for r in wc.SEED if r.cls != "A" and r.system not in ("graph", "sage")]
            for row in want:
                n += 1
                stub = self.stubs[aw.WRITE_SYSTEMS[row.system]]
                got = ex.run(write_item(n, row.system, row.operation), NOW)[0]
                with self.subTest(row=(row.system, row.operation)):
                    self.assertEqual((got["status"], got["reasons"]),
                                     ("dry_run", [f"class_{row.cls}_needs_a_person"]), got)
                    self.assertEqual(stub.performed, [])
            self.assertEqual(n, len(want))
            self.assertGreaterEqual(n, 20)
            for n, (system, op) in enumerate([("bc", "purchase_order_comment"),
                                              ("planner", "task_create"),
                                              ("portal", "activity.note"),
                                              ("mc", "task_note")], 100):
                got = ex.run(write_item(n, system, op), NOW)[0]
                with self.subTest(clean=(system, op)):
                    self.assertEqual(got["status"], "done", got)    # the harness does perform

    def test_a_person_approving_a_class_b_write_lets_it_go(self):
        ex = self.live()
        ex.environ[aw.APPROVED_ENV] = "bc_write"
        approve = aw.Approval(by=(OWNER,), confirmed_live=True)
        with mock.patch.object(aw, "routine", lambda *a, **k: []):
            got = ex.run(write_item(200, "bc", "sales_order_create"), NOW, approve)[0]
        self.assertEqual(got["status"], "done", got)
        self.assertEqual(len(self.stubs["bc_write"].performed), 1)

    def test_a_message_class_depends_on_its_text_and_its_readers(self):
        ex = self.live()
        send = Stub("graph", "send_mail", ["send_email"])
        ex.adapters["send_mail"] = send
        for n, (body, to, want) in enumerate([
                ("the CoA is with our QA team", OTHER, "done"),
                ("the CoA is with our QA team", "buyer@customer.example", "dry_run"),
                ("we ship it on 15 Dec", OTHER, "dry_run"),
                ("that is 500 cases", OTHER, "dry_run"),
                ("the CoA is with our QA team", "not-an-address", "not_routine")], 300):
            got = ex.run(mail_item(n, body, to), NOW)[0]
            with self.subTest(body=body, to=to):
                self.assertEqual(got["status"], want, got)
                if want == "dry_run":    # a date or a number is held by the commitment check first
                    self.assertTrue(got["reasons"] == ["class_B_needs_a_person"]
                                    or got["reasons"][0].startswith("commits:"), got["reasons"])
        self.assertEqual(len(send.performed), 1)

    def test_the_three_rules_fire_on_planted_payloads_and_cannot_say_means_fires(self):
        self.assertEqual(wc.write_class({"system": "calendar", "operation": "event_create",
                                         "payload": {"attendees": [{"emailAddress": {
                                             "address": "a@plx.example"}}]}},
                                        known_domains=COMPANY), "A")
        self.assertEqual(wc.write_class({"system": "calendar", "operation": "event_create",
                                         "payload": {"attendees": [{"emailAddress": {
                                             "address": "a@customer.example"}}]}},
                                        known_domains=COMPANY), "B")
        self.assertEqual(wc.write_class({"system": "calendar", "operation": "event_create",
                                         "payload": {"attendees": ["nobody"]}},
                                        known_domains=COMPANY), "B")        # not placeable
        self.assertEqual(wc.write_class({"system": "calendar", "operation": "event_respond",
                                         "payload": {}}, known_domains=COMPANY), "A")
        self.assertEqual(wc.write_class({"system": "graph", "operation": "chat_message",
                                         "payload": {"body": "thanks"}}), "B")   # readers unknown
        # an MC approval: routine only when the rules say so; without them, B
        item = mc_item()
        action = aw.actions_of(item)[0]
        rules = aw.load_rules()
        self.assertEqual(wc.write_class(action), "B")
        self.assertEqual(wc.write_class(action, item=item, rules=rules, workflow="internal_approval"),
                         "A")
        odd = aw.actions_of(mc_item(scope={"kind": "spend", "usd": 9}))[0]
        self.assertEqual(wc.write_class(odd, item=item, rules=rules, workflow="internal_approval"),
                         "B")

    # ---- (c) class C has no transport
    def test_a_class_c_system_has_no_transport_and_performs_nothing(self):
        adapters = wa.default_adapters({})
        for row in wc.SEED:
            if row.cls != "C":
                continue
            kind = aw.WRITE_SYSTEMS[row.system]
            adapter = adapters[kind]
            with self.subTest(row=(row.system, row.operation)):
                self.assertIsNone(adapter.transport)
                self.assertIsNotNone(adapter.unavailable())
                self.assertEqual(adapter.perform({"method": "POST", "url": "x", "body": {}},
                                                 "k")["status"], "not_performed")
        with self.assertRaises(wa.AdapterRefused):
            adapters["sage_write"].request({"system": "sage", "operation": "ar_invoice"})

    # ---- (d) the table in the database, and its CHECKs
    def test_the_migration_seed_is_the_modules_table(self):
        text = (ROOT / "operating/pg/migrations/0067_write_classes_seed.sql").read_text("utf-8")
        self.assertIn(wc.migration_sql(), text)


@unittest.skipIf(psycopg is None, "psycopg not installed")
class Journal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        from operating import assist_readings as ar
        from operating.pg.migrate import MIGRATIONS_DIR
        cls.pg = psycopg.connect(cls.dsn, autocommit=True)
        cls.addClassCleanup(cls.pg.close)
        for stmt in ar.ddl():
            cls.pg.execute(stmt)
        for name in ("0063_assist_work.sql", "0067_write_classes_seed.sql"):
            cls.pg.execute((MIGRATIONS_DIR / name).read_text("utf-8"))

    def row(self, **over) -> None:
        base = dict(prepared_id="p1", person="ann", origin="assist", system="bc",
                    operation="purchase_order_comment", cls="A", event="prepared", auto=False,
                    confirmed_by=None)
        base.update(over)
        self.pg.execute(
            "INSERT INTO assist.write_journal (prepared_id, person, origin, system, operation, "
            "class, event, auto, confirmed_by) VALUES (%(prepared_id)s, %(person)s, %(origin)s, "
            "%(system)s, %(operation)s, %(cls)s, %(event)s, %(auto)s, %(confirmed_by)s)", base)

    def test_the_seed_loaded_by_the_migration_is_the_table(self):
        got = self.pg.execute("SELECT system, operation, class, b_rule, why FROM "
                              "assist.write_classes ORDER BY 1, 2").fetchall()
        self.assertEqual(got, sorted((r.system, r.operation, r.cls, r.b_rule, r.why)
                                     for r in wc.SEED))

    def test_the_checks_reject_what_the_rules_forbid_and_accept_what_they_allow(self):
        self.row(event="performed", auto=True)                       # class A, by itself
        self.row(prepared_id="p2", cls="B", event="performed", confirmed_by="ann")   # a person
        for what, over in (
                ("an automatic class B", dict(cls="B", auto=True, event="prepared")),
                ("an automatic class C", dict(cls="C", auto=True, event="prepared")),
                ("performed by nobody", dict(cls="B", event="performed")),
                ("a class C performed", dict(cls="C", event="performed", confirmed_by="ann")),
                ("a class C confirmed", dict(cls="C", event="confirmed", confirmed_by="ann")),
                ("an unknown class", dict(cls="D")), ("an unknown event", dict(event="sent"))):
            with self.subTest(what):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    self.row(prepared_id="bad", **over)
        self.row(prepared_id="p3", cls="C", event="sheet_shown")      # a sheet is not a write


if __name__ == "__main__":
    unittest.main()
