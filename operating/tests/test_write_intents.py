"""Approve-to-write (``operating.write_intents``): an intent is validated against BC $metadata,
previewed against the record as read, approved only by a granted person who did not
prepare it and whom the people-exclusion guard clears, and executed once, as a dry run
while the write switches are off.

Evidence label: ``isolated-fixture`` (fake current-value reader, fake transport, the
committed $metadata pin and fixture; no HTTP).
"""

from __future__ import annotations

import ast
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from operating import write_intents as wi

COMPANY = "0f1e2d3c-4b5a-4968-8776-655443322110"
VENDOR = "11111111-2222-4333-8444-555555555555"
LINE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ALLOWED_OID = "99999999-8888-4777-8666-555555555555"
EXCLUDED_OID = "12121212-3434-4565-8787-909090909090"
ROOT = Path(__file__).resolve().parents[2]
METADATA = wi.default_metadata() + [wi.Metadata.from_csdl(
    (ROOT / "integrations" / "tests" / "fixtures" / "bc_api_v2_metadata.xml").read_bytes(),
    "fixture")]


class Records:
    """The records as read now (the fake read-only BC); counts every read."""

    def __init__(self) -> None:
        self.rows = {
            "vendors": {VENDOR: {"etag": 'W/"v1"', "fields": {
                "id": VENDOR, "number": "V-100", "displayName": "Vendor A",
                "blocked": "_x0020_"}}},
            "purchaseOrderLines": {LINE: {"etag": 'W/"l1"', "fields": {
                "id": LINE, "quantity": 10, "directUnitCost": 2.5,
                "receivedQuantity": 4.0, "lineObjectNumber": "ITEM-1"}}},
        }
        self.reads = 0

    def __call__(self, env: str, company: str, entity_set: str, record_id: str):
        self.reads += 1
        return self.rows.get(entity_set, {}).get(record_id)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, request):
        self.sent.append(request)
        return {"status": "written", "http_status": 201}


APPROVER = wi.Person("Approver", "approver@example.test", ALLOWED_OID)
PREPARER = wi.Person("Preparer", "preparer@example.test", "31313131-3434-4565-8787-909090909090")
AUTOPILOT = wi.Person("Reorder autopilot", autopilot="reorder")


def po_spec(lines=None, **over):
    spec = {"system": "bc", "operation": "purchase_order_create", "company_id": COMPANY,
            "payload": {"vendorNumber": "V-100", "orderDate": "2026-11-09",
                        "lines": lines or [{"lineType": "Item", "lineObjectNumber": "ITEM-1",
                                            "quantity": "3", "directUnitCost": "0.10"}]},
            "precondition": {"etag": 'W/"v1"', "party_id": VENDOR},
            "reason": "Reorder below safety stock", "citations": ["bc://production/x/items/1@1"]}
    spec.update(over)
    return spec


def line_spec(**payload):
    return {"system": "bc", "operation": "purchase_order_line_update", "company_id": COMPANY,
            "record_id": LINE, "payload": payload or {"quantity": "12"},
            "precondition": {"etag": 'W/"l1"'}, "reason": "Vendor confirmed 12",
            "citations": ["mail://m/1"]}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.records = Records()
        self.transport = FakeTransport()
        self.desk = wi.WriteDesk(wi.Journal(None), transport=None)
        self.grants = wi.WriteGrants({"approver@example.test": ["bc:purchaseOrders",
                                                                "bc:purchaseOrderLines"],
                                      "preparer@example.test": ["bc:purchaseOrders"]})
        self.guard = PeopleExclusion([EXCLUDED_OID])
        self.facts = wi.Facts(price_sheet=lambda item, party: (Decimal("0.05"), Decimal("0.20")),
                              open_demand=lambda item: Decimal("100"))

    def ctx(self, environ=None, **over):
        values = dict(environ=environ or {}, exclusion=self.guard, current=self.records,
                      facts=self.facts, metadata=METADATA, grants=self.grants)
        values.update(over)
        return wi.WriteContext(**values)

    def approved(self, spec=None, by=PREPARER, ctx=None):
        ctx = ctx or self.ctx()
        made = self.desk.prepare(spec or po_spec(), by)
        iid = made["intent"]["intent_id"]
        shown = self.desk.preview(iid, ctx)
        self.desk.approve(iid, APPROVER, ctx, confirmed=True, shown_hash=shown["preview_hash"],
                          acknowledge_warnings=True)
        return iid


