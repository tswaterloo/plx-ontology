"""Assist recipients: every candidate address, inside or outside, passes the people exclusion."""
import unittest

from operating import assist

ME = "00000000-0000-0000-0000-00000000000a"
BOSS = "00000000-0000-0000-0000-00000000000b"
BARRED = "00000000-0000-0000-0000-00000000000c"


class People:
    internal = ("plx.test",)

    def __init__(self, directory, excluded=(), unavailable=None):
        self.directory = directory
        self.excluded = set(excluded)
        self.unavailable = unavailable

    def identity_clear(self, value):
        value = str(value).lower()
        if value in self.directory:
            value = self.directory[value]
        return value not in self.excluded


def intent(name, side="supplier"):
    return assist.Intent(text=f"email {name}", channel="mail", verb="tell", mentions=[name],
                         team=False, side=side, topic="", records=[])


DIRECTORY = {"me@plx.test": ME, "boss@plx.test": BOSS,
             "dana@acme-parts.com": BARRED}     # an excluded person on an outside address


class OutsideRecipientTests(unittest.TestCase):
    def test_excluded_person_on_outside_address_from_sent_mail_is_withheld(self):
        sent = [{"to": ["dana@acme-parts.com"], "cc": []}]
        got = assist.resolve(intent("Acme"), me=ME, people=People(DIRECTORY, {BARRED}),
                             sent=sent)
        self.assertEqual(got.to, [])
        self.assertEqual(got.withheld, 1)

    def test_excluded_person_on_party_address_is_withheld(self):
        got = assist.resolve(intent("Acme"), me=ME, people=People(DIRECTORY, {BARRED}),
                             sent=[], party=lambda n, s: {"dana@acme-parts.com"})
        self.assertEqual(got.to, [])
        self.assertEqual(got.withheld, 1)

    def test_clear_outside_address_still_resolves(self):
        got = assist.resolve(intent("Acme"), me=ME, people=People(DIRECTORY),
                             sent=[{"to": ["dana@acme-parts.com"]}])
        self.assertEqual(got.to, ["dana@acme-parts.com"])
        self.assertEqual(got.withheld, 0)

    def test_guard_unavailable_refuses(self):
        for people in (People(DIRECTORY, unavailable="no_guard"), People({}), None):
            with self.assertRaises(assist.AssistRefused) as ctx:
                assist.resolve(intent("Acme"), me=ME, people=people,
                               sent=[{"to": ["dana@acme-parts.com"]}])
            self.assertEqual(ctx.exception.code, "people_exclusion_unavailable")


OTHER_ME = "00000000-0000-0000-0000-00000000000d"
LAURA_A, LAURA_B = ("00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-0000000000a2")
NAMES = {"me@plx.test": ME, "vince@plx.test": BOSS, "vince@sister.test": OTHER_ME,
         "laura.a@plx.test": LAURA_A, "laura@plx.test": LAURA_B,
         "dana@acme-parts.com": BARRED}


