"""plx.investigate families F18 (people on a record) and F19 (what we last said to a party).

Synthetic Portal records (a purchase order, its supplier, an item on its line, a quiet
supplier), static captured mail in four mailboxes and a Mission Control capture (the injected
fake MC of ``integrations/tests/test_mc_sources.py``) are projected with stored mention links.
The generic tools, ``plx.investigate`` and a per-reader citation resolver run their real code
(MC citations through ``CaseRuntime`` over the capture in its state directory), every output
is validated against the tool contract, and the people directory is the captured mailbox
rows under a real ``PeopleExclusion`` guard. Evidence label: isolated-fixture (synthetic
records, injected MC transport, never live PLX data).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import quote

from integrations.capture_mail import MailCaptureStore
from integrations.capture_store import CaptureStore
from integrations.live_source_guard import LiveSourceAudience
from integrations.mc_sources import CaptureSpec, McSourcesCapture, store_path
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AudienceMap
from integrations.tests import test_mc_sources as tmc
from ontology import identifiers, mentions, reviews, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import CapturedRecord, build_store, capture_source
from ontology.types import Actor
from operating import people_graph as pg
from operating.case_runtime import CaseRuntime, LiveReaders
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt

CONTRACTS = ot.load_contracts()
OUTPUT = CONTRACTS["tools"]["plx.investigate"]["output"]
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2026-09-25T10:00:00.000000Z"
DOMAIN = "petrasoap.com"   # the fake MC's people (tmc.OPERATOR) are at this tenant domain


def oid(digit: str) -> str:
    return f"{digit * 8}-{digit * 4}-4{digit * 3}-8{digit * 3}-{digit * 12}"


COS, ALICE, BOB, VINCE, DAN = oid("1"), oid("2"), oid("3"), oid("4"), oid("5")
ERIN = oid("7")   # a Teams participant only: an Entra object id no captured mailbox holds
MAILBOXES = [  # the captured directory: mailbox object id, UPN and every SMTP address
    {"mailbox_id": COS, "upn": tmc.OPERATOR, "identities": [tmc.OPERATOR]},
    {"mailbox_id": ALICE, "upn": f"alice@{DOMAIN}",
     "identities": [f"alice@{DOMAIN}", f"asmith@{DOMAIN}"]},
    {"mailbox_id": BOB, "upn": f"bob@{DOMAIN}", "identities": [f"bob@{DOMAIN}"]},
    {"mailbox_id": VINCE, "upn": f"vince@{DOMAIN}", "identities": [f"vince@{DOMAIN}"]},
    {"mailbox_id": DAN, "upn": f"dan@{DOMAIN}", "identities": [f"dan@{DOMAIN}"]}]
REP = "rep@acme-chem.example"   # the supplier's contact (its website's domain)
CAROL = f"carol@{DOMAIN}"       # a tenant address no captured mailbox holds
REVIEWER = "employee:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FULL = Actor("plx", frozenset({"staff:ops", "staff:mail", "staff:dan", "staff:pii"}),
             "restricted")
NO_DAN = Actor("plx", frozenset({"staff:ops", "staff:mail", "staff:pii"}), "restricted")
NO_PII = Actor("plx", frozenset({"staff:ops", "staff:mail", "staff:dan"}), "restricted")
WITH_TEAMS = Actor("plx", FULL.principals | {"staff:teams"}, "restricted")
PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"p1"}'
SUPPLIER = 'Supplier:portal.app_supplier:{"id":"s1"}'
QUIET = 'Supplier:portal.app_supplier:{"id":"s2"}'
BETA = 'Supplier:portal.app_supplier:{"id":"s3"}'   # no website; a free-mail contact
BETA_CONTACT = "beta.labs@gmail.com"
ITEM = 'Product:portal.app_product:{"id":"i1"}'


def mail(box: str, mid: str, sender: str, to: list[str], subject: str, at: str,
         cc: list[str] | None = None, body: str | None = None) -> dict:
    return {"mailbox": box, "id": mid, "from": sender, "to": to, "cc": cc or [],
            "subject": subject, "sent_at": at, "received_at": at,
            "body_preview": body or subject}


MAILS = [
    mail(ALICE, "M1", f"alice@{DOMAIN}", [REP], "PO100234 delivery date",
         "2026-10-01T10:00:00Z", cc=[f"bob@{DOMAIN}"], body="When will PO100234 ship?"),
    mail(ALICE, "M2", REP, [f"alice@{DOMAIN}"], "Re: PO100234 delivery date",
         "2026-10-02T09:00:00Z", body="PO100234 ships on the 9th."),
    mail(BOB, "M3", f"asmith@{DOMAIN}", [f"bob@{DOMAIN}"], "PO100234 internal note",
         "2026-10-03T08:00:00Z"),
    mail(BOB, "M4", CAROL, [f"bob@{DOMAIN}"], "PO100234 question", "2026-10-03T09:00:00Z"),
    mail(DAN, "M5", f"bob@{DOMAIN}", [REP], "PO100234 revised date", "2026-10-05T12:00:00Z",
         cc=[f"dan@{DOMAIN}"]),
    # Sent by the excluded person: the case runtime's gate withholds it; this fixture's gate
    # lets it through, so the engine's own exclusion check is what keeps Vince out.
    mail(ALICE, "M6", f"vince@{DOMAIN}", [f"alice@{DOMAIN}"], "PO100234 approval",
         "2026-10-04T08:00:00Z"),
    # Copied to the excluded person: withheld whole, as the capture drops such a message.
    mail(ALICE, "M10", f"alice@{DOMAIN}", [REP], "PO100234 pricing", "2026-10-04T15:00:00Z",
         cc=[f"vince@{DOMAIN}"]),
    # Beta Labs writes from a free-mail address a person linked to it (reviewed): that address
    # is the party's, its shared domain never is (M9 goes to someone else at gmail.com).
    mail(ALICE, "M7", BETA_CONTACT, [f"alice@{DOMAIN}"], "Our quote", "2026-09-28T09:00:00Z"),
    mail(ALICE, "M8", f"alice@{DOMAIN}", [BETA_CONTACT], "PO100235 placed",
         "2026-09-29T09:00:00Z"),
    mail(BOB, "M9", f"bob@{DOMAIN}", ["someone.else@gmail.com"], "PO100235 forwarded",
         "2026-09-30T09:00:00Z"),
    # The newest message to the supplier is copied to Carol, whom nothing can clear: it is
    # withheld whole, so the answer must not read as complete with M5 as the latest.
    mail(ALICE, "M11", f"alice@{DOMAIN}", [REP], "PO100234 final terms",
         "2026-10-06T08:00:00Z", cc=[CAROL]),
]


MAIL = {row["id"]: row for row in MAILS}


def handle(row: dict) -> str:
    return f"graph-mail://{row['mailbox']}/{row['id']}@CK-{row['id']}"


def email_id(row: dict) -> str:
    return f'Email:graph_mail.messages:{{"id":"{row["id"]}","mailbox":"{row["mailbox"]}"}}'


def record(source, relation, native, row, cite, digest, audience) -> CapturedRecord:
    return CapturedRecord(source, relation, native, row, cite, "row", digest, T0, None, T0,
                          audience)


class Static:
    def __init__(self, source: str, records: list[CapturedRecord]) -> None:
        self.source, self._records = source, records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


class StaticMail(Static):
    """Static captured mail that also offers its messages as mention documents (subject and
    body preview), as ``ontology.project.MailCaptures.documents`` does."""

    def documents(self):
        for rec in self._records:
            row = rec.row
            yield mentions.Document(
                rec.l1_handle, "email", (mentions.TextField(rec.l1_handle, "row.subject",
                                                            row["subject"]),
                                         mentions.TextField(rec.l1_handle, "row.body_preview",
                                                            row["body_preview"])),
                sender_class="unknown", digest=rec.digest, recorded_at=rec.recorded_at,
                date=row["sent_at"])


def portal_records() -> list[CapturedRecord]:
    rows = [("public.app_supplier", {"id": "s1", "name": "Acme Chemical", "code": "ACME01",
                                     "website": "https://www.acme-chem.example"}),
            ("public.app_supplier", {"id": "s2", "name": "Quiet Supplies", "code": "QUIET1"}),
            ("public.app_supplier", {"id": "s3", "name": "Beta Labs", "code": "BETA01"}),
            ("public.app_purchase_order", {"id": "p1", "po_number": "PO100234",
                                           "supplier_id": "s1"}),
            ("public.app_purchase_order", {"id": "p3", "po_number": "PO100235",
                                           "supplier_id": "s3"}),
            ("public.app_product", {"id": "i1", "code": "RM-1001", "name": "Glycerin"}),
            ("public.app_purchase_order_line", {"id": "l1", "purchase_order_id": "p1",
                                                "product_id": "i1", "quantity": 5})]
    return [record("portal_api", rel, json.dumps({"id": row["id"]}), row,
                   f"api://{rel}/{row['id']}@g-1", f"{n:064x}", f"api:{rel}")
            for n, (rel, row) in enumerate(rows, start=1)]


def mail_records(rows: list[dict]) -> list[CapturedRecord]:
    return [record("graph_mail", "messages", json.dumps([row["id"], row["mailbox"]]), row,
                   handle(row), f"{n:064x}", f"mail:{row['mailbox']}")
            for n, row in enumerate(rows, start=100)]


def mail_gate(actor: Actor, cite: str) -> bool:
    if cite.startswith("teams-chat://"):   # the chat's unit and membership, as one grant
        return "staff:teams" in actor.principals
    if not cite.startswith("graph-mail://"):
        return False
    return "staff:mail" in actor.principals and (
        not cite.startswith(f"graph-mail://{DAN}/") or "staff:dan" in actor.principals)


GROUP_CHAT, PAIR_CHAT = "19:synth-group@thread.v2", "19:synth-pair@thread.v2"


def participant(who: str, name: str) -> dict:
    return {"id": who, "display_name": name, "email": None}


def teams_records() -> list[CapturedRecord]:
    """Two chats naming PO100234: a group chat (Alice wrote it; Dan and Erin take part) and
    a one-on-one of Bob's whose only other participant is Vince, the excluded person."""
    chats = {GROUP_CHAT: ("PO100234 artwork", [participant(ALICE, "Alice Smith"),
                                               participant(DAN, "Dan Doe"),
                                               participant(ERIN, "Erin Lee")],
                          ALICE, "Alice Smith"),
             PAIR_CHAT: (None, [participant(BOB, "Bob Brown"), participant(VINCE, "Vince")],
                         BOB, "Bob Brown")}
    out = []
    for n, (chat, (topic, members, author, name)) in enumerate(sorted(chats.items())):
        base = f"teams-chat://{quote(chat, safe='')}/"
        out.append(record("teams", "chats", json.dumps([chat, "conversation"]), {
            "kind": "chat", "chat_id": chat, "topic": topic, "chat_type": "group",
            "created_at": "2026-09-30T08:00:00Z", "last_activity_at": "2026-10-04T10:00:00Z",
            "participants": members, "participants_basis": "chat_members"},
            f"{base}conversation@g-1", f"{n + 200:064x}", f"teams:chat:{chat}"))
        out.append(record("teams", "chatMessages", json.dumps([chat, "1001"]), {
            "kind": "chat_message", "chat_id": chat, "id": "1001",
            "created_at": "2026-10-04T10:00:00Z", "subject": None,
            "body_text": "PO100234 proof is ready",
            "from": {"kind": "user", "id": author, "display_name": name}},
            f"{base}1001@g-1", f"{n + 210:064x}", f"teams:chat:{chat}"))
    return out