class MetadataValidation(Base):
    def test_built_requests_hold_against_the_pin_and_the_fixture(self) -> None:
        for spec in (po_spec(), line_spec(quantity="12", expectedReceiptDate="2026-12-01")):
            iid = self.desk.prepare(spec, PREPARER)["intent"]["intent_id"]
            found = self.desk.preview(iid, self.ctx())
            self.assertEqual(found["problems"], [], spec["operation"])
            self.assertEqual({m.name.split(":")[0] for m in METADATA}, {"pin", "fixture"})
        request = found["request"]
        self.assertEqual(request["method"], "PATCH")
        self.assertTrue(request["path"].endswith(
            f"/companies({COMPANY})/purchaseOrderLines({LINE})"))
        self.assertEqual(request["headers"]["If-Match"], 'W/"l1"')
        self.assertEqual(json.loads(request["body_json"]),
                         {"quantity": 12, "expectedReceiptDate": "2026-12-01"})

    def test_a_field_type_or_entity_metadata_does_not_describe_is_refused(self) -> None:
        bad = {"method": "POST", "entity_set": "purchaseOrders", "body": {
            "vendorNumber": "V", "yourReference": "X", "orderDate": "2026-13-40",
            "purchaseOrderLines": [{"lineType": "Widget", "quantity": "x", "nope": 1}]}}
        problems = " | ".join(wi.validate_request(bad, METADATA))
        for needle in ("yourReference is not in purchaseOrders", "orderDate not a date",
                       "line 1 lineType not one of", "line 1 quantity not a decimal",
                       "line 1 nope is not in purchaseOrderLines"):
            self.assertIn(needle, problems)
        self.assertIn("no entity set widgets",
                      " ".join(wi.validate_request({"entity_set": "widgets", "body": {}},
                                                   METADATA)))

    def test_unknown_operation_system_or_field_fails_closed(self) -> None:
        for spec, needle in ((po_spec(system="sage"), "Unknown system"),
                             (po_spec(system="portal"), "No portal write entity"),
                             (po_spec(operation="delete_everything"), "Unknown operation"),
                             (line_spec(status="Released"), "status cannot be set"),
                             (po_spec(lines=[{"quantity": 2.5}]), "quantity must be above"),
                             (po_spec(citations=[]), "citations are required"),
                             (po_spec(reason=""), "A reason is required")):
            with self.assertRaises(wi.WriteRefused) as caught:
                self.desk.prepare(spec, PREPARER)
            self.assertIn(needle, " ".join(caught.exception.problems))

    def test_a_vendor_note_is_refused_because_api_v2_has_no_note_entity(self) -> None:
        spec = {"system": "bc", "operation": "vendor_note", "company_id": COMPANY,
                "record_id": VENDOR, "payload": {"note": "Prefers 30-day terms"},
                "precondition": {"etag": 'W/"v1"'}, "reason": "Call notes",
                "citations": ["mail://m/2"]}
        iid = self.desk.prepare(spec, PREPARER)["intent"]["intent_id"]
        found = self.desk.preview(iid, self.ctx())
        self.assertIn("no note or comment entity", " ".join(found["problems"]))
        self.assertIsNone(found["request"]["method"])


