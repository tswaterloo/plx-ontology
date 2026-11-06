"""The AI watcher (operating/ai_watcher.py): changes read off the delta layers, the batch,
the gate-and-screen of what the model raised, the truth check, dedup, the per-reader read
and the switch.

SYNTHETIC fixtures only: projection stores built with ``StoreBuilder`` from rows written
here (synthetic handles and values), delta layers marked as ``ontology.delta`` marks them,
a scripted agent run (no model, no host). Evidence label: isolated-fixture.
"""

from __future__ import annotations

from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ontology.store import StoreBuilder
from operating import ai_watcher as aw
from operating import briefing as bf
from operating import truth_check as tc

NOW = dt.datetime(2026, 10, 20, 12, tzinfo=dt.timezone.utc)
T0 = "2026-10-01T00:00:00.000000Z"
RECENT = "2026-10-20T09:00:00.000000Z"


def mail(n: int) -> str:
    return f"graph-mail://mbx-{n}/msg-{n}@v1"


def row(n: int) -> str:
    return f"api://public.fixture_rows/%7B%22id%22%3A{n}%7D@g{n}"


class Rows:
    """Rows of one synthetic store (records, one current version each, their values)."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {"objects": [], "object_versions": [],
                                            "object_values": []}

    def obj(self, oid: str, object_type: str, handle: str, values: dict, *,
            digest: str = "d", audience: str = "api:a", markings: str = "[]",
            recorded: str = T0, value_markings: dict | None = None) -> str:
        self.rows["objects"].append({
            "object_id": oid, "object_type": object_type, "binding_id": "fx",
            "native_key": json.dumps({"id": oid}), "audience_key": audience,
            "first_recorded_at": recorded})
        self.rows["object_versions"].append({
            "object_id": oid, "l1_handle": handle, "digest": digest, "lineage": "workbench",
            "markings": markings, "recorded_at": recorded})
        for prop, (kind, value) in values.items():
            self.rows["object_values"].append({
                "object_id": oid, "property": prop, "value_type": kind, "value": value,
                "unit": None, "quantity_role": None, "l1_handle": handle,
                "field_path": f"row.{prop}", "digest": digest, "lineage": "workbench",
                "binding_id": "fx", "parser": "fixture",
                "markings": (value_markings or {}).get(prop, markings),
                "recorded_at": recorded})
        return oid

    def build(self, path: Path) -> Path:
        builder = StoreBuilder(path)
        for table, rows in self.rows.items():
            builder.add(table, rows)
        builder.set_meta("audience_keys", sorted({r["audience_key"]
                                                   for r in self.rows["objects"]} or ["api:a"]))
        builder.commit(built_at=T0)
        return path


def make_layer(path: Path, rows: Rows, owned: list[str], served: Path) -> Path:
    """A delta layer as ``ontology.delta.finish_store`` leaves one: owned ids and the
    served generation's digests."""
    rows.build(path)
    with closing(sqlite3.connect(served)) as base:
        meta = dict(base.execute("SELECT key, value FROM store_meta WHERE key IN "
                                 "('content_digest', 'schema_digest')").fetchall())
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in owned])
        for key, value in meta.items():
            conn.execute("UPDATE store_meta SET value = ? WHERE key = ?", (value, key))
        conn.commit()
    return path


def publish(served: Path, layers: list[Path]) -> None:
    with closing(sqlite3.connect(served)) as base:
        digest = base.execute("SELECT value FROM store_meta WHERE key = 'content_digest'"
                              ).fetchone()[0]
    folder = served.parent / "delta"
    folder.mkdir(exist_ok=True)
    (folder / "CURRENT.json").write_text(json.dumps({
        "format": "plx-delta/v1", "base_digest": digest, "store": str(layers[0]),
        "owned": 1, "layers": [{"store": str(p), "owned": 1} for p in layers]}))


class Guard:
    """A people exclusion that excludes one mailbox; the tenant's own domain."""

    def known_domains(self):
        return frozenset({"own.example"})


def excluded(_guard, handle: str) -> bool:
    return "mbx-9" in handle


