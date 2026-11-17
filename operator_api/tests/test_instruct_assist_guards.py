"""Guards on the Ask box's drafting assist (``operator_api.instruct_assist``): a short
record number is read in full only when every recipient is settled and inside the company;
the reader's own mailbox alone is read, and a failed, late or unavailable read (or people
exclusion) proposes nobody rather than guessing; the notes beside a draft never name a
withheld person and say every record the line names. Evidence: isolated-fixture
(injected mail history, no network, no model)."""

from __future__ import annotations

from concurrent.futures import Future
import json
import threading
import time
import unittest
from unittest import mock

from operating import assist
from operator_api import instruct_assist as ia
from operator_api.tests import test_instruct as ti

ANA, BEN = ti.ANA, ti.BEN
EXCLUDED = ti.EXCLUDED

# Ana's own mail writes order numbers as "OR1000" and six digits, and once both ways: a
# line's "OR 120001" is read as OR1000120001 (``assist.expand_records``)
NUMBERED = ([{"at": "2026-11-01T10:00:00Z", "to": ["orders@plx.example"], "cc": [],
              "subject": f"OR1000{120100 + i} picked"} for i in range(24)]
            + [{"at": "2026-11-09T10:00:00Z", "to": ["orders@plx.example"], "cc": [],
                "subject": "OR 120102 and OR1000120102"}])
LINE = "email Ben about OR 120001 ship date"


class People:
    """The captures' directory: Ana, Ben, Dee, and an excluded colleague (Cal)."""
    unavailable = None
    internal = ("plx.example",)
    directory = {ANA[0]: ANA[1], BEN[0]: BEN[1], "dee@plx.example": "dddd",
                 "cal@plx.example": EXCLUDED}

    def identity_clear(self, value):
        return str(value).lower() not in (EXCLUDED, "cal@plx.example")


class Reader:
    def __init__(self, oid=ANA[1]):
        self.session = type("Session", (), {"subject_oid": oid})()


REAL = object()   # Host: the resolver's own answer


class Host(ia.InstructAssistMixin):
    """The mixin with Ana's mail injected; ``scripted`` replaces the resolver's answer so
    the read-in-full rule is tested on its own."""

    def __init__(self, mail=None, scripted=REAL):
        self.mail = (People(), list(NUMBERED)) if mail is None else mail
        self.scripted = scripted
        self.resolved = 0
        self.instruct_history = lambda reader: self.mail or None

    def _assist_resolve(self, reader, intent, people, sent):
        self.resolved += 1
        if self.scripted is REAL:
            return super()._assist_resolve(reader, intent, people, sent)
        return self.scripted


def to(*addresses, cc=(), **kw):
    return assist.Recipients(to=list(addresses), cc=list(cc), **kw)