class Preconditions(Base):
    def test_a_moved_etag_refuses_the_approval_and_the_execution(self) -> None:
        iid = self.desk.prepare(line_spec(), PREPARER)["intent"]["intent_id"]
        shown = self.desk.preview(iid, self.ctx())
        self.assertEqual(shown["changes"], [{"field": "quantity", "current": 10,
                                             "proposed": "12", "changed": True}])
        same = self.desk.prepare(line_spec(directUnitCost="2.50"), PREPARER)
        self.assertFalse(self.desk.preview(same["intent"]["intent_id"],
                                           self.ctx())["changes"][0]["changed"])
        self.records.rows["purchaseOrderLines"][LINE]["etag"] = 'W/"l2"'
        found = self.desk.preview(iid, self.ctx())
        self.assertIn("etag moved", " ".join(found["problems"]))
        with self.assertRaises(wi.WriteRefused) as caught:
            self.desk.approve(iid, APPROVER, self.ctx(), confirmed=True,
                              shown_hash=shown["preview_hash"])
        self.assertEqual(caught.exception.status, 409)
        # approved first, then the record moves: execution refuses
        self.records.rows["purchaseOrderLines"][LINE]["etag"] = 'W/"l1"'
        self.desk.approve(iid, APPROVER, self.ctx(), confirmed=True,
                          shown_hash=self.desk.preview(iid, self.ctx())["preview_hash"])
        self.records.rows["purchaseOrderLines"][LINE]["etag"] = 'W/"l3"'
        with self.assertRaises(wi.WriteRefused) as caught:
            self.desk.execute(iid, APPROVER, self.ctx(), idempotency_key="k" * 20)
        self.assertEqual(caught.exception.status, 409)

    def test_an_unreadable_record_refuses(self) -> None:
        iid = self.desk.prepare(line_spec(), PREPARER)["intent"]["intent_id"]

        def broken(*_a):
            raise RuntimeError("read failed")
        for current in (None, broken):
            found = self.desk.preview(iid, self.ctx(current=current))
            self.assertIn("could not be read", " ".join(found["problems"]))


