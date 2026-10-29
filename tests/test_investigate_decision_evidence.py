"""plx.investigate family ``decision_evidence`` (F11) at the tool boundary.

A Mission Control capture (``integrations/mc_sources.py`` over the injected fake MC of
``integrations/tests/test_mc_sources.py``), a static mail record and synthetic Portal purchase
orders are projected with stored mention links and a person's review decisions on them
(``ontology/reviews.py`` ``link_step``); a retraction and a superseding rejection are recorded
after the build, so their links stay projected until the next build. The generic tools,
``plx.investigate`` and a per-reader citation resolver over both sources run their real code
(MC citations through the product host's own resolver, ``CaseRuntime`` over the capture in its
state directory), and every output is validated against the tool contract. Evidence label: isolated-fixture (synthetic records, injected MC transport,
never live PLX data).
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest

from integrations.capture_store import CaptureStore
from integrations.live_source_guard import LiveSourceAudience
from integrations.mc_sources import CaptureSpec, McSourcesCapture, store_path
from integrations.source_mirror import AudienceMap
from integrations.tests import test_mc_sources as tmc
from ontology import identifiers, mentions, reviews, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import CapturedRecord, build_store, capture_source
from ontology.types import Actor
from operating import decision_evidence as de
from operating.case_runtime import CaseRuntime, LiveReaders
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt

CONTRACTS = ot.load_contracts()
OUTPUT = CONTRACTS["tools"]["plx.investigate"]["output"]
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)
REVIEWED_AT = dt.datetime(2026, 10, 3, 9, 30, tzinfo=dt.timezone.utc)
T0 = "2026-09-25T10:00:00.000000Z"
FULL = Actor("plx", frozenset({"staff:ops", "staff:orders"}), "restricted")
NO_MAIL = Actor("plx", frozenset({"staff:ops"}), "restricted")   # MC, never the mailbox
NO_PO = Actor("plx", frozenset({"staff:quotes"}), "restricted")   # quotes and mail, no orders
ONE_MAIL = Actor("plx", frozenset({"staff:msg2"}), "restricted")   # orders and MSG-2 only
LATER = dt.datetime(2026, 10, 5, 10, 0, tzinfo=dt.timezone.utc)   # after the build
REVIEWER = "employee:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LINK = "Email.mentions_purchase_order"
BOX = "orders@plx.test"
MAIL_HANDLE = f"graph-mail://{BOX}/MSG-1@CK-1"
MAIL_ROW = {"mailbox": BOX, "id": "MSG-1", "subject": "Go-ahead",
            "from": "supplier@outside.example", "sent_at": "2026-09-20T09:00:00Z",
            "received_at": "2026-09-20T09:00:05Z", "body_preview": "Go ahead with PO100234."}
EMAIL = 'Email:graph_mail.messages:{"id":"MSG-1","mailbox":"orders@plx.test"}'
PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"p1"}'
QUIET = 'PurchaseOrder:portal.app_purchase_order:{"id":"p2"}'
MAILED = 'PurchaseOrder:portal.app_purchase_order:{"id":"p3"}'   # named by a mail only
MAIL_2_HANDLE = f"graph-mail://{BOX}/MSG-2@CK-2"
MAIL_2_ROW = {**MAIL_ROW, "id": "MSG-2", "subject": "Approved, ship PO100377",
              "sent_at": "2026-09-21T09:00:00Z", "received_at": "2026-09-21T09:00:05Z",
              "body_preview": "Approved: ship PO100377 now."}
EMAIL_2 = 'Email:graph_mail.messages:{"id":"MSG-2","mailbox":"orders@plx.test"}'
# A quote: a root type the plan never listed, named by an MC task, its gate and a mail.
QUOTE = 'Quote:portal.app_quote:{"id":"q1"}'
MAIL_3_HANDLE = f"graph-mail://{BOX}/MSG-3@CK-3"
MAIL_3_ROW = {**MAIL_ROW, "id": "MSG-3", "subject": "Quote QUO-2026-0042",
              "sent_at": "2026-10-04T09:00:00Z", "received_at": "2026-10-04T09:00:05Z",
              "body_preview": "Please find quote QUO-2026-0042 attached."}
EMAIL_3 = 'Email:graph_mail.messages:{"id":"MSG-3","mailbox":"orders@plx.test"}'
REPLACED = 'PurchaseOrder:portal.app_purchase_order:{"id":"p4"}'   # review later rejected


def record(source: str, relation: str, native: str, row: dict, handle: str, digest: str,
           audience: str) -> CapturedRecord:
    return CapturedRecord(source, relation, native, row, handle, "row", digest, T0, None, T0,
                          audience)


class Static:
    def __init__(self, source: str, records: list[CapturedRecord]) -> None:
        self.source, self._records = source, records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


def mail_reader(actor: Actor, handle: str) -> bool:
    if "staff:msg2" in actor.principals:
        return handle == MAIL_2_HANDLE
    return bool({"staff:orders", "staff:quotes"} & actor.principals) and handle.startswith(
        f"graph-mail://{BOX}/")


def text(value):
    return value.get("untrusted_text") if isinstance(value, dict) else value


class DecisionEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        # MC: gate apg_one on TASK-2 (its reason names PO100234) was approved 2026-10-01
        # 12:00Z; TASK-4 names PO100234 and is completed 2026-10-02 08:00Z; TASK-1 names it
        # with progress events only (not decisions).
        fake = tmc.FakeMc()
        tmc.add_hr_project(fake)
        fake.events["TASK-4"].append(fake.event("TASK-4", "2", "task.completed",
                                                "2026-10-02T08:00:00Z"))
        # A cancel is a decision too (PLX_MC@2f653cf src/lib/sync/cancel.ts): TASK-1, which
        # names PO100234, was cancelled 2026-10-03 08:00Z.
        fake.events["TASK-1"].append(fake.event("TASK-1", "30", "task.cancelled",
                                                "2026-10-03T08:00:00Z"))
        fake.tasks["TASK-5"] = {
            "id": "TASK-5", "title": "Accept QUO-2026-0042", "stage": "done",
            "bucket": "BKT-OPS", "accountableOwner": tmc.OPERATOR, "approvalGates": [{
                "id": "apg_quote", "status": "approved", "reason": "Accept QUO-2026-0042",
                "requestedBy": tmc.OPERATOR, "requestedAt": "2026-10-04T10:00:00Z",
                "decidedBy": "vince@petrasoap.com", "decidedAt": "2026-10-04T11:00:00Z",
                "note": "Price matches the supplier's quote"}]}
        fake.events["TASK-5"] = [fake.event("TASK-5", "1", "task.completed",
                                            "2026-10-04T12:00:00Z")]
        mc = store_path(tmp)
        mc.parent.mkdir(parents=True)
        with CaptureStore(mc, tmc.audience(), clock=tmc.Clock()) as store:
            McSourcesCapture(store, tmc.mc_client(fake)).capture(CaptureSpec("ops", ()))
        portal = Static("portal_api", [
            record("portal_api", "public.app_purchase_order", json.dumps({"id": key}),
                   {"id": key, "po_number": number},
                   f"api://public.app_purchase_order/{key}@g-1", "0" * 64,
                   "api:public.app_purchase_order")
            for key, number in (("p1", "PO100234"), ("p2", "PO100299"), ("p3", "PO100377"),
                                ("p4", "PO100400"))]
            + [record("portal_api", "public.app_quote", json.dumps({"id": "q1"}),
                      {"id": "q1", "quote_number": "QUO-2026-0042"},
                      "api://public.app_quote/q1@g-1", "3" * 64, "api:public.app_quote")])
        mail = Static("graph_mail", [
            record("graph_mail", "messages", json.dumps([row["id"], BOX]), row, handle,
                   digest, f"mail:{BOX}")
            for row, handle, digest in ((MAIL_ROW, MAIL_HANDLE, "1" * 64),
                                        (MAIL_2_ROW, MAIL_2_HANDLE, "2" * 64),
                                        (MAIL_3_ROW, MAIL_3_HANDLE, "4" * 64))])
        # A person confirmed that the mail names the order; the build projects it.
        when = [REVIEWED_AT]
        cls.reviews = reviews.ReviewStore(tmp / "reviews.sqlite", clock=lambda: when[0])
        author = {"principal": REVIEWER, "channel": "console", "session": "0" * 64}
        mail_2 = {"object_id": EMAIL_2, "l1_handle": MAIL_2_HANDLE, "digest": "2" * 64,
                  "field_path": "row.body_preview"}
        cls.review, _ = cls.reviews.record(
            tenant="plx", author=author,
            decision="accept_link", subject=EMAIL, target=PO, link_type=LINK,
            evidence=[{"object_id": EMAIL, "l1_handle": MAIL_HANDLE, "digest": "1" * 64,
                       "field_path": "row.body_preview"}])
        cls.retracted, _ = cls.reviews.record(
            tenant="plx", author=author,
            decision="accept_link", subject=EMAIL_2, target=MAILED, link_type=LINK,
            evidence=[mail_2])
        # The person compared the quote with order p2: a reader without orders may see the
        # link but not this review.
        cls.quote_review, _ = cls.reviews.record(
            tenant="plx", author=author,
            decision="accept_link", subject=EMAIL_3, target=QUOTE,
            link_type="Email.mentions_quote",
            evidence=[{"object_id": EMAIL_3, "l1_handle": MAIL_3_HANDLE, "digest": "4" * 64,
                       "field_path": "row.body_preview"},
                      {"object_id": QUIET, "l1_handle": "api://public.app_purchase_order/p2@g-1",
                       "digest": "0" * 64}])
        cls.superseded, _ = cls.reviews.record(
            tenant="plx", author=author,
            decision="accept_link", subject=EMAIL_2, target=REPLACED, link_type=LINK,
            evidence=[mail_2])
        cls.compiled = schema_loader.load_schema()
        cls.path = tmp / "ontology.sqlite"
        with capture_source("mission_control", mc) as mc_source:
            build_store(cls.path, cls.compiled, {"portal_api": portal, "graph_mail": mail,
                                                 "mission_control": mc_source},
                        normalizer=identifiers.normalize, clock=lambda: NOW, post_project={
                            "mentions": mentions.store_mention_links,
                            "reviews": reviews.link_step(cls.reviews, "plx")})
        # After the build: one review retracted, one overruled by a rejection that also
        # cites MSG-3 (a mail ONE_MAIL may not read).
        when[0] = LATER
        cls.reviews.record(tenant="plx", author=author, decision="retract", subject=EMAIL_2,
                           evidence=[], retracts=cls.retracted["review_id"])
        cls.rejection, _ = cls.reviews.record(
            tenant="plx", author=author,
            decision="reject_link", subject=EMAIL_2, target=REPLACED, link_type=LINK,
            evidence=[mail_2, {"object_id": EMAIL_3, "l1_handle": MAIL_3_HANDLE,
                               "digest": "4" * 64, "field_path": "row.body_preview"}])
        # The host resolver reads MC citations from the capture in its state directory under
        # the live audience: MC for staff:ops only (FULL and NO_MAIL).
        cls.runtime = CaseRuntime(tmp, client=None, audience=AudienceMap({}, tenant="plx"),
                                  tenant="plx", live=LiveReaders(LiveSourceAudience(
                                      {"mc:ops:*": ["staff:ops"]}, tenant="plx")))
        cls.po, cls.quiet, cls.email = PO, QUIET, EMAIL

    @classmethod
    def tearDownClass(cls):
        cls.runtime.close()
        cls.tmp.cleanup()

    def rows(self, handle, actor):
        if handle.startswith("mc://"):
            return oi.citation_rows(self.runtime)(handle, actor)
        rows = {MAIL_HANDLE: MAIL_ROW, MAIL_2_HANDLE: MAIL_2_ROW, MAIL_3_HANDLE: MAIL_3_ROW}
        if handle in rows and mail_reader(actor, handle):
            return {"relation": "messages", "l1_handle": handle, "row": dict(rows[handle])}
        return None

    def table(self, schema: ot.SchemaView | None = None, reviews: bool = True) -> dict:
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:ops", "staff:orders", "staff:quotes", "staff:msg2"},
            "api:public.app_purchase_order": {"staff:ops", "staff:orders", "staff:msg2"},
            "mc:ops/*": {"staff:ops"}, "mail:*": {"staff:orders", "staff:quotes", "staff:msg2"}},
            markings={m: {"staff:ops", "staff:orders", "staff:quotes", "staff:msg2"}
                      for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        base = ot.OntologyTools(reader, policy, item_gate=mail_reader,
                                schema=schema or ot.SchemaView.from_compiled(self.compiled),
                                grammar=None, contracts=CONTRACTS, clock=lambda: NOW,
                                reviews=self.reviews if reviews else None)
        table: dict = {}
        ot.register(table, base)
        traverse = tt.TraverseTools(base)
        tt.register(table, traverse)
        oi.register(table, oi.InvestigateTools(table, rows=self.rows, scope=oi.read_scope(base),
                                               clock=lambda: NOW,
                                               reviews=traverse.review if reviews else None))
        return table

    def investigate(self, root: str, actor: Actor, reviews: bool = True) -> dict:
        table = self.table(reviews=reviews)
        out = table["plx.investigate"]["handler"]({"family": "F11", "root": root}, actor)
        self.assertEqual(validate(out, OUTPUT, root=CONTRACTS), [], json.dumps(out)[:1500])
        self.assertNotIn("error", out, json.dumps(out)[:1500])
        return out

    @staticmethod
    def decisions(out: dict) -> list[dict]:
        return [e for e in out["answer"] if e["name"].startswith("decisions_")
                and not e["name"].startswith("decisions_context_")]

    @staticmethod
    def context(out: dict) -> list[dict]:
        return [e for e in out["answer"] if e["name"].startswith("decisions_context_")]

    def test_a_po_gets_its_approval_and_task_decisions_in_time_order_and_mail_as_context(self):
        out = self.investigate(self.po, FULL)
        named = {e["name"]: e for e in out["answer"]}
        decisions = self.decisions(out)
        self.assertEqual([d["value"] for d in decisions],
                         ["2026-10-01T12:00:00.000000Z", "2026-10-02T08:00:00.000000Z",
                          "2026-10-03T08:00:00.000000Z", "2026-10-03T09:30:00.000000Z"])
        # The mail naming the order is cited context: its sender decided nothing and its
        # subject is no outcome, so it is not counted.
        self.assertEqual(named["decisions"]["value"], 4)
        approval, completed, cancelled, reviewed = decisions
        [mail] = self.context(out)
        self.assertEqual(mail["value"], "2026-09-20T09:00:00.000000Z")
        self.assertNotIn(mail["name"], named["decisions"]["inputs"])

        def cited(decision, prefix):  # the cited, re-checked entry of one role
            entry = next(named[n] for n in decision["inputs"] if n.startswith(prefix))
            self.assertEqual(entry["check"], "passed", entry)
            return entry
        self.assertEqual(text(cited(approval, "approval_decided_by")["value"]),
                         "vince@petrasoap.com")
        at = cited(approval, "approval_decided_at")["citation"]
        self.assertTrue(at["l1_handle"].startswith("mc://ops/approvals/apg_one@"))
        self.assertEqual(at["field_path"], "row.decidedAt")
        self.assertEqual(text(cited(approval, "approval_status")["value"]), "approved")
        # The gate's reason is why the requester asked (PLX_MC approvals.ts records the
        # decision as status/decidedBy/decidedAt/note): never shown as the basis.
        self.assertFalse([n for n in approval["inputs"] if n.startswith("approval_reason")])
        self.assertEqual(text(cited(mail, "mail_sender")["value"]), MAIL_ROW["from"])
        self.assertTrue(cited(mail, "mail_sent_at")["citation"]["l1_handle"].startswith(
            "graph-mail://"))
        self.assertEqual(text(cited(completed, "event_kind")["value"]), "task.completed")
        self.assertEqual(text(cited(cancelled, "event_kind")["value"]), "task.cancelled")
        self.assertEqual(text(cited(completed, "event_actor")["value"]), tmc.OPERATOR)
        # A mention is a lead and those decisions say so (the reviewed mail link too: a link to
        # a gold-native Portal row is capped at lead).
        self.assertTrue(all(d.get("lead_basis") for d in (mail, approval, completed)))
        # The person's review is a decision too: who and when, re-read from the review
        # store for this reader, on the two records it links.
        self.assertEqual(reviewed["basis"], [EMAIL, PO])
        self.assertIn(f"person_review {self.review['review_id']}", reviewed["steps"][0])
        self.assertEqual(reviewed["steps"][1:3], [f"actor: reviewer {REVIEWER}",
                                                  "outcome: accept_link"])
        self.assertFalse([g for g in out["gaps"] if g["reason"].startswith("review_")])
        # The gate states no note and task events carry no basis: typed gaps, never a reason
        # borrowed from the request, the mail or the gate. Steps name entries only.
        basis = [g for g in out["gaps"] if g["reason"] == "decision_basis_not_recorded"]
        self.assertEqual(sorted(g["subject"].split(" ")[0] for g in basis),
                         ["mc_approval", "mc_task_event", "mc_task_event"])
        self.assertFalse([g for g in out["gaps"] if g["subject"].startswith("mail ")])
        self.assertFalse(any("PO100234" in s or "vince" in s for d in decisions
                             for s in d["steps"]))
        self.assertEqual(out["answerability"]["state"], "partial")
        self.assertIn("decision_basis_not_recorded", out["answerability"]["reasons"])

    def test_an_object_with_no_recorded_decision_is_a_typed_gap(self):
        out = self.investigate(self.quiet, FULL)
        self.assertEqual(self.decisions(out), [])
        self.assertEqual(out["answerability"]["state"], "blocked")
        gap = next(g for g in out["gaps"] if g["reason"] == "no_recorded_decision")
        self.assertEqual(gap["state"], "unknown")
        self.assertEqual(gap["searched"]["sources"], ["mc_approval", "mc_task_event",
                                                       "decision_document"])
        self.assertIn("missing_decisions", out["answerability"]["reasons"])

    def test_a_root_named_only_by_mail_has_no_recorded_decision_and_cites_the_mail(self):
        out = self.investigate(MAILED, FULL)
        named = {e["name"]: e for e in out["answer"]}
        self.assertEqual(self.decisions(out), [])
        self.assertIsNone(named["decisions"]["value"])
        self.assertIn("missing_decisions", out["answerability"]["reasons"])
        [mail] = self.context(out)
        self.assertEqual(mail["value"], "2026-09-21T09:00:00.000000Z")
        self.assertTrue(mail.get("lead_basis"))
        sender = next(named[n] for n in mail["inputs"] if n.startswith("mail_sender"))
        self.assertEqual((text(sender["value"]), sender["check"]), (MAIL_2_ROW["from"], "passed"))
        self.assertFalse([g for g in out["gaps"] if g["reason"].startswith("decision_")])
        # The person's acceptance of that link was retracted after the build: its link is
        # still projected, but it is no decision in force.
        [retracted] = [g for g in out["gaps"] if g["reason"] == "review_retracted"]
        self.assertIn(self.retracted["review_id"], retracted["detail"])
        self.assertIn(LINK, retracted["subject"])

    def test_a_root_type_the_plan_never_listed_gets_its_mentions_through_one_step_set(self):
        """Steps walk mention links by navigation name, so a quote (a root type the plan
        listed nowhere) gets its MC gate, task decision and mail like an order does."""
        out = self.investigate(QUOTE, FULL)
        named = {e["name"]: e for e in out["answer"]}
        decisions = self.decisions(out)
        self.assertEqual([d["value"] for d in decisions],
                         ["2026-10-03T09:30:00.000000Z", "2026-10-04T11:00:00.000000Z",
                          "2026-10-04T12:00:00.000000Z"])
        reviewed, approval, completed = decisions
        self.assertIn(self.quote_review["review_id"], reviewed["steps"][0])
        note = next(named[n] for n in approval["inputs"] if n.startswith("approval_note"))
        self.assertEqual((text(note["value"]), note["check"]),
                         ("Price matches the supplier's quote", "passed"))
        self.assertEqual(text(next(named[n] for n in completed["inputs"]
                                   if n.startswith("event_kind"))["value"]), "task.completed")
        [mail] = self.context(out)
        self.assertEqual(mail["value"], "2026-10-04T09:00:00.000000Z")
        self.assertTrue(all(e.get("lead_basis") for e in (approval, completed, mail)))
        basis = [g["subject"].split(" ")[0] for g in out["gaps"]
                 if g["reason"] == "decision_basis_not_recorded"]
        self.assertEqual(basis, ["mc_task_event"])   # the gate's note is its basis
        # A reader without the mailbox keeps the MC decisions and never sees the mail (nor
        # the review link, whose evidence is the mail).
        out = self.investigate(QUOTE, NO_MAIL)
        self.assertEqual(len(self.decisions(out)), 2)
        self.assertEqual(self.context(out), [])
        self.assertIn("withheld_by_permission", out["answerability"]["reasons"])
        self.assertNotIn("graph-mail://", json.dumps(out))
        self.assertNotIn("QUO-2026-0042 attached", json.dumps(out))

    def around_reviews(self, root: str, actor: Actor, reviews: bool = True) -> list[dict]:
        out = self.table(reviews=reviews)["plx.around"]["handler"](
            {"from": root, "tier_floor": "contradicted", "path": [{"link": "mentioned_in_email"}]},
            actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.around"]["output"],
                                  root=CONTRACTS), [], out)
        return [e for e in out["edges"] if "review" in e]

    def test_around_serves_a_review_only_to_a_reader_of_everything_it_cites(self):
        [edge] = self.around_reviews(QUOTE, FULL)
        self.assertEqual(edge["review"], {
            "review_id": self.quote_review["review_id"], "decision": "accept_link",
            "reviewed_by": REVIEWER, "reviewed_at": "2026-10-03T09:30:00.000000Z",
            "state": "effective"})
        # NO_PO sees the mail and the quote, so the link, but not order p2 the review cites:
        # no reviewer, time or review id, in plx.around or in the investigation.
        [edge] = self.around_reviews(QUOTE, NO_PO)
        self.assertEqual(edge["review"], {"withheld": "not_readable"})
        self.assertTrue(edge["link_id"].startswith("rvw-withheld-"), edge)
        out = self.investigate(QUOTE, NO_PO)
        self.assertEqual(self.decisions(out), [])
        [gap] = [g for g in out["gaps"] if g["reason"] == "review_decision_withheld"]
        self.assertEqual(gap["state"], "inaccessible")
        self.assertIn("review_decision_withheld", out["answerability"]["reasons"])
        for dumped in (json.dumps(edge), json.dumps(out)):
            for hidden in (REVIEWER, self.quote_review["review_id"][4:], "2026-10-03T09:30",
                           "PO100299"):
                self.assertNotIn(hidden, dumped)
        # A host without the review store serves none: a typed gap, never a guessed time.
        [edge] = self.around_reviews(QUOTE, FULL, reviews=False)
        self.assertEqual(edge["review"], {"withheld": "not_served"})
        out = self.investigate(QUOTE, FULL, reviews=False)
        self.assertEqual(len(self.decisions(out)), 2)
        self.assertEqual([g["reason"] for g in out["gaps"] if g["reason"].startswith("review_")],
                         ["review_decision_not_served"])

    def test_a_review_superseded_after_the_build_names_the_review_in_force(self):
        [edge] = [e for e in self.around_reviews(REPLACED, FULL)
                  if e["review"].get("review_id") == self.superseded["review_id"]]
        self.assertEqual((edge["review"]["state"], edge["review"]["superseded_by"]),
                         ("superseded", self.rejection["review_id"]))
        out = self.investigate(REPLACED, FULL)
        self.assertEqual(self.decisions(out), [])
        [gap] = [g for g in out["gaps"] if g["reason"] == "review_superseded"]
        self.assertIn(self.rejection["review_id"], gap["detail"])
        # ONE_MAIL reads the accepted review (MSG-2) but not the rejection (it cites MSG-3):
        # the review in force is withheld, never named.
        [edge] = self.around_reviews(REPLACED, ONE_MAIL)
        self.assertEqual((edge["review"]["state"], edge["review"]["superseded_by"]),
                         ("superseded", None))
        out = self.investigate(REPLACED, ONE_MAIL)
        [gap] = [g for g in out["gaps"] if g["reason"] == "review_superseded"]
        self.assertIn("withheld from this reader", gap["detail"])
        self.assertNotIn(self.rejection["review_id"][4:], json.dumps(out))
        # As known before the rejection, the acceptance was in force and is listed.
        before = self.table()["plx.investigate"]["handler"](
            {"family": "F11", "root": REPLACED, "as_known_at": "2026-10-04T00:00:00Z"}, FULL)
        self.assertEqual(validate(before, OUTPUT, root=CONTRACTS), [], json.dumps(before)[:1500])
        self.assertEqual([d["value"] for d in self.decisions(before)],
                         ["2026-10-03T09:30:00.000000Z"])

    def test_around_walks_a_link_by_its_navigation_name_and_refuses_what_it_cannot_name(self):
        around = self.table()["plx.around"]["handler"]
        output = CONTRACTS["tools"]["plx.around"]["output"]
        out = around({"from": QUOTE, "tier_floor": "lead", "path": [
            {"link": "mentioned_in_mc_task"},
            {"link_type": "MCTaskEvent.event_of", "direction": "reverse"}]}, FULL)
        self.assertEqual(validate(out, output, root=CONTRACTS), [], out)
        self.assertEqual([(h["link_type"], h["direction"], h["count"]) for h in out["hops"]],
                         [("MCTask.mentions_quote", "reverse", 1),
                          ("MCTaskEvent.event_of", "reverse", 1)])
        refused = {
            "undeclared_link": [{"link": "mentioned_in_nowhere"}],
            # A name the previous hop's type does not declare is not looked up elsewhere.
            "undeclared_link ": [{"link": "mentioned_in_mc_task"},
                                 {"link": "mentioned_in_mc_task"}],
            "invalid_input": [{"link": "mentioned_in_mc_task", "direction": "reverse"}]}
        for code, path in refused.items():
            with self.subTest(code):
                out = around({"from": QUOTE, "path": path}, FULL)
                self.assertEqual(out["error"]["code"], code.strip(), out)
        # A schema view no loader checked may give one type a name twice: never guessed.
        compiled = ot.SchemaView.from_compiled(self.compiled)
        links = dict(compiled.link_types)
        links["Email.mentions_quote_twice"] = dict(links["Email.mentions_quote"],
                                                   id="Email.mentions_quote_twice",
                                                   name="mentions_quote_twice")
        twice = ot.SchemaView(compiled.object_types, links, compiled.event_types,
                              compiled.action_types, compiled.digest)
        out = self.table(twice)["plx.around"]["handler"](
            {"from": QUOTE, "path": [{"link": "mentioned_in_email"}]}, FULL)
        self.assertEqual(out["error"]["code"], "ambiguous_link", out)

    def test_a_withheld_mailbox_is_reported_withheld_not_absent(self):
        out = self.investigate(self.po, NO_MAIL)
        decisions = self.decisions(out)
        # The MC decisions stay; the mail and the review citing it are not this reader's.
        self.assertEqual([d["value"] for d in decisions],
                         ["2026-10-01T12:00:00.000000Z", "2026-10-02T08:00:00.000000Z",
                          "2026-10-03T08:00:00.000000Z"])
        self.assertIn("withheld_by_permission", out["answerability"]["reasons"])
        self.assertEqual(out["answerability"]["state"], "partial")
        withheld = [g for g in out["gaps"] if g["reason"] in tt.PERMISSION_LOSSES]
        self.assertTrue(withheld and all(g["state"] in ("inaccessible", "unknown")
                                         for g in withheld), out["gaps"])
        self.assertIn(LINK, " ".join(g["subject"] for g in withheld))
        self.assertFalse([g for g in out["gaps"] if g["state"] == "absent_in_source"
                          and ("Email" in g["subject"] or "mail" in g["subject"])], out["gaps"])
        dumped = json.dumps(out)
        for hidden in (self.email, "graph-mail://", "Go-ahead", self.review["review_id"][4:]):
            self.assertNotIn(hidden, dumped)
        self.assertEqual(self.context(out), [])
        out = self.investigate(MAILED, NO_MAIL)
        self.assertEqual((self.decisions(out), self.context(out)), ([], []))
        self.assertNotIn("graph-mail://", json.dumps(out))

    def test_the_plan_loads_and_a_broken_engine_argument_fails_closed(self):
        plans = oi.load_plans(schema=ot.default_schema())
        self.assertEqual(plans["decision_evidence"].doc["families"], ["F11"])
        doc = json.loads(json.dumps(plans["decision_evidence"].doc))
        compute = doc["evidence"][-1]
        compute["engine_args"]["records"][0]["actor"] = ["mail_sender"]   # another type
        compute["engine_args"]["records"][1]["time"] = ["no_such_item"]
        compute["engine_args"]["reviews"] = "reviews"                       # unknown argument
        compute["engine_args"]["context"][0]["outcome"] = ["mail_subject"]  # no context role
        problems = oi.check_plan(doc, ot.default_schema(), CONTRACTS)
        self.assertEqual(len(problems), 4, problems)

    def test_the_plan_serves_every_type_its_links_reach_with_no_per_type_steps(self):
        schema = ot.default_schema()
        plan = oi.load_plans(schema=schema)["decision_evidence"]
        self.assertNotIn("object_types", plan.doc["root"])
        # Every type MC approvals, MC tasks and mail can all name, the orders among them.
        root_types = oi.plan_root_types(plan.doc, schema)
        self.assertLessEqual({"PurchaseOrder", "SalesOrder", "CustomerPurchaseOrder",
                              "AssemblyOrder", "Quote", "Shipment"}, root_types)
        for type_id in root_types:
            reverse = {link["reverse_name"] for link in schema.link_types.values()
                       if link["to"] == type_id}
            self.assertLessEqual(set(plan.doc["root"]["links"]), reverse, type_id)
        # No step is a copy of another for one target type: a generated mention link is
        # never named, and every around step walks a distinct path.
        hops = [hop for step in plan.doc["steps"] for hop in step.get("args", {}).get("path", ())]
        self.assertFalse([h for h in hops if ".mentions_" in h.get("link_type", "")], hops)
        paths = [json.dumps(step["args"]["path"]) for step in plan.doc["steps"]
                 if step["tool"] == "plx.around"]
        self.assertEqual(len(paths), len(set(paths)))
        # Links no type declares, or a name the root types lack, fail the load closed.
        doc = json.loads(json.dumps(plan.doc))
        doc["root"]["links"] = ["mentioned_in_nowhere"]
        self.assertTrue(oi.check_plan(doc, schema, CONTRACTS))
        doc = json.loads(json.dumps(plan.doc))
        doc["steps"][0]["args"]["path"] = [{"link": "event_of"}]
        self.assertTrue(oi.check_plan(doc, schema, CONTRACTS))
        # Without a schema no type is a root: nothing is run on a guess.
        self.assertEqual(oi.plan_root_types(plan.doc, None), frozenset())
        # Every reader of a plan's roots resolves them the same way: the console offers
        # the family on an order and on a quote, the dev baseline syncs their relations.
        from operator_ui import object_workspace
        from scripts import eval_dev_baseline
        for type_id in ("PurchaseOrder", "Quote"):
            self.assertIn("decision_evidence", [p["family"] for p in
                                                object_workspace.plans_for(type_id)], type_id)
        self.assertLessEqual({"public.app_purchase_order", "public.app_quote"}, set(
            eval_dev_baseline.plan_relations(schema_loader.load_schema(), ["F11"])))

    def test_a_time_its_citation_does_not_state_or_a_bare_date_is_never_ordered_by_guess(self):
        def entry(name, key, value):
            return {"name": name, "kind": "property", "value": value,
                    "object_id": f"Gate:mc.gates:{key}",
                    "citation": {"l1_handle": f"h://{key}", "field_path": "row.t"}}
        rows = {"h://a": {"row": {"t": "2026-10-01"}},
                "h://b": {"row": {"t": "2026-10-01T15:00:00Z"}},
                "h://c": {"row": {"t": "2026-10-01T09:00:00Z"}}}   # c's entry says 10:00
        evidence = {"t": [entry("t_1", "a", "2026-10-01"),
                          entry("t_2", "b", "2026-10-01T15:00:00.000000Z"),
                          entry("t_3", "c", "2026-10-01T10:00:00Z")],
                    "who": [entry("who_1", "a", "x"), entry("who_2", "b", None)],
                    "what": [entry("what_1", "a", "y"), entry("what_2", "b", "y"),
                             entry("what_3", "c", "y")],
                    # a blank note is no basis, and no other item stands in for it
                    "note": [entry("note_1", "a", {"untrusted_text": " "}),
                             entry("note_2", "b", "ok")],
                    "mt": [entry("mt_1", "m", "2026-10-01T08:00:00Z")],
                    "ms": [entry("ms_1", "m", "z")]}
        out = de.plan_run({"records": [{"label": "k", "time": ["t"], "actor": ["who"],
                                        "outcome": ["what"], "basis": ["note"]}],
                           "context": [{"label": "ctx", "time": ["mt"], "cites": ["ms"]}]},
                          evidence, [], items={},
                          name="decisions", as_of="2026-10-06",
                          resolver=lambda handle, actor: rows.get(handle), actor=FULL,
                          complete=True, covers=lambda link: True)
        listed = [e for e in out["entries"] if e["name"] != "decisions"]
        self.assertEqual([(e["name"], e["basis"], e["value"]) for e in listed[2:]],
                         [("decisions_context_1", ["Gate:mc.gates:m"], None)])  # not re-read
        listed = listed[:2]
        self.assertEqual([e["basis"] for e in listed],
                         [["Gate:mc.gates:a"], ["Gate:mc.gates:b"]])
        self.assertEqual(out["entries"][-1]["value"], 2)
        gaps = {(g["reason"], g["state"], g["subject"]) for g in out["gaps"]}
        self.assertIn(("decision_time_unverified", "unknown", "k Gate:mc.gates:c"), gaps)
        self.assertIn(("decision_order_unknown", "unknown", "decisions_1, decisions_2"), gaps)
        self.assertIn(("decision_actor_not_recorded", "absent_in_source", "k Gate:mc.gates:b"), gaps)
        self.assertIn(("decision_basis_not_recorded", "absent_in_source", "k Gate:mc.gates:a"),
                      gaps)
        self.assertFalse([g for g in gaps if "Gate:mc.gates:m" in g[2] or g[2].endswith(":b")
                          and g[0] == "decision_basis_not_recorded"])
        for entry_ in listed:
            self.assertEqual(validate(entry_, OUTPUT["oneOf"][0]["properties"]["answer"]
                                      ["items"], root=CONTRACTS), [])

    def test_a_served_review_is_listed_only_as_the_review_store_re_reads_it(self):
        served = {"review_id": "rev-" + "a" * 32, "decision": "reject_link",
                  "reviewed_by": REVIEWER, "reviewed_at": "2026-10-03T09:30:00.000000Z",
                  "state": "effective"}
        edge = {"link_id": "rvw-" + "a" * 32 + "-x", "link_type": LINK, "from": EMAIL,
                "to": PO, "tier": "contradicted", "review": served,
                "reason": [{"feature": "person_reviewed", "agrees": False}], "why_not": []}

        def run(reread):
            out = de.plan_run({"records": [{"label": "k", "time": ["t"]}]}, {}, [edge, edge],
                              items={}, name="decisions", as_of="2026-10-06", resolver=None,
                              actor=FULL, complete=True, covers=lambda link: True,
                              reviews=reread)
            return ([e["value"] for e in out["entries"] if e["name"] != "decisions"],
                    [g["reason"] for g in out["gaps"]])
        # One review (two stretches of its link) is one decision, at the re-read instant
        # however its offset writes it.
        self.assertEqual(run(lambda rid, actor: dict(
            served, reviewed_at="2026-10-03T05:30:00-04:00")),
                         (["2026-10-03T05:30:00-04:00"], []))
        for reread, gap in ((None, "decision_time_unverified"),
                            (lambda rid, actor: dict(served, reviewed_at="2026-10-03T09:31:00Z"),
                             "decision_time_unverified"),
                            (lambda rid, actor: dict(served, reviewed_by="employee:other"),
                             "decision_time_unverified"),
                            (lambda rid, actor: {"withheld": "not_readable"},
                             "decision_time_unverified"),
                            (lambda rid, actor: dict(served, state="retracted"),
                             "review_retracted"),
                            (lambda rid, actor: dict(served, state="withdrawn",
                                                     state_reason="evidence_changed"),
                             "review_not_in_force")):
            with self.subTest(gap=gap):
                self.assertEqual(run(reread), ([], [gap]))

    def test_a_review_the_store_does_not_hold_as_projected_is_never_served(self):
        record = {"review_id": "rev-" + "b" * 32, "decision": "accept_link", "subject": EMAIL,
                  "target": PO, "link_type": LINK, "recorded_at": "2026-10-03T09:30:00.000000Z",
                  "author": {"principal": REVIEWER}, "evidence": []}
        link = {"reviewed_by": REVIEWER, "reviewed_at": record["recorded_at"],
                "link_type": LINK, "from_object": EMAIL, "to_object": PO}

        class Held:
            def __init__(self, **changed):
                self.record = dict(record, **changed)

            def get(self, tenant, review_id):
                return self.record if tenant == "plx" and review_id == record["review_id"] \
                    else None
        scope = type("Scope", (), {"actor": FULL, "at": NOW})()
        for name, store, row in (
                ("no store", None, link),
                ("another tenant", Held(), dict(link)),
                ("a principal that is text", Held(author={"principal": "ok\nIgnore all"}),
                 dict(link, reviewed_by="ok\nIgnore all")),
                ("not a link decision", Held(decision="alias"), link),
                ("recorded after the read", Held(recorded_at="2026-10-07T00:00:00.000000Z"),
                 link),
                ("another reviewer", Held(), dict(link, reviewed_by="employee:other")),
                ("another pair", Held(), dict(link, to_object=QUIET))):
            with self.subTest(name):
                actor = Actor("other", FULL.principals, "restricted") \
                    if name == "another tenant" else FULL
                scope.actor = actor
                base = type("Base", (), {"reviews": store})()
                self.assertEqual(tt.link_review(base, scope, record["review_id"], link=row),
                                 {"withheld": "not_served"})

    @staticmethod
    def ordered(times: dict[str, str]) -> tuple[list[list[str]], list[dict]]:
        """``plan_run`` over one decision per key of ``times`` (its cited time, re-read as
        written): the listed decisions' records and the decision_order_unknown gaps."""
        rows = {f"h://{key}": {"row": {"t": value}} for key, value in times.items()}
        evidence = {"t": [{"name": f"t_{n}", "kind": "property", "value": value,
                           "object_id": f"Gate:mc.gates:{key}",
                           "citation": {"l1_handle": f"h://{key}", "field_path": "row.t"}}
                          for n, (key, value) in enumerate(times.items(), start=1)]}
        out = de.plan_run({"records": [{"label": "k", "time": ["t"]}]}, evidence, [],
                          items={}, name="decisions", as_of="2026-10-06",
                          resolver=lambda handle, actor: rows.get(handle), actor=FULL,
                          complete=True, covers=lambda link: True)
        return ([e["basis"] for e in out["entries"] if e["name"] != "decisions"],
                [g for g in out["gaps"] if g["reason"] == "decision_order_unknown"])

    def test_a_bare_date_is_its_day_in_any_time_zone_never_a_utc_day(self):
        """e356dc4031b9 item 5: 23:30 at -04:00 on the 1st is the 2nd in UTC, so read as a
        UTC day the bare date was ordered before it with no gap."""
        def run_with(times):
            return sorted(g["subject"] for g in self.ordered(times)[1])
        self.assertEqual(run_with({"a": "2026-10-01", "b": "2026-10-01T23:30:00-04:00"}),
                         ["decisions_1, decisions_2"])
        self.assertEqual(run_with({"a": "2026-10-01", "b": "2026-09-30T21:00:00+05:00"}),
                         ["decisions_1, decisions_2"])
        # Ordered: two days as dates, an instant no offset puts on the day, two instants.
        for times in ({"a": "2026-10-01", "b": "2026-10-02"},
                      {"a": "2026-10-01", "b": "2026-10-03T00:00:00Z"},
                      {"a": "2026-10-01T23:30:00-04:00", "b": "2026-10-02T03:31:00Z"}):
            with self.subTest(times=times):
                self.assertEqual(run_with(times), [])

    def test_an_unordered_pair_names_both_records(self):
        # 22:30 at -05:00 is 03:30Z the next day, but some time zone puts it on the 1st.
        listed, [gap] = self.ordered({"z": "2026-10-01T22:30:00-05:00", "d": "2026-10-01"})
        self.assertEqual(listed, [["Gate:mc.gates:d"], ["Gate:mc.gates:z"]])
        self.assertEqual((gap["state"], gap["subject"]), ("unknown", "decisions_1, decisions_2"))
        self.assertIn("decisions_1 (k Gate:mc.gates:d)", gap["detail"])
        self.assertIn("decisions_2 (k Gate:mc.gates:z)", gap["detail"])

    def test_two_decisions_at_the_same_instant_have_no_known_order(self):
        listed, [gap] = self.ordered({"b": "2026-10-01T08:00:00-04:00",
                                      "a": "2026-10-01T12:00:00Z", "c": "2026-10-01T12:00:01Z"})
        self.assertEqual(listed, [["Gate:mc.gates:a"], ["Gate:mc.gates:b"], ["Gate:mc.gates:c"]])
        self.assertEqual(gap["subject"], "decisions_1, decisions_2")
        self.assertIn("(k Gate:mc.gates:a)", gap["detail"])
        self.assertIn("(k Gate:mc.gates:b)", gap["detail"])

    def test_times_on_another_day_than_a_bare_date_in_every_time_zone_are_ordered(self):
        # 04:00 at -05:00 on 09-30 is 09:00Z, before the 1st begins at +14:00; 14:00 at +01:00
        # on 10-02 is 13:00Z, after the 1st ends at -12:00: strictly ordered, no gap.
        listed, gaps = self.ordered({"late": "2026-10-02T14:00:00+01:00", "d": "2026-10-01",
                                     "early": "2026-09-30T04:00:00-05:00"})
        self.assertEqual(listed, [["Gate:mc.gates:early"], ["Gate:mc.gates:d"],
                                  ["Gate:mc.gates:late"]])
        self.assertEqual(gaps, [])


if __name__ == "__main__":
    unittest.main()
