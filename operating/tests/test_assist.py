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

    @staticmethod
    def asked(got):
        """The candidates the reader is asked to pick from for who it is for."""
        return next((q["candidates"] for q in got.ask
                     if q["field"] == "to" and q["pick"] == "one"), [])

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
            # only mail from them, never one the reader wrote: offered, not filled in
            self.assertEqual((got.to, self.asked(got)), ([], [want]), line)
            self.assertTrue(got.outside)
            # the reader has written to them: the one person there is filled in
            wrote = sent + [{"to": [want], "subject": "hello"}]
            self.assertEqual(self.resolve(line, wrote).to, [want], line)
        # a filler word alone ("united", "canada") never ties a name to a domain
        got = self.resolve("email United Canada: hi", sent)
        self.assertEqual((got.to, got.ask, got.unresolved), ([], [], ["United Canada"]))

    def test_a_thread_naming_the_record_picks_who_a_shared_word_only_asks(self):
        sent = [{"to": ["joe@atlas.example"], "subject": "Pallet pickup"},
                {"in": True, "from": "bj@atlas.example", "to": ["me@plx.test"],
                 "subject": "Gallon boxes quality"},
                {"to": ["amy@atlas.example"], "subject": "Lot 77120 COA"}]
        # the thread on the record the line names: theirs
        self.assertEqual(self.resolve("email Atlas: COA for lot 77120", sent).to,
                         ["amy@atlas.example"])
        # a word in common only: asked, that thread's person first
        got = self.resolve("email Atlas: Quality issues - gallon boxes", sent)
        self.assertEqual((got.to, self.asked(got)[0]), ([], "bj@atlas.example"))
        # nothing in common and no habit: asked, never the newest mail guessed
        got = self.resolve("email Atlas: invoice", sent)
        self.assertEqual(got.to, [])
        self.assertEqual(set(self.asked(got)),
                         {"joe@atlas.example", "amy@atlas.example", "bj@atlas.example"})

    def test_no_thread_on_the_topic_is_the_main_contact_only_when_the_mail_settles_it(self):
        # the reader writes to Joe first on 4 of 4 mails there: Joe alone, not the dozen
        # on a list mail, and nobody asked about who it is for
        crowd = [f"p{n}@atlas.example" for n in range(12)]
        sent = [{"to": ["joe@atlas.example"] + crowd, "subject": "Holiday hours"}] + \
            [{"to": ["joe@atlas.example"], "subject": f"Week {n}"} for n in range(3)]
        got = self.resolve("email Atlas: cabinet", sent)
        self.assertEqual(got.to, ["joe@atlas.example"])
        self.assertFalse([q for q in got.ask if q["pick"] == "one"])
        # first on 3 of 5: asked, Joe first
        sent = [{"to": crowd, "subject": "Holiday hours"},
                {"to": ["amy@atlas.example"], "subject": "Pallets"}] + \
            [{"to": ["joe@atlas.example", "amy@atlas.example"], "subject": f"Week {n}"}
             for n in range(3)]
        got = self.resolve("email Atlas: cabinet", sent)
        self.assertEqual((got.to, self.asked(got)[0]), ([], "joe@atlas.example"))
        # as often each: asked, the more recent first
        sent = [{"to": ["amy@atlas.example"], "subject": "a"}] * 2 + \
            [{"to": ["joe@atlas.example"], "subject": "b"}] * 2
        got = self.resolve("email Atlas: cabinet", sent)
        self.assertEqual((got.to, self.asked(got)), ([], ["amy@atlas.example",
                                                          "joe@atlas.example"]))
        # the main contact excluded: withheld, never filled in, never offered
        sent = [{"to": ["dana@atlas.example"], "subject": "a"}] * 3 + \
            [{"to": ["joe@atlas.example"], "subject": "b"}]
        people = People(dict(NAMES, **{"dana@atlas.example": BARRED}), {BARRED})
        people.internal = ("plx.test",)
        got = assist.resolve(assist.parse("email Atlas: cabinet"), me=ME, people=people,
                             sent=sent, roles={})
        self.assertEqual((got.to, self.asked(got), got.withheld),
                         ([], ["joe@atlas.example"], 1))

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
        # writes: it ties no thread; a real word in common does
        sent = [{"to": ["amy@atlas.example"], "subject": "Pallet pickup"},
                {"to": ["joe@atlas.example"], "subject": "Plxco samples"}]
        people = People(NAMES)
        people.internal = ("plxco-1.test", "plx.test")
        got = assist.resolve(assist.parse("email Atlas: Plxco quality"), me=ME,
                             people=people, sent=sent, roles={})
        self.assertEqual(self.asked(got)[0], "amy@atlas.example")
        got = assist.resolve(assist.parse("email Atlas: samples"), me=ME,
                             people=people, sent=sent, roles={})
        self.assertEqual(self.asked(got)[0], "joe@atlas.example")


