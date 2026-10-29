"""``plx.review``: a person's corrections reach the ontology and survive a restart.

Directive dd40ff1bf83f P3 (amendment bb664f58910f §1). A supplier confirmation mail
states the supplier's number PI-SWC024613 for an FM purchase order (Vince's case); the
employee confirms it as an alias of the PO through ``plx.review`` and the next
``plx.find`` (another process, the review store reopened) answers it as a ``reviewed``
alias match. A later capture of the mail with other content withdraws the review; a
reader without the mail grant sees the review withheld (never absent); the store is
append-only; the author is the session's employee, never an argument; link decisions
enter the projection as ``reviewed`` / ``contradicted`` links at the next build. Built
projection store over static captures; no network. Evidence: isolated-fixture.
"""

from __future__ import annotations

from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ontology import identifiers, reviews, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import CapturedRecord, build_store
from ontology.resolve import ReviewedAliases
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import kind_confirmation
from operating import ontology_tools_review as rt
from operating.employee_permissions import (EmployeeActorGate, EmployeePermissionPolicy,
                                            EmployeePermissionResolver, EmployeePrincipal,
                                            PortalAccessSnapshot)
from operating.session_identity import (CHANNEL_AUTHOR, CHANNEL_CONSOLE, CHANNEL_MCP,
                                        PROOF_CONFIGURED, SessionIdentity)

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
# The shipped employee policy; its review:identity entry is the owner's grant.
SHIPPED_POLICY = ROOT / "examples/employee_permission_policy.example.json"
ENTRA = "0f0f0f0f-0000-4000-8000-000000000001"
OID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"   # orders desk: Portal and the mailbox
OID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"   # Portal only
T0 = "2026-09-01T10:00:00.000000Z"
T_MAIL2 = "2026-10-03T09:00:00.000000Z"          # the mail is captured again, changed
REVIEWED_AT = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)
NOW = dt.datetime(2026, 10, 2, 12, 30, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
BOX = "orders@plx.test"
MAIL_HANDLE = f"graph-mail://{BOX}/MSG-1@CK-1"
MAIL2_HANDLE = f"graph-mail://{BOX}/MSG-1@CK-2"
D1, D2 = "1" * 64, "2" * 64
PO9 = 'PurchaseOrder:portal.fm_generated_purchase_order:{"id":9}'
PO10 = 'PurchaseOrder:portal.fm_generated_purchase_order:{"id":10}'
EMAIL = 'Email:graph_mail.messages:{"id":"MSG-1","mailbox":"orders@plx.test"}'
NUMBER = "PI-SWC024613"
LINK = "Email.mentions_purchase_order"


def employee_session(oid: str, channel: str = CHANNEL_CONSOLE) -> SessionIdentity:
    employee = EmployeePrincipal(ENTRA, oid)
    return SessionIdentity(tenant="plx", channel=channel, author_kind=CHANNEL_AUTHOR[channel],
                           proof=PROOF_CONFIGURED, employee=employee,
                           principals=frozenset({employee.principal}),
                           identities=frozenset({oid}))


def identity_reviewer(actor: Actor) -> Actor:
    """The host's employee gate for an employee whose entitlements grant identity
    review (the policy principal ``review:identity``)."""
    return Actor(actor.tenant, actor.principals | {rt.IDENTITY_REVIEW}, actor.clearance)


def resolved_gate(directory: Path, grants: tuple[str, ...], role: str = "STAFF",
                  access_level: str | None = None) -> EmployeeActorGate:
    """The host's employee gate over the real resolver: OID_A is an active Portal
    employee of ``role`` (at ``access_level``, default the role's bootstrap level) whose
    policy grants ``grants`` (each a VIEW_OPERATIONS rule on the ops department) beside
    the shipped ``review:identity`` entry unless ``grants`` names that principal,
    resolved from the policy and Portal snapshot on every call."""
    from operating.tests.test_session_identity import identity_files, user_row

    level = {} if access_level is None else {"accessLevel": access_level}
    policy, portal = identity_files(directory, {
        grant: ("VIEW_OPERATIONS", "operations.sales", "ops") for grant in grants},
        users=[user_row(OID_A, "orders@plx.test", role, **level)])
    doc = json.loads(policy.read_text("utf-8"))
    doc["principals"].setdefault(rt.IDENTITY_REVIEW, json.loads(SHIPPED_POLICY.read_text(
        "utf-8"))["principals"][rt.IDENTITY_REVIEW])
    policy.write_text(json.dumps(doc), "utf-8")
    return EmployeeActorGate(EmployeePermissionResolver(
        EmployeePermissionPolicy.from_file(policy), PortalAccessSnapshot(portal)), "plx",
        require_employee=True)


def po(key: int, number: str) -> CapturedRecord:
    relation = "public.generated_purchase_order"
    return CapturedRecord("portal_api", relation, json.dumps({"id": key}),
                          {"id": key, "ponumber": number}, f"api://{relation}/{key}@g-1", "row",
                          "0" * 64, T0, None, T0, f"api:{relation}")


def mail(handle: str, digest: str, subject: str, start: str, end: str | None) -> CapturedRecord:
    row = {"mailbox": BOX, "id": "MSG-1", "subject": subject, "from": "sales@supplier.test",
           "received_at": "2026-09-01T09:59:00Z"}
    return CapturedRecord("graph_mail", "messages", json.dumps(["MSG-1", BOX]), row, handle,
                          "row", digest, start, end, start, f"mail:{BOX}")


class Static:
    def __init__(self, source: str, records: list[CapturedRecord]) -> None:
        self.source, self._records = source, records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


FIRST = [mail(MAIL_HANDLE, D1, f"Order confirmation {NUMBER} for PO100236", T0, None)]
CHANGED = [mail(MAIL_HANDLE, D1, f"Order confirmation {NUMBER} for PO100236", T0, T_MAIL2),
           mail(MAIL2_HANDLE, D2, f"Revised confirmation {NUMBER} for PO100236", T_MAIL2, None)]


def build(path: Path, mails: list[CapturedRecord], review_store: reviews.ReviewStore | None = None
          ) -> dict:
    steps = None if review_store is None else {"reviews": reviews.link_step(review_store, "plx")}
    return build_store(path, schema_loader.load_schema(),
                       {"portal_api": Static("portal_api", [po(9, "PO100236"), po(10, "PO100237")]),
                        "graph_mail": Static("graph_mail", mails)},
                       normalizer=identifiers.normalize, clock=lambda: NOW, post_project=steps)


def policy() -> ot.ReadPolicy:
    a, b = f"employee:{OID_A}", f"employee:{OID_B}"
    return ot.ReadPolicy(tenant="plx", audiences={"api:*": {a, b}, "mail:*": {a}},
                         markings={m: {a, b} for m in store_ddl.MARKINGS})


def item_gate(actor: Actor, handle: str) -> bool:
    return f"employee:{OID_A}" in actor.principals and handle.startswith(f"graph-mail://{BOX}/")


def ontology(store: Path, review_store: reviews.ReviewStore, clock: dt.datetime
             ) -> ot.OntologyTools:
    reader = ot.StoreReader.open(store)
    return ot.OntologyTools(reader, policy(), item_gate=item_gate, grammar=identifiers,
                            schema=ot.SchemaView.from_compiled(schema_loader.load_schema()),
                            contracts=CONTRACTS, clock=lambda: clock,
                            aliases=ReviewedAliases(review_store))


# The next investigation, in another process: the review store and the projection
# store are only reopened from disk.
FIND_ELSEWHERE = """
import json, sys, datetime as dt
from ontology import identifiers, reviews, store_ddl, schema as schema_loader
from ontology.resolve import ReviewedAliases
from operating import ontology_tools as ot
store, review_file, oid, now = sys.argv[1:5]
principal = "employee:" + oid
policy = ot.ReadPolicy(tenant="plx", audiences={"api:*": {principal}, "mail:*": {principal}},
                       markings={m: {principal} for m in store_ddl.MARKINGS})
tools = ot.OntologyTools(ot.StoreReader.open(store), policy, grammar=identifiers,
                         item_gate=lambda actor, handle: True,
                         schema=ot.SchemaView.from_compiled(schema_loader.load_schema()),
                         clock=lambda: dt.datetime.fromisoformat(now),
                         aliases=ReviewedAliases(reviews.ReviewStore(review_file)))
print(json.dumps(tools.call("plx.find", {"query": sys.argv[5]},
                            ot.Actor("plx", frozenset({principal})))))
"""


class ReviewToolTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.review_file = reviews.store_path(self.dir / "state")
        self.reviews = reviews.ReviewStore(self.review_file, clock=lambda: REVIEWED_AT)
        self.store = self.dir / "projection.sqlite"
        build(self.store, FIRST)
        self.session_a = employee_session(OID_A)
        self.session_b = employee_session(OID_B)

    def tools(self, store=None, clock=NOW, session=None, review_store=None,
              gate=identity_reviewer):
        review_store = review_store or self.reviews
        base = ontology(store or self.store, review_store, clock)
        self.addCleanup(base.store.conn.close)
        return base, rt.ReviewTools(base, review_store, session=session or self.session_a,
                                    gate=gate)

    def ok(self, name, result):
        self.assertEqual(validate(result, CONTRACTS["tools"][name]["output"], root=CONTRACTS),
                         [], json.dumps(result)[:1500])
        self.assertNotIn("error", result, json.dumps(result)[:1500])
        return result

    def refused(self, result, code):
        self.assertEqual(validate(result, CONTRACTS["tools"]["plx.review"]["output"],
                                  root=CONTRACTS), [])
        self.assertEqual(result.get("error", {}).get("code"), code, json.dumps(result)[:800])

    def confirm_alias(self, review, subject=PO9, session=None, digest=D1, handle=MAIL_HANDLE):
        session = session or self.session_a
        return review.call("plx.review", {
            "mode": "record", "decision": "alias", "subject": subject,
            "alias": {"identifier_kind": "vendor_confirmation", "value": NUMBER.lower()},
            "evidence": [{"l1_handle": handle, "digest": digest, "field_path": "row.subject"}]},
            session.actor)

    def find_elsewhere(self, store: Path, oid: str, now: dt.datetime, query: str) -> dict:
        done = subprocess.run([sys.executable, "-c", FIND_ELSEWHERE, str(store),
                               str(self.review_file), oid, now.isoformat(), query],
                              cwd=ROOT, capture_output=True, text=True, timeout=120,
                              env={k: v for k, v in os.environ.items()
                                   if k != "PLX_WORKBENCH_API_KEY"} | {"PYTHONPATH": str(ROOT)})
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return self.ok("plx.find", json.loads(done.stdout))

    # ------------------------------------------------------------------ alias

    def test_a_confirmed_supplier_number_survives_restart_and_changes_the_next_find(self):
        base, review = self.tools()
        before = self.ok("plx.find", base.call("plx.find", {"query": NUMBER},
                                               self.session_a.actor))
        self.assertNotIn(PO9, [c["object"]["object_id"] for c in before["candidates"]])

        recorded = self.ok("plx.review", self.confirm_alias(review))
        self.assertTrue(recorded["created"])
        self.assertEqual(recorded["projection"], "plx_find_now")
        self.assertEqual(recorded["business_system_writes"], 0)
        self.assertEqual(recorded["review"]["alias"], {"identifier_kind": "vendor_confirmation",
                                                       "normalized": NUMBER})
        self.assertEqual(recorded["review"]["evidence"], [{
            "object_id": EMAIL, "l1_handle": MAIL_HANDLE, "digest": D1,
            "field_path": "row.subject"}])
        self.assertEqual(recorded["review"]["state"], "effective")

        after = self.find_elsewhere(self.store, OID_A, NOW, NUMBER)
        self.assertEqual(after["status"], "found", json.dumps(after)[:1500])
        candidate = after["candidates"][0]
        self.assertEqual(candidate["object"]["object_id"], PO9)
        self.assertEqual((candidate["reason"], candidate["tier"]), ("alias_match", "reviewed"))
        self.assertEqual(candidate["matched"]["role"], "alias")
        self.assertEqual(candidate["matched"]["normalized"], NUMBER)
        # The alias cites where it is written: the mail's subject, never the PO record.
        origin = candidate["matched"]["raw"]["origin"]
        self.assertEqual((origin["citation"], origin["field_path"]), (MAIL_HANDLE, "row.subject"))

    def test_one_alias_on_two_records_is_ambiguous_never_a_winner(self):
        base, review = self.tools()
        self.ok("plx.review", self.confirm_alias(review, PO9))
        self.ok("plx.review", self.confirm_alias(review, PO10))
        answer = self.ok("plx.find", base.call("plx.find", {"query": NUMBER},
                                               self.session_a.actor))
        self.assertEqual(answer["status"], "ambiguous")
        self.assertEqual({c["object"]["object_id"] for c in answer["candidates"]}, {PO9, PO10})
        self.assertEqual({c["tier"] for c in answer["candidates"]}, {"reviewed"})

    def test_a_verified_exact_match_ranks_before_an_evidence_based_candidate(self):
        # ER-03: deterministic first, evidence-based otherwise. A person cites the mail to
        # confirm PO100236 as an alias of PO10, while FM stores PO100236 as PO9's number:
        # both stay candidates (never a winner), the stored match first although the two
        # tie on role and reason and PO10's id sorts first.
        base, review = self.tools()
        self.ok("plx.review", review.call("plx.review", {
            "mode": "record", "decision": "alias", "subject": PO10,
            "alias": {"identifier_kind": "petra_po", "value": "PO100236"},
            "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1, "field_path": "row.subject"}]},
            self.session_a.actor))
        answer = self.ok("plx.find", base.call("plx.find", {"query": "PO100236"},
                                               self.session_a.actor))
        self.assertEqual(answer["status"], "ambiguous")
        self.assertEqual([(c["object"]["object_id"], c["tier"], c["reason"], c["rank"])
                          for c in answer["candidates"]],
                         [(PO9, "verified", "alias_match", 1),
                          (PO10, "reviewed", "alias_match", 2)])

    def test_an_alias_must_be_read_in_the_cited_field(self):
        _, review = self.tools()
        refused = review.call("plx.review", {
            "mode": "record", "decision": "alias", "subject": PO9,
            "alias": {"identifier_kind": "vendor_confirmation", "value": "PI-SWC099999"},
            "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1, "field_path": "row.subject"}]},
            self.session_a.actor)
        self.refused(refused, "alias_not_in_cited_field")
        self.assertEqual(self.reviews.history("plx", PO9), [])

    def test_a_rejected_alias_stops_answering_and_a_rejection_alone_never_answers(self):
        # ROADMAP A10 (decision 2026-10-19-reference-alias-leads): reject_alias shares the
        # alias's key, so the later decision is in force; a rejection joins nothing.
        base, review = self.tools()
        self.ok("plx.review", self.confirm_alias(review))
        rejected = self.ok("plx.review", review.call("plx.review", {
            "mode": "record", "decision": "reject_alias", "subject": PO9,
            "alias": {"identifier_kind": "vendor_confirmation", "value": NUMBER},
            "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1, "field_path": "row.subject"}]},
            self.session_a.actor))
        self.assertEqual((rejected["projection"], rejected["review"]["decision"]),
                         ("plx_find_now", "reject_alias"))
        now = self.ok("plx.find", base.call("plx.find", {"query": NUMBER},
                                            self.session_a.actor))
        self.assertNotIn(PO9, [c["object"]["object_id"] for c in now["candidates"]])
        status = self.ok("plx.review", review.call("plx.review", {"mode": "status",
                                                                  "subject": PO9},
                                                   self.session_a.actor))
        self.assertEqual([r["decision"] for r in status["reviews"]], ["alias", "reject_alias"])
        # Read in the cited field like an alias; another number is refused.
        self.refused(review.call("plx.review", {
            "mode": "record", "decision": "reject_alias", "subject": PO10,
            "alias": {"identifier_kind": "vendor_confirmation", "value": "PI-SWC099999"},
            "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1, "field_path": "row.subject"}]},
            self.session_a.actor), "alias_not_in_cited_field")
        self.ok("plx.review", review.call("plx.review", {
            "mode": "record", "decision": "reject_alias", "subject": PO10,
            "alias": {"identifier_kind": "vendor_confirmation", "value": NUMBER},
            "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1, "field_path": "row.subject"}]},
            self.session_a.actor))
        self.assertNotIn(PO10, [c["object"]["object_id"] for c in self.ok("plx.find", base.call(
            "plx.find", {"query": NUMBER}, self.session_a.actor))["candidates"]])

    # ------------------------------------------------------------------ withdrawal

    def test_a_changed_evidence_digest_withdraws_the_review(self):
        _, review = self.tools()
        recorded = self.ok("plx.review", self.confirm_alias(review))["review"]
        changed = self.dir / "projection-2.sqlite"
        build(changed, CHANGED)
        base, review = self.tools(changed, LATER)
        now = self.ok("plx.find", base.call("plx.find", {"query": NUMBER}, self.session_a.actor))
        self.assertNotIn(PO9, [c["object"]["object_id"] for c in now["candidates"]])
        # As known before the mail changed, the review still held.
        then = self.ok("plx.find", base.call("plx.find", {
            "query": NUMBER, "as_known_at": "2026-10-02T13:00:00Z"}, self.session_a.actor))
        self.assertEqual([c["object"]["object_id"] for c in then["candidates"]], [PO9])
        status = self.ok("plx.review", review.call("plx.review", {"mode": "status",
                                                                  "subject": PO9},
                                                   self.session_a.actor))
        self.assertEqual([(r["review_id"], r["state"], r.get("state_reason"))
                          for r in status["reviews"]],
                         [(recorded["review_id"], "withdrawn", "evidence_changed")])
        # The person reviewed the old content: a review citing it is refused.
        self.refused(self.confirm_alias(review), "evidence_changed")
        # Reviewing the new version applies again.
        renewed = self.ok("plx.review", self.confirm_alias(review, digest=D2,
                                                           handle=MAIL2_HANDLE))
        self.assertEqual(renewed["review"]["state"], "effective")
        found = self.ok("plx.find", base.call("plx.find", {"query": NUMBER},
                                              self.session_a.actor))
        self.assertEqual([c["object"]["object_id"] for c in found["candidates"]], [PO9])

    # ------------------------------------------------------------------ permissions

    def test_a_reader_without_the_evidence_grant_sees_the_review_withheld(self):
        base, review = self.tools()
        self.ok("plx.review", self.confirm_alias(review))
        reader = self.session_b.actor
        found = self.ok("plx.find", base.call("plx.find", {"query": NUMBER}, reader))
        self.assertEqual(found["status"], "not_found")
        self.assertNotIn("absent_in_source", {gap["state"] for gap in found["gaps"]})
        _, review_b = self.tools(session=self.session_b)
        status = self.ok("plx.review", review_b.call("plx.review", {"mode": "status",
                                                                    "subject": PO9}, reader))
        self.assertEqual(status["reviews"], [])
        self.assertEqual(status["withheld"], {"reviews": 1, "reason": "evidence_not_visible"})
        # Nor can that reader review evidence it may not read.
        self.refused(self.confirm_alias(review_b, session=self.session_b), "evidence_not_found")

    def test_an_identity_decision_needs_the_identity_review_entitlement(self):
        # The employee's entitlements, resolved by the real resolver now, lack
        # review:identity: no link decision, alias or retraction of one is written, so
        # neither the next plx.find nor the next build sees one; with the policy granting
        # it the same decisions are recorded. The gate is write-time: once the grant is
        # gone, a decision recorded with it stays in force.
        base, plain = self.tools(gate=resolved_gate(self.dir / "plain", ("staff:orders",)))
        _, entitled = self.tools(gate=resolved_gate(
            self.dir / "entitled", ("staff:orders", rt.IDENTITY_REVIEW)))
        actor = self.session_a.actor
        evidence = [{"l1_handle": MAIL_HANDLE, "digest": D1, "field_path": "row.subject"}]
        link = {"mode": "record", "subject": EMAIL, "target": PO9, "link_type": LINK,
                "evidence": evidence}
        for arguments in (dict(link, decision="accept_link"), dict(link, decision="reject_link"),
                          # refused before anything is read: a record it may not read
                          # gives the same answer
                          dict(link, decision="accept_link", target=PO9 + "x")):
            self.refused(plain.call("plx.review", arguments, actor), "identity_review_required")
        self.refused(self.confirm_alias(plain), "identity_review_required")
        self.assertEqual((self.reviews.history("plx", EMAIL), self.reviews.history("plx", PO9)),
                         ([], []))
        found = self.ok("plx.find", base.call("plx.find", {"query": NUMBER}, actor))
        self.assertNotIn(PO9, [c["object"]["object_id"] for c in found["candidates"]])
        report = build(self.dir / "refused.sqlite", FIRST, self.reviews)
        self.assertEqual(report["post_project"]["reviews"]["counts"]["links"], 0)

        accepted = self.ok("plx.review", entitled.call(
            "plx.review", dict(link, decision="accept_link"), actor))["review"]
        self.ok("plx.review", self.confirm_alias(entitled))
        self.refused(plain.call("plx.review", {"mode": "record", "decision": "retract",
                                               "retracts": accepted["review_id"]}, actor),
                     "identity_review_required")
        self.assertEqual([(r["decision"], r["state"]) for r in self.ok("plx.review", plain.call(
            "plx.review", {"mode": "status", "subject": EMAIL}, actor))["reviews"]],
                         [("accept_link", "effective")])
        retracted = self.ok("plx.review", entitled.call(
            "plx.review", {"mode": "record", "decision": "retract",
                           "retracts": accepted["review_id"]}, actor))
        self.assertEqual(retracted["review"]["retracts"], accepted["review_id"])

    def test_the_owner_and_admin_roles_hold_identity_review(self):
        # Owner decision ab1b0f5d9fa8 item 2 through the shipped policy entry: an employee
        # whose Portal role is ADMIN or SUPER_ADMIN (the owner tier) records link decisions
        # and aliases; STAFF, MANAGER (even at L5) and an ADMIN whose access level does not
        # back the role are refused and nothing is written. It names roles, never a person.
        actor = self.session_a.actor
        link = {"mode": "record", "subject": EMAIL, "target": PO9, "link_type": LINK,
                "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1,
                              "field_path": "row.subject"}]}
        decisions = [dict(link, decision="accept_link"), dict(link, decision="reject_link")]
        for role, level in (("STAFF", None), ("MANAGER", None), ("MANAGER", "L5"),
                            ("ADMIN", "L3")):
            with self.subTest(role=role, level=level):
                _, review = self.tools(gate=resolved_gate(
                    self.dir / f"{role}-{level}", ("staff:orders",), role, level))
                for arguments in decisions:
                    self.refused(review.call("plx.review", arguments, actor),
                                 "identity_review_required")
                self.refused(self.confirm_alias(review), "identity_review_required")
                self.assertEqual((self.reviews.history("plx", EMAIL),
                                  self.reviews.history("plx", PO9)), ([], []))
        for role in ("ADMIN", "SUPER_ADMIN"):
            with self.subTest(role=role):
                store = reviews.ReviewStore(reviews.store_path(self.dir / role),
                                            clock=lambda: REVIEWED_AT)
                _, review = self.tools(review_store=store, gate=resolved_gate(
                    self.dir / role, ("staff:orders",), role))
                recorded = [self.ok("plx.review", review.call("plx.review", arguments, actor))
                            for arguments in decisions]
                recorded.append(self.ok("plx.review", self.confirm_alias(review)))
                self.assertEqual([(r["created"], r["review"]["decision"],
                                   r["review"]["author"]["principal"]) for r in recorded],
                                 [(True, d, f"employee:{OID_A}")
                                  for d in ("accept_link", "reject_link", "alias")])

    # ------------------------------------------------------------------ append-only

    def test_corrections_are_new_records_and_the_store_refuses_edits(self):
        _, review = self.tools()
        link = {"mode": "record", "subject": EMAIL, "target": PO9, "link_type": LINK,
                "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1,
                              "field_path": "row.subject"}]}
        actor = self.session_a.actor
        accepted = self.ok("plx.review", review.call(
            "plx.review", dict(link, decision="accept_link"), actor))["review"]
        rejected = self.ok("plx.review", review.call(
            "plx.review", dict(link, decision="reject_link"), actor))
        again = self.ok("plx.review", review.call(
            "plx.review", dict(link, decision="reject_link"), actor))
        self.assertFalse(again["created"])       # the same decision again appends nothing
        self.assertEqual(again["review"]["review_id"], rejected["review"]["review_id"])
        status = self.ok("plx.review", review.call("plx.review", {"mode": "status",
                                                                  "subject": PO9}, actor))
        self.assertEqual([(r["decision"], r["state"]) for r in status["reviews"]],
                         [("accept_link", "superseded"), ("reject_link", "effective")])
        self.ok("plx.review", review.call("plx.review", {
            "mode": "record", "decision": "retract",
            "retracts": rejected["review"]["review_id"]}, actor))
        status = self.ok("plx.review", review.call("plx.review", {"mode": "status",
                                                                  "subject": PO9}, actor))
        self.assertEqual([(r["decision"], r["state"]) for r in status["reviews"]],
                         [("accept_link", "effective"), ("reject_link", "retracted"),
                          ("retract", "effective")])
        self.assertEqual(status["reviews"][0]["review_id"], accepted["review_id"])
        # No edit path: neither the API nor the file accepts an update or a delete.
        self.assertFalse({n for n in dir(reviews.ReviewStore) if any(
            verb in n for verb in ("update", "delete", "remove", "edit", "replace"))})
        with sqlite3.connect(self.review_file) as db:
            for statement in ("UPDATE reviews SET decision = 'accept_link'",
                              "DELETE FROM reviews"):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "reviews_append_only"):
                    db.execute(statement)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 3)

    # ------------------------------------------------------------------ attribution

    def test_the_author_is_the_session_employee_never_an_argument(self):
        _, review = self.tools()
        recorded = self.ok("plx.review", self.confirm_alias(review))["review"]
        self.assertEqual(recorded["author"]["principal"], f"employee:{OID_A}")
        self.assertEqual(recorded["author"]["session"], self.session_a.session_ref)
        forged = review.call("plx.review", {
            "mode": "record", "decision": "alias", "subject": PO9, "author": f"employee:{OID_B}",
            "alias": {"identifier_kind": "vendor_confirmation", "value": NUMBER},
            "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1, "field_path": "row.subject"}]},
            self.session_a.actor)
        self.refused(forged, "invalid_input")
        # The caller must be the bound session; an agent session never confirms.
        self.refused(self.confirm_alias(review, session=self.session_b), "session_actor_mismatch")
        agent = employee_session(OID_A, CHANNEL_MCP)
        _, review_mcp = self.tools(session=agent)
        self.refused(self.confirm_alias(review_mcp, session=agent), "review_needs_a_person")
        self.assertEqual(len(self.reviews.history("plx", PO9)), 1)

    def test_the_host_operation_attributes_the_host_session(self):
        base, _ = self.tools()

        class Host:   # the CaseHostTools surface register uses
            def __init__(self, tools, store, actor):
                self.tools, self.reviews, self.actor, self.served = tools, store, actor, {}
                self.runtime = type("Runtime", (), {"employee_gate": staticmethod(
                    identity_reviewer)})()

            def policy_now(self, actor):
                return policy()

            def register_tool(self, name, handler):
                self.served[name] = handler

        host = Host(base, self.reviews, self.session_a.actor)
        rt.register(host, self.session_a)
        handler = host.served["plx.review"]
        recorded = self.ok("plx.review", handler({
            "mode": "record", "decision": "accept_link", "subject": EMAIL, "target": PO9,
            "link_type": LINK, "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1}]},
            host.actor))
        self.assertEqual((recorded["review"]["author"]["principal"], recorded["projection"]),
                         (f"employee:{OID_A}", "next_projection_build"))
        # What a document is is recorded by plx.document_fields alone.
        refused = handler({"mode": "record", "decision": "document_kind", "subject": EMAIL,
                           "document_kind": "QUOTE",
                           "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1}]}, host.actor)
        self.refused(refused, "document_kind_via_document_fields")
        self.assertIn("plx.document_fields", refused["error"]["hint"])

    # ------------------------------------------------------------------ earlier kinds

    def test_a_document_type_reviewed_on_a_mail_before_stays_listed_and_retractable(self):
        # Recorded by plx.review before kinds moved: a mail has no SharePoint bytes, so it
        # is imported unbound into the kind store, listed from there and withdrawn there.
        legacy = "rev-" + "e" * 32
        author = {"principal": f"employee:{OID_A}", "channel": "console", "session": "5" * 64}
        with closing(sqlite3.connect(self.review_file)) as db, db:
            db.execute("INSERT INTO reviews (review_id, tenant, review_key, decision, subject, "
                       "document_kind, evidence, author, recorded_at) VALUES "
                       "(?, 'plx', ?, 'document_kind', ?, 'QUOTE', ?, ?, ?)",
                       (legacy, reviews.review_key("document_kind", EMAIL), EMAIL,
                        json.dumps([{"object_id": EMAIL, "l1_handle": MAIL_HANDLE,
                                     "digest": D1}]), json.dumps(author), T0))
        _, review = self.tools()
        actor = self.session_a.actor

        def status():
            return self.ok("plx.review", review.call("plx.review", {"mode": "status",
                                                                    "subject": EMAIL}, actor))

        self.assertEqual([(r["review_id"], r["decision"], r.get("document_kind"), r["state"])
                          for r in status()["reviews"]],
                         [(legacy, "document_kind", "QUOTE", "effective")])
        # A reader who may not read the mail can neither list nor retract it.
        _, review_b = self.tools(session=self.session_b)
        self.refused(review_b.call("plx.review", {"mode": "status", "subject": EMAIL},
                                   self.session_b.actor), "object_not_found")
        self.refused(review_b.call("plx.review", {"mode": "record", "decision": "retract",
                                                  "retracts": legacy}, self.session_b.actor),
                     "review_not_found")
        retracted = self.ok("plx.review", review.call(
            "plx.review", {"mode": "record", "decision": "retract", "retracts": legacy}, actor))
        self.assertEqual((retracted["created"], retracted["review"]["retracts"],
                          retracted["projection"]), (True, legacy, "next_projection_build"))
        self.assertEqual([(r["decision"], r["state"]) for r in status()["reviews"]],
                         [("document_kind", "retracted"), ("retract", "effective")])
        self.refused(review.call("plx.review", {"mode": "record", "decision": "retract",
                                                "retracts": legacy}, actor), "already_retracted")
        # The review store keeps its history untouched; the kind store holds both records.
        self.assertEqual([r["review_id"] for r in self.reviews.kind_history("plx")], [legacy])
        kinds = kind_confirmation.KindConfirmations(
            kind_confirmation.store_path(self.review_file.parent))
        self.assertEqual([(r["id"], r.get("retracts")) for r in kinds.history("plx", EMAIL)],
                         [(legacy, None), (retracted["review"]["review_id"], legacy)])

    # ------------------------------------------------------------------ removal and return

    def test_a_reviewed_alias_whose_evidence_is_removed_and_returns_holds_around_it(self):
        _, review = self.tools()
        self.ok("plx.review", self.confirm_alias(review))
        with closing(sqlite3.connect(self.store)) as db, db:   # the source soft-deletes the
            db.execute("INSERT INTO object_removals (object_id, basis, since, until) VALUES "
                       "(?, 'source_soft_delete', ?, ?)",       # mail, then holds it again
                       (EMAIL, "2026-10-03T00:00:00.000000Z", "2026-10-03T12:00:00.000000Z"))
        base, _ = self.tools(clock=LATER)
        found = [PO9 in [c["object"]["object_id"] for c in self.ok("plx.find", base.call(
            "plx.find", {"query": NUMBER, "as_known_at": at}, self.session_a.actor)
        ).get("candidates", ())] for at in ("2026-10-02T18:00:00Z", "2026-10-03T06:00:00Z",
                                            "2026-10-04T00:00:00Z")]
        self.assertEqual(found, [True, False, True])   # held, withdrawn inside, held again
        # The build resolves mentions through the same stretches.
        with closing(sqlite3.connect(self.store)) as conn:
            built = reviews.BuildAliases(self.reviews, "plx").rows(
                conn, "vendor_confirmation", identifiers.normalize("vendor_confirmation", NUMBER))
        self.assertEqual([(r["object_id"], r["recorded_at"], r["superseded_at"]) for r in built],
                         [(PO9, "2026-10-02T12:00:00.000000Z", "2026-10-03T00:00:00.000000Z"),
                          (PO9, "2026-10-03T12:00:00.000000Z", None)])

    # ------------------------------------------------------------------ projection

    def test_link_decisions_enter_the_next_projection_as_reviewed_links(self):
        _, review = self.tools()
        link = {"mode": "record", "subject": EMAIL, "target": PO9, "link_type": LINK,
                "evidence": [{"l1_handle": MAIL_HANDLE, "digest": D1,
                              "field_path": "row.subject"}]}
        self.ok("plx.review", review.call("plx.review", dict(link, decision="accept_link"),
                                          self.session_a.actor))

        def tiers(mails, reader, clock=NOW):
            path = self.dir / f"projection-{len(list(self.dir.glob('*.sqlite')))}.sqlite"
            report = build(path, mails, self.reviews)
            base, _ = self.tools(path, clock)
            answer = self.ok("plx.object", base.call("plx.object", {"object_id": PO9}, reader))
            counts = {c["link_type"]: c.get("tiers") for c in answer["linked_counts"]}
            return counts.get(LINK), report

        accepted, report = tiers(FIRST, self.session_a.actor)
        self.assertEqual(accepted, {"reviewed": 1})
        self.assertEqual(report["post_project"]["reviews"]["counts"]["links"], 1)
        self.assertIsNone(tiers(FIRST, self.session_b.actor)[0])   # the mail end is withheld
        self.ok("plx.review", review.call("plx.review", dict(link, decision="reject_link"),
                                          self.session_a.actor))
        self.assertEqual(tiers(FIRST, self.session_a.actor)[0], {"contradicted": 1})
        # The mail changed after the decision: the link no longer holds now.
        self.assertIsNone(tiers(CHANGED, self.session_a.actor, LATER)[0])


if __name__ == "__main__":
    unittest.main()
