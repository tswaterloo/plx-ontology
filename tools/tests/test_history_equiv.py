"""The History gate's explained differences: a difference is accepted only when the copy's rows
prove it is an approved class; anything else, or a proof that does not hold, stays unexplained
(the go line counts it)."""
import json
import unittest

from tools import history_equiv as he

EMP = "ea0fa97c-0000"


def mail(**kw):
    f = {"kind": "link_added", "other": "Email:x", "emp": EMP, "audience": f"mail:{EMP}",
         "excluded": 0, "chat": None, "items": [{"seq": 3, "cites": {
             "graph-mail://m/1#a": [EMP, False]}}]}
    f.update(kw)
    return f


class OwnMail(unittest.TestCase):
    def test_own_mailbox_link_is_accepted(self):
        self.assertEqual(he.why_not_own_mail(mail()), (None, "own_mail"))

    def test_fails_closed(self):
        cases = {
            "another mailbox": mail(audience="mail:someone-else"),
            "not a link": mail(kind="changed"),
            "excluded": mail(excluded=1),
            "no stored item": mail(items=[]),
            "cite elsewhere": mail(items=[{"seq": 1, "cites": {"graph-mail://n/2": ["other",
                                                                                False]}}]),
            "cite excluded": mail(items=[{"seq": 1, "cites": {"graph-mail://n/2": [EMP, True]}}]),
            "cite unknown": mail(items=[{"seq": 1, "cites": {"graph-mail://n/2": None}}]),
        }
        for name, f in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(he.why_not_own_mail(f)[0])

    def test_own_chat_needs_membership(self):
        f = mail(audience="teams:chat:c1", chat=["teams-chat://c1/9", False], items=[
            {"seq": 2, "cites": {}}])
        self.assertEqual(he.why_not_own_mail({**f, "chat_readable": True}), (None, "own_chat"))
        self.assertIsNotNone(he.why_not_own_mail({**f, "chat_readable": False})[0])
        self.assertIsNotNone(he.why_not_own_mail({**f, "chat": ["h", True],
                                                  "chat_readable": True})[0])
        # resting on the chat message itself: accepted for a member only; on another chat: not
        rest = {**f, "items": [{"seq": 2, "cites": {"teams-chat://c1/9": None}}]}
        self.assertEqual(he.why_not_own_mail({**rest, "chat_readable": True}), (None, "own_chat"))
        self.assertIsNotNone(he.why_not_own_mail({**rest, "chat_readable": False})[0])
        other = {**f, "items": [{"seq": 2, "cites": {"teams-chat://c2/1": None}}]}
        self.assertIsNotNone(he.why_not_own_mail({**other, "chat_readable": True})[0])
        self.assertIsNotNone(he.why_not_own_mail({**rest, "audience": f"mail:{EMP}",
                                                  "chat_readable": True})[0])


class ExcludedOnly(unittest.TestCase):
    def facts(self, **kw):
        f = {"head_partial": False, "items": 9, "old_reasons": ["audience"], "hidden": [
            {"section": "history", "seq": 4, "owner_only": True, "excluded_lineage": True}]}
        f.update(kw)
        return f

    def test_only_people_exclusion_rows_held_back(self):
        self.assertIsNone(he.why_not_excluded_only(self.facts()))

    def test_an_undercount_is_not_explained(self):
        # a held-back change that is not the owner's alone: the new tab should have said partial
        for hidden in ([], [{"section": "history", "seq": 1, "owner_only": False,
                             "excluded_lineage": False}],
                       [{"section": "history", "seq": 1, "owner_only": True,
                         "excluded_lineage": False}]):
            with self.subTest(hidden=hidden):
                self.assertIsNotNone(he.why_not_excluded_only(self.facts(hidden=hidden)))
        self.assertIsNotNone(he.why_not_excluded_only(self.facts(head_partial=True)))


class Explain(unittest.TestCase):
    def test_unknown_difference_stays_unexplained(self):
        diff = [{"section": "history", "kind": "stored_missing", "key": "{}"},
                {"section": "partial", "kind": "value", "key": "old False new True"},
                {"section": "history", "kind": "stored_extra", "key": "not json"}]
        he.explain(None, "biz", EMP, "PurchaseOrder:1", diff, lambda k: set(), (),
                   lambda hs: set())
        self.assertTrue(all("unexplained" in d and "accepted" not in d for d in diff))

    def test_accepted_carries_its_evidence(self):
        entry = {"kind": "link_added", "link": {"object_id": "Email:x"}}
        diff = [{"section": "history", "kind": "stored_extra", "key": json.dumps(entry)}]
        orig = he.own_mail_facts
        he.own_mail_facts = lambda *a: mail()
        try:
            he.explain(None, "biz", EMP, "PurchaseOrder:1", diff, None, (), lambda hs: set())
        finally:
            he.own_mail_facts = orig
        self.assertEqual(diff[0]["accepted"], "own_mail")
        self.assertEqual(diff[0]["evidence"]["audience"], f"mail:{EMP}")


if __name__ == "__main__":
    unittest.main()
