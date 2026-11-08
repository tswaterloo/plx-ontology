"""Who knows what (``operating.who_knows``, ``plx.aggregate`` mode ``who_knows``): per
person, the customers, suppliers and items they correspond about, counts only; the people
exclusion fails closed and a reader sees only the people, messages and records it may read.

SYNTHETIC fixtures only (``World`` stores, made-up ids and addresses); evidence label:
isolated-fixture.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from integrations.people_exclusion import PeopleExclusion
from operating import who_knows as wk
from operating.tests.test_proactive_engine import World

OURS = "00000000-0000-4000-8000-0000000000a1"
OTHER = "00000000-0000-4000-8000-0000000000a2"
EXCLUDED = "00000000-0000-4000-8000-0000000000e1"
UNBOUND = PeopleExclusion([EXCLUDED])   # decides object ids only


class Decided:
    """A guard bound to the tenant directory: it decides every address."""
    unavailable = None

    def decides(self, value):
        return True

    def is_excluded(self, value):
        return value in (EXCLUDED, "boss@synth-tenant.example")


GUARD = Decided()
EVERY = frozenset({"pii", "finance"})


def person(w: World, oid: str, upn: str, *, box: str, kind: str = "staff") -> str:
    return w.obj("Person", {"object_id": oid, "upn": upn, "kind": kind,
                            "addresses": f'["{upn}"]'}, audience=f"mail:{box}",
                 markings='["pii"]', binding="people")


def mail(w: World, sender: str, about: list[str], *, box: str, to: list[str] = ()) -> str:
    message = w.obj("Email", {"subject": "SECRET BODY TEXT"}, audience=f"mail:{box}",
                    binding="mail")
    w.link("Email.sent_by_person", message, sender)
    for other in to:
        w.link("Email.addressed_to_person", message, other)
    for record in about:
        kind = record.split(":", 1)[0]
        w.link({"Customer": "Email.mentions_customer", "Supplier": "Email.mentions_supplier",
                "Product": "Email.mentions_product"}[kind], message, record)
    return message


class WhoKnows(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store.sqlite"

    def ask(self, w: World, *, may_read=lambda _k: True, held=EVERY, guard=GUARD,
            waiting=None, person_id=None) -> dict:
        if not self.store.exists():
            w.build(self.store)
        conn = sqlite3.connect(self.store)
        self.addCleanup(conn.close)
        return wk.answer(conn, may_read=may_read, held=held, guard=guard,
                         waiting={} if waiting is None else waiting, person=person_id)

    def world(self):
        w = World()
        dana = person(w, OURS, "dana@synth-tenant.example", box="dana")
        lee = person(w, OTHER, "lee@synth-tenant.example", box="lee")
        boss = person(w, EXCLUDED, "boss@synth-tenant.example", box="boss")
        outside = person(w, "", "buyer@customer.example", box="dana", kind="external")
        acme = w.obj("Customer", {"name": "SYNTH Customer A"}, audience="bc:customers")
        beta = w.obj("Customer", {"name": "SYNTH Customer B"}, audience="bc:customers")
        vend = w.obj("Supplier", {"name": "SYNTH Supplier"}, audience="bc:vendors")
        part = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        secret = w.obj("Customer", {"name": "SYNTH Hidden"}, audience="sage:customers")
        m1 = mail(w, dana, [acme, part], box="dana", to=[outside])
        mail(w, dana, [acme], box="dana")
        mail(w, dana, [beta, vend, secret], box="dana")
        mail(w, lee, [vend], box="lee")
        mail(w, boss, [acme], box="boss", to=[dana])
        return w, dict(dana=dana, lee=lee, boss=boss, acme=acme, beta=beta, vend=vend,
                       part=part, secret=secret, m1=m1)

    def test_counts_per_person_and_record_without_any_message_text(self):
        w, ids = self.world()
        got = self.ask(w, may_read=lambda k: k != "sage:customers",
                       waiting={OURS: [ids["m1"]]})
        self.assertEqual(wk.WhoKnowsTool._checked(None, got), got)   # the contract holds
        people = {p["person"]: p for p in got["people"]}
        self.assertEqual(set(people), {ids["dana"], ids["lee"]})       # staff, not excluded
        dana = people[ids["dana"]]
        self.assertEqual(dana["messages"], 3)
        text = lambda rows: [(r["object_id"], r["label"]["untrusted_text"], r["messages"])
                             for r in rows]
        self.assertEqual(text(dana["customers"]), [(ids["acme"], "SYNTH Customer A", 2),
                                                   (ids["beta"], "SYNTH Customer B", 1)])
        self.assertEqual([s["label"]["untrusted_text"] for s in dana["suppliers"]],
                         ["SYNTH Supplier"])
        self.assertEqual([i["label"]["untrusted_text"] for i in dana["items"]], ["SYNTH-ITEM"])
        # every name is source text in its envelope, cited to the value it was read from:
        # none reaches the reader bare
        for person in got["people"]:
            for label in [person["label"], *(r["label"] for kind in ("customers", "suppliers",
                                                                     "items")
                                             for r in person[kind])]:
                self.assertEqual(set(label), {"untrusted_text", "origin"}, label)
                self.assertTrue(label["origin"]["citation"], label)
        self.assertEqual(dana["waiting_on_them"], 1)
        self.assertEqual(people[ids["lee"]]["waiting_on_them"], 0)
        self.assertTrue(got["complete"])
        text = repr(got)
        for hidden in ("SECRET BODY TEXT", "SYNTH Hidden", "boss@", "buyer@"):
            self.assertNotIn(hidden, text)

    def test_an_undecided_outside_contact_or_hidden_person_withholds_its_messages(self):
        w, ids = self.world()
        # the guard cannot decide the outside contact's address: its message is withheld
        got = self.ask(w, guard=UNBOUND)
        dana = [p for p in got["people"] if p["person"] == ids["dana"]][0]
        self.assertEqual(dana["messages"], 2)
        self.assertFalse(got["complete"])
        # a waiting message naming a person this reader may not see is not counted
        lee_msg = mail(w, ids["lee"], [], box="dana", to=[ids["dana"]])
        self.store.unlink()
        hidden = self.ask(w, may_read=lambda k: k != "mail:lee",
                          waiting={OURS: [ids["m1"], lee_msg]})
        dana = [p for p in hidden["people"] if p["person"] == ids["dana"]][0]
        self.assertEqual(dana["waiting_on_them"], 1)

    def test_the_exclusion_fails_closed_and_a_restricted_reader_sees_only_its_own(self):
        w, ids = self.world()
        for broken in (PeopleExclusion(unavailable="excluded_people_oids_absent"), None):
            got = self.ask(w, guard=broken)
            self.assertEqual((got["people"], got["complete"], got["withheld"]),
                             ([], False, "people_exclusion_unavailable"))
        self.assertEqual(self.ask(w, held=frozenset())["people"], [])  # no pii: nobody
        lee_box = self.ask(w, may_read=lambda k: k in ("mail:lee", "bc:vendors"))
        self.assertEqual([(p["person"], p["messages"], p["customers"], len(p["suppliers"]))
                          for p in lee_box["people"]], [(ids["lee"], 1, [], 1)])
        # the excluded person's message to dana stays withheld for a reader who cannot
        # even read the excluded person's mailbox
        not_boss = self.ask(w, may_read=lambda k: k != "mail:boss")
        self.assertEqual([p["messages"] for p in not_boss["people"]
                          if p["person"] == ids["dana"]], [3])
        no_records = self.ask(w, may_read=lambda k: k.startswith("mail:"))
        self.assertTrue(all(p["customers"] == p["suppliers"] == p["items"] == []
                            for p in no_records["people"]))
        unknown = self.ask(w, waiting=None)
        self.assertTrue(all(p["waiting_on_them"] == 0 for p in unknown["people"]))
        one = self.ask(w, person_id=ids["dana"])
        self.assertEqual([p["person"] for p in one["people"]], [ids["dana"]])

    def test_a_mode_of_plx_aggregate_that_leaves_its_other_calls_alone(self):
        w, ids = self.world()
        w.build(self.store)
        conn = sqlite3.connect(self.store)
        self.addCleanup(conn.close)
        base = SimpleNamespace(store=SimpleNamespace(conn=conn), policy=SimpleNamespace(
            held_markings=lambda actor: EVERY, may_read=lambda actor, key: True))
        table = {"plx.aggregate": {"name": "plx.aggregate",
                                   "handler": lambda arguments, actor: {"groups": []}}}
        tool = wk.register(table, base)
        tool.guard = lambda: GUARD
        call = table["plx.aggregate"]["handler"]
        self.assertEqual(call({"measure": {"op": "count"}, "object_type": "X"}, "a"),
                         {"groups": []})
        self.assertEqual(call({"mode": "cash_forecast"}, "a"), {"groups": []})
        self.assertEqual(call({"mode": "who_knows", "person": "Customer:x"}, "a")["error"]
                         ["code"], "invalid_input")
        got = call({"mode": "who_knows", "measure": {"op": "count"}}, "a")
        self.assertEqual(got["format"], wk.FORMAT)
        self.assertEqual(len(got["people"]), 2)
        self.assertFalse(got["complete"])          # no inbox: waiting unknown, never 0

    def test_every_person_and_message_counted_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.store = Path(tempfile.mkdtemp()) / "store.sqlite"
                w = World()
                customer = w.obj("Customer", {"name": "SYNTH Customer"}, audience="bc:c")
                for i in range(n):
                    p = person(w, f"00000000-0000-4000-8000-{i:012d}",
                               f"p{i}@synth-tenant.example", box=f"b{i}")
                    for _ in range(3):
                        mail(w, p, [customer], box=f"b{i}")
                started = time.monotonic()
                got = self.ask(w)
                self.assertLess(time.monotonic() - started, 10)
                self.assertEqual(len(got["people"]), min(n, wk.MAX_PEOPLE))
                self.assertEqual({p["messages"] for p in got["people"]}, {3})
                self.assertEqual({p["customers"][0]["messages"] for p in got["people"]}, {3})
                self.assertEqual(got.get("more_people"), n - wk.MAX_PEOPLE
                                 if n > wk.MAX_PEOPLE else None)
                self.assertEqual(got["complete"], n <= wk.MAX_PEOPLE)


if __name__ == "__main__":
    unittest.main()
