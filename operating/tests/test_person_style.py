"""Per-person style from the person's own sent mail (``operating.person_style``) through the
prose style hook: rules learned, the exclusion fail-closed, the capture read-only.

Evidence label: isolated-fixture (a synthetic capture store; no real mail)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, SCOPE, UPN_A,
                                                   UPN_B, Clock, FakeGraph, capture_audience)
from operating import person_style as ps
from operating import prose_style

KEPT_OUT = "kept.out@example-plx.com"
CUSTOMER = "pat@customer-one.example"


def reply(i: int, *, greet: str = "Hi Pat,", close: str = "Cheers,\nOrders desk",
          words: str = "The order ships Monday.") -> str:
    return f"{greet}\n\n{words} Ref {i}.\n\n{close}"


class Guard:
    """Decides every address but ``undecided``; keeps out ``out``."""

    def __init__(self, out=(), undecided=(), stamp="s1"):
        self.out, self.undecided, self.stamp = set(out), set(undecided), stamp

    def decides(self, value):
        return str(value).lower() not in self.undecided

    def is_excluded(self, value):
        return str(value).lower() in self.out

    def fingerprint(self):
        return self.stamp


class Learned(unittest.TestCase):
    def test_greeting_sign_off_length_and_dashes_from_what_they_sent(self):
        got = ps.learned([reply(i, words="Sure — it ships Monday.") for i in range(6)])
        self.assertEqual(got["rules"][:2], ["Opens with \"Hi <name>,\".",
                                            "Signs off with \"Cheers,\"."])
        self.assertTrue(got["rules"][2].startswith("Keeps replies short"))
        self.assertEqual(got["allow"], ["em dash"])

    def test_too_few_or_finance_messages_teach_nothing(self):
        self.assertIsNone(ps.learned([reply(i) for i in range(4)]))
        pay = [reply(i, words="Your salary and bonus are below.") for i in range(6)]
        self.assertIsNone(ps.learned(pay))

    def test_no_shared_form_gives_no_greeting_or_sign_off_rule(self):
        mixed = [reply(0, greet="Hello Pat,"), reply(1, greet="Dear Pat,"),
                 reply(2, greet="Hey Pat,"), reply(3, close="Best,"), reply(4, close="Thanks,"),
                 reply(5, greet="Good morning Pat,", close="Regards,")]
        rules = ps.learned(mixed)["rules"]
        self.assertFalse([r for r in rules if r.startswith(("Opens", "Signs"))], rules)


class Source(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def source(self, guard, replies=None, directory=None, lessons=None, clock=None):
        def read(oid, address):
            self.calls.append((oid, address))
            return replies if replies is not None else [reply(i) for i in range(6)]
        src = ps.StyleSource(Path("/nonexistent"), {}, guard=guard, replies=read,
                             lessons=lessons, clock=clock or (lambda: 0.0))
        src.directory = directory if directory is not None else {UPN_A: BOX_A}
        return src

    def test_a_mailbox_person_gets_their_rules(self):
        got = self.source(Guard())(UPN_A.upper())
        self.assertIn("Signs off with \"Cheers,\".", got["rules"])
        self.assertEqual(self.calls, [(BOX_A, UPN_A)])

    def test_kept_out_undecided_or_unknown_people_read_nothing(self):
        for guard, address in ((Guard(out={UPN_A}), UPN_A), (Guard(out={BOX_A}), UPN_A),
                               (Guard(undecided={UPN_A}), UPN_A), (Guard(), CUSTOMER),
                               (Guard(), "not-an-address")):
            with self.subTest(address=address):
                self.calls.clear()
                self.assertIsNone(self.source(guard)(address))
                self.assertEqual(self.calls, [])

    def test_no_exclusion_reads_nothing(self):
        src = ps.StyleSource(Path("/nonexistent"), {}, replies=lambda *a: self.fail())
        src._people = lambda: None
        self.assertIsNone(src(UPN_A))

    def test_cached_an_hour_and_re_read_when_the_exclusion_changes(self):
        now = [0.0]
        guard = Guard()
        src = self.source(guard, clock=lambda: now[0])
        src(UPN_A)
        src(UPN_A)
        self.assertEqual(len(self.calls), 1)
        guard.stamp = "s2"
        src(UPN_A)
        self.assertEqual(len(self.calls), 2)
        now[0] = ps.TTL + 1
        src(UPN_A)
        self.assertEqual(len(self.calls), 3)

    def test_a_failing_read_is_no_style(self):
        src = ps.StyleSource(Path("/nonexistent"), {}, guard=Guard(),
                             replies=lambda *a: 1 / 0)
        src.directory = {UPN_A: BOX_A}
        self.assertIsNone(src(UPN_A))

    def test_stated_and_distilled_rules_come_first(self):
        class Lessons:
            def of(self, person):
                assert person == BOX_A
                return []
        src = self.source(Guard(), lessons=Lessons())
        from operating import learning_fewshot as fs
        original = fs.rules
        fs.rules = lambda lessons: ["Never writes \"hope this finds you well\"."]
        try:
            got = src(UPN_A)
        finally:
            fs.rules = original
        self.assertEqual(got["rules"][0], "Never writes \"hope this finds you well\".")

    def test_registered_hook_feeds_the_style_rule_of_their_drafts(self):
        self.addCleanup(prose_style.set_person_style, None)
        prose_style.set_person_style(self.source(Guard()))
        text = prose_style.style_rule(UPN_A, draft=True)
        self.assertIn("Signs off with \"Cheers,\".", text)
        self.assertNotIn("Cheers", prose_style.style_rule(CUSTOMER, draft=True))

    def test_register_needs_a_state_and_honours_off(self):
        self.addCleanup(prose_style.set_person_style, None)
        self.assertFalse(ps.register(None, {}))
        self.assertFalse(ps.register(Path("/x"), {ps.OFF_ENV: "off"}))
        self.assertTrue(ps.register(Path("/x"), {}))
        self.assertIsInstance(prose_style._provider, ps.StyleSource)


class CaptureRead(unittest.TestCase):
    """The real read over a synthetic capture store, opened read-only."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        graph = FakeGraph()
        for i in range(8):
            graph.put(BOX_A, f"S{i}", f"CK{i}", subject=f"RE: order {i}",
                      body=reply(i), received=f"2026-09-{10 + i:02d}T09:00:00Z")
            graph.messages[BOX_A][f"S{i}"]["from"] = {"emailAddress": {"address": UPN_A}}
            graph.messages[BOX_A][f"S{i}"]["toRecipients"] = [
                {"emailAddress": {"address": CUSTOMER}}]
        # one to a person kept out, one received: neither is read
        graph.put(BOX_A, "SX", "CKX", body=reply(99, close="SECRET-CLOSE,"),
                  received="2026-09-25T09:00:00Z")
        graph.messages[BOX_A]["SX"]["from"] = {"emailAddress": {"address": UPN_A}}
        graph.messages[BOX_A]["SX"]["toRecipients"] = [{"emailAddress": {"address": KEPT_OUT}}]
        graph.put(BOX_A, "IN", "CKI", body="Hello from outside, RECEIVED-WORDS.")
        graph.put(BOX_B, "B1", "CKB", body=reply(1, close="OTHER-BOX,"))
        graph.messages[BOX_B]["B1"]["from"] = {"emailAddress": {"address": UPN_A}}
        store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
        store.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_only_their_own_sent_mail_to_people_not_kept_out(self):
        db = self.state / "mail-capture" / "capture.db"
        before = db.stat().st_mtime_ns
        got = ps.sent_replies(self.state, BOX_A, UPN_A, Guard(out={KEPT_OUT}))
        self.assertEqual(len(got), 8)
        joined = "\n".join(got)
        for never in ("SECRET-CLOSE", "RECEIVED-WORDS", "OTHER-BOX"):
            self.assertNotIn(never, joined)
        self.assertEqual(db.stat().st_mtime_ns, before)       # read-only

    def test_the_whole_path_over_the_store(self):
        src = ps.StyleSource(self.state, {}, guard=Guard(out={KEPT_OUT}))
        self.assertEqual(src.__call__(UPN_A)["rules"][:2],
                         ["Opens with \"Hi <name>,\".", "Signs off with \"Cheers,\"."])
        self.assertIsNone(src(UPN_B))       # their mailbox has no sent mail of theirs
        self.assertEqual(ps.mailbox_directory(self.state)[UPN_A], BOX_A)


if __name__ == "__main__":
    unittest.main()
