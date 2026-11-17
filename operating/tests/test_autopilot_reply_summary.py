import unittest

from operating.autopilot_work import reply_summary


class ReplySummary(unittest.TestCase):
    """The reply item's line names who waits by address, never by a bare object id."""

    def test_a_teams_sender_the_directory_could_not_name_reads_someone(self):
        got = reply_summary({"sender": "e1dd02bd-0017-4203-95ef-585f1c801321", "channel": "teams"})
        self.assertEqual(got, "Someone is waiting on a reply: (Teams message with no text)")
        self.assertNotIn("e1dd02bd", got)

    def test_a_teams_sender_reads_by_the_stated_name_and_the_message_is_quoted(self):
        oid = "e1dd02bd-0017-4203-95ef-585f1c801321"
        got = reply_summary({"sender": oid, "sender_name": "Ana Ruiz", "channel": "teams",
                             "text": "Can you\n check   PO 4410 today? " + "x" * 300}, 1)
        self.assertTrue(got.startswith(
            "Ana Ruiz is waiting on a reply: \u201cCan you check PO 4410 today?"), got)
        self.assertTrue(got.endswith("\u2026\u201d (1 question)"), got)
        # a stated "name" that is an id is no name
        self.assertTrue(reply_summary({"sender": oid, "sender_name": oid.upper(),
                                       "channel": "teams", "text": "hi"}).startswith("Someone "))

    def test_a_known_sender_keeps_their_address_and_the_questions_are_counted(self):
        got = reply_summary({"sender": "vince@example.com", "subject": "PO 90412 date", "channel": "mail"}, 2)
        self.assertEqual(got, "vince@example.com is waiting on a reply: PO 90412 date (2 questions)")
        self.assertEqual(reply_summary({"sender": "a@b.c", "channel": "mail"}, 1),
                         "a@b.c is waiting on a reply: (no subject) (1 question)")

    def test_a_card_number_across_either_cut_is_masked_before_the_cut(self):
        """Review 11-17: the card mask ran only at serving, after the cuts; a number
        straddling the snippet's (200) or the title's (100) cut left a partial number the
        serving mask no longer recognises. Every placement across both cuts: no more than
        the last four digits survive, in the snippet, the line and served through the mask."""
        import re
        from operating.autopilot_engine import SNIPPET_CHARS, message_snippet
        from operating.autopilot_work import REPLY_SNIPPET
        from operating.card_text import mask_served
        for card in ("4111 1111 1111 1111", "4111111111111111", "4111-1111-1111-1111"):
            for cut in (SNIPPET_CHARS, REPLY_SNIPPET):
                for offset in range(-len(card) - 2, 3):
                    # the number set off by a space, as the card mask reads one
                    head = ("ab " * cut)[:cut + offset - 1].rstrip() + " "
                    text = f"{head}{card} is the card, thanks"
                    for shown in (message_snippet(text),
                                  reply_summary({"channel": "teams", "text": text}),
                                  mask_served(reply_summary({"channel": "teams", "text": text}))):
                        digits = "".join(re.findall(r"\d", shown))
                        self.assertLessEqual(len(digits), 4, (card, cut, offset, shown[-40:]))
                        self.assertNotIn("4111", shown)

    def test_no_sender_reads_someone_with_a_capital(self):
        self.assertTrue(reply_summary({"channel": "teams"}).startswith("Someone is waiting"))

    def test_the_one_sender_label_shows_an_address_or_someone(self):
        from operating import autopilot as ap
        from operating import autopilot_engine
        for raw, shown in (("E1DD02BD-0017-4203-95EF-585F1C801321", "Someone"),
                           ("", "Someone"), (None, "Someone"), ("  ", "Someone"),
                           ("12345", "Someone"), ("ana@plx.example", "ana@plx.example")):
            self.assertEqual(autopilot_engine.sender_label(raw), shown, raw)


if __name__ == "__main__":
    unittest.main()