class Approval(Base):
    def _refused(self, who, ctx, needle, *, spec=None, by=PREPARER):
        made = self.desk.prepare(spec or po_spec(), by)
        iid = made["intent"]["intent_id"]
        shown = self.desk.preview(iid, self.ctx())
        with self.assertRaises(wi.WriteRefused) as caught:
            self.desk.approve(iid, who, ctx, confirmed=True,
                              shown_hash=shown["preview_hash"], acknowledge_warnings=True)
        self.assertIn(needle, " ".join(caught.exception.problems))
        self.assertEqual(self.desk.status(iid)["status"], "prepared")
        return caught.exception

    def test_no_grant_no_approve(self) -> None:
        other = wi.Person("Other", "other@example.test", ALLOWED_OID.replace("9", "7"))
        self.assertEqual(self._refused(other, self.ctx(), "no write grant").status, 403)
        # a grant for another entity is not this one's
        self._refused(APPROVER, self.ctx(), "no write grant",
                      spec=po_spec() | {"operation": "sales_order_create", "payload": {
                          "customerNumber": "C-1", "lines": [{"quantity": "1"}]}})
        # with no grants file nobody holds a grant
        self._refused(APPROVER, self.ctx(grants=None, environ={}), "no write grant",
                      spec=po_spec(reason="second"))
        self.assertFalse(wi.WriteGrants.from_env({wi.GRANTS_FILE_ENV: "/nonexistent"}).grants)

    def test_two_person_exclusion_and_four_eyes(self) -> None:
        self._refused(APPROVER, self.ctx(exclusion=PeopleExclusion()), "guard is unavailable")
        excluded = wi.Person("X", "x@example.test", EXCLUDED_OID)
        grants = wi.WriteGrants({"x@example.test": ["bc:purchaseOrders"],
                                 "approver@example.test": ["bc:purchaseOrders"]})
        self._refused(excluded, self.ctx(grants=grants), "may not take part",
                      spec=po_spec(reason="third"))
        undecided = wi.Person("U", "approver@example.test")   # no oid, guard unbound
        self._refused(undecided, self.ctx(), "cannot decide", spec=po_spec(reason="fourth"))
        self._refused(wi.Person("Approver", "approver@example.test", ALLOWED_OID), self.ctx(),
                      "prepared a write cannot approve", spec=po_spec(reason="fifth"),
                      by=wi.Person("Approver", "approver@example.test"))
        self._refused(wi.Person("Pilot", autopilot="reorder"), self.ctx(), "signed-in, named",
                      spec=po_spec(reason="sixth"))

    def test_the_preparer_cannot_approve_under_another_mailbox_or_without_one(self) -> None:
        # SEC-002: the same Entra object id, or the same mailbox (case and +tag folded), is
        # the same person; a renamed mailbox, an alias and a session without email included.
        prep = wi.Person("Approver", "approver@example.test", ALLOWED_OID)
        grants = wi.WriteGrants({k: ["bc:purchaseOrders"] for k in (
            "renamed@example.test", ALLOWED_OID, "approver+w@example.test")})
        for n, who in enumerate((
                wi.Person("Approver", "renamed@example.test", ALLOWED_OID.upper()),  # rename
                wi.Person("Approver", None, ALLOWED_OID),                           # no email
                wi.Person("Approver", " Approver+W@Example.test", None))):          # alias
            with self.subTest(who=who):
                self._refused(who, self.ctx(grants=grants), "prepared a write cannot approve",
                              spec=po_spec(reason=f"same {n}"), by=prep)
        # a preparer recorded without an email is still matched by object id
        self._refused(wi.Person("Approver", "renamed@example.test", ALLOWED_OID),
                      self.ctx(grants=grants), "prepared a write cannot approve",
                      spec=po_spec(reason="no email"), by=wi.Person("Approver", None,
                                                                   ALLOWED_OID))

    def test_confirm_hash_and_warnings_are_required(self) -> None:
        iid = self.desk.prepare(po_spec(), AUTOPILOT)["intent"]["intent_id"]
        shown = self.desk.preview(iid, self.ctx())
        for kwargs, needle in ((dict(confirmed=False, shown_hash=shown["preview_hash"]),
                                "without your confirm"),
                               (dict(confirmed=True, shown_hash="0" * 64), "changed since")):
            with self.assertRaises(wi.WriteRefused) as caught:
                self.desk.approve(iid, APPROVER, self.ctx(), **kwargs)
            self.assertIn(needle, " ".join(caught.exception.problems))
        self.records.rows["vendors"][VENDOR]["fields"]["blocked"] = "Payment"
        blocked = self.desk.preview(iid, self.ctx())
        self.assertIn("party_blocked", [w["code"] for w in blocked["warnings"]])
        with self.assertRaises(wi.WriteRefused) as caught:
            self.desk.approve(iid, APPROVER, self.ctx(), confirmed=True,
                              shown_hash=blocked["preview_hash"])
        self.assertIn("Acknowledge the warnings", " ".join(caught.exception.problems))
        done = self.desk.approve(iid, APPROVER, self.ctx(), confirmed=True,
                                 shown_hash=blocked["preview_hash"], acknowledge_warnings=True)
        self.assertEqual((done["status"], done["approved_by"]["email"]),
                         ("approved", "approver@example.test"))

    def test_warnings_price_demand_and_unchecked_sources(self) -> None:
        lines = [{"lineObjectNumber": "ITEM-1", "quantity": "150", "directUnitCost": "0.30"}]
        iid = self.desk.prepare(po_spec(lines=lines), PREPARER)["intent"]["intent_id"]
        codes = [w["code"] for w in self.desk.preview(iid, self.ctx())["warnings"]]
        self.assertEqual(codes, ["price_outside_sheet", "quantity_over_demand"])
        codes = [w["code"] for w in self.desk.preview(iid, self.ctx(facts=wi.Facts()))["warnings"]]
        self.assertEqual(codes, ["price_not_checked", "demand_not_checked"])
        lid = self.desk.prepare(line_spec(quantity="3"), PREPARER)["intent"]["intent_id"]
        self.assertIn("below_received",
                      [w["code"] for w in self.desk.preview(lid, self.ctx())["warnings"]])


