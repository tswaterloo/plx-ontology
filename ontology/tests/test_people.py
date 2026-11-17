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

    def test_every_form_of_an_excluded_address_is_excluded(self):
        # CC-NY-8: the excluded person's own address at a shared provider (never one of
        # our domains), its plus-addressed form and another host of it are no Person;
        # another sender at the provider still is
        boxes = BOXES[:2] + [box(GONE, "gone@ours.example", "gone.home@gmail.com")]
        counts = {("gone.home@gmail.com", ANA): 2, ("gone.home+shop@gmail.com", ANA): 1,
                  ("Gone.Home+x@GMAIL.com", BEN): 1, ("gone.home@mail.gmail.com", ANA): 1,
                  ("someone@gmail.com", ANA): 1}
        found, stats = pd.people(boxes, counts, PeopleExclusion([GONE]))
        rows = json.dumps([p["row"] for p in found])
        self.assertNotIn("gone", rows)
        self.assertEqual(stats["withheld_alias"], 4)
        self.assertIn("someone@gmail.com", rows)
        # so the people the links step reads hold no form of it either
        people = _people(found)
        for key in ("gone.home@gmail.com", "gone.home+shop@gmail.com"):
            self.assertEqual(people.judge("address", key), ("no_person", None))
        self.assertFalse(any("gone" in json.dumps(h.cite) for h in
                             (x for held in people.by["address"].values() for x in held.values())))

    def test_an_excluded_address_the_guard_decides_is_withheld_in_every_form(self):
        # not one of our mailboxes' addresses: the guard decides it (a bound listing),
        # and its plus base or registrable-domain form decides the variants
        class Guard(PeopleExclusion):
            def decides(self, value):
                return True

            def is_excluded(self, value):   # a bound listing: every address decided
                if "@" in value:
                    return value == "boss.home@gmail.com" or value == "gone@ours.example"
                return super().is_excluded(value)

        counts = {("boss.home+news@gmail.com", ANA): 1, ("boss.home@mail.gmail.com", ANA): 1,
                  ("boss.home@gmail.com", ANA): 1, ("buyer@client.example", ANA): 1}
        found, stats = pd.people(BOXES, counts, Guard([GONE]))
        self.assertEqual([p["row"]["addresses"] for p in found if p["row"]["kind"] == "outside"],
                         [["buyer@client.example"]])
        self.assertEqual(stats["withheld_alias"], 3)

    def test_plus_base_and_alias_key(self):
        self.assertEqual(pd.plus_base("A+Tag@X.example"), "a@x.example")
        self.assertEqual(pd.plus_base("a@x.example"), "a@x.example")
        self.assertIsNone(pd.plus_base("+tag@x.example"))
        self.assertIsNone(pd.plus_base("no address"))
        self.assertEqual(pd.alias_key("a+t@mail.acme.co.uk"), "a@acme.co.uk")
        self.assertEqual(pd.alias_key("a@acme.com"), "a@acme.com")

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

    def test_an_excluded_teams_participant_gets_no_participant_link(self):
        members = [{"id": GONE, "display_name": "x", "email": "gone@ours.example"},
                   {"id": ANA.upper(), "display_name": "y", "email": None}]
        got, counts = self.links([
            (_value("TeamsChat:1", json.dumps(members)), "participants"),
            (_value("TeamsChannel:1", json.dumps(members[:1])), "participants")])
        # The staff member links; the excluded one is no Person, so nothing names them.
        self.assertEqual(got, {("TeamsChat:1", "TeamsChat.with_person", f"Person:{ANA}")})
        self.assertEqual(counts["TeamsChannel.with_person:no_person"], 1)

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

    def test_an_alias_at_another_of_our_domains_is_its_one_staff_mailbox(self):
        # served shape (11-12): staff hold x@ at two tenant domains with one local part, the
        # directory lists one address of most; their mail from the other domain named nobody
        found, _ = pd.people([box(ANA, "ana@ours.example", "ana@ours-two.example"),
                              box(BEN, "ben@ours.example"),
                              box(GONE, "gone@ours.example")], {}, PeopleExclusion([GONE]))
        people = _people(found)
        people.guard = PeopleExclusion([GONE])

        def run(rows):
            counts = Counter()
            with mock.patch.object(pl, "_values", lambda *_a, **_k: iter(rows)):
                got = list(pl.judge_values(None, people, pl.FAMILIES, counts))
            return {(r["object_id"], lt, h.person) for r, lt, h, _p in got}, counts

        # an alias the exclusion cannot decide (not in the tenant listing) links nobody:
        # it may be an excluded person's
        got, counts = run([(_value("Email:6", "Ben@Ours-Two.example"), "sender")])
        self.assertEqual(got, set())
        self.assertEqual(counts["Email.sent_by_person:alias_undecided"], 1)
        # a decided guard (the tenant listing holds the address): only gone@ is excluded
        decided = [mock.patch.object(people.guard, "decides", lambda v: True),
                   mock.patch.object(people.guard, "is_excluded",
                                     lambda v: v.lower().startswith("gone@"))]
        for patch in decided:
            patch.start()
        got, counts = run([
            (_value("Email:6", "Ben@Ours-Two.example"), "sender"),        # alias -> Ben
            (_value("Email:7", "gone@ours-two.example"), "sender"),       # excluded: no Person
            (_value("Email:8", "ben@client.example"), "sender"),          # not ours
            (_value("Email:9", "ben@ours-two.zendesk.com"), "sender"),     # help desk host
            (_value("Email:9", json.dumps(["ben@ours-two.example"])), "to_addresses")])
        self.assertEqual(got, {("Email:6", "Email.sent_by_person", f"Person:{BEN}")})
        self.assertEqual((counts["Email.sent_by_person:alias_linked"],
                          counts["Email.sent_by_person:no_person"]), (1, 3))
        # an address the exclusion decides is an excluded person's links nobody
        with mock.patch.object(people.guard, "is_excluded", lambda v: True):
            self.assertEqual(run([(_value("Email:6", "ben@ours-two.example"), "sender")])[0],
                             set())
        for patch in decided:
            patch.stop()
        # no usable exclusion: the alias rule links nothing (exact addresses still do)
        people.guard = None
        got, counts = run([(_value("Email:6", "ben@ours-two.example"), "sender"),
                           (_value("Email:10", "ana@ours-two.example"), "sender")])
        self.assertEqual(got, {("Email:10", "Email.sent_by_person", f"Person:{ANA}")})
        self.assertEqual(counts["Email.sent_by_person:alias_exclusion_unavailable"], 1)

    def test_outside_contact_is_no_local_part_match(self):
        got, _ = self.links([(_value("MCTask:4", "buyer"), "assignee")])
        self.assertEqual(got, set())

    def test_teams_participants_name_people_by_entra_id_only(self):
        oid = "0f0e0d0c-0b0a-4908-8706-050403020100"
        value = json.dumps([{"id": oid.upper(), "display_name": "A", "email": "a@x.test"},
                            {"id": "not-a-guid", "email": "b@x.test"}, "loose", {"email": None}])
        self.assertEqual(pl.keys("participants", value), [("oid", oid)])
        self.assertEqual(pl.keys("participants", "not json"), [])
        self.assertEqual(pl.FAMILIES["TeamsChat"]["participants"][0], "TeamsChat.with_person")

    def test_a_contact_email_links_its_one_person_exactly(self):
        got, counts = self.links([
            (_value("Contact:bc.contacts:1", "Buyer@Client.example "), "email"),
            (_value("Contact:bc.contacts:2", "buyer+x@client.example"), "email"),
            (_value("Contact:bc.contacts:3", "gone@ours.example"), "email")])
        self.assertEqual(got, {("Contact:bc.contacts:1", "Contact.same_person_as",
                                self.outside)})
        self.assertEqual(counts["Contact.same_person_as:no_person"], 2)

    def test_link_row_cites_values_not_keys(self):
        _kind, holder = self.people.judge("address", "ana@ours.example")
        row = pl.link_row(_value("Email:5", "ana@ours.example"), "Email.sent_by_person",
                          holder, "sender", {"l1_handle": "pv", "markings": '["pii"]',
                                             "recorded_at": T})
        self.assertTrue(row["link_id"].startswith(pl.PREFIX))
        self.assertEqual(row["tier"], "corroborated")
        self.assertNotIn("ana@", row["reason"])
        self.assertIn("pii", row["markings"])