class ReadInFullOnlyInside(unittest.TestCase):
    """``assist_before``: a short number is read in full only when every recipient is
    settled and inside the company; anything less says it was not read."""

    def before(self, recipients, replying=False, line=LINE):
        """(host, ``assist_before`` of ``line`` with ``recipients`` as resolved)."""
        host = Host(scripted=recipients)
        return host, host.assist_before(Reader(), line, replying)

    def assertRead(self, aim):
        self.assertEqual(aim.expanded, ["OR1000120001"])
        self.assertEqual(aim.notes, ["120001 was read as OR1000120001, the form your own "
                                     "mail writes it in."])

    def assertNotRead(self, aim):
        self.assertEqual(aim.expanded, [])
        self.assertEqual(len(aim.notes), 1)
        self.assertIn("not read, as this message may go outside", aim.notes[0])

    def test_every_recipient_inside_reads_it_in_full(self):
        self.assertRead(self.before(to(BEN[0], cc=["dee@plx.example"]))[1])

    def test_one_outside_recipient_among_several_to_keeps_it_unread(self):
        self.assertNotRead(self.before(to(BEN[0], "pat@maple.example"))[1])

    def test_an_outside_cc_keeps_it_unread(self):
        self.assertNotRead(self.before(to(BEN[0], cc=["pat@maple.example"]))[1])

    def test_an_outside_candidate_asked_about_keeps_it_unread(self):
        asked = to(BEN[0], ask=[{"field": "cc", "pick": "any", "mention": None,
                                 "candidates": ["dee@plx.example", "pat@maple.example"]}])
        self.assertNotRead(self.before(asked)[1])

    def test_a_choice_or_an_unresolved_name_keeps_it_unread(self):
        self.assertNotRead(self.before(to(BEN[0], choices={"Dee": ["dee@plx.example"]}))[1])
        self.assertNotRead(self.before(to(BEN[0], unresolved=["Pat"]))[1])

    def test_nobody_settled_or_marked_outside_keeps_it_unread(self):
        self.assertNotRead(self.before(to(withheld=1))[1])          # only a withheld person
        self.assertNotRead(self.before(to(BEN[0], outside=True))[1])
        self.assertNotRead(self.before(None)[1])                     # not resolved

    def test_address_case_does_not_make_an_inside_recipient_outside(self):
        self.assertRead(self.before(to("BEN@PLX.Example", cc=["Dee@Plx.Example"]))[1])

    def test_a_subdomain_is_inside_a_lookalike_domain_is_not(self):
        self.assertRead(self.before(to("ben@uk.plx.example"))[1])
        self.assertNotRead(self.before(to("ben@evilplx.example"))[1])
        self.assertNotRead(self.before(to("ben@plx.example.evil.com"))[1])

    def test_a_reply_never_reads_it_and_resolves_no_recipients(self):
        host, aim = self.before(to(BEN[0]), replying=True)
        self.assertNotRead(aim)
        self.assertIsNone(aim.recipients)
        self.assertEqual(host.resolved, 0)

    def test_no_mail_at_hand_reads_nothing_and_never_waits(self):
        host = Host(mail=False, scripted=to(BEN[0]))
        waits = []
        host._assist_mail = lambda reader, wait: waits.append(wait)
        aim = host.assist_before(Reader(), LINE, False)
        self.assertEqual((aim.expanded, aim.notes, aim.recipients), ([], [], None))
        self.assertEqual(host.resolved, 0)
        self.assertEqual(waits, [0.0])     # the mail is used only when already at hand


