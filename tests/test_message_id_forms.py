"""Email.internet_message_id parses the Message-IDs real senders write (CT-D-09): 9% of
PLX mail (sample 2026-11-12) carried ids the RFC msg-id grammar does not allow (a second
@ with a colon in the left part, ; , | ^ [ { $ in it, 300+ characters, no @ at all) and
were left unparsed, so copies of one message across mailboxes were never grouped. The id
is opaque header text; grouping still needs equal header and body fields."""
import copy
import json
import unittest

from ontology import identifiers

# shapes seen in the sample, letters and digits replaced
SEEN = [
    "<sf:1234567890123@example12.com@mail.example12.com>",
    "<AbCdEfGhIjKlMnOpQrStUvWx-0a1b2c3d-1111-2222-3333-444455556666.ABCD_0a1b2c3d-1111-"
    "2222-3333-444455556666.xy-abc_d-efg_hi@x.example.com>",
    "0a1b2c3d-1111-2222-3333-444455556666-1",
    "<Qa12-0a1b2c3d-1111-2222-3333-444455556666;Abcd_0a1b2c3d-1111-2222-3333-444455556666;"
    "Xyz@example.com>",
    "<abcdef^ab|cdefghij[abcd.^a.abcdefghijk@mta.abcde.example.com>",
    "<abcdefghijkl$abcdefgh$abcdefgh$@abcdefgh@abcdefgh-abcd.com>",
    "<abcdefghijklmn.abcde-{abcd:abcdefghijklmn-abcd-ab}-abcdefg@example.com>",
    "<" + "a1-" * 110 + "@example.com>",          # a 340-character id
    "<CAF1x2y3.4z@mail.example.com>",               # the plain RFC form still parses
]


class MessageIdForms(unittest.TestCase):
    def test_every_seen_form_parses_without_its_brackets(self):
        for raw in SEEN:
            with self.subTest(raw=raw[:40]):
                got = identifiers.normalize("internet_message_id", raw)
                bare = raw[1:-1] if raw.startswith("<") else raw
                self.assertEqual(got, bare)

    def test_what_is_no_header_value_stays_unparsed(self):
        for raw in ("<a b@example.com>", "<a@b>c>", "a\tb@c", "<>", "", "x" * 999,
                    "<" + "y" * 997 + ">", "café@example.com", None, 7):
            with self.subTest(raw=str(raw)[:20]):
                self.assertIsNone(identifiers.normalize("internet_message_id", raw))
        # another kind keeps the shared 200-character bound
        self.assertIsNone(identifiers.parse("content_sha256", "a" * 201))

    def test_copies_still_need_equal_fields(self):
        record = {"internet_message_id": SEEN[0], "from": "a@x.com", "to": ["b@y.com"],
                  "cc": [], "sent_at": "2026-11-01T10:00:00Z", "subject": "PO 123",
                  "body_preview": "see attached"}
        same = identifiers.copy_key("mail", "messages", dict(record))
        self.assertIsNotNone(same)
        self.assertEqual(identifiers.copy_key("mail", "messages", dict(record)), same)
        forged = {**record, "subject": "something else"}
        self.assertNotEqual(identifiers.copy_key("mail", "messages", forged), same)

    def test_a_kind_length_outside_the_line_limit_is_refused(self):
        data = copy.deepcopy(json.loads(identifiers.SCHEMA_PATH.read_text()))
        for bad in (999, 150, "998", 998.0, True):
            data["kinds"]["internet_message_id"]["max_length"] = bad
            with self.subTest(bad=bad), \
                    self.assertRaisesRegex(identifiers.IdentifierSchemaError, "max_length"):
                identifiers._Compiled(data)


if __name__ == "__main__":
    unittest.main()