class World(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        (self.state / "projection").mkdir(parents=True)
        self.served = self.state / "projection" / "ontology.sqlite"
        self.watch = self.root / "watch"

    def base(self) -> Rows:
        rows = Rows()
        rows.obj("PurchaseOrder:fx:1", "PurchaseOrder", row(1),
                 {"status": ("text", "open"), "expected_date": ("date", "2026-11-01")},
                 digest="p1")
        rows.obj("PurchaseOrder:fx:2", "PurchaseOrder", row(2),
                 {"status": ("text", "open"), "portal_updated_at": ("text", "x")},
                 digest="p2")
        rows.obj("PurchaseOrder:fx:3", "PurchaseOrder", row(3), {"status": ("text", "open")},
                 digest="p3")
        return rows

    def layer(self) -> Rows:
        rows = Rows()
        # changed: a real field moved
        rows.obj("PurchaseOrder:fx:1", "PurchaseOrder", row(1) + "x",
                 {"status": ("text", "open"), "expected_date": ("date", "2026-11-20")},
                 digest="p1b", recorded=RECENT)
        # a restamp: only the write stamp moved
        rows.obj("PurchaseOrder:fx:2", "PurchaseOrder", row(2) + "x",
                 {"status": ("text", "open"), "portal_updated_at": ("text", "y")},
                 digest="p2b", recorded=RECENT)
        # unchanged digest, re-owned
        rows.obj("PurchaseOrder:fx:3", "PurchaseOrder", row(3), {"status": ("text", "open")},
                 digest="p3", recorded=RECENT)
        # a new message of today, another of its thread later, an attachment, an old
        # message captured now, and one in an excluded mailbox
        rows.obj("Email:fx:10", "Email", mail(10),
                 {"sent_at": ("timestamp", "2026-10-20T08:00:00Z"),
                  "conversation_id": ("text", "t1")}, digest="m10", recorded=RECENT)
        rows.obj("Email:fx:11", "Email", mail(11),
                 {"sent_at": ("timestamp", "2026-10-20T10:00:00Z"),
                  "conversation_id": ("text", "t1")}, digest="m11", recorded=RECENT)
        rows.obj("Attachment:fx:12", "Attachment", mail(11) + "#attachment=a",
                 {}, digest="a12", recorded=RECENT)
        rows.obj("Email:fx:13", "Email", mail(13),
                 {"sent_at": ("timestamp", "2025-01-01T08:00:00Z")}, digest="m13",
                 recorded=RECENT)
        rows.obj("Email:fx:19", "Email", "graph-mail://mbx-9/msg-19@v1",
                 {"sent_at": ("timestamp", "2026-10-20T09:00:00Z")}, digest="m19",
                 recorded=RECENT)
        return rows

    def world(self) -> None:
        self.base().build(self.served)
        layer = self.layer()
        path = make_layer(self.state / "projection" / "layer1.sqlite", layer,
                          [r["object_id"] for r in layer.rows["objects"]], self.served)
        publish(self.served, [path])


class DiscoverTest(World):
    def test_only_real_changes_are_queued(self) -> None:
        self.world()
        with closing(aw.open_store(self.watch)) as ws:
            found = aw.discover(ws, self.served, now=NOW, guard=Guard(), excluded=excluded)
            queue = {r["object_id"]: dict(r) for r in ws.execute("SELECT * FROM queue")}
            self.assertEqual(set(queue), {"PurchaseOrder:fx:1", "Email:fx:11"})
            self.assertEqual(json.loads(queue["PurchaseOrder:fx:1"]["changed"]),
                             ["expected_date"])
            self.assertEqual(queue["Email:fx:11"]["change"], "new")
            for reason in ("restamped", "unchanged", "part_of_message", "old_news",
                           "people_withheld", "same_thread"):
                self.assertEqual(found[reason], 1, reason)
            # a layer looked at once is not looked at again
            self.assertEqual(aw.discover(ws, self.served, now=NOW, guard=Guard(),
                                         excluded=excluded)["layers"], 0)

    def test_no_people_guard_reads_no_message(self) -> None:
        self.world()
        with closing(aw.open_store(self.watch)) as ws:
            found = aw.discover(ws, self.served, now=NOW, guard=None, excluded=excluded)
            queued = [r[0] for r in ws.execute("SELECT object_id FROM queue")]
        self.assertEqual(queued, ["PurchaseOrder:fx:1"])
        self.assertEqual(found["people_withheld"], 4)

    def test_another_generations_layers_are_ignored(self) -> None:
        self.world()
        pointer = self.state / "projection" / "delta" / "CURRENT.json"
        body = json.loads(pointer.read_text())
        pointer.write_text(json.dumps({**body, "base_digest": "sha256:other"}))
        with closing(aw.open_store(self.watch)) as ws:
            self.assertEqual(aw.discover(ws, self.served, now=NOW, guard=Guard(),
                                         excluded=excluded)["layers"], 0)


def queued(ws, oid: str, object_type: str, handle: str, change: str = "new",
           changed=(), event: str = "2026-10-20T11:00:00", traits=None) -> None:
    ws.execute("INSERT OR REPLACE INTO queue (object_id, object_type, handle, change, "
               "changed, event_at, since, thread, queued_at, traits) VALUES "
               "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (oid, object_type, handle, change, json.dumps(list(changed)), event,
                aw._iso(NOW - dt.timedelta(days=1)) if change == "changed" else None, None,
                aw._iso(NOW), json.dumps(traits) if traits is not None else None))