class StaticTeams(Static):
    """Static Teams captures whose messages are mention documents (as ``TeamsCaptures``)."""

    def documents(self):
        for rec in self._records:
            if rec.relation == "chatMessages":
                yield mentions.Document(
                    rec.l1_handle, "teams_message", (mentions.TextField(
                        rec.l1_handle, "row.body_text", rec.row["body_text"]),),
                    sender_class="unknown", digest=rec.digest, recorded_at=rec.recorded_at,
                    date=rec.row["created_at"])


def text(value):
    return value.get("untrusted_text") if isinstance(value, dict) else value


class World:
    """One projected fixture world (mail rows given) with its MC capture and runtime."""

    def __init__(self, rows: list[dict], *, assignee: str | None = "Alice Smith",
                 teams: list[CapturedRecord] = ()) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        fake = tmc.FakeMc()
        tmc.add_hr_project(fake)
        if assignee is not None:   # a name alone: no shared key with anyone
            fake.tasks["TASK-1"]["assignee"] = assignee
        mc = store_path(tmp)
        mc.parent.mkdir(parents=True)
        with CaptureStore(mc, tmc.audience(), clock=tmc.Clock()) as store:
            McSourcesCapture(store, tmc.mc_client(fake)).capture(CaptureSpec("ops", ()))
        self.rows = {handle(r): r for r in rows}
        self.portal = {r.l1_handle: r for r in portal_records()}
        self.compiled = schema_loader.load_schema()
        self.path = tmp / "ontology.sqlite"
        mails = mail_records(rows)
        self.teams = {r.l1_handle: r for r in teams}
        # A person confirmed that M7 was sent by Beta Labs (the reviewed sender link).
        store = reviews.ReviewStore(tmp / "reviews.sqlite", clock=lambda: NOW)
        for found in mails:
            if found.row["from"] == BETA_CONTACT:
                store.record(tenant="plx", author={"principal": REVIEWER, "channel": "console",
                                                   "session": "0" * 64},
                             decision="accept_link", subject=email_id(found.row), target=BETA,
                             link_type="Email.sent_by_supplier",
                             evidence=[{"object_id": email_id(found.row),
                                        "l1_handle": found.l1_handle, "digest": found.digest,
                                        "field_path": "row.from"}])
        with capture_source("mission_control", mc) as mc_source:
            build_store(self.path, self.compiled, {
                "portal_api": Static("portal_api", portal_records()),
                "graph_mail": StaticMail("graph_mail", mails),
                "mission_control": mc_source,
                **({"teams": StaticTeams("teams", list(teams))} if teams else {})},
                normalizer=identifiers.normalize,
                clock=lambda: NOW, post_project={"mentions": mentions.store_mention_links,
                                                 "reviews": reviews.link_step(store, "plx")})
        self.runtime = CaseRuntime(tmp, client=None, audience=AudienceMap({}, tenant="plx"),
                                   tenant="plx", live=LiveReaders(LiveSourceAudience(
                                       {"mc:ops:*": ["staff:ops"]}, tenant="plx")))

    def close(self) -> None:
        self.runtime.close()
        self.tmp.cleanup()

    def rows_of(self, cite, actor):
        if cite.startswith("mc://"):
            return oi.citation_rows(self.runtime)(cite, actor)
        if cite in self.teams and mail_gate(actor, cite):
            found = self.teams[cite]
            return {"relation": found.relation, "l1_handle": cite, "row": dict(found.row)}
        if cite in self.rows and mail_gate(actor, cite):
            return {"relation": "messages", "l1_handle": cite, "row": dict(self.rows[cite])}
        if cite in self.portal and "staff:ops" in actor.principals:
            found = self.portal[cite]
            return {"relation": found.relation, "l1_handle": cite, "row": dict(found.row)}
        return None

    def table(self, people) -> dict:
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:ops"}, "mc:ops/*": {"staff:ops"},
            "mail:*": {"staff:mail"}, f"mail:{DAN}": {"staff:dan"},
            "teams:*": {"staff:teams"}},
            markings={m: {"staff:pii"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        base = ot.OntologyTools(reader, policy, item_gate=mail_gate,
                                schema=ot.SchemaView.from_compiled(self.compiled),
                                grammar=None, contracts=CONTRACTS, clock=lambda: NOW)
        table: dict = {"_reader": reader}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(
            table, rows=self.rows_of, scope=oi.read_scope(base), clock=lambda: NOW,
            held_markings=policy.held_markings, people=people))
        return table


def directory(guard: PeopleExclusion | None = None, **options) -> pg.PeopleDirectory:
    guard = PeopleExclusion([VINCE]) if guard is None else guard
    return pg.PeopleDirectory(guard, MAILBOXES, withheld=[VINCE], **options)


class PeopleGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.world = World(MAILS)

    @classmethod
    def tearDownClass(cls):
        cls.world.close()

    def ask(self, family: str, root: str, actor: Actor, people=None, *,
            world: World | None = None) -> dict:
        world = world or self.world
        provider = (lambda: directory()) if people is None else people
        table = world.table(provider)
        self.addCleanup(table["_reader"].conn.close)
        arguments = {"family": family, "root": root}
        pages = []
        while True:   # every answer page, merged: a paged answer is one answer
            out = table["plx.investigate"]["handler"](arguments, actor)
            self.assertEqual(validate(out, OUTPUT, root=CONTRACTS), [], json.dumps(out)[:1500])
            self.assertNotIn("error", out, json.dumps(out)[:1500])
            pages.append(out)
            cursor = out["completeness"].get("next_cursor")
            if not cursor or "answer_page" not in out:
                break
            arguments = {**arguments, "cursor": cursor}
        merged = dict(pages[0])
        merged["answer"] = list({e["name"]: e for page in pages for e in page["answer"]}
                                .values())
        merged["values"] = [v for page in pages for v in page["values"]]
        merged["pages"] = pages
        return merged

    @staticmethod
    def people(out: dict) -> dict[str, dict]:
        """label -> person entry."""
        return {text(e["value"]): e for e in out["answer"]
                if e["name"].startswith("people_") and isinstance(e["value"], dict)}

    @staticmethod
    def role(entry: dict, role: str, label: str) -> bool:
        """Whether a person entry states ``role`` on records of kind ``label``."""
        return any(re.match(rf"{role} on [0-9]+ {label} record", s) for s in entry["steps"])

    def assert_cited(self, out: dict, entry: dict) -> None:
        named = {e["name"]: e for e in out["answer"]}
        # A role read from a record's own citation (To/Cc) cites no entry: the record is in
        # the basis and the step says so.
        self.assertTrue(entry["inputs"] or (entry["basis"] and any(
            "re-read from each record's citation" in s for s in entry["steps"])), entry)
        for name in entry["inputs"]:
            self.assertIn(name, named, entry)
            if named[name]["kind"] == "property":
                self.assertEqual(named[name]["check"], "passed", named[name])

    def assert_no_excluded(self, out: dict) -> None:
        dumped = json.dumps(out)   # every page (``pages``) included
        for needle in ("vince", VINCE, "carol", "Alice Smith"):
            self.assertNotIn(needle, dumped)

    # ------------------------------------------------------------------ F18

    def test_who_works_on_a_po_resolves_people_across_mail_and_mc_by_shared_keys(self):
        out = self.ask("F18", PO, FULL)
        people = self.people(out)
        self.assertEqual(set(people), {tmc.OPERATOR, f"alice@{DOMAIN}", f"bob@{DOMAIN}",
                                       f"dan@{DOMAIN}"})
        alice = people[f"alice@{DOMAIN}"]
        # Alice wrote M1 as alice@ and M3 as her proxy address: one person by the directory.
        self.assertIn(email_id(MAIL["M1"]), alice["basis"])
        self.assertIn(email_id(MAIL["M3"]), alice["basis"])
        self.assertIn("identity verified: one captured directory mailbox", alice["steps"][0])
        # MC's accountable owner and the task history's actor is the operator, joined to his
        # mailbox by address; his approval request too.
        cos = people[tmc.OPERATOR]
        self.assertTrue(self.role(cos, "owner", "mc_task"), cos)
        self.assertTrue(self.role(cos, "actor", "mc_task_event"))
        bob = people[f"bob@{DOMAIN}"]
        self.assertTrue(self.role(bob, "recipient", "mail"))
        self.assertTrue(self.role(bob, "sender", "mail"))
        for entry in people.values():
            self.assert_cited(out, entry)
            self.assertTrue(entry["value"]["origin"]["citation"].startswith(("graph-mail://",
                                                                             "mc://")))
            self.assertTrue(entry.get("lead_basis"))   # mentions are lead links
        count = next(e for e in out["answer"] if e["name"] == "people")
        self.assertEqual(count["value"], 4)
        # Owners rank first: the operator owns the tasks naming the order.
        self.assertEqual(text(next(e for e in out["answer"] if e["name"] == "people_1")
                              ["value"]), tmc.OPERATOR)

    def test_excluded_and_undecided_people_never_appear_and_their_entries_are_withheld(self):
        out = self.ask("F18", PO, FULL)
        self.assert_no_excluded(out)
        named = {e["name"]: e for e in out["answer"]}
        senders = [text(e["value"]) for e in out["answer"] if e["name"].startswith(
            "mail_sender")]
        # A record naming Vince (M6 he sent, M10 he is copied on, the gate he decided) or
        # Carol (M4: no mailbox, an unbound guard) is withheld whole; the MC assignee is a
        # name alone and only its entry goes.
        self.assertNotIn(CAROL, senders)
        dumped = json.dumps(out)
        for record in (MAIL["M4"], MAIL["M6"], MAIL["M10"]):
            self.assertNotIn(handle(record), dumped)
            self.assertFalse([e for e in out["answer"] if e.get("object_id") == email_id(
                record)])
            self.assertFalse([p for p in self.people(out).values()
                              if email_id(record) in p["basis"]])
        self.assertFalse([n for n in named if n.startswith("approval_")])
        self.assertFalse([n for n in named if n.startswith("task_assignee")])
        self.assertTrue([n for n in named if n.startswith("task_owner")])
        self.assertFalse([v for v in out["values"] if "vince" in json.dumps(v)])
        gap = next(g for g in out["gaps"] if g["reason"] == "participants_not_cleared")
        self.assertNotRegex(gap["detail"], r"\d")   # no count of the withheld
        # With a guard bound to the tenant directory, Carol is cleared and named.
        bound = PeopleExclusion([VINCE])
        bound._bound, bound._addresses = True, frozenset({f"vince@{DOMAIN}"})
        cleared = self.ask("F18", PO, FULL, lambda: directory(bound))
        self.assertIn(CAROL, self.people(cleared))
        self.assertIn(handle(MAIL["M4"]), json.dumps(cleared))
        self.assertNotIn("vince", json.dumps(cleared))
        self.assertNotIn(handle(MAIL["M10"]), json.dumps(cleared))

    def test_a_restricted_reader_sees_no_person_edge_through_a_mailbox_it_may_not_read(self):
        out = self.ask("F18", PO, NO_DAN)
        people = self.people(out)
        self.assertNotIn(f"dan@{DOMAIN}", people)   # Dan is only on M5, in his mailbox
        dumped = json.dumps(out)
        self.assertNotIn("revised date", dumped)
        self.assertNotIn(handle(MAIL["M5"]), dumped)
        self.assertNotIn(email_id(MAIL["M5"]), dumped)
        bob = people[f"bob@{DOMAIN}"]
        self.assertNotIn(email_id(MAIL["M5"]), bob["basis"])
        self.assertFalse(self.role(bob, "sender", "mail"))
        self.assertNotIn(DAN, dumped)

    def test_a_reader_without_pii_gets_no_mc_person_and_mail_people_only(self):
        out = self.ask("F18", PO, NO_PII)
        people = self.people(out)
        self.assertNotIn(tmc.OPERATOR, people)   # MC people fields are pii-marked
        self.assertIn(f"alice@{DOMAIN}", people)
        self.assertTrue([g for g in out["gaps"] if g["reason"] == "marking_withheld"]
                        or not [e for e in out["answer"] if e["name"].startswith("task_owner")])

    def test_who_to_ask_about_an_item_follows_the_orders_that_carry_it(self):
        out = self.ask("F18", ITEM, FULL)
        people = self.people(out)
        self.assertLessEqual({f"alice@{DOMAIN}", f"bob@{DOMAIN}"}, set(people))
        hop = next(t for t in out["trace"] if t["step"] == "item_purchases")
        self.assertEqual((hop["status"], hop["objects"]), ("ok", 1))
        self.assert_no_excluded(out)

    def test_an_unusable_guard_or_no_directory_names_nobody(self):
        for provider, code in ((lambda: directory(PeopleExclusion([])),
                                "people_exclusion_unavailable"),
                               (None, "people_directory_unavailable")):
            table = self.world.table(provider)
            self.addCleanup(table["_reader"].conn.close)
            out = table["plx.investigate"]["handler"]({"family": "F18", "root": PO}, FULL)
            self.assertEqual(validate(out, OUTPUT, root=CONTRACTS), [])
            self.assertEqual(self.people(out), {})
            self.assertIn(code, [g["reason"] for g in out["gaps"]])
            dumped = json.dumps(out)
            for row in MAILS:
                self.assertNotIn(row["from"], dumped)
            self.assertNotIn(tmc.OPERATOR, dumped)

    # ------------------------------------------------------------------ F19

    def test_what_we_last_said_to_a_supplier_and_who_said_it(self):
        out = self.ask("F19", SUPPLIER, FULL)
        named = {e["name"]: e for e in out["answer"]}
        last = named["last_said"]
        self.assertEqual(last["value"], "2026-10-05T12:00:00Z")
        self.assertEqual(last["basis"], [email_id(MAIL["M5"])])
        self.assertEqual(text(named["last_said_by"]["value"]), f"bob@{DOMAIN}")
        said = [named[n] for n in last["inputs"] if n.startswith(("mail_subject", "mail_body"))]
        self.assertEqual({text(e["value"]) for e in said}, {"PO100234 revised date"})
        self.assert_cited(out, last)
        earlier = named["last_said_earlier_1"]
        self.assertEqual(earlier["basis"], [email_id(MAIL["M1"])])
        self.assertEqual(text(named["last_said_earlier_1_by"]["value"]), f"alice@{DOMAIN}")
        # The supplier's own reply and internal mail are never "what we said".
        bases = [b for e in out["answer"] if e["name"].startswith("last_said")
                 for b in e.get("basis", ())]
        self.assertNotIn(email_id(MAIL["M2"]), bases)
        self.assertNotIn(email_id(MAIL["M3"]), bases)
        self.assert_no_excluded(out)
        # M10 (to the supplier, copied to Vince) is later than M1 but withheld whole, and so
        # are the mails Vince and Carol sent: no entry of theirs, not even the subject.
        dumped = json.dumps(out)
        for subject in ("PO100234 pricing", "PO100234 approval", "PO100234 question"):
            self.assertNotIn(subject, dumped)

    def test_an_uncleared_later_message_makes_last_said_partial_never_complete(self):
        out = self.ask("F19", SUPPLIER, FULL)
        named = {e["name"]: e for e in out["answer"]}
        self.assertEqual(named["last_said"]["basis"], [email_id(MAIL["M5"])])
        self.assertNotIn("PO100234 final terms", json.dumps(out))
        self.assertIn("participants_not_cleared", out["answerability"]["reasons"])
        self.assertNotEqual(out["answerability"]["state"], "answerable")
        # Cleared by a guard bound to the tenant directory, M11 is the latest.
        bound = PeopleExclusion([VINCE])
        bound._bound, bound._addresses = True, frozenset({f"vince@{DOMAIN}"})
        cleared = self.ask("F19", SUPPLIER, FULL, lambda: directory(bound))
        named = {e["name"]: e for e in cleared["answer"]}
        self.assertEqual(named["last_said"]["basis"], [email_id(MAIL["M11"])])
        self.assertNotIn("participants_not_cleared", cleared["answerability"]["reasons"])
        # F18 alike: an uncleared participant makes "who works on it" partial.
        people = self.ask("F18", PO, FULL)
        self.assertIn("participants_not_cleared", people["answerability"]["reasons"])

    def test_a_loosely_matched_twins_website_is_not_the_partys_domain(self):
        """Engine boundary: only the root's website and those of twins an identity-tier
        same-record link joins count; a text-match twin's never does."""
        loose = 'Supplier:bc.vendors:{"id":"v9"}'
        firm = 'Supplier:portal.fm_supplier:{"id":"7"}'
        sites = {SUPPLIER: "https://other.example", loose: "https://acme-chem.example"}
        rows = {f"site://{k}": {"website": v} for k, v in sites.items()}
        msg = MAIL["M1"]
        rows[handle(msg)] = msg

        def entry(name, oid_, cite, path, value):
            return {"name": name, "kind": "property", "object_id": oid_, "value": value,
                    "citation": {"l1_handle": cite, "field_path": path}, "check": "passed"}
        evidence = {
            "party_website": [entry(f"party_website_{i}", k, f"site://{k}", "row.website", v)
                              for i, (k, v) in enumerate(sites.items(), start=1)],
            "mail_sent_at": [entry("mail_sent_at", email_id(msg), handle(msg), "row.sent_at",
                                   msg["sent_at"])],
            "mail_sender": [entry("mail_sender", email_id(msg), handle(msg), "row.from",
                                  msg["from"])],
            "mail_subject": [entry("mail_subject", email_id(msg), handle(msg), "row.subject",
                                   msg["subject"])]}
        args = {"party_websites": ["party_website"], "twin_links": ["Supplier.same_record_as"],
                "messages": {"time": ["mail_sent_at"], "sender": ["mail_sender"],
                             "said": ["mail_subject"], "recipients": ["row.to", "row.cc"]}}

        def run(tier):
            edges = [{"link_type": "Supplier.same_record_as", "from": SUPPLIER, "to": loose,
                      "tier": tier}, {"link_type": "Supplier.same_record_as", "from": SUPPLIER,
                                      "to": firm, "tier": "verified"}]
            return pg.said_run(args, evidence, edges, items={}, name="last_said",
                               as_of="2026-10-06", resolver=lambda c, a: (
                                   {"row": rows[c]} if c in rows else None),
                               actor=FULL, complete=True, covers=lambda _: False,
                               people=lambda: directory(), roots=[SUPPLIER])
        self.assertIn("nothing_said_found", [g["reason"] for g in run("lead")["gaps"]])
        self.assertIn("nothing_said_found", [g["reason"] for g in run(
            "stored_by_text_join")["gaps"]])
        self.assertEqual(run("verified")["entries"][0]["basis"], [email_id(msg)])

    def test_a_restricted_reader_gets_the_last_message_it_may_read(self):
        out = self.ask("F19", SUPPLIER, NO_DAN)
        named = {e["name"]: e for e in out["answer"]}
        self.assertEqual(named["last_said"]["basis"], [email_id(MAIL["M1"])])
        self.assertEqual(text(named["last_said_by"]["value"]), f"alice@{DOMAIN}")
        self.assertNotIn("revised date", json.dumps(out))

    def test_a_reviewed_sender_address_is_the_party_and_its_free_mail_domain_never_is(self):
        out = self.ask("F19", BETA, FULL)
        named = {e["name"]: e for e in out["answer"]}
        self.assertEqual(named["last_said"]["basis"], [email_id(MAIL["M8"])])   # to Beta
        self.assertEqual(text(named["last_said_by"]["value"]), f"alice@{DOMAIN}")
        self.assertIn("an address the party sent us mail from",
                      " ".join(named["last_said"]["steps"]))
        self.assertNotIn("last_said_earlier_1", named)   # M9 went to another gmail user

    def test_a_party_with_no_known_address_is_a_typed_gap_never_nothing_said(self):
        out = self.ask("F19", QUIET, FULL)
        reasons = [g["reason"] for g in out["gaps"]]
        self.assertIn("party_address_unknown", reasons)
        gap = next(g for g in out["gaps"] if g["reason"] == "nothing_said_found")
        self.assertEqual(gap["state"], "unknown")
        self.assertIsNone(next(e for e in out["answer"] if e["name"] == "last_said")["value"])
        self.assertNotEqual(out["answerability"]["state"], "answerable")

    # ------------------------------------------------------------------ plans, directory

    def test_engine_arguments_that_do_not_check_are_refused(self):
        plan = json.loads((oi.PLAN_DIR / "people_on_record.json").read_text("utf-8"))
        schema = ot.SchemaView.from_compiled(self.world.compiled)
        self.assertEqual(oi.check_plan(plan, schema, CONTRACTS), [])
        for mutate in (lambda a: a["records"][0]["people"][0].update(role="boss"),
                       lambda a: a["records"][0]["people"][0].update(identity="guess"),
                       lambda a: a["records"][0]["people"][1].update(reread=["body"]),
                       lambda a: a.update(extra=True),
                       lambda a: a["records"][0].update(time=["no_such_item"])):
            broken = copy.deepcopy(plan)
            mutate(broken["evidence"][-1]["engine_args"])
            self.assertTrue(oi.check_plan(broken, schema, CONTRACTS), broken)
        said = json.loads((oi.PLAN_DIR / "party_last_said.json").read_text("utf-8"))
        self.assertEqual(oi.check_plan(said, schema, CONTRACTS), [])
        said["evidence"][-1]["engine_args"]["party_links"] = ["Email.sent_by_nobody"]
        self.assertTrue(oi.check_plan(said, schema, CONTRACTS))

    def test_the_runtime_directory_reads_the_mail_capture_under_its_exclusion(self):
        with tempfile.TemporaryDirectory() as state:
            store = MailCaptureStore(state)
            with store.db:
                for row in MAILBOXES:
                    store.db.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,?,?)",
                                     (row["mailbox_id"], row["upn"],
                                      json.dumps(row["identities"]), "[]", "{}", T0, T0))
            store.db.close()

            class Runtime:   # the CaseRuntime attributes the directory reads
                people_exclusion = PeopleExclusion([VINCE])
                mail_capture_dir = Path(state)
                mail_scope = None

                @staticmethod
                def address_exclusion():
                    return None
            found = pg.PeopleDirectory.from_runtime(Runtime())
            self.assertEqual(found.address(f"asmith@{DOMAIN}").key, f"mailbox:{ALICE}")
            self.assertEqual(found.address(f"vince@{DOMAIN}").state, pg.WITHHELD)
            self.assertEqual(found.object_id(VINCE).state, pg.WITHHELD)
            self.assertEqual(found.address(CAROL).state, pg.UNDECIDED)
            self.assertEqual(found.address(REP).state, pg.EXTERNAL)
            self.assertEqual(found.address("not an address").state, pg.UNDECIDED)
            Runtime.people_exclusion = PeopleExclusion(["not-an-oid"])
            self.assertEqual(pg.PeopleDirectory.from_runtime(Runtime()).address(
                f"alice@{DOMAIN}").state, pg.WITHHELD)

    def test_teams_authors_join_people_by_object_id_and_names_stay_leads(self):
        """Engine boundary (no fixture here holds a Teams capture): the engine's Teams path
        is proven on cited entries."""
        rows = {
            "teams-channel://t/c/m1@g-1": {"from": {"kind": "user", "id": ALICE,
                                                    "display_name": "Alice Smith"},
                                           "created_at": "2026-10-04T10:00:00Z"},
            "teams-channel://t/c/m2@g-1": {"from": {"kind": "user", "id": VINCE,
                                                    "display_name": "Vince"},
                                           "created_at": "2026-10-04T11:00:00Z"},
            "teams-channel://t/c/m3@g-1": {"from": {"kind": "application", "id": "bot",
                                                    "display_name": "Bot"},
                                           "created_at": "2026-10-04T12:00:00Z"},
            "mc://ops/tasks/TASK-9@g-1": {"assignee": "alice smith",
                                          "updatedAt": "2026-10-04T09:00:00Z"}}

        def entry(name, oid_, cite, path, value):
            return {"name": name, "kind": "property", "object_id": oid_, "value": value,
                    "citation": {"l1_handle": cite, "field_path": path}, "check": "passed"}
        msgs = [f'TeamsChannelMessage:gen.teams.channel_messages:["t","c","m{i}"]'
                for i in (1, 2, 3)]
        task = 'MCTask:mission_control.tasks:{"id":"TASK-9"}'
        evidence = {
            "channel_from": [entry(f"channel_from_{i}", msgs[i - 1],
                                   f"teams-channel://t/c/m{i}@g-1", "row.from",
                                   json.dumps(rows[f"teams-channel://t/c/m{i}@g-1"]["from"]))
                             for i in (1, 2, 3)],
            "channel_at": [entry(f"channel_at_{i}", msgs[i - 1],
                                 f"teams-channel://t/c/m{i}@g-1", "row.created_at",
                                 rows[f"teams-channel://t/c/m{i}@g-1"]["created_at"])
                           for i in (1, 2, 3)],
            "task_assignee": [entry("task_assignee", task, "mc://ops/tasks/TASK-9@g-1",
                                    "row.assignee", "alice smith")],
            "task_at": [entry("task_at", task, "mc://ops/tasks/TASK-9@g-1", "row.updatedAt",
                              "2026-10-04T09:00:00Z")]}
        mails = [entry("mail_sender", email_id(MAIL["M1"]), handle(MAIL["M1"]), "row.from",
                       MAIL["M1"]["from"])]
        evidence["mail_sender"] = mails

        def resolver(cite, actor):
            row = rows.get(cite) or (dict(MAIL["M1"]) if cite == handle(MAIL["M1"]) else None)
            return None if row is None else {"row": row}
        args = {"records": [
            {"label": "teams_channel", "time": ["channel_at"], "people": [
                {"role": "author", "items": ["channel_from"], "identity": "teams_party"}]},
            {"label": "mail", "time": ["mail_sender"], "people": [
                {"role": "sender", "items": ["mail_sender"], "identity": "address"}]},
            {"label": "mc_task", "time": ["task_at"], "people": [
                {"role": "assignee", "items": ["task_assignee"], "identity": "mixed"}]}]}
        out = pg.people_run(args, evidence, [], items={}, name="people", as_of="2026-10-06",
                            resolver=resolver, actor=FULL, complete=True,
                            covers=lambda _: False, people=lambda: directory())
        persons = [e for e in out["entries"] if e["name"].startswith("people_")]
        self.assertEqual(len(persons), 1)   # Alice by object id and by address: one person
        [alice] = persons
        self.assertEqual(text(alice["value"]), "Alice Smith")   # her Teams display name
        self.assertEqual(alice["value"]["origin"], {"citation": "teams-channel://t/c/m1@g-1",
                                                    "field_path": "row.from"})
        self.assertEqual(set(alice["basis"]), {msgs[0], email_id(MAIL["M1"])})
        # The MC name alone matches her name: a lead, never a role of hers.
        self.assertTrue(any(s.startswith("lead: the name on mc_task") for s in alice["steps"]))
        self.assertNotIn(task, alice["basis"])
        self.assertEqual(out["withhold"], ["channel_at_2", "channel_from_2"])   # Vince wrote it
        self.assertNotIn("Vince", json.dumps(out["entries"]))
        self.assertNotIn("Bot", json.dumps(out["entries"]))

    def test_chat_participants_join_people_gated_per_chat_and_excluded_ones_withhold_it(self):
        """F18 with Teams chats projected: the participants of a chat a message naming the
        order was posted in join as people by Entra object id; a reader who cannot read the
        chat sees neither it nor its participants; a chat whose only other participant is
        excluded is withheld whole (no participant, no handle), its author's own message
        kept."""
        world = World(MAILS, teams=teams_records())
        self.addCleanup(world.close)
        group = f'TeamsChat:teams.chats:{{"chat_id":"{GROUP_CHAT}"}}'
        pair = f'TeamsChat:teams.chats:{{"chat_id":"{PAIR_CHAT}"}}'
        out = self.ask("F18", PO, WITH_TEAMS, world=world)
        people = self.people(out)
        erin = people["Erin Lee"]   # her directory display name, read beside her object id
        self.assertEqual(erin["basis"], [group])
        self.assertTrue(self.role(erin, "participant", "teams_chat_members"), erin)
        self.assertIn("identity verified: one Entra object id", erin["steps"][0])
        # Re-read from the chat's own citation for this reader, never listed as an entry.
        self.assertEqual(erin["value"]["origin"]["field_path"], "row.participants")
        self.assertNotIn("Erin", json.dumps([e for e in out["answer"]
                                             if not e["name"].startswith("people")]))
        self.assert_cited(out, erin)
        alice = people["Alice Smith"]   # the author is one person with her mail identity
        self.assertTrue(self.role(alice, "author", "teams_chat"))
        self.assertTrue(self.role(alice, "participant", "teams_chat_members"))
        self.assertIn(email_id(MAIL["M1"]), alice["basis"])
        self.assertTrue(self.role(people["Dan Doe"], "participant", "teams_chat_members"))
        bob = next(p for label, p in people.items() if label in ("Bob Brown", f"bob@{DOMAIN}"))
        self.assertTrue(self.role(bob, "author", "teams_chat"))
        self.assertFalse(self.role(bob, "participant", "teams_chat_members"))
        dumped = json.dumps(out)
        for needle in (pair, f"{quote(PAIR_CHAT, safe='')}/conversation", "Vince", VINCE):
            self.assertNotIn(needle, dumped)
        # A reader who may not read the chats: no chat, no participant, no edge through it.
        hidden = self.ask("F18", PO, FULL, world=world)
        self.assertNotIn("Erin Lee", self.people(hidden))
        for needle in (group, "teams-chat://", ERIN):
            self.assertNotIn(needle, json.dumps(hidden))

    def test_chat_participants_at_one_ten_sixty(self):
        """N participants of one reached chat: each one person, nobody twice or lost up to
        the listing cut (named), within budget."""
        chat = 'TeamsChat:teams.chats:{"chat_id":"19:c@thread.v2"}'
        cite = "teams-chat://19%3Ac%40thread.v2/conversation@g-1"
        for count in (1, 10, 60):
            members = [participant(oid("8")[:-4] + f"{n:04d}", f"Person {n:02d}")
                       for n in range(count)]
            entry = {"name": "chat_active_at", "kind": "property", "object_id": chat,
                     "value": "2026-10-04T10:00:00Z", "check": "passed",
                     "citation": {"l1_handle": cite, "field_path": "row.last_activity_at"}}
            args = {"records": [{"label": "teams_chat_members", "time": ["chat_active_at"],
                                 "people": [{"role": "participant",
                                             "reread": ["row.participants"],
                                             "identity": "teams_members"}]}]}
            started = time.monotonic()
            out = pg.people_run(args, {"chat_active_at": [entry]}, [], items={},
                                name="people", as_of="2026-10-06",
                                resolver=lambda c, a: {"row": {"participants": members}},
                                actor=FULL, complete=True, covers=lambda _: False,
                                people=lambda: directory())
            self.assertLess(time.monotonic() - started, 5, count)
            labels = [text(e["value"]) for e in out["entries"]
                      if e["name"].startswith("people_")]
            self.assertEqual(len(labels), len(set(labels)), count)
            self.assertEqual(len(labels), min(count, pg.MAX_PEOPLE), count)
            self.assertEqual("people_truncated" in out["reasons"], count > pg.MAX_PEOPLE)
            self.assertEqual(out["withhold"], [])

    def test_cardinality_one_ten_sixty_senders(self):
        """N senders of N mails naming the order: nobody lost or listed twice up to the cuts,
        the cuts named (40 mails read per step, 20 people listed), each call in budget."""
        for count in (1, 10, 60):
            boxes = [{"mailbox_id": oid("6")[:-4] + f"{n:04d}", "upn": f"p{n}@{DOMAIN}",
                      "identities": [f"p{n}@{DOMAIN}"]} for n in range(count)]
            rows = [mail(ALICE, f"N{n}", f"p{n}@{DOMAIN}", [f"alice@{DOMAIN}"],
                         f"PO100234 note {n}", f"2026-10-01T{n // 60:02d}:{n % 60:02d}:00Z")
                    for n in range(count)]
            world = World(rows, assignee=None)
            self.addCleanup(world.close)
            people = pg.PeopleDirectory(PeopleExclusion([VINCE]), [*MAILBOXES, *boxes],
                                        withheld=[VINCE])
            senders = {f"p{n}@{DOMAIN}" for n in range(count)}
            if count <= 10:
                out = self.ask("F18", PO, FULL, lambda: people, world=world)
                labels = [text(e["value"]) for e in out["answer"]
                          if e["name"].startswith("people_") and isinstance(e["value"], dict)]
                self.assertEqual(len(labels), len(set(labels)), count)   # nobody twice
                self.assertLessEqual(senders, set(labels), count)        # nobody lost
                self.assertEqual(len([e for e in out["answer"]
                                      if e["name"].startswith("mail_sender")]), count)
                self.assertEqual(set(labels) - senders, {f"alice@{DOMAIN}", tmc.OPERATOR})
                continue
            table = world.table(lambda: people)
            self.addCleanup(table["_reader"].conn.close)
            started = time.monotonic()
            out = table["plx.investigate"]["handler"]({"family": "F18", "root": PO}, FULL)
            self.assertLess(time.monotonic() - started, 120)   # one call, a loaded machine
            self.assertEqual(validate(out, OUTPUT, root=CONTRACTS), [])
            count_entry = next(e for e in out["answer"] if e["name"] == "people")
            self.assertEqual(count_entry["value"], pg.MAX_PEOPLE)   # every page states it
            self.assertEqual(len(set(count_entry["inputs"])), pg.MAX_PEOPLE)
            reasons = [g["reason"] for g in out["gaps"]]
            self.assertIn("people_truncated", reasons)
            self.assertIn("step_truncated", out["answerability"]["reasons"])
            self.assertFalse(out["completeness"]["complete"])

if __name__ == "__main__":
    unittest.main()
