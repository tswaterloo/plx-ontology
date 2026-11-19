"""A follow-up's pointer at the previous answer's records (``operating.conversation_refs``).

Evidence label: ``isolated-fixture`` (pure: the previous answer's records are given). The
backend's use of it, reader-scoped, is tested in ``operator_api.tests.test_agent_ask``.
"""
from __future__ import annotations

import unittest

from operating import conversation_refs as refs

LIST = ([{"object_id": f"Email:{i}", "type": "Email", "label": f"Subject {i}"}
         for i in range(3)] + [{"object_id": "Customer:1", "type": "Customer",
                                "label": "One Wellness"}])


def picked(question, earlier=LIST):
    got = refs.pick(question, earlier)
    return None if got is None else ([r["object_id"] for r in got[1].records], got[1].why)


class PickTests(unittest.TestCase):
    def test_ordinals_and_deictics_point_at_the_listed_records_in_order(self):
        self.assertEqual(picked("open the second one"), (["Email:1"], ""))
        self.assertEqual(picked("who sent the 3rd email?"), (["Email:2"], ""))
        self.assertEqual(picked("show me the last one"), (["Email:2"], ""))
        self.assertEqual(picked("the second of those"), (["Email:1"], ""))
        self.assertEqual(picked("is that customer late on payment?"), (["Customer:1"], ""))
        self.assertEqual(picked("open those"), (["Email:0", "Email:1", "Email:2"], ""))

    def test_one_is_never_a_name_and_an_unsettled_pointer_says_why(self):
        self.assertEqual(picked("open the fifth one")[0], [])
        self.assertIn("listed only 3 emails", picked("open the fifth one")[1])
        self.assertIn("listed 3 emails", picked("that one")[1])
        self.assertIn("no earlier answer", picked("open the second one", [])[1])
        mixed = [{"object_id": "A:1", "type": "SalesOrder", "label": "1"},
                 {"object_id": "B:1", "type": "PurchaseOrder", "label": "2"}] * 2
        self.assertIn("more than one kind", picked("open the second one", mixed)[1])
        ref, got = refs.pick("open the fifth one", LIST)
        self.assertEqual(refs.cannot(ref, got.why),
                         "I can't tell which record \"the fifth one\" means: the last "
                         "answer listed only 3 emails. Ask again with its number or name.")

    def test_a_phrase_that_frames_a_new_question_is_read_as_typed(self):
        for question in ("the last email from Unilever", "emails this week",
                         "the first quarter results", "One Wellness emails",
                         "latest emails from customers", "first, open the list",
                         "the last emails", "orders this month"):
            with self.subTest(question=question):
                self.assertIsNone(picked(question))
        self.assertIsNone(picked("orders this month", []))

    def test_a_question_naming_its_own_subject_is_not_a_pointer(self):
        orders = [{"object_id": f"SO-{100 + i}", "type": "SalesOrder", "label": f"SO-{100 + i}"}
                  for i in range(2)]
        pos = [{"object_id": "PO-1", "type": "PurchaseOrder", "label": "PO-1"}]
        self.assertIsNone(picked("What was the first order Acme ever placed?", orders))
        self.assertIsNone(picked("Which suppliers ship to Texas and how do I reach them?", pos))
        self.assertIsNone(picked("List suppliers in Ontario and email them", pos))
        self.assertEqual(picked("who placed the first order?", orders), (["SO-100"], ""))
        self.assertEqual(picked("email them", pos), (["PO-1"], ""))
        self.assertEqual(picked("Are any of those late?", pos), (["PO-1"], ""))

    def test_open_only_and_the_rewrite_with_the_records_own_labels(self):
        ref, got = refs.pick("who sent the second one?", LIST)
        self.assertFalse(refs.opens_only("who sent the second one?", ref))
        self.assertEqual(refs.rewritten("who sent the second one?", ref, ["Subject 1"]),
                         'who sent "Subject 1"?')
        ref, _ = refs.pick("please open the second one", LIST)
        self.assertTrue(refs.opens_only("please open the second one", ref))


if __name__ == "__main__":
    unittest.main()


class FreshProcessImportTests(unittest.TestCase):
    """The follow-up check must not load fast_listing before fast_answer (a circular import
    once left fast_record/fast_chain bound to a stale copy of fast_answer)."""

    def _run(self, code: str) -> str:
        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                              text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return done.stdout.strip()

    def test_follow_up_check_first_leaves_one_fast_answer(self):
        out = self._run(
            "from operating import conversation_refs as r\n"
            "r.pick('are those late?', [{'type': 'PurchaseOrder', 'id': 'a'}])\n"
            "r._record_words()\n"
            "import sys\n"
            "from operating import fast_answer, fast_record, fast_chain\n"
            "live = sys.modules['operating.fast_answer']\n"
            "print(fast_record.fa is live and fast_chain.fa is live and fast_answer is live)\n")
        self.assertEqual(out, "True")

    def test_record_words_first_name_business_records_and_keep_modules_live(self):
        out = self._run(
            "import sys\n"
            "from operating import conversation_refs as r\n"
            "w = r._record_words()\n"
            "from operating import fast_record\n"
            "print('supplier' in w, 'order' in w, 'status' in w, "
            "fast_record.fa is sys.modules['operating.fast_answer'])\n")
        self.assertEqual(out, "True True False True")

    def test_refs_tests_before_agent_ask_still_route_the_po_question_to_the_agent(self):
        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        done = subprocess.run(
            [sys.executable, "-m", "unittest", "operating.tests.test_conversation_refs.PickTests",
             "operator_api.tests.test_agent_ask.AgentPath."
             "test_progress_names_plain_steps_and_the_verdict_comes_before_the_answer"],
            cwd=root, capture_output=True, text=True, timeout=900)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