class MailTest(World):
    """Who writes: one copy of a message across mailboxes, who the company writes to, and
    the order messages are read in (made-up addresses)."""

    def test_copies_correspondents_and_the_reading_order(self) -> None:
        self.base().build(self.served)
        rows = Rows()

        def email(n, mbx, sender, *, key=None, to="", sent="2026-10-20T10:00:00Z"):
            values = {"sent_at": ("timestamp", sent), "sender": ("text", sender),
                      "to_addresses": ("text", to)}
            if key:
                values["internet_message_id"] = ("identifier", key)
            rows.obj(f"Email:fx:{n}", "Email", f"graph-mail://mbx-{mbx}/msg-{n}@v1",
                     values, digest=f"e{n}", recorded=RECENT)
        # a colleague wrote to the buyer last year (backlog, never queued, still learned)
        email(30, 1, "pat@own.example", to="buyer@customer.example",
              sent="2025-05-01T10:00:00Z")
        # the buyer's mail, delivered to two mailboxes: one copy is read
        email(31, 1, "buyer@customer.example", key="<m31@customer.example>",
              to="pat@own.example", sent="2026-10-20T09:00:00Z")
        email(32, 2, "buyer@customer.example", key="<m31@customer.example>",
              to="lee@own.example", sent="2026-10-20T09:00:00Z")
        # a stranger and a notifier, newer than the buyer's mail
        email(33, 1, "sales@pitch.example", to="pat@own.example")
        for n in range(40, 46):
            email(n, 1, "no-reply@alerts.example", to="pat@own.example",
                  sent=f"2026-10-20T10:{n}:00Z")
        layer = make_layer(self.state / "projection" / "mail.sqlite", rows,
                           [r["object_id"] for r in rows.rows["objects"]], self.served)
        publish(self.served, [layer])
        with closing(aw.open_store(self.watch)) as ws:
            found = aw.discover(ws, self.served, now=NOW, guard=Guard(), excluded=excluded,
                                sender_excluded=lambda g, a: False)
            self.assertEqual((found["copy"], found["old_news"]), (1, 1))
            stored = " ".join(str(tuple(r)) for r in ws.execute(
                "SELECT * FROM correspondents"))
            self.assertNotIn("customer.example", stored)   # digests, never addresses
            batch = aw.pick(ws, now=NOW, max_reads=6, message_bulk_min=4)
        self.assertEqual([(r["object_id"], r["tier"]) for r in batch["entries"]],
                         [("Email:fx:31", "correspondent"), ("Email:fx:33", "other")])
        [stream] = batch["bulk"]
        self.assertEqual((stream["messages"], stream["automated"], stream["count"]),
                         (True, True, 6))
        prompt = aw.task(batch, [], [], now=NOW, departments=["PROC"])
        self.assertIn("Email:fx:31: new, 2026-10-20T09:00 [from someone the company "
                      "writes to]", prompt)
        self.assertIn("6 Email messages from one automated sender", prompt)
        self.assertNotIn("customer.example", prompt)
        self.assertIn(("plx.read", {"citation": "graph-mail://mbx-1/msg-45@v1",
                                    "max_chars": aw.READ_CHARS}), aw.reads_for(batch))


    def test_an_excluded_colleagues_mail_teaches_nothing(self) -> None:
        """With the real people exclusion (made-up ids and addresses): what an excluded
        colleague sent, even as a copy in another mailbox, teaches no correspondent; a
        colleague's own mail does. Digests are salted per store, the store is 0600."""
        import os
        from integrations.people_exclusion import PeopleExclusion
        excluded_oid = "22222222-2222-4222-8222-222222222222"
        pat_oid = "33333333-3333-4333-8333-333333333333"
        guard = PeopleExclusion([excluded_oid])
        guard.attach_directory([(excluded_oid, "ex@own.example", []),
                                (pat_oid, "pat@own.example", [])])
        self.base().build(self.served)
        rows = Rows()
        for n, sender, to in ((50, "ex@own.example", "quiet@partner.example"),
                              (51, "pat@own.example", "buyer@customer.example")):
            rows.obj(f"Email:fx:{n}", "Email", f"graph-mail://{pat_oid}/msg-{n}@v1",
                     {"sent_at": ("timestamp", "2026-10-20T10:00:00Z"),
                      "sender": ("text", sender), "to_addresses": ("text", to)},
                     digest=f"e{n}", recorded=RECENT)
        layer = make_layer(self.state / "projection" / "mail.sqlite", rows,
                           [r["object_id"] for r in rows.rows["objects"]], self.served)
        publish(self.served, [layer])
        with closing(aw.open_store(self.watch)) as ws:
            aw.discover(ws, self.served, now=NOW, guard=guard)
            salt = aw.key_salt(ws)
            learned = {r[0] for r in ws.execute("SELECT key FROM correspondents")}
        self.assertEqual(learned, {aw._key("buyer@customer.example", salt),
                                   aw._key("@customer.example", salt)})
        # a store that has to learn again (a new salt) learns the layers it already saw
        with closing(aw.open_store(self.watch)) as ws:
            ws.execute("DELETE FROM correspondents")
            ws.execute("DELETE FROM learned")
            self.assertEqual(aw.discover(ws, self.served, now=NOW, guard=guard)["layers"], 0)
            self.assertEqual({r[0] for r in ws.execute("SELECT key FROM correspondents")},
                             learned)
        self.assertNotIn(aw._key("quiet@partner.example", salt), learned)
        self.assertNotEqual(aw._key("buyer@customer.example", salt),
                            aw._key("buyer@customer.example", b"another store"))
        self.assertEqual(os.stat(aw.store_path(self.watch)).st_mode & 0o777, 0o600)


class PickTest(unittest.TestCase):
    def test_within_a_tier_the_message_that_ages_out_first_is_read_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, closing(aw.open_store(Path(tmp))) as ws:
            ws.execute("INSERT INTO correspondents VALUES ('k1', 1, 'x')")
            for n, at in ((1, "2026-10-20T11:00:00"), (2, "2026-10-19T13:00:00"),
                          (3, "2026-10-20T11:30:00")):
                queued(ws, f"Email:fx:{n}", "Email", mail(n), event=at,
                       traits={"sender": f"s{n}", "parties": ["k1"] if n < 3 else ["k9"]})
            batch = aw.pick(ws, now=NOW, max_reads=12)
        self.assertEqual([r["object_id"] for r in batch["entries"]],
                         ["Email:fx:2", "Email:fx:1", "Email:fx:3"])

    def test_bulk_runs_are_one_line_and_the_budget_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, closing(aw.open_store(Path(tmp))) as ws:
            for n in range(30):   # one recompute: same type, same properties
                queued(ws, f"Aged:fx:{n}", "Aged", row(100 + n), "changed", ["bucket"])
            for n in range(10):
                queued(ws, f"Email:fx:{n}", "Email", mail(n))
            queued(ws, "PurchaseOrder:fx:1", "PurchaseOrder", row(1), "changed", ["date"])
            queued(ws, "RelRaw:fx:1", "RelRaw", row(2))
            queued(ws, "Old:fx:1", "Old", row(3), event="2026-10-01T00:00:00")
            batch = aw.pick(ws, now=NOW, max_reads=12, bulk_min=12,
                            origin={"RelRaw": "generated"})
        self.assertEqual([(g["object_type"], g["count"]) for g in batch["bulk"]],
                         [("Aged", 30)])
        types = [r["object_type"] for r in batch["entries"]]
        self.assertIn("PurchaseOrder", types)
        self.assertIn("RelRaw", types)
        self.assertLessEqual(sum(aw._reads(r) for r in batch["entries"])
                             + len(batch["bulk"]), 12)
        self.assertEqual(batch["aged_out"], 1)
        reads = aw.reads_for(batch)
        self.assertIn(("plx.read", {"citation": mail(0), "max_chars": aw.READ_CHARS}), reads)
        self.assertTrue(any(name == "plx.object" and args.get("as_known_at")
                            for name, args in reads))


class FakeRun:
    def __init__(self, events, final_text) -> None:
        self.events, self.final_text = events, final_text
        self.usage = {"subtype": "success"}
        self.agent = {"answer_gate": {"outcomes": []}}


def gate_event(question: str, text: str, claims, outcome: str = "answered") -> dict:
    return {"type": "tool_call", "name": "plx.case_get",
            "arguments": {"answer": {"question": question, "text": text, "claims": []}},
            "result": {"outcome": outcome, "claims": claims, "unsupported_claims": []}}


def claim(cid: str, handle: str, field: str, value: str) -> dict:
    return {"id": cid, "citations": [handle], "text": f"{field}: {value}",
            "value_claims": [{"citation": handle, "field": f"row.{field}", "value": value}]}


