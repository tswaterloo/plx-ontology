"""The work a message leaves its reader (``operating.autopilot_message_work``, registry
``message_work``): only the triage model's reading makes the item, in its own words; on the
replay fixture's mail. Evidence: isolated-fixture."""
from __future__ import annotations

import unittest

from operating import autopilot as ap
from operating.autopilot_queue import matches
from operating.tests import test_autopilot_replay as trp
from operating.tests.test_autopilot_replay import OURS, REP, SENT, mail
from operating.tests.test_autopilot_triage import StubTriage as _Stub

WORK = "process the BOL for today's pickup"


class MessageWork(trp.WaitingOnYou):
    BOL = "BOL attached for today's pickup, 7 pallets."
    AUTO = "Your shipment has been picked up. Track it here."
    NOTE = "Thanks, all good on our side."

    def work_items(self) -> list[dict]:
        q = self.queue_file()
        return [i for i in q.items(None) if i["autopilot"] == "message_work"]

    def test_cardinality_only_fyi_with_work_from_a_known_sender_makes_the_to_do(self):
        """Each message read once by the model; only one it read as FYI with work named makes
        the to-do, titled in the model's own words, holding the to-do and the message link;
        one read as owed a reply goes to the reply item, an automated one and an FYI with no
        work make nothing. N=1/10/60: no item lost or doubled."""
        kinds = {self.BOL: ("fyi", WORK), self.REQUEST: ("reply", ""),
                 self.AUTO: ("automated", "track the shipment"), self.NOTE: ("fyi", "")}
        texts = (self.BOL, self.REQUEST, self.AUTO, self.NOTE)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.correspondent()
                for i in range(n):
                    self.thread(i, ask=texts[i % 4], earlier=False)
                self.store()
                model = _Stub(kinds)
                out = self.mail_run(model)
                want = (n + 3) // 4
                self.assertEqual(len([m for b in model.sent for m in b]), n)
                self.assertEqual(out["prepared_by_autopilot"].get("message_work", 0), want)
                self.assertEqual(out["prepared_by_autopilot"].get("reply_prep", 0),
                                 (n + 2) // 4)
                got = self.work_items()
                self.assertEqual(len(got), want)
                self.assertEqual(len({i["event_id"] for i in got}), want)
                for item in got:
                    prepared = item["body"]["prepared"]
                    self.assertEqual(prepared["kind"], "todo")
                    self.assertEqual(prepared["todos"], [{"system": "", "record": "",
                                                          "what": WORK}])
                    self.assertTrue(prepared["summary"].startswith("Process the BOL"))
                    self.assertIn(WORK.split()[1], item["title"].lower())
                    self.assertEqual(prepared["links"][0]["label"], "Open the message")

    def test_no_reading_no_item_and_a_cold_or_copied_reader_gets_none(self):
        """The item stands on the model's reading alone: with no model (or a failed call)
        the same mail makes no to-do. Read FYI with work, it still needs a sender the reader
        knows (a cold sender's 'work' is a pitch) and the reader in To (only copied: the work
        is the addressee's)."""
        from operating.tests.test_autopilot_replay import OTHER
        for case in ("no_model", "failed", "cold", "copied", "known"):
            with self.subTest(case=case):
                self.setUp()
                if case != "cold":
                    self.correspondent()
                if case == "copied":
                    self.sent(mail(self.w, SENT, sender=REP, to=(OTHER,), cc=(OURS,),
                                   body=self.BOL, subject="BOL", mailbox=ap_box(),
                                   conversation="copied", n=78_000, hour=10), 10)
                else:
                    self.thread(0, ask=self.BOL, earlier=False)
                self.store()
                if case == "no_model":
                    out = self.replay()
                else:
                    model = _Stub({self.BOL: ("fyi", WORK)})
                    if case == "failed":
                        model.fail = "none"
                        model.__class__ = _Failing
                    out = self.mail_run(model)
                self.assertEqual(out["prepared_by_autopilot"].get("message_work", 0),
                                 1 if case == "known" else 0, case)

    def test_cardinality_a_message_another_autopilot_prepared_gets_no_second_item(self):
        """A supplier's date for one PO is the supplier ETA item's: read FYI with work too,
        it gets no to-do beside it (the registry's ``fallback``). N=1/10/60."""
        ship = "PO P-{} will ship on Nov 20."
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    self.supplier_mail(i, ship.format(900 + i), po=self.po(i))
                self.store()
                model = _Stub({ship.format(900 + i): ("fyi", "update the PO's expected date")
                               for i in range(n)})
                out = self.mail_run(model)
                made = out["prepared_by_autopilot"]
                self.assertEqual(made.get("supplier_eta"), n)
                self.assertEqual(made.get("message_work", 0), 0)

    def test_the_registry_reads_the_work_only_from_a_reading(self):
        """``triage_work_in`` needs the model's reading of that kind with work named;
        ``triage_work_none`` (the job autopilot) passes every other message, unread ones
        too, so the same message never starts two job sessions."""
        reg = {a["id"]: a for a in ap.load_registry()["autopilots"]}
        mw, jm = reg["message_work"]["when"], reg["job_message"]["when"]
        base = {"object_type": "Email", "to_me": True, "addressed_other": False,
                "side": "supplier"}
        for reading, mw_want in ((None, False), (("fyi", WORK), True), (("fyi", ""), False),
                                 (("fyi", "  "), False), (("reply", WORK), False),
                                 (("automated", WORK), False), (("notice", WORK), False)):
            with self.subTest(reading=reading):
                event = dict(base)
                if reading:
                    event["triage"], event["triage_work"] = reading
                self.assertEqual(matches({"triage_work_in": mw["triage_work_in"]}, event),
                                 mw_want)
                self.assertEqual(matches({"triage_work_none": jm["triage_work_none"]}, event),
                                 not mw_want)
        with self.assertRaises(ap.RegistryInvalid):
            ap._check_when({"triage_work_in": ["fyi", "maybe"]}, "x")
        with self.assertRaises(ap.RegistryInvalid):
            ap._check_when({"triage_work_none": []}, "x")

    def test_an_excluded_sender_is_never_read_and_gets_no_to_do(self):
        """People first: a mail from someone the exclusion does not clear never reaches the
        model, so no reading and no to-do (the screen runs before any call), while the same
        mail from a cleared sender in the same run does."""
        from operating.tests.test_autopilot_replay import BOSS, OURS_OID
        self.correspondent()
        self.thread(0, ask=self.BOL, earlier=False)
        self.sent(mail(self.w, SENT, sender=BOSS, to=(OURS,), body=self.BOL + " (boss)",
                       subject="BOL", mailbox=OURS_OID, conversation="barred", n=77_000,
                       hour=10), 10)
        self.store()
        model = _Stub({self.BOL: ("fyi", WORK), self.BOL + " (boss)": ("fyi", WORK)})
        out = self.mail_run(model)
        self.assertEqual(out["prepared_by_autopilot"].get("message_work", 0), 1)
        read = [m["text"] for b in model.sent for m in b]
        self.assertEqual(read, [self.BOL])


class Switch(unittest.TestCase):
    def test_the_to_dos_are_their_own_choice_off_for_everyone_until_turned_on(self):
        """A person turns message to-dos on or off on their own (``DRAFT_CHOICES``), apart
        from replies; no Draft file turns them on for anyone (owner and Vince included)."""
        import json as _json
        from operating import autopilot_write as aw, workflows as wf
        self.assertEqual(wf.of_item("message_work", {"subject": "PO 4410", "text": "x"}),
                         "message_todos")
        self.assertIn("message_todos", wf.names())
        self.assertEqual(aw.draft_choice("message_todos"), ("message_todos",))
        others = [f for cid, _l, flows in aw.DRAFT_CHOICES if cid != "message_todos"
                  for f in flows]
        self.assertNotIn("message_todos", others)
        draft = _json.loads((ap.REGISTRY_FILE.parent / "autopilot_draft.json").read_text())
        self.assertFalse(any("message_todos" in v for v in draft["people"].values()))


# only this module's tests: the fixture's own run in its module
for _name in dir(trp.WaitingOnYou):
    if _name.startswith("test") and _name not in MessageWork.__dict__:
        setattr(MessageWork, _name, None)
del _name


class _Failing(_Stub):
    def __call__(self, batch):
        self.sent.append([dict(m) for m in batch])
        return [None] * len(batch)


def ap_box() -> str:
    from operating.tests.test_autopilot_replay import OURS_OID
    return OURS_OID


if __name__ == "__main__":
    unittest.main()