class HistoryRecipientTests(unittest.TestCase):
    """Recipients from the reader's own mail, sent and received (``capture_history``)."""

    def resolve(self, line, sent, me=ME, excluded=()):
        people = People(NAMES, set(excluded))
        people.internal = ("plx.test", "sister.test")
        return assist.resolve(assist.parse(line), me=me, people=people, sent=sent, roles={})

    def test_company_names_fit_their_domain_from_received_mail(self):
        sent = [{"in": True, "from": "aron.s@unitedscents.com", "to": ["me@plx.test"],
                 "subject": "Invoice"},
                {"in": True, "from": "keith.ali@barentz.com", "to": [], "subject": "Meeting"},
                {"in": True, "from": "sajeed@mmproducts.net", "to": [], "subject": "x"},
                {"in": True, "from": "ray@thenationalclub.com", "to": [], "subject": "x"},
                {"in": True, "from": "news@united.example", "to": [], "subject": "x"}]
        for line, want in (("email United Scents: September invoice", "aron.s@unitedscents.com"),
                           ("email Barentz Canada ULC: action items", "keith.ali@barentz.com"),
                           ("email M & M PRODUCTS COMPANY: EXACT11", "sajeed@mmproducts.net"),
                           ("email The National Club: order confirmation",
                            "ray@thenationalclub.com")):
            got = self.resolve(line, sent)
            self.assertEqual(got.to, [want], line)
            self.assertTrue(got.outside)
        # a filler word alone ("united", "canada") never ties a name to a domain
        self.assertEqual(self.resolve("email United Canada: hi", sent).to, [])

    def test_the_thread_on_this_topic_picks_who_at_the_company(self):
        sent = [{"to": ["joe@atlas.example"], "subject": "Pallet pickup"},
                {"in": True, "from": "bj@atlas.example", "to": ["me@plx.test"],
                 "subject": "Gallon boxes quality"}]
        self.assertEqual(self.resolve("email Atlas: Quality issues - gallon boxes", sent).to,
                         ["bj@atlas.example"])
        # no shared topic: the newest mail the reader sent there
        self.assertEqual(self.resolve("email Atlas: invoice", sent).to, ["joe@atlas.example"])

    def test_excluded_person_reached_through_received_mail_is_withheld(self):
        sent = [{"in": True, "from": "dana@acme-parts.com", "to": [], "subject": "Acme quote"}]
        got = self.resolve("email Acme: quote", sent, excluded={BARRED})
        self.assertEqual((got.to, got.withheld), ([], 1))

    def test_own_name_is_a_note_to_self_unless_another_is_written_to(self):
        to_self = [{"to": ["vince@plx.test"], "subject": "note"}] * 3
        got = self.resolve("email Vince: invoice 30314", to_self, me=BOSS)
        self.assertEqual(got.to, ["vince@plx.test"])
        to_other = [{"to": ["vince@sister.test"], "subject": "note"}] * 3
        got = self.resolve("email Vince: invoice 30314", to_other, me=BOSS)
        self.assertEqual(got.to, ["vince@sister.test"])
        # never written to either: the other person of that name, not the reader
        self.assertEqual(self.resolve("email Vince: hi", [], me=BOSS).to,
                         ["vince@sister.test"])

    def test_two_of_a_name_the_one_written_to_far_more_else_a_choice(self):
        mostly = [{"to": ["laura.a@plx.test"]}] * 4 + [{"to": ["laura@plx.test"]}]
        self.assertEqual(self.resolve("email Laura: OT", mostly).to, ["laura.a@plx.test"])
        even = [{"to": ["laura.a@plx.test"]}] * 2 + [{"to": ["laura@plx.test"]}] * 2
        got = self.resolve("email Laura: OT", even)
        self.assertEqual(got.to, [])
        self.assertEqual(sorted(got.choices["Laura"]), ["laura.a@plx.test", "laura@plx.test"])
        # one of them excluded: never offered, never picked by mail volume
        got = self.resolve("email Laura: OT", even, excluded={LAURA_A})
        self.assertEqual((got.to, got.withheld), (["laura@plx.test"], 1))


    def test_the_company_own_name_is_no_topic(self):
        # the company's own name (its internal mail domain's) is in every subject it
        # writes: it ties no thread; the newest mail sent there decides
        sent = [{"to": ["amy@atlas.example"], "subject": "Pallet pickup"},
                {"to": ["joe@atlas.example"], "subject": "Plxco samples"}]
        people = People(NAMES)
        people.internal = ("plxco-1.test", "plx.test")
        got = assist.resolve(assist.parse("email Atlas: Plxco quality"), me=ME,
                             people=people, sent=sent, roles={})
        self.assertEqual(got.to, ["amy@atlas.example"])
        got = assist.resolve(assist.parse("email Atlas: samples"), me=ME,
                             people=people, sent=sent, roles={})
        self.assertEqual(got.to, ["joe@atlas.example"])


class RecordsNamedTests(unittest.TestCase):
    def test_a_day_range_is_not_a_record_an_id_is(self):
        self.assertEqual(assist.parse("email Pat: Time sheet Oct. 05-10").records, [])
        self.assertEqual(assist.parse("email Pat: return MC3025-GR and PO 90412").records,
                         ["MC3025-GR", "90412"])
        # a quantity with its unit is not a record
        self.assertEqual(assist.parse("email Pat: 100ml amber dropper, 250 pcs of 12x20kg"
                                      ).records, [])


class DraftTests(unittest.TestCase):
    def test_a_long_subject_fits_the_copy_check_and_outside_facts_name_the_record(self):
        line = ("email Stefan: CoA | Sahajan | AO26278A - (Finished Product - Sahajan "
                "Essential Cleansing Oil)")
        it = assist.parse(line)
        made = assist.draft(it, assist.Recipients(to=["stefan@acme.example"]), [
            {"question": "status of AO26278A", "facts": [
                {"text": "Assembly order AO-26278A in FileMaker: status Released.",
                 "sources": ["h1"]},
                {"text": "Lot 26261F expires on 2027-01-01.", "sources": ["h2"]}]}],
            outside=True)
        self.assertLessEqual(len(made["subject"]), 60)
        self.assertIsNotNone(made["body"], made["gaps"])
        # the record in another written form goes out; another record stays inside
        self.assertEqual([f["text"][:24] for f in made["facts"]], ["Assembly order AO-26278A"])
        self.assertIn("1 facts about other records were left out: the message goes outside "
                      "the company", made["gaps"])


if __name__ == "__main__":
    unittest.main()
