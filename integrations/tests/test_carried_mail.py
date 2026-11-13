"""DOC-008: a carried mail's people decide whether its container may be kept."""

from __future__ import annotations

from io import BytesIO
import unittest
from unittest import mock
from zipfile import ZipFile

from integrations import carried_mail as cm
from integrations.people_exclusion import PeopleExclusion
from integrations.tests.test_doc_convert import _unicode, make_cfb, msg_tree

GONE = "99999999-9999-4999-8999-999999999999"
ANA = "11111111-1111-4111-8111-111111111111"


def guard() -> PeopleExclusion:
    held = PeopleExclusion([GONE])
    held.attach_directory([(GONE, "gone@plx.example", []), (ANA, "ana@plx.example", [])])
    return held


def eml(sender: str, to: str = "orders@supplier.example", *, bcc: str | None = None,
        body: str = "See the quote.") -> bytes:
    head = (f"From: {sender}\r\n" if sender else "") + f"To: {to}\r\n"
    head += f"Bcc: {bcc}\r\n" if bcc else ""
    return (head + "Subject: quote\r\nContent-Type: text/plain\r\n\r\n" + body).encode()


def zipped(**files: bytes) -> bytes:
    out = BytesIO()
    with ZipFile(out, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


def msg(recipient: str | None, *, sender: str = "sales@acme.example", embedded=None) -> bytes:
    tree = msg_tree([("fwd.msg", "application/vnd.ms-outlook", embedded)] if embedded else [])
    tree["__substg1.0_0C1F001F"] = _unicode(sender)
    if recipient is not None:
        tree["__recip_version1.0_#00000000"]["__substg1.0_39FE001F"] = _unicode(recipient)
    return make_cfb(tree)


class CarriedMailVerdict(unittest.TestCase):
    def test_a_carried_mail_of_an_excluded_person_withholds_it_and_what_it_carries(self):
        inner = zipped(**{"fwd.eml": eml("gone@plx.example"), "quote.txt": b"42 boxes"})
        found = cm.verdict(inner, "files.zip", "application/zip", guard())
        self.assertEqual((found.state, found.withheld, found.carried),
                         (cm.EXCLUDED, frozenset({"fwd.eml"}), 1))
        self.assertTrue(found.withholds("fwd.eml"))
        self.assertTrue(found.withholds("fwd.eml!/1/report.pdf"))
        self.assertFalse(found.withholds("quote.txt"))
        # an attached .eml naming them only in Bcc, at the top: the whole container
        top = cm.verdict(eml("ana@plx.example", bcc="Gone <GONE@plx.example>"), "a.eml",
                         "message/rfc822", guard())
        self.assertEqual((top.state, top.withheld), (cm.EXCLUDED, frozenset({"."})))
        self.assertTrue(top.withholds("anything"))

    def test_only_a_mail_every_identity_of_which_is_cleared_is_kept(self):
        g = guard()
        ok = cm.verdict(zipped(**{"a.eml": eml("ana@plx.example")}), "f.zip", None, g)
        self.assertEqual((ok.state, ok.withheld, ok.carried), (cm.CLEAR, frozenset(), 1))
        # not a container: nothing carried
        self.assertEqual(cm.verdict(b"%PDF-1.4 x", "a.pdf", "application/pdf", g).state,
                         cm.CLEAR)
        # fail closed: a tenant address the directory does not hold, no sender, a guard
        # that cannot decide, a guard without tenant domains
        for content, guard_used in (
                (eml("who@plx.example"), g), (eml(""), g),
                (eml("ana@plx.example"), PeopleExclusion(unavailable="excluded_people_oids_absent")),
                (eml("ana@plx.example"), None), (eml("ana@plx.example"), PeopleExclusion([GONE]))):
            with self.subTest(content=content[:30]):
                found = cm.verdict(content, "a.eml", "message/rfc822", guard_used)
                self.assertEqual((found.state, found.withheld),
                                 (cm.UNDECIDED, frozenset({"."})))

    def test_an_outlook_msg_is_decided_by_its_sender_and_every_recipient(self):
        g = guard()
        self.assertEqual(cm.verdict(msg("ana@plx.example"), "m.msg", None, g).state, cm.CLEAR)
        self.assertEqual(cm.verdict(msg("gone@plx.example"), "m.msg", None, g).state,
                         cm.EXCLUDED)
        # a recipient with no SMTP address (an Exchange DN only) cannot be cleared
        tree_dn = msg(None)
        self.assertEqual(cm.verdict(tree_dn, "m.msg", None, g).state, cm.UNDECIDED)
        # an excluded person's mail embedded in a cleared one: that member is withheld
        nested = msg("ana@plx.example", embedded=msg_tree([], header=24)
                     | {"__substg1.0_0C1F001F": _unicode("gone@plx.example")})
        found = cm.verdict(nested, "m.msg", None, g)
        self.assertEqual(found.state, cm.EXCLUDED)
        self.assertNotIn(".", found.withheld)
        self.assertEqual(len(found.withheld), 1)

    def test_one_ten_sixty_carried_mails_each_decided_none_lost(self):
        g = guard()
        for n in (1, 10, 60):
            files = {f"m{i}.eml": eml("gone@plx.example" if i == n - 1 else "ana@plx.example")
                     for i in range(n)}
            found = cm.verdict(zipped(**files), "f.zip", None, g)
            self.assertEqual((found.state, found.carried, found.withheld),
                             (cm.EXCLUDED, n, frozenset({f"m{n - 1}.eml"})), n)

    def test_the_verdict_keeps_no_address(self):
        found = cm.verdict(eml("gone@plx.example"), "a.eml", None, guard())
        self.assertNotIn("@", repr(found))
        self.assertEqual(dict(cm.counts([found])), {cm.CLEAR: 0, cm.EXCLUDED: 1,
                                                    cm.UNDECIDED: 0})

    def test_a_container_that_cannot_be_walked_or_hides_a_part_clears_nobody(self):
        g = guard()
        with mock.patch.object(cm, "carried_people", side_effect=RuntimeError("x")):
            found = cm.verdict(zipped(**{"a.txt": b"x"}), "f.zip", None, g)
        self.assertEqual((found.state, found.withheld), (cm.UNDECIDED, frozenset({"."})))
        # a part skipped over a cap may be a mail a reader of the text still reads
        with mock.patch("operating.use_case_parts.MAX_DEPTH", 1):
            deep = zipped(**{"in.zip": zipped(**{"in2.zip": zipped(**{"a.eml": eml(
                "ana@plx.example")})})})
            self.assertEqual(cm.verdict(deep, "f.zip", None, g).state, cm.UNDECIDED)
        # bytes no reader opens hold nothing anybody reads: no carried mail
        self.assertEqual(cm.verdict(b"PK\x03\x04 not a zip" * 4, "f.zip", None, g).state,
                         cm.CLEAR)

    def test_a_kept_walk_is_decided_again_with_the_guard_of_the_moment(self):
        content = zipped(**{"a.eml": eml("ana@plx.example", bcc="gone@plx.example"),
                            "b.txt": b"x"})
        walked = cm.walk(content, "f.zip", None)
        kept = cm.decode(cm.encode(walked))
        self.assertEqual(kept, walked)
        self.assertIsNone(cm.decode(cm.encode(None)))
        self.assertEqual(cm.decided(kept, guard()).state, cm.EXCLUDED)
        nobody = PeopleExclusion([GONE.replace("9", "8")])
        nobody.attach_directory([(GONE, "gone@plx.example", []),
                                 (ANA, "ana@plx.example", [])])
        self.assertEqual(cm.decided(kept, nobody).state, cm.CLEAR)
        for bad in ('{"whole": 1, "mails": []}', '{"whole": true, "mails": [[1, [], true]]}',
                    '[]'):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                cm.decode(bad)

    def test_only_what_may_be_a_container_is_read_whole(self):
        self.assertTrue(cm.may_carry(b"PK\x03\x04", "a.bin", None))
        self.assertTrue(cm.may_carry(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "a.doc", None))
        self.assertTrue(cm.may_carry(b"Received: x", "a.eml", None))
        self.assertTrue(cm.may_carry(b"", "fwd", "message/rfc822"))
        self.assertFalse(cm.may_carry(b"%PDF-1.7", "a.pdf", "application/pdf"))
        self.assertFalse(cm.may_carry(b"hello", "a.txt", "text/plain"))


if __name__ == "__main__":
    unittest.main()
