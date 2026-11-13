"""The general answer's screen (operating/general_answer.py ``screen``): harmless general
lines stay, company facts still go. The kept lines are the real model's words from the dev
general set (11-11 probe, no record read). isolated-fixture."""

from __future__ import annotations

import unittest

from operating.general_answer import screen


class Kept(unittest.TestCase):
    def test_harmless_general_lines_stay(self):
        for question, line in (
                # a topic acronym is no party: a regulation's number and year stay
                ("what are the main GMP requirements for cosmetics manufacturing",
                 "The EU makes GMP mandatory under Regulation (EC) 1223/2009 (Article 8) "
                 "and presumes compliance when a site follows ISO 22716."),
                # "the batch" of no batch the asker named is any batch: a worked example
                ("how do I compute gross margin from price and cost",
                 "For example, if the batch yield is 97%, divide material cost by 0.97."),
                # "e.g." ends no sentence: a draft's placeholder stays whole (a draft is
                # screened strictly: a figure outside a placeholder goes)
                ("draft a message to the team announcing a new cycle count schedule",
                 "- Any variance over [tolerance, e.g. 2% or $ amount] gets a recount by "
                 "a second person."),
                # regulation and standard names in capitals are topics, not parties
                ("how should a QC hold process work",
                 "For cosmetics that means ISO 22716; for OTC drugs such as hand "
                 "sanitizers it means 21 CFR 211.100 and 211.192."),
                # "the" supplier of a worked example says nothing it did
                ("how do I compare supplier quotes",
                 "If the supplier price is $5 and freight adds 10%, the landed price is "
                 "$5.50."),
                # IN-17 (11-12 probe): a cosmetics term in capitals is a topic too
                ("what is the shelf life of an emulsion lotion typically",
                 "After opening, a 12 month PAO is common."),
                # a draft's subject line is title case: its capitals name nobody
                ("what is a good email to chase a late shipment",
                 "Subject: Update on our order")):
            with self.subTest(question=question):
                text, removed = screen(line, question, strict="draft" in question)
                self.assertEqual((text, removed), (line, []))


class Removed(unittest.TestCase):
    def test_company_and_named_party_facts_still_go(self):
        for question, line in (
                ("what should I check on the batch for lot L2024-17",
                 "The batch yield was 97% on that run."),
                ("how do I chase a late supplier",
                 "Your supplier quoted $4.10 per kg."),
                ("how should I word a reminder to Acme Labs",
                 "Acme Labs usually pays within 45 days."),
                ("what should I tell ULINE about the late pallets",
                 "ULINE shipped them on March 3, 2026."),
                ("explain GMP",
                 "PLX makes soaps under contract."),
                ("how do I compute gross margin",
                 "Our margin on PO 4501 was 31%."),
                # a name in capitals is a party (supplier, carrier), dated or not; only a
                # known topic term (GMP, ISO, FIFO) is not
                ("what should I tell ULINE about pallet pricing",
                 "ULINE usually charges $45 per pallet."),
                ("how do I plan around BASF lead times",
                 "BASF has a 12-week lead time."),
                # a subject line still writes no record id or dated party fact
                ("what is a good email to chase a late shipment",
                 "Subject: Update on PO 4501"),
                ("how do I chase a late supplier",
                 "Subject: ULINE shipped on March 3, 2026"),
                # IN-17 review: a subject line still names no invented company
                ("how do I chase a late supplier",
                 "Subject: our supplier Acme raised prices"),
                ("what is a good email to chase a late shipment",
                 "Subject: Delay on our order from Acme Supply"),
                ("can I negotiate a better rate with UPS",
                 "UPS gave you 15% off last year."),
                ("how do I pick a pallet supplier",
                 "Distributors like ULINE charge about $45 per pallet."),
                # what "the" supplier did, or "that" supplier, is one supplier's fact
                ("how do I chase a late supplier",
                 "The supplier quoted $4.10 per kg."),
                ("how do I chase a late supplier",
                 "That supplier owes 3 shipments.")):
            with self.subTest(question=question):
                text, removed = screen(line, question)
                self.assertEqual(text, "")
                self.assertEqual(len(removed), 1)

    def test_invented_company_in_a_subject_line_goes_from_a_strict_draft_too(self):
        for line in ("Subject: our supplier Acme raised prices",
                     "Subject: Delay on our order from Acme Supply"):
            with self.subTest(line=line):
                text, removed = screen(line, "write a draft email about a delay",
                                       strict=True)
                self.assertEqual(text, "")
                self.assertEqual(len(removed), 1)

    def test_not_a_string_is_nothing(self):
        self.assertEqual(screen(None, "q"), ("", ["<unreadable>"]))


if __name__ == "__main__":
    unittest.main()
