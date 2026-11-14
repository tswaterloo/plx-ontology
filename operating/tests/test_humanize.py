"""Model-written text reads as a person wrote it, and no fact is ever changed doing so:
the tic checker (``prose_style.check``), the repair and rewrite pass (``humanize.polish``),
the per-person style hook, and the generation paths that use them."""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from operating import humanize, prose_style as style

PLAIN = ("PO 106001 is 12 days late. Acme confirmed a ship date of 11-20 in their reply on "
         "11-10 [1]. The BC order has 400 cases open against 1,200 ordered, and Dana Ruiz "
         "asked the plant whether the second lot can go first [2]. Nothing has shipped yet. ")


class Checker(unittest.TestCase):
    def test_staged_and_chatbot_tics_are_flagged(self):
        for text, family in (
                ("It is not a delay. It is a cancellation by Acme.", "negation-first reveal"),
                ("The real question is whether PO 106001 ships.", "significance designation"),
                ("Bottom line: PO 106001 is late.", "label-first"),
                ("You might think Acme shipped, but it did not.", "straw man"),
                ("Great question! PO 106001 is late.", "chatbot residue"),
                ("The delay, highlighting the risk to 400 cases.", "participle tail"),
                ("So why is it late? Because Acme has no resin.", "rhetorical question"),
                ("It's worth noting that Acme is late.", "self-grading")):
            report = style.check(text)
            self.assertTrue(report.flagged, text)
            self.assertIn(family, report.families(), text)

    def test_plain_business_prose_is_not_flagged(self):
        for text in (PLAIN, "Not on record.", "Draft, not sent.",
                     "The order is not received. It is still open in BC.",
                     "Hi Dana,\n\nCan you confirm the ship date for PO 106001?\n\nThanks"):
            self.assertFalse(style.check(text).flagged, text)
            self.assertFalse(style.check(text, kind="draft").flagged, text)

    def test_one_weak_tic_alone_is_not_a_trigger(self):
        report = style.check(PLAIN + "The supplier is robust.")
        self.assertEqual(["stock word"], report.families())
        self.assertFalse(report.flagged)

    def test_quoted_source_words_are_not_the_writers(self):
        self.assertFalse(style.check('Acme wrote "Great question, I hope this helps" [1].')
                         .flagged)

    def test_draft_kind_reads_the_copy_rules_phrases(self):
        text = "I wanted to reach out about PO 106001."
        self.assertFalse(style.check(text, kind="answer").flagged)
        self.assertIn("stock phrase", style.check(text, kind="draft").families())

    def test_a_sign_off_is_the_senders_not_a_tic(self):
        self.assertFalse(style.check("PO 7 ships on 11-20.\n\nBest regards,", kind="draft")
                         .flagged)

    def test_unknown_kind_fails_closed(self):
        with self.assertRaises(ValueError):
            style.check(PLAIN, kind="poem")

    def test_checks_a_long_answer_in_under_five_ms(self):
        text = PLAIN * 8                      # about 2,300 characters
        best = min(self._time(text) for _ in range(15))
        self.assertLess(best, 5.0)

    @staticmethod
    def _time(text):
        started = time.perf_counter()
        style.check(text)
        return (time.perf_counter() - started) * 1000


class Facts(unittest.TestCase):
    def test_every_kind_of_fact_change_is_caught(self):
        before = "Acme sent 400 cases on 11-10 for PO 106001 [1]; it has not shipped."
        for after in (
                "Acme sent 410 cases on 11-10 for PO 106001 [1]; it has not shipped.",
                "Acme sent 400 boxes on 11-10 for PO 106001 [1]; it has not shipped.",
                "Acme sent 400 cases on 11-10 for PO 106001; it has not shipped.",
                "Acme sent 400 cases on 11-10 for PO 106001 [1]; it has shipped.",
                "Globex sent 400 cases on 11-10 for PO 106001 [1]; it has not shipped.",
                "The supplier sent 400 cases on 11-10 for PO 106001 [1]; it has not shipped.",
                "Acme sent about 400 cases on 11-10 for PO 106001 [1]; it has not shipped.",
                "Acme sent 400 cases on 11-10 for PO 106001 [1] and [2]; it has not shipped."):
            self.assertFalse(humanize.preserved(before, after), after)

    def test_a_text_against_itself_keeps_every_fact(self):
        for text in ("AO-26036E is Realized; QT-FM-QT-7 and U.S. orders [1].",
                     "DPI 300 scans for BC-24297D (FS) and R-0162 at P/O 7.",
                     PLAIN, "Hi Dana,\n\n- **PO 7:** ships 11-20.\n\nThanks"):
            self.assertEqual([], humanize.lost(text, text), text)

    def test_a_wording_change_keeps_the_facts(self):
        self.assertTrue(humanize.preserved(
            "It is not a delay. It is a cancellation by Acme for PO 106001 [1].",
            "Acme cancelled PO 106001 [1], which is a cancellation rather than a delay."))
        self.assertTrue(humanize.preserved("Shipping is late for PO 7.",
                                           "PO 7 is late, and shipping is the reason."))