class AliasLinksTest(unittest.TestCase):
    """CC-NY-8: one person's Person records joined, never merged."""

    def pairs(self, senders):
        found, _ = directory({(a, ANA): 1 for a in senders})
        people = _people(found)
        counts = Counter()
        key = {p.person: a for a, held in people.by["address"].items() for p in held.values()}
        return {(key[f.person], key[t.person], tier, feature)
                for f, t, tier, feature in pl.alias_pairs(people, counts)}, counts, people

    def test_plus_forms_are_one_person_other_hosts_a_lead(self):
        got, _, _ = self.pairs(["a@x.example", "a+promo@x.example", "a@mail.x.example",
                                "b@x.example"])
        self.assertEqual(got, {
            ("a+promo@x.example", "a@x.example", "corroborated", "plus_address"),
            ("a@mail.x.example", "a@x.example", "lead", "sub_domain")
            if pd.address_key("a@mail.x.example") < pd.address_key("a@x.example") else
            ("a@x.example", "a@mail.x.example", "lead", "sub_domain"),
            ("a+promo@x.example", "a@mail.x.example", "lead", "sub_domain")
            if pd.address_key("a+promo@x.example") < pd.address_key("a@mail.x.example") else
            ("a@mail.x.example", "a+promo@x.example", "lead", "sub_domain")})

    def test_two_tags_without_the_base_join_each_other(self):
        got, _, _ = self.pairs(["a+one@x.example", "a+two@x.example"])
        self.assertEqual({(t, f) for _a, _b, t, f in got}, {("corroborated", "plus_address")})
        self.assertEqual(len(got), 1)

    def test_a_crowded_alias_links_nothing_and_no_excluded_form_is_reached(self):
        hosts = [f"info@h{n}.x.example" for n in range(pl.ALIAS_GROUP_MAX + 1)]
        got, counts, _ = self.pairs(hosts + ["gone+x@ours.example"])
        self.assertEqual(got, set())
        self.assertEqual(counts["Person.same_person_as:alias_group_too_large"], 1)

    def test_the_link_is_seen_only_with_both_persons(self):
        _, _, people = self.pairs(["a@x.example", "a+promo@x.example"])
        [(first, second, tier, feature)] = list(pl.alias_pairs(people, Counter()))
        versions = {first.person: {"l1_handle": "v1", "markings": '["pii"]', "recorded_at": T},
                    second.person: {"l1_handle": "v2", "markings": '["l5"]', "recorded_at": T}}
        row = pl.alias_row(first, second, tier, feature, versions)
        self.assertEqual(row["link_type"], "Person.same_person_as")
        self.assertEqual(row["tier"], "corroborated")
        handles = json.loads(row["evidence_handles"])
        self.assertTrue({first.handle, second.handle, "v1", "v2"} <= set(handles))
        self.assertEqual(set(json.loads(row["markings"])), {"pii", "l5"})
        self.assertNotIn("a+promo", row["reason"])
        self.assertIsNone(pl.alias_row(first, second, tier, feature, {first.person: versions[
            first.person]}))