class OnlyTheReadersOwnMail(unittest.TestCase):
    """``_assist_mail`` and ``HISTORY``: the reader's own mailbox only, keyed by their own
    object id; a failed or slow read proposes nobody and is tried again."""

    def setUp(self):
        self.history = ia._History()
        stamp = mock.patch("operating.pg.permissions.exclusion_stamp", return_value="s1")
        self.stamp = stamp.start()
        self.addCleanup(stamp.stop)
        self.reads = []

    def read(self, state, oid):
        self.reads.append(oid)
        return People(), [{"to": [f"for-{oid}@maple.example"], "cc": [], "subject": "x"}]

    def test_the_mail_read_is_the_signed_in_reader_s_own_lowercased(self):
        host = ia.InstructAssistMixin()
        host.state_dir = "/nonexistent-state"
        seen = []
        with mock.patch.object(ia, "HISTORY") as history:
            history.get.side_effect = lambda state, oid, wait: seen.append(oid)
            host._assist_mail(Reader("AAAAAAAA-1111-4111-8111-111111111111"), 0.0)
            # no object id or no captures: nothing read at all
            self.assertIsNone(host._assist_mail(Reader(""), 0.0))
            host.state_dir = None
            self.assertIsNone(host._assist_mail(Reader(), 0.0))
        self.assertEqual(seen, [ANA[1]])

    def test_each_reader_s_mail_is_kept_apart(self):
        with mock.patch.object(ia, "_read_mail", side_effect=self.read):
            ana = self.history.get(ti.Path("/s"), ANA[1], 5.0)
            ben = self.history.get(ti.Path("/s"), BEN[1], 5.0)
            again = self.history.get(ti.Path("/s"), ANA[1], 5.0)
        self.assertEqual(self.reads, [ANA[1], BEN[1]])
        self.assertEqual(ana[1][0]["to"], [f"for-{ANA[1]}@maple.example"])
        self.assertEqual(ben[1][0]["to"], [f"for-{BEN[1]}@maple.example"])
        self.assertIs(again, ana)

    def test_a_failed_read_proposes_nobody_and_is_read_again(self):
        calls = []

        def flaky(state, oid):
            calls.append(oid)
            if len(calls) == 1:
                raise OSError("capture.db locked")
            return self.read(state, oid)
        with mock.patch.object(ia, "_read_mail", side_effect=flaky):
            self.assertIsNone(self.history.get(ti.Path("/s"), ANA[1], 5.0))
            self.assertIsNotNone(self.history.get(ti.Path("/s"), ANA[1], 5.0))
        self.assertEqual(len(calls), 2)

    def test_a_changed_exclusion_or_an_old_read_is_read_again(self):
        with mock.patch.object(ia, "_read_mail", side_effect=self.read):
            self.history.get(ti.Path("/s"), ANA[1], 5.0)
            self.stamp.return_value = "s2"            # the people exclusion changed
            self.history.get(ti.Path("/s"), ANA[1], 5.0)
            with mock.patch.object(ia, "HISTORY_TTL", -1.0):
                self.history.get(ti.Path("/s"), ANA[1], 5.0)
        self.assertEqual(len(self.reads), 3)

    def test_a_slow_read_returns_nothing_without_waiting_for_it(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(state, oid):
            gate.wait(10)
            return self.read(state, oid)
        with mock.patch.object(ia, "_read_mail", side_effect=slow):
            started = time.monotonic()
            self.assertIsNone(self.history.get(ti.Path("/s"), ANA[1], 0.0))
            self.assertLess(time.monotonic() - started, 1.0)
            gate.set()


class FailClosedOnPeople(unittest.TestCase):
    """``_read_mail``: an unavailable or empty people exclusion reads no mail and proposes
    nobody; the mailbox read is the reader's own, with only their own addresses."""

    def test_an_exclusion_that_cannot_load_reads_no_mail(self):
        down = type("Down", (), {"unavailable": "file missing", "directory": People.directory})()
        empty = type("Empty", (), {"unavailable": None, "directory": {}})()
        for people in (None, down, empty):
            with mock.patch("operating.chunking.permissions.load_exclusion",
                            return_value=people), \
                    mock.patch.object(assist, "capture_history") as history:
                self.assertIsNone(ia._read_mail(ti.Path("/s"), ANA[1]))
                history.assert_not_called()

    def test_the_mailbox_read_is_the_reader_s_with_their_own_addresses_only(self):
        people = People()
        people.directory = {**People.directory, "ana.r@plx.example": ANA[1]}
        with mock.patch("operating.chunking.permissions.load_exclusion",
                        return_value=people), \
                mock.patch.object(assist, "capture_history", return_value=[]) as history:
            self.assertEqual(ia._read_mail(ti.Path("/s"), ANA[1]), (people, []))
        (_state, oid, own), _kw = history.call_args
        self.assertEqual(oid, ANA[1])
        self.assertEqual(own, {ANA[0], "ana.r@plx.example"})

    def test_a_reader_with_no_address_of_their_own_reads_no_mailbox(self):
        with mock.patch("operating.chunking.permissions.load_exclusion",
                        return_value=People()), \
                mock.patch.object(assist, "capture_history") as history:
            self.assertEqual(ia._read_mail(ti.Path("/s"), "eeee")[1], [])
            history.assert_not_called()

    def test_an_unavailable_exclusion_proposes_nobody_not_everybody(self):
        down = People()
        down.unavailable = "guard down"
        host = Host(mail=(down, list(NUMBERED)))
        self.assertIsNone(host.assist_recipients(Reader(), "email Ben about it").result(5))
        aim = host.assist_before(Reader(), LINE, False)
        self.assertEqual((aim.recipients, aim.expanded), (None, []))
        self.assertEqual(ia.address_notes(aim.recipients, str),
                         (None, ["Add the recipient's address before sending."]))


class Resolving(unittest.TestCase):
    """``_assist_intent`` and ``_assist_resolve``: words that point at the page name nobody;
    the reader is themselves (case aside); a failing record read or roles file never
    fails the draft; an excluded person is never proposed or offered."""

    def test_words_pointing_at_the_page_name_nobody_whatever_their_case(self):
        host = Host()
        intent = host._assist_intent("email Them and Ben about it", [])
        self.assertEqual(intent.mentions, ["Ben"])
        self.assertIsNone(host.assist_recipients(Reader(), "email THEM about it").result(5))

    def test_the_reader_is_passed_as_themselves_whatever_the_id_s_case(self):
        host = Host()
        seen = []
        with mock.patch.object(assist, "resolve",
                               side_effect=lambda intent, **kw: seen.append(kw["me"])):
            host.assist_recipients(Reader(ANA[1].upper()), "email Ben about it").result(5)
        self.assertEqual(seen, [ANA[1]])

    def test_a_failing_record_read_or_roles_file_never_fails_the_draft(self):
        host = Host(mail=(People(), [{"at": "2026-11-01T10:00:00Z",
                                     "to": ["pat.lee@maple.example"], "cc": [],
                                     "subject": "Lavender bars"}]))
        host.reads = type("Reads", (), {"run": lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("Portal down"))})()

        def roles():
            raise OSError("roles file unreadable")
        host._apa_roles = roles
        got = host.assist_recipients(Reader(), "email Pat Lee about the bars").result(5)
        self.assertEqual(got.to, ["pat.lee@maple.example"])
        team = host.assist_recipients(Reader(), "email the team about the bars").result(5)
        self.assertEqual((team.to, team.unresolved), ([], ["team"]))

    def test_an_excluded_colleague_on_the_reader_s_habit_is_never_proposed_or_offered(self):
        mail = [{"at": f"2026-11-0{1 + i}T10:00:00Z", "to": [BEN[0]],
                 "cc": ["cal@plx.example"] if i < 5 else ["dee@plx.example"],
                 "subject": "Lavender bars"} for i in range(8)]
        host = Host(mail=(People(), mail))
        got = host.assist_recipients(Reader(), "email Ben about the lavender bars").result(5)
        proposed, notes = ia.address_notes(got, str)
        self.assertEqual(proposed, BEN[0])
        self.assertNotIn("cal", json.dumps([got.to, got.cc, got.ask, notes]).lower())
        self.assertIn("Someone this message would go to can't be added here: check the "
                      "recipients.", notes)

    def test_an_empty_mailbox_settles_no_one_outside(self):
        got = Host(mail=(People(), [])).assist_recipients(
            Reader(), "email Pat about the bars").result(5)
        self.assertEqual(ia.address_notes(got, str)[0], None)
        self.assertEqual(got.unresolved, ["Pat"])

    def test_a_resolver_refusal_proposes_nobody(self):
        host = Host()
        with mock.patch.object(assist, "resolve",
                               side_effect=assist.AssistRefused("not_an_employee")):
            self.assertIsNone(host.assist_recipients(Reader(), "email Ben").result(5))