class Polish(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {humanize.MODE_ENV: "repair"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clean_text_never_reaches_the_model(self):
        rewrite = mock.Mock(return_value="x")
        got = humanize.polish(PLAIN, rewrite=rewrite)
        self.assertEqual(("clean", PLAIN), (got.action, got.text))
        rewrite.assert_not_called()

    def test_deterministic_repair_keeps_every_fact(self):
        text = ("Great question! It turns out that PO 106001 is 12 days late. To be clear, "
                "Acme confirmed 11-20 [1]. Bottom line: we need to utilize the backup "
                "supplier in order to hit 400 cases. I hope this helps!")
        got = humanize.polish(text, rewrite=mock.Mock(return_value=None))
        self.assertEqual("PO 106001 is 12 days late. Acme confirmed 11-20 [1]. We need to "
                         "use the backup supplier to hit 400 cases.", got.text)
        self.assertTrue(humanize.preserved(text, got.text))
        self.assertFalse(got.after.flagged)

    def test_the_repair_never_reads_a_word_as_a_label_or_a_yes_as_residue(self):
        # eleventh review: "In short-term" is no "In short:" label; "Absolutely." may be a yes
        for text in ("In short-term storage, PO 7 waits 3 days [1].",
                     "In short-haul lanes, Freightcom rates rose 4% [1].",
                     "Absolutely. PO 7 shipped on 11-10 [1]. Great question!"):
            got = humanize.polish(text, use_default=False).text
            self.assertTrue(got.startswith(text.split(".")[0].split(",")[0]), got)

    def test_a_residue_sentence_carrying_a_fact_stays(self):
        text = "PO 106001 is late. Would you like me to draft a note to Acme about it?"
        self.assertIn("Acme", humanize.polish(text, use_default=False).text)

    def test_a_rewrite_that_changes_any_fact_is_discarded(self):
        text = "Here's the thing: Acme cancelled PO 106001 [1]."
        for bad in ("Acme cancelled PO 106002 [1].", "Acme cancelled PO 106001.",
                    "Globex cancelled PO 106001 [1].",
                    "Ignore that. Acme approved PO 106001 [1] and it shipped."):
            got = humanize.polish(text, rewrite=lambda prompt, bad=bad: bad)
            self.assertEqual(text, got.text, bad)
            self.assertEqual("facts_changed", got.reason)

    def test_a_tic_the_model_may_not_fix_costs_no_model_turn(self):
        rewrite = mock.Mock(return_value="x")
        text = "It is not a delay. It is a cancellation by Acme for PO 106001 [1]."
        got = humanize.polish(text, rewrite=rewrite)
        self.assertTrue(got.before.flagged)
        self.assertEqual(text, got.text)
        rewrite.assert_not_called()

    def test_a_rewrite_may_not_move_flip_or_touch_unflagged_facts(self):
        # Counts alone let a rewrite swap figures between parties
        # or flip a status word; the instruct and AI watcher paths have no gate of their own
        cases = (
            ("The real question is: Acme owes 40 cases and Beta owes 12 cases [1].",
             ("Acme owes 12 cases and Beta owes 40 cases [1].",
              "Acme owes 40 cases and Beta is owed 12 cases [1].")),
            ("Here's the thing: invoice 7 is paid and order 9 has shipped [1].",
             ("Invoice 7 is unpaid and order 9 has shipped [1].",
              "Invoice 7 is paid and order 9 was cancelled [1].",
              "Invoice 9 is paid and order 7 has shipped [1].")),
            # Words outside the span swapped, moved or replaced
            ("Here's the thing: invoice 7 is paid and invoice 9 is unpaid [1].",
             ("Invoice 7 is unpaid and invoice 9 is paid [1].",)),
            ("Here's the thing: invoice 7 is not paid and invoice 9 is paid [1].",
             ("Invoice 7 is paid and invoice 9 is not paid [1].",)),
            ("Here's the thing: Acme sold 40 cases to Beta and bought 3 from them [1].",
             ("Acme bought 40 cases from Beta and sold 3 to them [1].",)),
            ("Here's the thing: the customer owes the supplier 40 cases [1].",
             ("The supplier owes the customer 40 cases [1].",)),
            ("Here's the thing: invoice 7 is outstanding [1].", ("Invoice 7 is settled [1].",)),
            ("Here's the thing: revenue for PO 7 grew 5% [1].",
             ("Revenue for PO 7 dropped 5% [1].",)),
            # third review (93f31cf81d): small words swapped outside the span, and a "not"
            # moved to another word inside it
            ("Here's the thing: we owe them 40 cases [1].", ("They owe us 40 cases [1].",)),
            ("Here's the thing: our invoice 7 is open [1].", ("Your invoice 7 is open [1].",)),
            ("Here's the thing: PO 7 ships if Acme pays [1].",
             ("PO 7 ships when Acme pays [1].",)),
            ("Here's the thing: Acme will pay invoice 7 [1].",
             ("Acme did pay invoice 7 [1].", "Acme must pay invoice 7 [1].")),
            ("Here's the thing: Acme has more cases than Beta [1].",
             ("Acme has less cases than Beta [1].",)),
            ("Here's the thing: the new invoice 9 replaces the old invoice 8 [1].",
             ("The old invoice 9 replaces the new invoice 8 [1].",)),
            ("It is not a delay. It is a cancellation by Acme for PO 106001 [1].",
             ("It is a delay, not a cancellation by Acme for PO 106001 [1].",)),
            # fourth review (f880b72e01): a new word in the span, a party moved to another
            # noun, a "rather than" attached to the sentence before
            ("Acme shipped PO 7, highlighting the delay at Beta [1].",
             ("Acme shipped PO 7, causing the delay at Beta [1].",)),
            ("It is not a delay for Acme. It is a cancellation by Beta for PO 8 [1].",
             ("It is a cancellation for Acme, not a delay by Beta for PO 8 [1].",)),
            ("Acme pays invoice 7. It is not a delay. It is a cancellation of PO 8 [1].",
             ("Acme pays invoice 7 instead of a delay. It is a cancellation of PO 8 [1].",)),
            # fifth review (7ff8f88c57): a negation moved to another party, a cause or an
            # order brought in with a filler; and the reveal turned into "Y rather than X"
            # (a word moved): a rewrite now only deletes
            ("It's not just Acme. It's Beta that owes invoice 8 [1].",
             ("It's Acme. It's not just Beta that owes invoice 8 [1].",)),
            ("Beta cancelled PO 8, highlighting that Acme shipped PO 7 [1].",
             ("Beta cancelled PO 8, so Acme shipped PO 7 [1].",
              "Beta cancelled PO 8, then Acme shipped PO 7 [1].")),
            ("It is not a delay. It is a cancellation by Acme for PO 106001 [1].",
             ("It is a cancellation by Acme for PO 106001 [1] rather than a delay.",)),
            # sixth review (4eaf582b4b): deleting the scope word after a negation flips it
            ("The delay doesn't just affect order 1042, it affects every Acme order.",
             ("The delay doesn't affect order 1042, it affects every Acme order.",)),
            ("Acme didn't just miss the 3 May delivery, they missed the 10 May one too.",
             ("Acme didn't miss the 3 May delivery, they missed the 10 May one too.",)),
            # seventh review (3ca4a839c4): an article dropped before a quantifier
            ("This seamless, robust, comprehensive plan works, and Acme has a few pallets "
             "of PO 7 left.", ("This plan works, and Acme has few pallets of PO 7 left.",)),
            ("Acme ships the most pallets on PO 7 with a seamless, robust, comprehensive "
             "process.", ("Acme ships most pallets on PO 7 with a process.",)),
            # a flagged stock word or adverb may carry a claim: never deleted
            ("Acme deliberately held PO 7 for a comprehensive, robust, seamless audit [1].",
             ("Acme held PO 7 for an audit [1].", "Acme held PO 7 for a audit [1].")),
            # eighth review (b2065baea1): a reveal's span holds the writer's own claim
            ("It isn't late because of weather, it's late because of customs. Here's the "
             "thing: PO 7 clears today.",
             ("It isn't late, it's late because of customs. PO 7 clears today.",)),
            ("It wasn't approved by finance, it was approved by ops [1].",
             ("It wasn't approved, it was approved by ops [1].",)),
            ("You might think Acme shipped, but it did not [1].",
             ("Acme shipped, but it did not [1].",)),
            # ninth review (14b1e21b95): a deadline read as a closer; a hedge deleted
            ("Payment of $4,200 is due at the end of the day. Here's the thing: PO 7 is late.",
             ("Payment of $4,200 is due. PO 7 is late.",)),
            ("Based on the available information, the order shipped. Here's the thing: "
             "PO 7 is late.", ("The order shipped. PO 7 is late.",)),
            # tenth review (f02f86402e): an emoji verdict, an offer, a time, a scope word
            ("PO 7 shipped \u274c, PO 8 shipped \u2705 [1].",
             ("PO 7 shipped , PO 8 shipped [1].",)),
            ("PO 7 is late; let me know if you'd like the $4,200 refund issued.",
             ("PO 7 is late; the $4,200 refund issued.",)),
            ("PO 7 is late, so would you like me to cancel PO 8?", ("PO 7 is late, so cancel PO 8?",)),
            ("At the end of the day, release the hold on PO 7 [1].",
             ("Release the hold on PO 7 [1].",)),
            ("Line 2 margin rose 4% [1]. Overall, margin fell 1% [2].",
             ("Line 2 margin rose 4% [1]. Margin fell 1% [2].",)),
            ("PO 7 ships, but that's the difference.", ("PO 7 ships, but.",)),
            # eleventh review (076ad95662): noun-phrase designations hold the object
            ("Replace the part that counts the pallets on line 2 [1].",
             ("Replace the pallets on line 2 [1].",)),
            ("Call the number that answers after 5 PM [1].", ("Call after 5 PM [1].",)),
            # twelfth review (e65915bf1b): an opener-shaped subject
            ("The real problem was fixed on 11-10 [1].", ("Fixed on 11-10 [1].",)),
            ("The key issue was escalated to Acme [1].", ("Escalated to Acme [1].",)),
            ("PO 7 is fine. The big problem is overdue by 9 days [1].",
             ("PO 7 is fine. Overdue by 9 days [1].",)),
            ("PO 7 has not shipped. The real question is when Acme ships PO 8 [2].",
             ("PO 7 has shipped. When Acme ships PO 8 is open [2].",
              "PO 7 shipped late. Acme has not shipped PO 8 [2].")))
        for text, bad in cases:
            for candidate in bad:
                got = humanize.polish(text, rewrite=lambda p, c=candidate: c)
                self.assertEqual(text, got.text, candidate)
        for good in ("Acme owes 40 cases and Beta owes 12 cases [1].",):
            self.assertEqual(good, humanize.polish(cases[0][0], rewrite=lambda p, g=good: g)
                             .text)

    def test_a_faithful_rewrite_is_used(self):
        # a rewrite is accepted only as deletions of the flagged words
        text = "Here's the thing: PO 106001 is late for Acme [1]."
        seen = []
        got = humanize.polish(text, rewrite=lambda p: seen.append(p) or "PO 106001 is late [1].")
        self.assertEqual(text, got.text)    # "for Acme" is not flagged: kept
        good = "PO 106001 is late for Acme [1]."
        got = humanize.polish(text, rewrite=lambda p: seen.append(p) or good)
        self.assertEqual(("rewritten", good), (got.action, got.text))
        self.assertIn("significance designation", seen[0])
        self.assertIn(text, seen[0])

    def test_a_rewrite_never_adds_a_greeting_or_sign_off(self):
        text = "Bottom line: PO 7 ships 11-20."
        got = humanize.polish(text, kind="draft", rewrite=lambda p: "Hi,\nPO 7 ships 11-20.\nThanks,",
                              use_default=False)
        # the deterministic repair already removes the label; a model turn adding a frame
        # is refused either way
        self.assertEqual("PO 7 ships 11-20.", got.text)
        text = "Here's the thing: PO 7 ships 11-20."
        got = humanize.polish(text, kind="draft", rewrite=lambda p: "Hi,\nPO 7 ships 11-20.\nThanks,")
        self.assertEqual((text, "greeting_or_signoff_added"), (got.text, got.reason))

    def test_the_callers_gate_vetoes_a_rewrite(self):
        text = "Here's the thing: PO 106001 is late [1]."
        got = humanize.polish(text, rewrite=lambda p: "PO 106001 is late [1].",
                              verify=lambda t: False)
        self.assertEqual((text, "gate"), (got.text, got.reason))

    def test_a_failing_or_silent_model_keeps_the_text(self):
        text = "It is not a delay. It is a cancellation by Acme."

        def boom(prompt):
            raise RuntimeError("model down")
        for call in (boom, lambda p: None, lambda p: ""):
            self.assertEqual(text, humanize.polish(text, rewrite=call).text)

    def test_no_model_call_unless_the_unit_enables_it(self):
        self.assertIsNone(humanize.default_rewriter())
        with mock.patch.dict(os.environ, {humanize.MODE_ENV: "rewrite"}):
            self.assertIsNotNone(humanize.default_rewriter())
        with mock.patch.dict(os.environ, {humanize.MODE_ENV: "off"}):
            text = "Great question! PO 7 is late."
            self.assertEqual(text, humanize.polish(text).text)


class PersonStyle(unittest.TestCase):
    def tearDown(self):
        style.set_person_style(None)

    def test_learned_rules_layer_after_the_house_style(self):
        asked = []
        style.set_person_style(lambda person: asked.append(person) or {
            "rules": ["Open with the PO number.", "Sign off with just 'V'."],
            "allow": ["em dash", "no such family"]})
        rule = style.style_rule("Vince@Example.com", draft=True, who="Vince")
        self.assertTrue(rule.startswith(style.PROSE_STYLE_RULE))
        self.assertIn("How Vince writes", rule)
        self.assertLess(rule.index(style.DRAFT_STYLE_RULE), rule.index("Sign off with"))
        self.assertEqual(["vince@example.com"], asked)
        self.assertEqual(frozenset({"em dash"}), style.person_style("v@x.com").allow)
        self.assertNotIn("em dash", style.check("A — B — C — D", kind="draft",
                                                allow=style.person_style("v@x.com").allow)
                         .families())

    def test_a_broken_or_odd_hook_means_house_style_only(self):
        for provider in (lambda p: 1 / 0, lambda p: "rules", lambda p: {"rules": "one"},
                         lambda p: None):
            style.set_person_style(provider)
            self.assertIsNone(style.person_style("v@x.com"))
            self.assertEqual(style.PROSE_STYLE_RULE, style.style_rule("v@x.com"))

    def test_rules_are_capped(self):
        style.set_person_style(lambda p: {"rules": ["x" * 500] * 40})
        got = style.person_style("v@x.com")
        self.assertEqual(style.MAX_PERSON_RULES, len(got.rules))
        self.assertEqual(style.MAX_RULE_CHARS, len(got.rules[0]))


class Paths(unittest.TestCase):
    """The generation paths: what a reader is shown has been through the pass."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {humanize.MODE_ENV: "repair"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(style.set_person_style, None)

    def test_general_answer_and_its_email_draft(self):
        from operating import general_answer
        style.set_person_style(lambda p: {"rules": ["Writes in short lines."]})
        prompts = []
        said = ("Great question! Lead times are set by the slowest input. I hope this helps!"
                "\n[[DRAFT email]]\nSubject: Lead times\nI hope this email finds you well. "
                "Can you send your lead times?\n[[END]]")
        got = general_answer.answer("how are lead times set?", person="v@x.com",
                                    call=lambda text: prompts.append(text) or said)
        self.assertEqual("Lead times are set by the slowest input.", got.text)
        self.assertEqual("Can you send your lead times?", got.drafts[0].body)
        self.assertIn("Writes in short lines.", prompts[0])

    def test_instruct_drafts(self):
        from operating import instruct
        style.set_person_style(lambda p: {"rules": ["Writes in short lines."]})
        prompt = instruct.prompt("reply to this", [], [], who="Vince", page="mail",
                                 person="v@x.com")
        self.assertIn("How Vince writes", prompt)
        outcome = instruct.Outcome("drafted", said="Bottom line: drafted.", drafts=(
            instruct.Drafted("email", "PO 7", "I hope this email finds you well. PO 7 "
                             "ships [date]."),))
        got = instruct.humanized(outcome, person="v@x.com")
        self.assertEqual("PO 7 ships [date].", got.drafts[0].body)
        self.assertEqual("Drafted.", got.said)


if __name__ == "__main__":
    unittest.main()