class ProposalTest(unittest.TestCase):
    def test_an_item_stands_only_on_what_the_gate_passed(self) -> None:
        passed = [claim("c1", row(1), "number", "PO-77"),
                  claim("c2", mail(5), "sent_at", "2026-10-20")]
        events = [gate_event("PO-77 slips", "PO-77 was sent on 2026-10-20. It now arrives "
                             "on 2026-12-03.", passed),
                  gate_event("Nothing backed", "x", [], outcome="abstained")]
        final = "done\n```json\n" + json.dumps({"items": [
            {"title": "PO-77 slips", "subject": "PurchaseOrder:fx:1",
             "why": "PO-77 now arrives after 2026-11-30.", "departments": ["PROC", "NOPE"],
             "priority": "urgent", "rests_on": "order_open"},
            {"title": "Nothing backed", "why": "it matters"},
            {"title": "Never submitted"}]}) + "\n```"
        items = {i["title"]: i for i in aw.proposals(FakeRun(events, final),
                                                     departments={"PROC", "OPS"})}
        item = items["PO-77 slips"]
        self.assertEqual(item["gate"], "passed")
        self.assertEqual(item["groups"], [[row(1)], [mail(5)]])
        self.assertEqual(item["departments"], ["PROC"])
        self.assertEqual(item["priority"], "medium")
        # a date no claim states is screened out, with its sentence
        self.assertEqual(item["text"], "PO-77 was sent on 2026-10-20.")
        self.assertNotIn("2026-11-30", item["why"])
        self.assertEqual(items["Nothing backed"]["gate"], "not_passed:abstained")
        self.assertEqual(items["Never submitted"]["gate"], "not_submitted")

    def test_a_reason_stands_on_the_cited_lines_and_nothing_else(self) -> None:
        quote = "Can we add 2 test cases to our shipment friday"
        passed = [{"id": "c1", "citations": [mail(5)], "text": f"body text: {quote}",
                   "value_claims": [{"citation": mail(5), "field": "row.body_text",
                                     "quote": quote, "value": quote}]},
                  claim("c2", mail(5), "from", "buyer@customer.example")]
        why = ("The customer asked to add 2 test cases to the shipment, so it must be "
               "settled before it leaves Friday. It is worth 9000 USD. Pat must approve it. "
               "They did not ask for a quote.")
        self.assertEqual(aw.screened(why, passed),
                         "The customer asked to add 2 test cases to the shipment, so it must "
                         "be settled before it leaves Friday.")
        self.assertEqual(aw.screened("It is worth 9000 USD.", passed), "")
        # whole numbers with their currency, by the Ask's own rule: "$1,200" is neither
        # the cited "$1,450" nor the cited "200"
        price = "Price is $1,450 per lot, 200 lots, ship by 2026-12-03."
        quoted = [{"id": "c1", "citations": [mail(6)], "text": f"body text: {price}",
                   "value_claims": [{"citation": mail(6), "field": "row.body_text",
                                     "quote": price, "value": price}]}]
        for said, kept in (("They quoted $1,450 per lot.", True),
                           ("They quoted $1,200 per lot.", False),
                           ("They quoted EUR 1,450 per lot.", False),
                           ("They want 200 lots by 2026-12-03.", True),
                           ("They want it by 2026-12-05.", False)):
            self.assertEqual(bool(aw.screened(said, quoted)), kept, said)
        # the system a cited record is in is the citation's own
        ledger = claim("c3", "api://ledger.errors/%7B%22id%22%3A1%7D@g1", "errdesc", "closed")
        self.assertEqual(aw.screened("Ledger refused it.", [ledger]), "Ledger refused it.")
        self.assertEqual(aw.screened("Ledger refused 77 rows.", [ledger]), "")
        # a suggestion may name who acts; a figure it adds is still screened out
        self.assertEqual(aw.screened("Pat should confirm it. Bill 9000 USD.", passed,
                                     suggestion=True), "Pat should confirm it.")

    def test_an_item_whose_own_words_the_claims_do_not_carry_is_refused(self) -> None:
        """Passed fragments (a number, a status) under a statement none of whose sentences
        they carry: the item is not shown on them; it gets its own gate pass instead."""
        passed = [claim("c1", row(1), "number", "AO-7"), claim("c2", row(1), "status", "open")]
        events = [gate_event("AO-7 quantities disagree",
                             "AO-7 is planned at 2500 in one system and 2077 in the other.",
                             passed)]
        final = "```json\n" + json.dumps({"items": [{
            "title": "AO-7 quantities disagree", "why": "Planning works from 2077."}]}) + "\n```"
        [item] = aw.proposals(FakeRun(events, final), departments=set())
        self.assertEqual(item["gate"], "not_passed:statement_unsupported")
        self.assertIn("statement_unsupported", item["refusals"])
        self.assertIn("retry", item)

    def test_a_date_with_an_abbreviated_month_is_one_sentence(self) -> None:
        self.assertEqual(aw._sentences("The holiday runs Jan. 30th to Feb. 14th. Order now."),
                         ["The holiday runs Jan. 30th to Feb. 14th.", "Order now."])

    def test_a_session_cut_short_keeps_what_its_gate_passed(self) -> None:
        events = [gate_event("Hold on batch", "The batch is held.",
                             [claim("c1", row(4), "status", "held")])]
        items = aw.proposals(FakeRun(events, ""), departments=set())
        self.assertEqual([(i["title"], i["gate"]) for i in items],
                         [("Hold on batch", "passed")])


class FakePlaces:
    def __init__(self, objects, places) -> None:
        self.objects, self.places = objects, places

    def object_of(self, handle):
        return self.objects.get(handle)

    def place(self, oid):
        return self.places.get(oid)

    def markings_of(self, handle, oid, fields):
        return self.places[oid]["markings"]


def portal_po(oid_id: str, number: str, **row_fields) -> dict:
    return {"row": {"id": oid_id, "po_number": number, "status": "open", **row_fields},
            "citation": f"api://public.app_purchase_order/{oid_id}@g-1"}


