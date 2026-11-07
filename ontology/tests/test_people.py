"""Person records from the mail capture (``ontology.people_directory``) and the links to
them (``ontology.people_links``). Every id and address here is made up."""

from __future__ import annotations

from collections import Counter
import json
import unittest
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from ontology import people_directory as pd
from ontology import people_links as pl

ANA = "00000000-0000-4000-8000-00000000000a"
BEN = "00000000-0000-4000-8000-00000000000b"
GONE = "00000000-0000-4000-8000-0000000000ee"   # an excluded person (made up)
T = "2026-01-01T00:00:00Z"


def box(oid, upn, *identities):
    return pd.Mailbox(oid, upn, tuple(identities), T, T)


BOXES = [box(ANA, "ana@ours.example", "ana.alias@ours.example"),
         box(BEN, "ben@ours.example"),
         box(GONE, "gone@ours.example")]


def directory(counts=None, exclusion=None):
    if exclusion is None:
        exclusion = PeopleExclusion([GONE])
    return pd.people(BOXES, counts or {}, exclusion)


class PeopleTest(unittest.TestCase):
    def test_unavailable_exclusion_projects_nobody(self):
        found, stats = directory({("buyer@client.example", ANA): 3},
                                 PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assertEqual(found, [])
        self.assertEqual(stats["exclusion_unavailable"], 1)

    def test_staff_and_outside_kinds(self):
        found, stats = directory({("buyer@client.example", ANA): 3,
                                  ("buyer@client.example", BEN): 3,
                                  ("ben@ours.example", ANA): 9})
        kinds = {p["row"]["kind"]: p for p in found if p["row"]["kind"] == "outside"}
        staff = {p["key"]: p for p in found if p["row"]["kind"] == "staff"}
        self.assertEqual(set(staff), {ANA, BEN})
        self.assertEqual(staff[ANA]["audience"], ANA)
        self.assertEqual(staff[ANA]["row"]["addresses"],
                         ["ana.alias@ours.example", "ana@ours.example"])
        outside = kinds["outside"]
        self.assertNotIn("message_count", outside["row"])   # no cross-mailbox count served
        self.assertEqual(outside["row"]["domain"], "client.example")
        self.assertEqual(outside["audience"], min(ANA, BEN))   # a tie: the smaller id
        self.assertEqual(outside["key"], pd.address_key("buyer@client.example"))
        self.assertEqual((stats["staff"], stats["outside"]), (2, 1))

    def test_outside_audience_is_the_mailbox_holding_most(self):
        found, _ = directory({("buyer@client.example", ANA): 1,
                              ("buyer@client.example", BEN): 4})
        (outside,) = [p for p in found if p["row"]["kind"] == "outside"]
        self.assertEqual(outside["audience"], BEN)

    def test_excluded_person_is_never_projected_nor_counted_into_contacts(self):
        found, stats = directory({("buyer@client.example", GONE): 5,
                                  ("other@client.example", ANA): 1})
        keys = {p["key"] for p in found}
        self.assertNotIn(GONE, keys)
        self.assertNotIn(pd.address_key("buyer@client.example"), keys)
        self.assertEqual(stats["withheld_staff"], 1)
        self.assertFalse(any("gone@" in json.dumps(p["row"]) for p in found))

    def test_unmapped_address_at_our_domain_is_withheld(self):
        found, stats = directory({("nobox@ours.example", ANA): 7})
        self.assertFalse(any("nobox@" in json.dumps(p["row"]) for p in found))
        self.assertEqual(stats["withheld_unmapped"], 1)

    def test_automated_senders_are_no_contacts(self):
        found, stats = directory({("no-reply@client.example", ANA): 7})
        self.assertEqual([p for p in found if p["row"]["kind"] == "outside"], [])
        self.assertEqual(stats["automated"], 1)

    def test_person_handle_round_trip(self):
        # An outside contact's citation names no mailbox (a mailbox id is an employee's);
        # the earlier form naming one still parses; a staff person's names its own.
        key = pd.address_key("buyer@client.example")
        handle = pd.person_handle(ANA, key)
        self.assertEqual(handle, f"graph-mail://people/person/{key}")
        self.assertNotIn(ANA, handle)
        self.assertEqual(pd.parse_person_handle(handle), (None, key))
        self.assertEqual(pd.parse_person_handle(f"graph-mail://{ANA}/person/{key}"), (ANA, key))
        self.assertEqual(pd.parse_person_handle(pd.person_handle(ANA, ANA)), (ANA, ANA))
        for bad in (f"graph-mail://{ANA}/AAMk=@ck", f"graph-mail://people/person/{ANA}",
                    f"graph-mail://people/person/{key[:-1]}", f"graph-mail://{ANA}/person/zz",
                    f"graph-mail://a/b/person/{key}", None, 7):
            self.assertIsNone(pd.parse_person_handle(bad), bad)


def _value(oid, value):
    return {"object_id": oid, "value": value, "l1_handle": f"h://{oid}", "field_path": "row.x",
            "digest": "d", "lineage": "mail", "vmarks": "[]", "kmarks": "[]",
            "recorded_at": T}


def _people(found):
    """A People index of the projected Person records (as ``read_people`` reads them)."""
    rows = []
    for p in found:
        oid = f"Person:{p['key']}"
        for prop in pl.PERSON_PROPS:
            value = p["row"][prop]
            rows.append((_value(oid, json.dumps(value) if prop == "addresses" else value),
                         prop))
    with mock.patch.object(pl, "_values", lambda *_a, **_k: iter(rows)):
        return pl.read_people(None, ["ours.example"])


class LinksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        found, _ = directory({("buyer@client.example", ANA): 2,
                              ("anna@ours.example", ANA): 1})
        cls.people = _people(found)
        cls.outside = "Person:" + pd.address_key("buyer@client.example")

    def links(self, values):
        counts = Counter()
        with mock.patch.object(pl, "_values", lambda *_a, **_k: iter(values)):
            got = list(pl.judge_values(None, self.people, pl.FAMILIES, counts))
        return {(r["object_id"], lt, h.person) for r, lt, h, _p in got}, counts

    def test_each_family_links_by_its_exact_key(self):
        values = [
            (_value("Email:1", "Buyer@Client.example "), "sender"),
            (_value("Email:1", json.dumps(["ana@ours.example", "ben@ours.example"])),
             "to_addresses"),
            (_value("Email:1", json.dumps(["ana.alias@ours.example"])), "cc_addresses"),
            (_value("CalendarEvent:1", json.dumps({"address": "ben@ours.example"})),
             "organizer"),
            (_value("Meeting:1", json.dumps([{"address": "buyer@client.example"}])),
             "attendees"),
            (_value("TeamsChatMessage:1", json.dumps({"id": ANA.upper(), "kind": "user"})),
             "from"),
            (_value("TeamsChannelMessage:1", json.dumps({"id": BEN, "kind": "application"})),
             "from"),
            (_value("PlannerTask:1", BEN), "created_by"),
            (_value("PlannerTask:1", json.dumps([ANA])), "assignees"),
            (_value("MCTask:1", "Ben"), "accountable_owner"),
            (_value("MCApproval:1", "ana@ours.example"), "decided_by"),
        ]
        got, _ = self.links(values)
        A, B = f"Person:{ANA}", f"Person:{BEN}"
        self.assertEqual(got, {
            ("Email:1", "Email.sent_by_person", self.outside),
            ("Email:1", "Email.addressed_to_person", A),
            ("Email:1", "Email.addressed_to_person", B),
            ("CalendarEvent:1", "CalendarEvent.with_person", B),
            ("Meeting:1", "Meeting.with_person", self.outside),
            ("TeamsChatMessage:1", "TeamsChatMessage.sent_by_person", A),
            ("PlannerTask:1", "PlannerTask.with_person", B),
            ("PlannerTask:1", "PlannerTask.with_person", A),
            ("MCTask:1", "MCTask.with_person", B),
            ("MCApproval:1", "MCApproval.with_person", A)})

    def test_excluded_and_withheld_people_are_never_linked(self):
        got, counts = self.links([
            (_value("Email:2", "gone@ours.example"), "sender"),
            (_value("TeamsChatMessage:2", json.dumps({"id": GONE, "kind": "user"})), "from"),
            (_value("PlannerTask:2", GONE), "completed_by"),
            (_value("Email:2", json.dumps(["anna@ours.example"])), "to_addresses"),
            (_value("MCTask:2", "gone"), "assignee")])
        self.assertEqual(got, set())
        self.assertEqual(counts["Email.sent_by_person:no_person"], 1)

    def test_a_name_two_staff_share_links_nothing(self):
        found, _ = pd.people([box(ANA, "lee@ours.example"), box(BEN, "lee@ours2.example")],
                             {}, PeopleExclusion([GONE]))
        people = _people(found)
        counts = Counter()
        with mock.patch.object(pl, "_values",
                               lambda *_a, **_k: iter([(_value("MCTask:3", "lee"), "assignee")])):
            got = list(pl.judge_values(None, people, pl.FAMILIES, counts))
        self.assertEqual(got, [])
        self.assertEqual(counts["MCTask.with_person:ambiguous"], 1)

    def test_outside_contact_is_no_local_part_match(self):
        got, _ = self.links([(_value("MCTask:4", "buyer"), "assignee")])
        self.assertEqual(got, set())

    def test_link_row_cites_values_not_keys(self):
        _kind, holder = self.people.judge("address", "ana@ours.example")
        row = pl.link_row(_value("Email:5", "ana@ours.example"), "Email.sent_by_person",
                          holder, "sender", {"l1_handle": "pv", "markings": '["pii"]',
                                             "recorded_at": T})
        self.assertTrue(row["link_id"].startswith(pl.PREFIX))
        self.assertEqual(row["tier"], "corroborated")
        self.assertNotIn("ana@", row["reason"])
        self.assertIn("pii", row["markings"])


if __name__ == "__main__":
    unittest.main()