class Execution(Base):
    def test_execute_is_idempotent_and_a_dry_run_by_default(self) -> None:
        iid = self.approved()
        first = self.desk.execute(iid, APPROVER, self.ctx(), idempotency_key="a" * 20)
        self.assertEqual(first["status"], "would_write")
        self.assertIn(f"Writing is off ({wi.LIVE_FLAG}=1 not set)", first["missing"])
        self.assertEqual(first["request"]["method"], "POST")
        again = self.desk.execute(iid, APPROVER, self.ctx(), idempotency_key="a" * 20)
        other = self.desk.execute(iid, APPROVER, self.ctx(), idempotency_key="b" * 20)
        self.assertTrue(again["replayed"] and other["replayed"])
        rows = [r for r in self.desk.journal.rows() if r["status"] == "would_write"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.desk.prepare(po_spec(), PREPARER)["intent"]["intent_id"], iid)
        exact = self.desk.prepare(po_spec(lines=[{"quantity": Decimal("3"),
                                                  "directUnitCost": Decimal("0.10")}],
                                          reason="autopilot decimals"), AUTOPILOT)
        self.assertEqual(exact["intent"]["payload"]["lines"][0]["directUnitCost"], "0.10")

    def test_not_approved_or_ungranted_does_not_execute(self) -> None:
        iid = self.desk.prepare(po_spec(), PREPARER)["intent"]["intent_id"]
        with self.assertRaises(wi.WriteRefused):
            self.desk.execute(iid, APPROVER, self.ctx(), idempotency_key="c" * 20)
        iid = self.approved(po_spec(reason="other"))
        with self.assertRaises(wi.WriteRefused) as caught:
            self.desk.execute(iid, wi.Person("N", "n@example.test", ALLOWED_OID), self.ctx(),
                              idempotency_key="c" * 20)
        self.assertEqual(caught.exception.status, 403)

    def test_switches_off_never_reach_the_transport_and_only_all_on_does(self) -> None:
        desk = self.desk
        desk.transport = self.transport
        opened = {wi.APPROVED_ENV: wi.DESK_KIND}
        off_cases = [{}, {wi.LIVE_FLAG: "1", **opened}, {wi.SCOPES_FLAG: "bc", **opened},
                     {wi.SANDBOX_FLAG: "1", wi.SANDBOX_ENV: "Production", wi.SCOPES_FLAG: "bc",
                      **opened},
                     # every desk switch on but bc_write not opened to approved writes
                     {wi.SANDBOX_FLAG: "1", wi.SANDBOX_ENV: "Sandbox", wi.SCOPES_FLAG: "bc"},
                     {wi.SANDBOX_FLAG: "1", wi.SANDBOX_ENV: "Sandbox", wi.SCOPES_FLAG: "bc",
                      wi.APPROVED_ENV: "send_mail,bc_writes"}]
        for n, env in enumerate(off_cases):
            iid = self.approved(po_spec(reason=f"off {n}"), ctx=self.ctx(env))
            done = desk.execute(iid, APPROVER, self.ctx(env), idempotency_key=f"off{n}" + "x" * 16)
            self.assertEqual(done["status"], "would_write", env)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(wi.live_gate({}, "Production", None)[:2],
                         [f"Writing is off ({wi.LIVE_FLAG}=1 not set)",
                          f"BC write scopes are not granted ({wi.SCOPES_FLAG})"])
        sandbox = {wi.SANDBOX_FLAG: "1", wi.SANDBOX_ENV: "Sandbox", wi.SCOPES_FLAG: "bc",
                   **opened}
        iid = self.approved(po_spec(reason="sandbox", environment="Sandbox"),
                            ctx=self.ctx(sandbox))
        done = desk.execute(iid, APPROVER, self.ctx(sandbox), idempotency_key="s" * 20)
        self.assertEqual((done["status"], len(self.transport.sent)), ("written", 1))
        self.assertIn("/Sandbox/api/v2.0/", self.transport.sent[0]["path"])

    def test_a_run_the_page_showed_as_a_dry_run_never_writes_live(self) -> None:
        """WEB-008: with every switch on, a run whose page said "Write (dry run)" (the API's
        default live=False) is refused and nothing is sent; the page showing the live
        write (live=True) writes once; a dry-run gate ignores the flag."""
        self.desk.transport = self.transport
        on = {wi.SANDBOX_FLAG: "1", wi.SANDBOX_ENV: "Sandbox", wi.SCOPES_FLAG: "bc",
              wi.APPROVED_ENV: wi.DESK_KIND}
        iid = self.approved(po_spec(reason="shown dry", environment="Sandbox"),
                            ctx=self.ctx(on))
        self.assertEqual(self.desk.preview(iid, self.ctx(on))["live_missing"], [])
        with self.assertRaises(wi.WriteRefused) as caught:
            self.desk.execute(iid, APPROVER, self.ctx(on), idempotency_key="d" * 20,
                              live=False)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("showed a dry run", " ".join(caught.exception.problems))
        self.assertEqual(self.transport.sent, [])
        done = self.desk.execute(iid, APPROVER, self.ctx(on), idempotency_key="e" * 20,
                                 live=True)
        self.assertEqual((done["status"], len(self.transport.sent)), ("written", 1))
        # switches off: whatever the page said, a dry run
        iid = self.approved(po_spec(reason="off anyway"), ctx=self.ctx({}))
        done = self.desk.execute(iid, APPROVER, self.ctx({}), idempotency_key="f" * 20,
                                 live=True)
        self.assertEqual((done["status"], len(self.transport.sent)), ("would_write", 1))

    def test_an_unknown_outcome_blocks_a_repeat_until_bc_is_checked(self) -> None:
        env = {wi.SANDBOX_FLAG: "1", wi.SANDBOX_ENV: "Sandbox", wi.SCOPES_FLAG: "bc",
               wi.APPROVED_ENV: wi.DESK_KIND}

        class Cut(FakeTransport):
            def send(self, request):
                self.sent.append(request)
                return None
        self.desk.transport = Cut()
        iid = self.approved(po_spec(environment="Sandbox"), ctx=self.ctx(env))
        self.assertEqual(self.desk.execute(iid, APPROVER, self.ctx(env),
                                           idempotency_key="u" * 20)["status"], "unknown")
        with self.assertRaises(wi.WriteRefused) as caught:
            self.desk.execute(iid, APPROVER, self.ctx(env), idempotency_key="v" * 20)
        self.assertIn("check BC", " ".join(caught.exception.problems))
        self.assertEqual(len(self.desk.transport.sent), 1)

    def test_an_active_run_cannot_be_dispatched_again_through_recovery(self) -> None:
        # ACT-001 invariant: while the first run is out, a second run with "I checked BC"
        # is refused, in this process and (by its young pending row) in any other.
        env = {wi.SANDBOX_FLAG: "1", wi.SANDBOX_ENV: "Sandbox", wi.SCOPES_FLAG: "bc",
               wi.APPROVED_ENV: wi.DESK_KIND}
        started, release = threading.Event(), threading.Event()

        class Slow(FakeTransport):
            def send(self, request):
                self.sent.append(request)
                started.set()
                release.wait(5)
                return {"status": "written", "http_status": 201}
        self.desk.transport = Slow()
        iid = self.approved(po_spec(environment="Sandbox"), ctx=self.ctx(env))
        first: list = []
        worker = threading.Thread(target=lambda: first.append(self.desk.execute(
            iid, APPROVER, self.ctx(env), idempotency_key="a" * 20)))
        worker.start()
        self.assertTrue(started.wait(5))
        other = wi.WriteDesk(self.desk.journal, transport=self.desk.transport)
        for desk in (self.desk, other):
            with self.assertRaises(wi.WriteRefused) as caught:
                desk.execute(iid, APPROVER, self.ctx(env), idempotency_key="b" * 20,
                             checked_bc=True)
            self.assertIn("in progress", " ".join(caught.exception.problems))
        release.set()
        worker.join(5)
        self.assertEqual((first[0]["status"], len(self.desk.transport.sent)), ("written", 1))

    def test_journal_rows_audit_every_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.desk = wi.WriteDesk.for_state_dir(Path(tmp))
            iid = self.approved()
            self.desk.execute(iid, APPROVER, self.ctx(), idempotency_key="j" * 20)
            rows = wi.Journal(Path(tmp) / "writes" / "journal.jsonl").rows()
        self.assertEqual([(r["status"], r["actor"]["name"]) for r in rows],
                         [("prepared", "Preparer"), ("approved", "Approver"),
                          ("would_write", "Approver")])
        self.assertEqual(rows[2]["request"]["headers"], {"Content-Type": "application/json"})