class JudgeTest(unittest.TestCase):
    PO = 'PurchaseOrder:portal.app_purchase_order:{"id": "u1"}'

    def item(self, **more) -> dict:
        return {"title": "t", "subject": self.PO, "text": "x", "why": "", "next_step": "",
                "departments": [], "priority": "medium", "claim_kind": "other",
                "gate": "passed", "claims": [], "groups": [[row(1)]],
                "refused_claims": 0, **more}

    def judge(self, items, *, rows=(), places=None, inbox=Path("/nonexistent")):
        src = tc.Sources(portal={tc.PO: list(rows)}, bc=[], as_of=NOW.date())
        places = places or FakePlaces({row(1): self.PO},
                                      {self.PO: {"object_type": "PurchaseOrder",
                                                 "audience_key": "api:a", "markings": "[]"}})
        return aw.judge(items, places=places, inbox=inbox, sources=lambda objects: src,
                        guard=Guard(), excluded=excluded)

    def test_truth_check_refuses_a_test_record(self) -> None:
        [item] = self.judge([self.item()], rows=[portal_po("u1", "PO-1",
                                                           note="INVOICETEST#1")])
        self.assertEqual((item["state"], item["reason"]), ("refused", "truth_contradicted"))
        self.assertEqual(item["truth"]["verdict"], "contradicted")

    def test_an_open_order_claim_passes_when_nothing_contradicts_it(self) -> None:
        [item] = self.judge([self.item(claim_kind="order_open")],
                            rows=[portal_po("u1", "PO-1")])
        self.assertEqual(item["state"], "open")
        self.assertEqual(item["markings"], {row(1): "[]"})

    def test_a_check_with_nothing_to_test_says_so(self) -> None:
        [item] = self.judge([self.item(claim_kind="customer_order_at_risk",
                                       subject="Email:fx:1")],
                            places=FakePlaces({row(1): "Email:fx:1"}, {"Email:fx:1": {
                                "object_type": "Email", "audience_key": "m",
                                "markings": "[]"}}))
        self.assertEqual((item["state"], item["truth"]["verdict"]), ("open", "not_checkable"))

    def test_an_unserved_or_excluded_citation_refuses_the_item(self) -> None:
        [unserved] = self.judge([self.item(groups=[[row(1), row(2)]])])
        self.assertEqual(unserved["reason"], "cite_not_served")
        hidden = "graph-mail://mbx-9/msg-1@v1"
        places = FakePlaces({row(1): self.PO, hidden: "Email:fx:9"}, {
            self.PO: {"object_type": "PurchaseOrder", "audience_key": "a", "markings": "[]"},
            "Email:fx:9": {"object_type": "Email", "audience_key": "m", "markings": "[]"}})
        [people] = self.judge([self.item(groups=[[row(1)], [hidden]])], places=places)
        self.assertEqual(people["reason"], "people_withheld")
        [gate] = self.judge([self.item(gate="not_passed:repair")])
        self.assertEqual((gate["state"], gate["reason"]), ("refused", "not_passed:repair"))

    def test_an_open_detector_event_with_the_same_evidence_is_a_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox.sqlite"
            with closing(sqlite3.connect(inbox)) as conn:
                conn.executescript(
                    "CREATE TABLE gaps (gap_id TEXT, object_id TEXT, state TEXT, kind TEXT);"
                    "CREATE TABLE gap_cites (gap_id TEXT, handle TEXT, object_id TEXT);")
                conn.execute("INSERT INTO gaps VALUES ('g', ?, 'open', 'late_po')", (self.PO,))
                conn.execute("INSERT INTO gap_cites VALUES ('g', ?, ?)", (row(1), self.PO))
                conn.commit()
            [item] = self.judge([self.item()], rows=[portal_po("u1", "PO-1")], inbox=inbox)
        self.assertEqual((item["state"], item["reason"]), ("duplicate", "detector_event"))


def admit(handles):
    return {h: None for h in handles}


