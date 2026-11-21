"""Every message triaged by the model before a reply item (``operating.autopilot_triage``,
``chat_ask_model``): the triage reading's format, the registry's ``triage_none``, and on the
replay fixture's mail that only a reply reading makes a reply item. Evidence: isolated-fixture."""
from __future__ import annotations

import json
import unittest

from operating.tests.test_autopilot_replay import StubAsks, WaitingOnYou


class StubTriage(StubAsks):
    """The triage model's stand-in: ``kinds`` maps a text to its (kind, work); any other
    text is "fyi" with no work."""

    def __init__(self, kinds):
        super().__init__()
        self.kinds = dict(kinds)

    def __call__(self, batch):
        self.sent.append([dict(m) for m in batch])
        self.batches.append([m["text"] for m in batch])
        return [(k == "reply", k, w) for k, w in
                (self.kinds.get(m["text"], ("fyi", "")) for m in batch)]


class Triage(WaitingOnYou):
    def test_cardinality_only_mail_triaged_as_owed_a_reply_makes_a_reply_item(self):
        """Every mail to the reader is triaged by the model, whatever the phrase rules
        found: one the rules read as asking but the model reads as automated or FYI makes
        no reply item (its work is kept with the verdict); a request the rules missed that
        the model reads as owed a reply does. Each mail read once; N=1/10/60."""
        auto = "Can you confirm the quantity for next week?"
        kinds = {self.REQUEST: ("reply", ""), auto: ("automated", ""),
                 self.FYI: ("fyi", "pass the labels to shipping")}
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.correspondent()
                texts = (self.REQUEST, auto, self.FYI)
                for i in range(n):
                    self.thread(i, ask=texts[i % 3], earlier=False)
                self.store()
                model = StubTriage(kinds)
                out = self.mail_run(model)
                self.assertEqual(out["prepared_by_autopilot"].get("reply_prep", 0),
                                 (n + 2) // 3)
                sent = [m for b in model.sent for m in b]
                self.assertEqual(len(sent), n)          # each mail read once, the rules' asks too
                if n > 1:
                    self.assertIn(auto, [m["text"] for m in sent])
                self.assertTrue(all(m["side"] in ("supplier", "customer", "internal", "unknown")
                                    and isinstance(m["to_reader"], bool) for m in sent))
                self.assertFalse(any("@" in json.dumps(m) for m in sent))
                q = self.queue_file()
                # one reading per mail by its message id, and the same by its record id (what
                # Home's reply lines name)
                kept = dict(q.conn.execute("SELECT reason, count(*) FROM chat_asks WHERE "
                                           "message_id LIKE 'Email:%' GROUP BY 1"))
                self.assertEqual(q.conn.execute("SELECT count(*) FROM chat_asks").fetchone()[0],
                                 2 * n)
                fyi = sum(1 for i in range(n) if i % 3 == 2)
                self.assertEqual(kept.get("fyi:pass the labels to shipping", 0), fyi)
                self.assertEqual(kept.get("automated:", 0), sum(1 for i in range(n) if i % 3 == 1))
                self.assertEqual(sum(kept.values()), n)

    def test_a_reply_reading_never_makes_an_item_for_a_cold_sender(self):
        """Review 51f740c92d: a reply reading stands for the phrase rules, but the sender
        must still be no stranger (the desk rule's screen: a known customer, supplier or
        colleague, a known party, a thread the person is in, a correspondent). A cold
        sender's mail read as reply makes no item; the same mail from a correspondent does;
        read fyi, or with no model, it makes none."""
        for kind, known, want in (("reply", False, 0), ("reply", True, 1), ("fyi", True, 0),
                                  (None, True, 0)):
            with self.subTest(kind=kind, known=known):
                self.setUp()
                if known:
                    self.correspondent()
                self.thread(0, ask=self.REQUEST, earlier=False)
                self.store()
                if kind is None:
                    out = self.replay()
                else:
                    out = self.mail_run(StubTriage({self.REQUEST: (kind, "")}))
                self.assertEqual(out["prepared_by_autopilot"].get("reply_prep", 0), want)


class Reading(unittest.TestCase):
    def test_a_triage_reading_names_its_kind_and_work_and_an_unknown_kind_is_not_known(self):
        from operating import chat_ask_model as cam
        reply = ('[{"n": 1, "kind": "automated", "work": ""}, '
                 '{"n": 2, "kind": "fyi", "work": "send the sample to the new address"}, '
                 '{"n": 3, "kind": "Reply"}, {"n": 4, "kind": "urgent"}, '
                 '{"n": 5, "kind": "notice", "work": "' + "x" * 400 + '"}]')
        got = cam.parse(reply, 5)
        self.assertEqual(got[:3], [(False, "automated", ""),
                                   (False, "fyi", "send the sample to the new address"),
                                   (True, "reply", "")])
        self.assertIsNone(got[3])                       # not a kind: kept, never a no
        self.assertEqual(len(got[4][2]), cam.WORK_CHARS)
        self.assertEqual(cam.triage_of("fyi:send it"), ("fyi", "send it"))
        self.assertEqual(cam.triage_of("request"), ("", ""))   # an older yes/no reason

    def test_a_registry_may_skip_only_known_kinds_never_a_reply(self):
        from operating import autopilot as ap
        for bad in (["reply"], ["fyi", "reply"], ["spam"], [], "fyi"):
            with self.subTest(bad=bad), self.assertRaises(ap.RegistryInvalid):
                ap._check_when({"triage_none": bad}, "x")
        ap._check_when({"triage_none": ["fyi", "automated", "notice"]}, "x")


def load_tests(loader, _tests, _pattern):
    """Only this module's tests: ``Triage`` borrows the mail fixture of ``WaitingOnYou``,
    whose own tests run in their module."""
    suite = unittest.TestSuite()
    for case in (Triage, Reading):
        suite.addTests(case(name) for name in vars(case) if name.startswith("test_"))
    return suite


if __name__ == "__main__":
    unittest.main()
