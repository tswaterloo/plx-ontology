"""A name several records of different types share is on the question only for the record
whose type the question writes beside that name (GW-0406 class: a Teams chat, its meeting
and the meeting's invitations all titled "Gleamin meeting prep")."""
import unittest

from operating.mcp_server import _found_on_question


def _cand(oid, object_type, text):
    return {"object": {"object_id": oid, "object_type": object_type,
                       "display": {"untrusted_text": text}}}


NAME = "Gleamin meeting prep"
FIND = {"query": NAME}
RESULT = {"status": "ambiguous", "candidates": [
    _cand("teams-chat://c1", "TeamsChat", NAME),
    _cand("calendar://e1", "CalendarEvent", NAME),
    _cand("mail://m1", "MailMessage", NAME),
]}


class FoundOnQuestionTypes(unittest.TestCase):
    def test_type_beside_name_picks_that_record(self):
        asked = ("As of November 10, 2026, what's the number of messages created at before "
                 "December 9, 2025 for Teams chat Gleamin meeting prep?")
        mentions, ids = _found_on_question(asked, [(FIND, RESULT)])
        self.assertEqual(ids, {"teams-chat://c1"})
        self.assertEqual(mentions, [NAME])

    def test_no_type_beside_name_picks_nothing(self):
        asked = "What happened with Gleamin meeting prep last week?"
        self.assertEqual(_found_on_question(asked, [(FIND, RESULT)]), ([], set()))

    def test_two_records_of_the_named_type_pick_nothing(self):
        result = {"status": "ambiguous", "candidates": [
            _cand("teams-chat://c1", "TeamsChat", NAME),
            _cand("teams-chat://c2", "TeamsChat", NAME)]}
        asked = "How many messages are in the Teams chat Gleamin meeting prep?"
        self.assertEqual(_found_on_question(asked, [(FIND, result)]), ([], set()))

    def test_one_named_record_is_unchanged(self):
        result = {"status": "found", "candidates": [_cand("teams-chat://c1", "TeamsChat", NAME)]}
        asked = "What was said in Gleamin meeting prep?"
        self.assertEqual(_found_on_question(asked, [(FIND, result)])[1], {"teams-chat://c1"})


if __name__ == "__main__":
    unittest.main()