class ReadTest(World):
    """An item is shown only to a reader who may open its subject and a citation of every
    evidence group, at markings it holds, with the host's per-item decision."""

    def setUp(self) -> None:
        super().setUp()
        rows = Rows()
        rows.obj("PurchaseOrder:fx:1", "PurchaseOrder", row(1), {}, audience="api:a")
        rows.obj("Email:fx:5", "Email", mail(5), {}, audience="mail:m5")
        rows.obj("Invoice:fx:6", "Invoice", row(6), {}, audience="api:a",
                 markings='["finance"]')
        rows.build(self.served)
        items = [{
            "title": "t", "subject": "PurchaseOrder:fx:1", "subject_type": "PurchaseOrder",
            "text": "PO moved", "why": "", "next_step": "", "departments": ["PROC"],
            "priority": "high", "claim_kind": "other", "gate": "passed", "claims": [],
            "groups": [[row(1)], [mail(5)]], "refused_claims": 0, "state": "open",
            "objects": {row(1): "PurchaseOrder:fx:1", mail(5): "Email:fx:5"},
            "markings": {row(1): "[]", mail(5): "[]"}}, {
            "title": "money", "subject": "PurchaseOrder:fx:1", "subject_type": "x",
            "text": "money", "why": "", "next_step": "", "departments": [],
            "priority": "low", "claim_kind": "other", "gate": "passed", "claims": [],
            "groups": [[row(6)]], "refused_claims": 0, "state": "open",
            "objects": {row(6): "Invoice:fx:6"}, "markings": {row(6): '["finance"]'}}, {
            # one claim computed from two records, one of them finance-marked
            "title": "computed", "subject": "PurchaseOrder:fx:1", "subject_type": "x",
            "text": "computed", "why": "", "next_step": "", "departments": [],
            "priority": "low", "claim_kind": "other", "gate": "passed", "claims": [],
            "groups": [[row(1), row(6)]], "refused_claims": 0, "state": "open",
            "objects": {row(1): "PurchaseOrder:fx:1", row(6): "Invoice:fx:6"},
            "markings": {row(1): "[]", row(6): '["finance"]'}}]
        with closing(aw.open_store(self.watch)) as ws:
            aw.fold(ws, items, run_id=1, now=NOW)

    def shown(self, may_read=lambda key: True, held=("finance",), decide=admit):
        return sorted(i["summary"] for i in aw.read_items(
            self.watch, self.served, may_read=may_read, held=held, decide=decide))

    def test_every_record_of_every_claim_must_open_for_the_reader(self) -> None:
        self.assertEqual(self.shown(), ["PO moved", "computed", "money"])
        self.assertEqual(self.shown(may_read=lambda key: key == "api:a"),
                         ["computed", "money"])
        # a claim computed from an unmarked and a finance-marked record is the finance one's
        self.assertEqual(self.shown(held=()), ["PO moved"])
        self.assertEqual(self.shown(decide=lambda hs: {h: ("excluded" if h == mail(5)
                                                           else None) for h in hs}),
                         ["computed", "money"])
        self.assertEqual(self.shown(may_read=lambda key: key != "api:a"), [])

    def test_a_marked_value_of_an_unmarked_record_needs_its_marking(self) -> None:
        """A claim reading a finance-marked cost of an order whose version is unmarked is
        stored at finance; one reading its status is not."""
        rows = Rows()
        rows.obj("SalesOrder:fx:9", "SalesOrder", row(9),
                 {"status": ("text", "open"), "unit_cost": ("text", "41.45")},
                 value_markings={"unit_cost": '["finance"]'})
        served = self.root / "other" / "projection" / "ontology.sqlite"
        served.parent.mkdir(parents=True)
        rows.build(served)
        places = aw.Places(served)
        try:
            self.assertEqual(places.markings_of(row(9), "SalesOrder:fx:9", ["row.unit_cost"]),
                             '["finance"]')
            self.assertEqual(places.markings_of(row(9), "SalesOrder:fx:9", ["row.status"]),
                             "[]")
            # a field no value holds (a message's text): every value's, fail closed
            self.assertEqual(places.markings_of(row(9), "SalesOrder:fx:9", None),
                             '["finance"]')
        finally:
            places.close()
        passed = {"id": "c1", "citations": [row(9)], "text": "unit_cost: 41.45",
                  "value_claims": [{"citation": row(9), "field": "row.unit_cost",
                                    "value": "41.45"}]}
        events = [gate_event("Cost", "The cost moved.", [passed])]
        [item] = aw.proposals(FakeRun(events, ""), departments=set())
        places = aw.Places(served)
        try:
            [judged] = aw.judge([item], places=places, inbox=Path("/nonexistent"),
                                sources=lambda o: tc.Sources(portal={}, bc=[],
                                                             as_of=NOW.date()),
                                guard=Guard(), excluded=excluded)
        finally:
            places.close()
        self.assertEqual(judged["markings"], {row(9): '["finance"]'})

    def test_the_briefing_lists_items_only_while_the_switch_is_on(self) -> None:
        found = {"status": "read", "gaps": [], "detectors": {}}
        grant = {"may_read": lambda key: True, "held": ("finance",), "decide": admit}
        aw.set_switch(self.state, "dry")
        self.assertEqual(aw.merge_briefing(found, self.served, **grant)["gaps"], [])
        aw.set_switch(self.state, "on")
        merged = aw.merge_briefing(found, self.served, **grant)
        self.assertEqual(len(merged["gaps"]), 0)   # its store is the state's own, empty
        with closing(aw.open_store(aw.watch_dir(self.state))) as ws, \
                closing(aw.open_store(self.watch)) as src:
            for table in ("items", "item_cites"):
                rows = [tuple(r) for r in src.execute(f"SELECT * FROM {table}")]
                marks = ",".join("?" * len(rows[0]))
                ws.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
        brief = bf.build(aw.merge_briefing(found, self.served, **grant),
                         routing=bf.Routing(departments=frozenset({"PROC"})), hidden=None,
                         last_visit=None)
        self.assertEqual([i["event_type"] for i in brief["items"]], ["watcher_attention"])
        self.assertEqual(brief["items"][0]["summary"], "PO moved")

    def test_a_reader_dismisses_or_snoozes_an_item_for_themselves_only(self) -> None:
        grant = {"may_read": lambda key: True, "held": ("finance",), "decide": admit}
        ids = {i["summary"]: i["id"] for i in aw.read_items(self.watch, self.served, **grant)}
        moved = aw.set_item_state(self.served, ids["PO moved"], reader="r1", state="dismissed",
                                  directory=self.watch, clock=lambda: NOW, **grant)
        self.assertEqual(moved["state"], "dismissed")
        snooze = aw.set_item_state(self.served, ids["money"], reader="r1", state="snoozed",
                                   directory=self.watch, clock=lambda: NOW, **grant)
        self.assertEqual(snooze["until"], "2026-10-27")

        def shown(reader, today=NOW.date()):
            return sorted(i["summary"] for i in aw.read_items(
                self.watch, self.served, reader=reader, today=today, **grant))
        self.assertEqual(shown("r1"), ["computed"])
        self.assertEqual(shown("r2"), ["PO moved", "computed", "money"])  # another reader's
        self.assertEqual(shown("r1", dt.date(2026, 10, 28)), ["computed", "money"])  # ended
        aw.set_item_state(self.served, ids["PO moved"], reader="r1", state="open",
                          directory=self.watch, clock=lambda: NOW, **grant)
        self.assertEqual(shown("r1"), ["PO moved", "computed"])
        # an item the reader may not see is refused as if it did not exist; a bad day too
        with self.assertRaises(KeyError):
            aw.set_item_state(self.served, ids["money"], reader="r3", state="dismissed",
                              directory=self.watch, may_read=lambda key: True, held=(),
                              decide=admit)
        with self.assertRaises(KeyError):
            aw.set_item_state(self.served, "aw-none", reader="r1", state="dismissed",
                              directory=self.watch, **grant)
        with self.assertRaises(ValueError):
            aw.set_item_state(self.served, ids["money"], reader="r1", state="snoozed",
                              until="2026-10-01", directory=self.watch, clock=lambda: NOW,
                              **grant)
        # with the switch off no briefing lists it: nothing to dismiss
        with self.assertRaises(KeyError):
            aw.set_item_state(self.served, ids["money"], reader="r1", state="dismissed",
                              **grant)

    def test_switch_is_off_unless_set(self) -> None:
        self.assertEqual(aw.switch(self.state), "off")
        with self.assertRaises(ValueError):
            aw.set_switch(self.state, "maybe")