class Settling(unittest.TestCase):
    """``settle``: recipients that fail or come late are none (no guess, no crash); a
    resolution already made is kept."""

    def test_a_failed_resolution_is_no_proposal(self):
        failed = Future()
        failed.set_exception(OSError("mail store gone"))
        self.assertIsNone(ia.settle(failed, ia.Aim()).recipients)

    def test_a_late_resolution_is_no_proposal(self):
        with mock.patch.object(ia, "RECIPIENT_WAIT", 0.01):
            self.assertIsNone(ia.settle(Future(), ia.Aim()).recipients)

    def test_recipients_already_resolved_are_kept(self):
        mine, theirs = to(BEN[0]), Future()
        theirs.set_result(to("pat@maple.example"))
        self.assertIs(ia.settle(theirs, ia.Aim(recipients=mine)).recipients, mine)
        self.assertIsNone(ia.settle(None, ia.Aim()).recipients)


def strip_cal(text):
    """The draft's own cleaning (``feedback.clean``) as the notes are said: an excluded
    person's name and address withheld."""
    return text.replace("cal@plx.example", "").replace("Cal", "").strip()


class AddressNotes(unittest.TestCase):
    """``address_notes``: the To proposed only when settled; every question cleaned; a
    withheld person said without a name."""

    def test_a_withheld_person_is_said_without_a_name(self):
        proposed, notes = ia.address_notes(to(BEN[0], withheld=1), strip_cal)
        self.assertEqual(proposed, BEN[0])
        withheld = [n for n in notes if "can't be added here" in n]
        self.assertEqual(withheld, ["Someone this message would go to can't be added here: "
                                    "check the recipients."])

    def test_a_question_names_nobody_the_cleaning_withholds(self):
        asked = to(ask=[{"field": "to", "pick": "one", "mention": "Cal Diaz",
                         "candidates": ["cal@plx.example", "pat@maple.example"]}])
        proposed, notes = ia.address_notes(asked, strip_cal)
        self.assertIsNone(proposed)
        self.assertNotIn("cal", " ".join(notes).lower())
        # the question is a one-tap pick (``recipient_questions``) offering only who the
        # cleaning keeps
        [question] = ia.recipient_questions(asked, strip_cal)
        self.assertEqual((question["field"], question["pick"], question["candidates"]),
                         ("to", "one", ["pat@maple.example"]))
        self.assertNotIn("cal", repr(question).lower())

    def test_candidates_all_withheld_ask_only_to_check(self):
        asked = to(BEN[0], ask=[{"field": "cc", "pick": "any", "mention": None,
                                 "candidates": ["cal@plx.example"]}])
        _proposed, notes = ia.address_notes(asked, strip_cal)
        self.assertIn("Check the recipients before sending.", notes)
        self.assertFalse([n for n in notes if n.startswith("Add to Cc")])

    def test_a_cc_alone_is_no_proposed_to(self):
        proposed, notes = ia.address_notes(to(cc=["dee@plx.example"]), str)
        self.assertIsNone(proposed)
        self.assertEqual(notes[-1], "Add the recipient's address before sending.")
        self.assertFalse([n for n in notes if "filled in from your own mail" in n])

    def test_several_to_propose_the_first_and_say_the_rest(self):
        # the rest go on the draft's ``also_to`` (instruct_api), said by the plural note
        proposed, notes = ia.address_notes(to(BEN[0], "dee@plx.example"), str)
        self.assertEqual(proposed, BEN[0])
        self.assertEqual(notes[0], "The recipients are filled in from your own mail: check "
                                   "them before sending.")
        self.assertEqual(notes.count(notes[0]), 1)

    def test_a_choice_already_asked_is_asked_once(self):
        asked = to(choices={"Dee": ["dee@plx.example", "dee.b@plx.example"]},
                   ask=[{"field": "to", "pick": "one", "mention": "Dee",
                         "candidates": ["dee@plx.example", "dee.b@plx.example"]}])
        _proposed, notes = ia.address_notes(asked, str)
        questions = ia.recipient_questions(asked, str)
        self.assertEqual(sum("Dee" in n for n in notes)
                         + sum("Dee" in q["label"] for q in questions), 1)
        self.assertEqual([q["candidates"] for q in questions],
                         [["dee@plx.example", "dee.b@plx.example"]])