class Cardinality(Base):
    def test_lines_kept_and_money_exact_at_1_10_60(self) -> None:
        for count in (1, 10, 60):
            lines = [{"lineObjectNumber": f"ITEM-{n}", "quantity": "3",
                      "directUnitCost": "0.10"} for n in range(count)]
            iid = self.desk.prepare(po_spec(lines=lines, reason=f"n{count}"),
                                    PREPARER)["intent"]["intent_id"]
            found = self.desk.preview(iid, self.ctx())
            self.assertEqual(len(found["lines"]), count)
            self.assertEqual({line["amount"] for line in found["lines"]}, {"0.30"})
            body = json.loads(found["request"]["body_json"], parse_float=Decimal)
            self.assertEqual(len(body["purchaseOrderLines"]), count)
            self.assertEqual(body["purchaseOrderLines"][0]["directUnitCost"], Decimal("0.10"))


class Visibility(Base):
    def test_only_the_preparer_and_granted_readers_list_an_intent(self) -> None:
        self.desk.prepare(line_spec(), PREPARER)
        stranger = wi.Person("S", "s@example.test", "s")
        self.assertEqual(self.desk.listing(stranger, self.ctx()), [])
        self.assertEqual(len(self.desk.listing(PREPARER, self.ctx())), 1)
        self.assertEqual(len(self.desk.listing(APPROVER, self.ctx())), 1)

    def test_the_preparer_is_known_by_object_id_not_by_a_reused_mailbox(self) -> None:
        """ACT-006: an intent that recorded the preparer's object id is theirs alone: a
        different person on the same (reused) mailbox, or a session without an object id,
        does not see it without a grant; the preparer under a renamed mailbox does. An
        intent recorded before object ids were kept is matched by its exact mailbox."""
        self.desk.prepare(line_spec(), PREPARER)
        other = "42424242-4242-4424-8424-424242424242"
        for who, sees in (
                (wi.Person("Next holder", PREPARER.email, other), 0),
                (wi.Person("No oid", PREPARER.email, None), 0),
                (wi.Person("Renamed", "renamed@example.test", PREPARER.oid.upper()), 1),
                (PREPARER, 1)):
            with self.subTest(who=who.name):
                self.assertEqual(len(self.desk.listing(who, self.ctx())), sees)
        legacy = {"name": "Old", "email": "old@example.test", "oid": None, "autopilot": None}
        self.assertTrue(wi._prepared_by(legacy, wi.Person("Old", "old@example.test", other)))
        self.assertFalse(wi._prepared_by(legacy, wi.Person("X", "x@example.test", other)))
        self.assertFalse(wi._prepared_by(legacy, wi.Person("X", "OLD+x@example.test", None)))