class WhoElseTests(unittest.TestCase):
    """Who else goes on it: the reader's own habit with that person, filled in only when
    it holds on nearly every mail; anyone less is asked, never added on a guess."""

    def resolve(self, line, sent, excluded=()):
        people = People(dict(NAMES, **{"dana@atlas.example": BARRED}), set(excluded))
        people.internal = ("plx.test",)
        return assist.resolve(assist.parse(line), me=ME, people=people, sent=sent, roles={})

    def test_a_usual_cc_is_filled_in_an_occasional_one_asked_an_excluded_one_never(self):
        sent = [{"to": ["joe@atlas.example"], "cc": ["boss@plx.test"], "subject": f"w{n}"}
                for n in range(4)]
        sent[0] = dict(sent[0], cc=["boss@plx.test", "amy@atlas.example",
                                    "dana@atlas.example"])
        got = self.resolve("email Atlas: cabinet", sent, excluded={BARRED})
        self.assertEqual((got.to, got.cc), (["joe@atlas.example"], ["boss@plx.test"]))
        cc_ask = [q for q in got.ask if q["field"] == "cc"]
        self.assertEqual([q["candidates"] for q in cc_ask], [["amy@atlas.example"]])
        self.assertEqual(got.withheld, 1)
        self.assertNotIn("dana@atlas.example", str(got))

    def test_the_reader_s_recipient_lessons_take_people_off_and_put_them_first(self):
        sent = [{"to": ["joe@atlas.example"], "cc": ["boss@plx.test"], "subject": f"w{n}"}
                for n in range(4)]
        sent[0] = dict(sent[0], cc=["boss@plx.test", "amy@atlas.example", "kim@atlas.example"])
        sent[1] = dict(sent[1], cc=["boss@plx.test", "amy@atlas.example", "kim@atlas.example"])
        people = People(NAMES)
        people.internal = ("plx.test",)
        got = assist.resolve(assist.parse("email Atlas: cabinet"), me=ME, people=people,
                             sent=sent, roles={},
                             learned={"boss@plx.test": "never", "kim@atlas.example": "cc"})
        self.assertEqual((got.to, got.cc), (["joe@atlas.example"], []))
        self.assertEqual([q["candidates"] for q in got.ask if q["field"] == "cc"],
                         [["kim@atlas.example", "amy@atlas.example"]])
        # typed by the reader, a "never" lesson gives way
        got = assist.resolve(assist.parse("email boss@plx.test: cabinet"), me=ME,
                             people=people, sent=sent, roles={},
                             learned={"boss@plx.test": "never"})
        self.assertEqual(got.to, ["boss@plx.test"])

    def test_a_colleague_s_mail_copying_the_company_never_makes_the_colleague_theirs(self):
        sent = [{"in": True, "from": "boss@plx.test", "to": ["me@plx.test"],
                 "cc": ["joe@atlas.example"], "subject": "Lot 77120 COA"},
                {"to": ["amy@atlas.example"], "subject": "Pallets"}]
        got = self.resolve("email Atlas: COA for lot 77120", sent)
        self.assertNotIn("boss@plx.test", got.to)
        self.assertEqual(got.to, [])
        self.assertEqual(HistoryRecipientTests.asked(got)[0], "joe@atlas.example")

    def test_the_reader_s_busiest_correspondent_is_never_filled_in_on_another_s_mail(self):
        # 12 of 15 outside mails go to a warehouse, 3 to Joe at Atlas (to him first,
        # always alone): a mail to Atlas is Joe's, the warehouse at most offered
        sent = [{"to": ["ops@warehouse.example"], "cc": ["boss@plx.test"], "subject": f"p{n}"}
                for n in range(12)] + \
            [{"to": ["joe@atlas.example"], "subject": f"w{n}"} for n in range(3)]
        got = self.resolve("email Atlas: cabinet", sent)
        self.assertEqual(got.to, ["joe@atlas.example"])
        self.assertNotIn("ops@warehouse.example", got.to + got.cc)
        # a colleague the reader copies on mail going outside: a Cc of their own
        self.assertEqual(got.cc, ["boss@plx.test"])

    def test_an_excluded_typed_address_is_said_without_naming_them(self):
        made = assist.draft(assist.parse("email dana@atlas.example: cabinet"),
                            self.resolve("email dana@atlas.example: cabinet", [],
                                         excluded={BARRED}), [])
        self.assertEqual(made["to"], [])
        self.assertIn("Someone this message would go to can't be added here: check the "
                      "recipients", made["gaps"])
        self.assertNotIn("dana", " ".join(made["gaps"]))

    def test_a_tapped_candidate_resolves_to_that_address_with_its_habit(self):
        sent = [{"to": ["joe@atlas.example", "amy@atlas.example"], "subject": f"w{n}"}
                for n in range(3)]
        got = self.resolve("email joe@atlas.example: cabinet", sent)
        self.assertEqual(got.to, ["joe@atlas.example", "amy@atlas.example"])
        self.assertTrue(got.outside)
        # a tapped address of an excluded person: withheld, nothing filled in
        got = self.resolve("email dana@atlas.example: cabinet", sent, excluded={BARRED})
        self.assertEqual((got.to, got.withheld), ([], 1))

    def test_draft_says_every_question_on_the_recipients(self):
        sent = [{"to": ["amy@atlas.example"], "subject": "a"},
                {"to": ["joe@atlas.example"], "subject": "b"}]
        it = assist.parse("email Atlas: cabinet")
        made = assist.draft(it, self.resolve("email Atlas: cabinet", sent), [])
        self.assertEqual(made["to"], [])
        self.assertEqual(made["ask"][0]["pick"], "one")
        self.assertTrue(any(g.startswith('Who is "Atlas"? Pick one: ') for g in made["gaps"]))