class PartedStepTest(unittest.TestCase):
    """TRAIN2-B1: ``with_people`` run in hash parts (``pg_step_inplace.PARTED_STEPS``)
    writes, part by part, exactly the unparted step's links, each from an object of its
    own part (what staging's ``hash_part`` scope holds a part to), and its counts sum to
    the unparted counts. The Person alias links were one global pass every part wrote
    whole (``store_link_outside_scope`` on the second part)."""

    @classmethod
    def setUpClass(cls):
        hosts = [f"x{i}.example" for i in range(9)]
        senders = [f"{a}@{h}" for h in hosts for a in ("a", "a+promo", "b")]
        senders += [f"a@mail.{h}" for h in hosts] + [f"b+{t}@x0.example" for t in "pq"]
        senders += [f"info@h{n}.y.example" for n in range(pl.ALIAS_GROUP_MAX + 1)]
        found, _ = directory({(a, ANA): 1 for a in senders})
        cls.people = _people(found)
        persons = sorted(cls.people.persons())
        addresses = sorted(cls.people.by["address"])
        cls.values = []
        for j in range(60):
            cls.values.append((_value(f"Email:{j}", addresses[j % len(addresses)]),
                               "sender"))
            cls.values.append((_value(f"Email:{j}", json.dumps(
                [addresses[(j * 7) % len(addresses)], "ben@ours.example",
                 "nobody@nowhere.example"])), "to_addresses"))
            cls.values.append((_value(f"Contact:bc.contacts:{j}",
                                      addresses[(j * 5) % len(addresses)]), "email"))
        cls.versions = {p: {"l1_handle": f"v://{p}", "markings": "[]", "recorded_at": T}
                        for p in persons}
        # a person's rejections: of an Email link and of an alias link
        email_to = next(h.person for h in cls.people.by["address"][addresses[3]].values())
        cls.rejected = {"Email:3": [(email_to, "Email.sent_by_person")]}
        first = next(pl.alias_pairs(cls.people, Counter()))
        cls.rejected[first[0].person] = [(first[1].person, pl.ALIAS_LINK)]
        cls.links = {lt: {"bindings": [{"kind": pl.mentions.BINDING_KIND}]}
                     for lt in (*pl.LINK_TYPES, pl.ALIAS_LINK)}

    def run_step(self, part=None):
        from ontology.pg_step_inplace import object_part
        test = self

        class Conn:
            def execute(self, *_a):
                return None

        conn = Conn()
        if part is not None:
            conn.part = (*part, ("Person:",))

        def values(c, props, prefixes):
            # as ``pg_step_inplace.parted_read`` narrows the read: the part's objects and
            # every Person
            part = getattr(c, "part", None)
            for row, prop in test.values:
                oid = row["object_id"]
                if prop in props and oid.startswith(tuple(prefixes)) and (
                        part is None or oid.startswith("Person:")
                        or object_part(oid, part[1]) == part[0]):
                    yield row, prop

        class Builder:
            connection = conn
            rows: list = []

            def add(self, kind, rows):
                self.rows.extend(rows)

        builder = Builder()
        builder.rows = []
        projector = mock.Mock(links=self.links)
        with mock.patch.object(pl, "read_people", lambda *_a: self.people), \
                mock.patch.object(pl, "own_domains", lambda _p: ["ours.example"]), \
                mock.patch.object(pl, "_excluder", lambda: None), \
                mock.patch.object(pl, "_versions", lambda *_a: self.versions), \
                mock.patch.object(pl, "_values", values), \
                mock.patch.object(pl, "_q", lambda *_a: []), \
                mock.patch.object(pl.SR, "rejected_pairs", lambda _c, objs: {
                    o: self.rejected[o] for o in set(objs) if o in self.rejected}):
            out = pl.people_links_step(builder, projector)
        return builder.rows, out

    def test_the_parts_union_is_the_unparted_output(self):
        from ontology.pg_step_inplace import object_part
        whole, out = self.run_step()
        ids = [r["link_id"] for r in whole]
        self.assertEqual(len(ids), len(set(ids)))
        alias = [r for r in whole if r["link_type"] == pl.ALIAS_LINK]
        # the fixture reaches every kind of row and count the step has
        self.assertTrue({"corroborated", "lead"} <= {r["tier"] for r in alias})
        self.assertGreater(len(alias), 10)
        for kind in ("plus_address", "sub_domain", "alias_group_too_large"):
            self.assertTrue(out["counts"].get(f"{pl.ALIAS_LINK}:{kind}"), kind)
        self.assertEqual(out["counts"]["rejected"], 2)
        self.assertTrue({"Email.sent_by_person", "Email.addressed_to_person",
                         "Contact.same_person_as"} <= set(out["link_types"]))
        key = lambda r: json.dumps(r, sort_keys=True)
        for n in (2, 3, 5, 16):
            with self.subTest(parts=n):
                union, counts, written = [], Counter(), Counter()
                for k in range(n):
                    rows, got = self.run_step((k, n))
                    # every link is from an object of its part (staging's hash_part scope)
                    self.assertEqual({object_part(r["from_object"], n) for r in rows} - {k},
                                     set())
                    union += rows
                    counts.update(got["counts"])
                    written.update(got["link_types"])
                self.assertEqual(sorted(map(key, union)), sorted(map(key, whole)))
                self.assertEqual(+counts, +Counter(out["counts"]))
                self.assertEqual(+written, +Counter(out["link_types"]))

    def test_unparted_alias_pairs_are_every_pair(self):
        got = list(pl.alias_pairs(self.people, Counter()))
        for n in (1, 4):
            parted = [x for k in range(n)
                      for x in pl.alias_pairs(self.people, Counter(), (k, n))]
            self.assertEqual(sorted((f.person, t.person, tier, ft) for f, t, tier, ft in parted),
                             sorted((f.person, t.person, tier, ft) for f, t, tier, ft in got))

if __name__ == "__main__":
    unittest.main()