class UnreadNotes(unittest.TestCase):
    """``unread_notes``: every record the line names ends in a plain note, once; a short
    number read in full is said by its full form only; nothing the line never named."""

    def test_a_short_number_read_in_full_is_said_by_its_full_form_only(self):
        notes = ia.unread_notes("email Ben about OR 120001", ["120001", "OR1000120001"],
                                ["OR1000120001"])
        self.assertEqual(len(notes), 1)
        self.assertIn("No record of OR1000120001 found", notes[0])

    def test_each_record_is_said_once(self):
        notes = ia.unread_notes("email Ben about PO 77777", ["77777", "77777", "?77777"], [])
        self.assertEqual(sum(n.startswith("No record of 77777") for n in notes), 1)

    def test_nothing_the_line_never_named_is_said(self):
        self.assertEqual(ia.unread_notes("email Ben about PO 77777", ["55555", "?44444"], []),
                         [])


class AssistInTheDraft(ti.Desk):
    """Through the one box (``instruct_api``): an address the draft's own cleaning would
    strip is never proposed; an unavailable people exclusion proposes nobody and reads
    no short number in full."""

    def drafted(self, line):
        self.backend.instruct_call = ti.Model(
            "[[DRAFT email]]\nSubject: Order\nHi,\nAbout the order.\nAna\n[[END]]")
        out = self.instruct(self.ana, line, mode="do")
        self.assertEqual(out.status_code, 200, out.text)
        return out.json()["drafts"][0]

    def test_an_address_the_draft_s_cleaning_withholds_is_never_proposed(self):
        # the captures' directory still clears Dee; the desk's exclusion no longer does
        self.backend.instruct_history = lambda reader: (People(), [])
        self.guard.is_excluded = lambda value: str(value).lower() in (
            EXCLUDED, "dee@plx.example")
        draft = self.drafted("email Dee about the lavender bars")
        self.assertIsNone(draft["to"])
        self.assertNotIn("dee@plx.example", json.dumps(draft))

    def test_an_unavailable_people_exclusion_proposes_nobody(self):
        down = People()
        down.unavailable = "guard down"
        self.backend.instruct_history = lambda reader: (down, list(NUMBERED)) \
            if reader.session.subject_oid == ANA[1] else None
        looked = []
        plain = self.backend.suggest
        self.backend.suggest = lambda reader, q, limit: looked.append(q) or plain(
            reader, q, limit)
        draft = self.drafted(LINE)
        self.assertIsNone(draft["to"])
        self.assertIn("Add the recipient's address before sending.", draft["missing"])
        self.assertNotIn("OR1000120001", looked)


if __name__ == "__main__":
    unittest.main()