class AnswersTests(unittest.TestCase):
    def test_the_agent_answers_whatever_the_fast_path_left(self):
        it = assist.parse("email Pat: PO8741 and OR1000120001")
        ran = []

        def fast(q):
            return ("PO8741 is open.", None) if "PO8741" in q else ("", "none")

        def agent(q):
            ran.append(q)
            return ""
        answers, settled = assist.answers_for(it, fast, agent)
        self.assertEqual(ran, ["status of OR1000120001"])
        self.assertEqual(settled, ["fast", "none"])
        self.assertEqual([a["found"] for a in answers], [None, "none"])
        made = assist.draft(it, assist.Recipients(to=["pat@plx.test"]), answers)
        self.assertEqual([c["outcome"] for c in made["checks"]], ["fact", "not_found"])


class RecordChecksTests(unittest.TestCase):
    """Every record the line names ends in a fact in the message or a plain note to the
    person: never left out silently, never a search note told to the recipient."""

    def made(self, answers, line="email Pat: PO8741 and OR1000120001"):
        return assist.draft(assist.parse(line), assist.Recipients(to=["pat@plx.test"]),
                            answers)

    def test_each_record_has_its_outcome(self):
        made = self.made([
            {"question": "status of PO8741", "facts": [], "found": "none"},
            {"question": "status of OR1000120001", "facts": [], "found": "several"}])
        self.assertEqual([(c["record"], c["outcome"]) for c in made["checks"]],
                         [("PO8741", "not_found"), ("OR1000120001", "several")])
        self.assertIn("No record of PO8741 found in the records you can read", made["gaps"][0])
        self.assertIn("OR1000120001 matches several records", made["gaps"][1])

    def test_a_search_note_is_never_a_fact(self):
        made = self.made([{"question": "status of PO8741", "facts": [
            {"text": "I stopped researching at the time limit.", "sources": ["gated"]},
            {"text": "PO8741 is open.", "sources": ["gated"]}]},
            {"question": "status of OR1000120001", "facts": [
                {"text": "I couldn't confirm its status.", "sources": ["gated"]}]}])
        self.assertEqual([f["text"] for f in made["facts"]], ["PO8741 is open."])
        self.assertEqual([c["outcome"] for c in made["checks"]], ["fact", "unsettled"])
        self.assertIn("I couldn't confirm its status.", made["checks"][1]["note"])
        self.assertNotIn("time limit", made["body"] or "")
        for note in ("No matching purchase order was found.", "The search timed out.",
                     "Lookup failed for PO8741."):
            made = self.made([{"question": "status of PO8741", "facts": [
                {"text": note, "sources": ["gated"]}]}])
            self.assertEqual((made["facts"], made["checks"][0]["outcome"]), ([], "unsettled"))
        # "I" as a record's letter or inside a word is no search note
        made = self.made([{"question": "status of PO8741", "facts": [
            {"text": "PO8741 is for item I-200 in Bin A.", "sources": ["gated"]}]}])
        self.assertEqual(len(made["facts"]), 1)

    def test_a_record_without_an_answer_is_still_said(self):
        made = self.made([{"question": "status of PO8741", "facts": [
            {"text": "PO8741 is open.", "sources": ["h"]}]}])
        self.assertEqual([(c["record"], c["outcome"]) for c in made["checks"]],
                         [("PO8741", "fact"), ("OR1000120001", "unsettled")])