class FakeAgent:
    """A scripted session: records the task and reads it was given."""

    def __init__(self, *runs) -> None:
        self.runs, self.calls = list(runs), []

    def run(self, prompt, host):
        self.calls.append((prompt, list(self.first_reads)))
        return self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]


class RetryTest(World):
    def test_a_refused_item_gets_its_own_gate_pass(self) -> None:
        """The session's one repair spent, a later item the gate refused is decided again
        in its own small session; one refused again stays refused."""
        self.world()
        refused = {"type": "tool_call", "name": "plx.case_get", "arguments": {"answer": {
            "question": "Delay notice", "text": "Delay.",
            "claims": [{"citation": mail(11), "field": "row.subject"}]}},
            "result": {"outcome": "abstained", "claims": [], "unsupported_claims": [
                {"reason": "cites_what_this_run_did_not_return"}]}}
        first = FakeRun([refused], "")
        again = FakeRun([gate_event("Delay notice", "Delay.",
                                    [claim("c1", mail(11), "subject", "Delay")])],
                        "```json\n" + json.dumps({"items": [{
                            "title": "Delay notice", "subject": "Email:fx:11",
                            "why": "Delay."}]}) + "\n```")
        agent = FakeAgent(first, again)
        summary = aw.cycle(self.state, directory=self.watch, host=None, agent=agent, now=NOW,
                           guard=Guard(), excluded=excluded, max_sessions_per_hour=5,
                           sources=lambda objects: tc.Sources(portal={}, bc=[],
                                                              as_of=NOW.date()))
        self.assertEqual((summary["retried"], summary["recovered"]), (1, 1))
        prompt, reads = agent.calls[1]
        self.assertIn("cites_what_this_run_did_not_return", prompt)
        self.assertIn(mail(11), prompt)
        self.assertEqual(reads, [])
        [item] = summary["items"]
        self.assertEqual((item["state"], item["gate"], item.get("retried")),
                         ("open", "passed", True))
        # refused on its own pass too: the item stays refused, nothing else is tried
        with closing(aw.open_store(self.watch)) as ws:
            queued(ws, "Email:fx:11", "Email", mail(11))
        agent = FakeAgent(first, FakeRun([refused], ""))
        summary = aw.cycle(self.state, directory=self.watch, host=None, agent=agent, now=NOW,
                           guard=Guard(), excluded=excluded, max_sessions_per_hour=5,
                           sources=lambda objects: tc.Sources(portal={}, bc=[],
                                                              as_of=NOW.date()))
        self.assertEqual((summary["retried"], summary["recovered"]), (1, 0))
        self.assertEqual(summary["items"][0]["state"], "refused")
        self.assertEqual(len(agent.calls), 2)


class ReadOnlyTest(unittest.TestCase):
    def test_the_watcher_session_can_only_read(self) -> None:
        """Dry or on, the watcher's sessions get the read-only tools and the answer gate
        only: never a case writer, a proposal, an approval or a send."""
        from operating import agent_session
        for agent in (aw.claude_agent("opus"), aw.claude_agent(
                "opus", budget=aw.RETRY_BUDGET_SECONDS, timeout=aw.RETRY_TIMEOUT_SECONDS)):
            allowed, _refused = agent_session.allowed_tools(
                [{"name": "plx.read", "read_only": True},
                 {"name": "plx.case_get", "read_only": True},
                 {"name": "plx.case_propose"}, {"name": "plx.case_add_finding"},
                 {"name": "plx.act"}, {"name": "plx.send_mail"}, {"name": "plx.approve"}],
                case_tools=agent.case_tools, proposals=agent.proposals)
            self.assertEqual(allowed, ["plx.read", "plx.case_get"])


class IsolatedStateTest(unittest.TestCase):
    def test_a_dry_run_state_writes_nothing_of_the_real_one(self) -> None:
        """Small files and stores are copies, a large rollback store a hard link (what is
        kept beside it lands beside the link), a WAL store and a large subtree symbolic
        links; the watcher's own directory is not carried; the real state is unchanged."""
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            real, scratch = Path(tmp) / "real", Path(tmp) / "scratch"
            (real / "projection").mkdir(parents=True)
            (real / "ai-watcher").mkdir()
            (real / "ai-watcher" / "switch.json").write_text('{"state": "on"}')
            (real / "label.json").write_text("{}")
            big = real / "projection" / "ontology.sqlite"
            with closing(sqlite3.connect(big)) as conn:
                conn.execute("CREATE TABLE t (v)")
                conn.executemany("INSERT INTO t VALUES (?)", [("x" * 500,)] * 50)
                conn.commit()
            with closing(sqlite3.connect(real / "projection" / "log.sqlite")) as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("CREATE TABLE t (v)")
                conn.executemany("INSERT INTO t VALUES (?)", [("y" * 500,)] * 50)
                conn.commit()
            with closing(sqlite3.connect(real / "small.sqlite")) as conn:
                conn.execute("CREATE TABLE t (v)")
                conn.commit()
            (real / "blobs" / "ab").mkdir(parents=True)
            for n in range(5):
                (real / "blobs" / "ab" / f"{n}").write_text("blob")
            with mock.patch.object(aw, "ISOLATE_DIR_ENTRIES", 4), \
                    mock.patch.object(aw, "ISOLATE_COPY_SMALL", 1024):
                aw.isolated_state(real, scratch, copy_bytes=8192)
            linked = scratch / "projection" / "ontology.sqlite"
            self.assertFalse(linked.is_symlink())
            self.assertEqual(os.stat(linked).st_ino, os.stat(big).st_ino)
            self.assertTrue((scratch / "projection" / "log.sqlite").is_symlink())
            self.assertTrue((scratch / "blobs").is_symlink())
            self.assertFalse((scratch / "ai-watcher").exists())
            with closing(sqlite3.connect(scratch / "small.sqlite")) as conn:
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()
            (scratch / "label.json").write_text('{"changed": true}')
            with closing(sqlite3.connect(real / "small.sqlite")) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM t").fetchone()[0], 0)
            self.assertEqual((real / "label.json").read_text(), "{}")
            with self.assertRaises(ValueError):
                aw.isolated_state(real, real / "inside")