class NoWriteReach(unittest.TestCase):
    """While the switches are off no code path reaches a non-GET BC call: product code
    hands the desk only the transport ``write_transports.for_system`` gives, which is None
    until the owner opened BC to approved writes, and this module imports no HTTP client.
    (Replaced 11-11 the rule "no transport is ever constructed": the decided design writes
    on a person's approval)"""

    def test_product_code_hands_the_desk_a_transport_only_once_bc_is_opened(self) -> None:
        from integrations import live_source_guard as guard
        from integrations import write_transports as wt
        self.assertIsNone(wt.for_system("bc", {}))
        self.assertIsNone(wt.for_system("bc", {guard.WRITES_FLAG: "planner,1,all"}))

        def gated(call: ast.Call) -> bool:
            value = next(k.value for k in call.keywords if k.arg == "transport")
            return (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "for_system" and bool(value.args)
                    and isinstance(value.args[0], ast.Constant) and value.args[0].value == "bc")
        found = []
        for path in ROOT.glob("**/*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or rel.startswith(("tests/", ".worktrees/", "wt/")) \
                    or "node_modules" in rel:
                continue
            text = path.read_text("utf-8", errors="replace")
            if "WriteDesk" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Call) and any(k.arg == "transport"
                                                      for k in node.keywords) \
                        and not gated(node):
                    found.append(f"{rel}:{node.lineno}")
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Attribute) and t.attr == "transport"
                        for t in node.targets) and rel != "operating/write_intents.py":
                    found.append(f"{rel}:{node.lineno}")
        self.assertEqual(found, [])
        tree = ast.parse((ROOT / "operating" / "write_intents.py").read_text("utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree)
                    if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in getattr(n, "names", [])} | {
            (n.module or "").split(".")[0] for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)}
        self.assertFalse(imported & {"urllib", "http", "requests", "socket", "integrations"})


if __name__ == "__main__":
    unittest.main()