class RecordsNamedTests(unittest.TestCase):
    def test_a_day_range_is_not_a_record_an_id_is(self):
        self.assertEqual(assist.parse("email Pat: Time sheet Oct. 05-10").records, [])
        self.assertEqual(assist.parse("email Pat: return MC3025-GR and PO 90412").records,
                         ["MC3025-GR", "90412"])
        # a quantity with its unit is not a record
        self.assertEqual(assist.parse("email Pat: 100ml amber dropper, 250 pcs of 12x20kg"
                                      ).records, [])


class ShortFormTests(unittest.TestCase):
    """A record written short ("OR 120001") is asked by the full form the reader's own mail
    shows it stands for, and only then."""

    @staticmethod
    def mail(full=30, short=(), both=True, other=()):
        rows = [{"to": ["x@acme.example"], "subject": f"Order OR1000{110000 + n}"}
                for n in range(full)]
        rows += [{"to": ["x@acme.example"], "subject": f"OR {s}"} for s in short]
        if both:     # one order the mail writes both ways
            rows += [{"to": ["x@acme.example"], "subject": "OR 110003 short"}]
        rows += [{"to": ["x@acme.example"], "subject": s} for s in other]
        return rows

    def test_the_numbering_the_reader_writes_expands_the_short_form(self):
        it = assist.with_thread(assist.parse("email Acme: OR 120001"), self.mail())
        self.assertEqual(it.records, ["OR1000120001"])
        self.assertEqual(assist.questions(it), ["status of OR1000120001"])
        made = assist.draft(it, assist.Recipients(to=["x@acme.example"]), [
            {"question": "status of OR1000120001", "facts": [
                {"text": "Sales order OR1000120001: status Open.", "sources": ["h1"]}]}])
        # the fact names the full form; how the number was read is said
        self.assertIn("120001 was read as OR1000120001, the form your own mail writes it in",
                      made["gaps"])
        self.assertEqual(len(made["facts"]), 1)
        # going outside, a record read from a short form may be another customer's: its
        # facts stay inside
        made = assist.draft(it, assist.Recipients(to=["x@acme.example"]), [
            {"question": "status of OR1000120001", "facts": [
                {"text": "Sales order OR1000120001: status Open.", "sources": ["h1"]}]}],
            outside=True)
        self.assertEqual(made["facts"], [])
        self.assertIn("1 facts about other records were left out: the message goes outside "
                      "the company", made["gaps"])

    def test_no_attested_numbering_keeps_the_record_as_written(self):
        line = "email Acme: OR 120001"
        for sent in (self.mail(both=False),                     # never written both ways
                     self.mail(full=SHAPE_MIN_LESS),            # too few full forms
                     # as many written short: the short form is a numbering of its own
                     self.mail(short=[str(130000 + n) for n in range(30)]),
                     # two openings of the same length: none is the numbering
                     self.mail(other=[f"OR2000{140000 + n}" for n in range(30)])):
            it = assist.with_thread(assist.parse(line), sent)
            self.assertEqual((it.records, it.read_as), (["120001"], {}))
        # a number shorter than a short form: as written
        for line in ("email Acme: OR 1201",):
            it = assist.with_thread(assist.parse(line), self.mail())
            self.assertEqual(it.read_as, {}, line)


    def test_a_bare_number_is_asked_as_written_and_as_the_one_form_the_mail_ends_in_it(self):
        one = [{"to": ["x@acme.example"], "subject": "Invoice IN0179213 copy"}]
        it = assist.with_thread(assist.parse("email Acme: invoice 179213 overdue"), one)
        self.assertEqual(assist.questions(it), ["status of 179213", "status of IN0179213"])
        made = assist.draft(it, assist.Recipients(to=["x@acme.example"]), [
            {"question": "status of 179213", "facts": [], "found": "none"},
            {"question": "status of IN0179213", "facts": [
                {"text": "Invoice IN0179213: open.", "sources": ["h1"]}]}], outside=True)
        # the form read from the mail may be another's: it stays inside, and is said
        self.assertEqual([c["outcome"] for c in made["checks"]], ["not_found", "unsettled"])
        self.assertEqual(made["facts"], [])
        # a fact of the read form naming the bare number stays inside too
        made = assist.draft(it, assist.Recipients(to=["x@acme.example"]), [
            {"question": "status of 179213", "facts": [], "found": "none"},
            {"question": "status of IN0179213", "facts": [
                {"text": "Order 179213 shipped.", "sources": ["h1"]}]}], outside=True)
        self.assertEqual(made["facts"], [])
        # two forms end in it, or none: as written only
        two = one + [{"to": ["x@acme.example"], "subject": "SO10179213"}]
        for sent in (two, []):
            it = assist.with_thread(assist.parse("email Acme: invoice 179213"), sent)
            self.assertEqual((it.records, it.read_as), (["179213"], {}))


SHAPE_MIN_LESS = assist.SHAPE_MIN - 1


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