class Killed(BaseException):
    """A run killed mid-way (a unit timeout, a restart): nothing in the run catches it."""


class KilledAgent(FakeAgent):
    def run(self, prompt, host):
        self.calls.append((prompt, list(self.first_reads)))
        raise Killed()


class SessionTest(World):
    """The cap counts sessions as they start, retries included; a killed run is resumed,
    never replayed whole again and again."""

    def cycle(self, agent, **more):
        more.setdefault("sources", lambda objects: tc.Sources(portal={}, bc=[],
                                                              as_of=NOW.date()))
        return aw.cycle(self.state, directory=self.watch, host=None, agent=agent, now=NOW,
                        guard=Guard(), excluded=excluded, **more)

    def refused_then(self):
        refused = {"type": "tool_call", "name": "plx.case_get", "arguments": {"answer": {
            "question": "Delay notice", "text": "Delay.",
            "claims": [{"citation": mail(11), "field": "row.subject"}]}},
            "result": {"outcome": "abstained", "claims": [], "unsupported_claims": []}}
        return FakeRun([refused], "")

    def test_a_retry_is_a_session_the_cap_counts(self) -> None:
        self.world()
        agent = FakeAgent(self.refused_then())
        summary = self.cycle(agent, max_sessions_per_hour=1)
        self.assertEqual((summary["sessions"], summary["retried"],
                          summary["retries_deferred"]), (1, 0, 1))
        self.assertEqual(len(agent.calls), 1)
        with closing(aw.open_store(self.watch)) as ws:
            self.assertEqual(aw.sessions_last_hour(ws, NOW), 1)

    def test_a_session_starts_only_if_it_ends_within_the_run(self) -> None:
        self.world()
        clock = iter([0.0, 0.0])   # the run starts, its batch fits; later: 900 s on

        class Slow(FakeAgent):
            timeout = 600
        agent = Slow(self.refused_then())
        summary = self.cycle(agent, max_sessions_per_hour=9, run_seconds=1000,
                             monotonic=lambda: next(clock, 900.0))
        # the batch fit (0 + 600 <= 1000); its retry would not (900 + 600 > 1000)
        self.assertEqual((summary["sessions"], summary["retries_deferred"]), (1, 1))

    def test_a_run_killed_in_its_session_gives_the_batch_one_more_attempt(self) -> None:
        self.world()
        with self.assertRaises(Killed):
            self.cycle(KilledAgent(FakeRun([], "")), max_sessions_per_hour=9)
        with self.assertRaises(Killed):
            self.cycle(KilledAgent(FakeRun([], "")), max_sessions_per_hour=9)
        agent = FakeAgent(FakeRun([], ""))
        summary = self.cycle(agent, max_sessions_per_hour=9)
        self.assertEqual(summary["recovered"], {"interrupted": 1, "abandoned": 2})
        self.assertEqual(summary["status"], "nothing_new")
        self.assertEqual(agent.calls, [])   # never a third session for the same batch
        with closing(aw.open_store(self.watch)) as ws:
            self.assertEqual(aw.sessions_last_hour(ws, NOW), 2)   # both killed ones count

    def test_a_run_killed_after_its_session_is_folded_without_a_model_call(self) -> None:
        self.world()
        answered = FakeRun([gate_event("Delay notice", "Delay.",
                                       [claim("c1", mail(11), "subject", "Delay")])], "")

        def killed(objects):
            raise Killed()
        with self.assertRaises(Killed):
            self.cycle(FakeAgent(answered), max_sessions_per_hour=9, sources=killed)
        agent = KilledAgent(FakeRun([], ""))
        summary = self.cycle(agent, max_sessions_per_hour=9)
        self.assertEqual(summary["resumed"], 1)
        self.assertEqual([i["state"] for i in summary["items"]], ["open"])
        self.assertEqual(agent.calls, [])
        with closing(aw.open_store(self.watch)) as ws:
            self.assertEqual(ws.execute("SELECT count(*) FROM queue").fetchone()[0], 0)
            self.assertEqual([r[0] for r in ws.execute("SELECT state FROM sessions")],
                             ["folded"])


class CycleTest(World):
    def cycle(self, agent, **more):
        return aw.cycle(self.state, directory=self.watch, host=None, agent=agent, now=NOW,
                        guard=Guard(), excluded=excluded, sources=lambda objects: tc.Sources(
                            portal={}, bc=[], as_of=NOW.date()), **more)

    def test_a_run_raises_dedups_and_respects_its_caps(self) -> None:
        self.world()
        passed = [claim("c1", mail(11), "subject", "Delay")]
        final = "```json\n" + json.dumps({"items": [{"title": "Delay notice",
                                                       "subject": "Email:fx:11"}]}) + "\n```"
        agent = FakeAgent(FakeRun([gate_event("Delay notice", "Delay.", passed)], final))
        first = self.cycle(agent, max_sessions_per_hour=5)
        self.assertEqual(first["status"], "ran")
        prompt, reads = agent.calls[0]
        self.assertIn(aw.INSTRUCTION, prompt)
        self.assertIn(("plx.read", {"citation": mail(11), "max_chars": aw.READ_CHARS}), reads)
        self.assertEqual(first["folded"].get("open"), 1)
        # nothing new: no model call
        second = self.cycle(agent, max_sessions_per_hour=5)
        self.assertEqual(second["status"], "nothing_new")
        self.assertEqual(len(agent.calls), 1)
        # the same item raised again refreshes the open one; the cap stops the third run
        with closing(aw.open_store(self.watch)) as ws:
            queued(ws, "Email:fx:11", "Email", mail(11))
        third = self.cycle(agent, max_sessions_per_hour=5)
        self.assertEqual(third["folded"].get("refreshed"), 1)
        with closing(aw.open_store(self.watch)) as ws:
            queued(ws, "Email:fx:11", "Email", mail(11))
        self.assertEqual(self.cycle(agent, max_sessions_per_hour=2)["status"], "rate_capped")
        with closing(aw.open_store(self.watch)) as ws:
            states = [r[0] for r in ws.execute("SELECT state FROM items")]
        self.assertEqual(states, ["open"])


if __name__ == "__main__":
    unittest.main()
