"""Outlook's MsoDataStore (Office custom-XML data stores) in a ``.msg``: read when it is
only XML text, never when it could hide something else. Boundary: ``use_case_parts.account``
(the parts and their text) and ``carried_mail.carried_people`` (whether the walk read every
part a carried mail could hide in: ``whole``), which the people-exclusion gate reads."""
from __future__ import annotations

import unittest

from integrations import carried_mail
from operating.tests.test_use_case_intake import cfb, outlook_message

ITEM = ('<?xml version="1.0" encoding="utf-8" standalone="no"?>'
        '<ds:datastoreItem ds:itemID="{3F2504E0-4F89-11D3-9A0C-0305E82C3301}" '
        'xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/customXml">'
        '<ds:schemaRefs><ds:schemaRef ds:uri="http://example.invalid/forms"/></ds:schemaRefs>'
        '</ds:datastoreItem>').encode()
PROPS = (b'<?xml version="1.0"?><Properties><Owner who="Quarterly forecast form"/>'
         b'<![CDATA[cdata text]]><!-- comment --></Properties>')
MSO_PI = b'<?mso-application progid="InfoPath.Document"?><my:form xmlns:my="urn:x"/>'


def message_with(store: dict | None, **kw) -> bytes:
    """The Outlook message of the intake tests with a top-level MsoDataStore added."""
    from operating.msg_reader import Cfb
    base = outlook_message(**kw)
    if store is None:
        return base
    # rebuild from the same tree plus the store: the intake helper builds the tree inline,
    # so read it back through the reader's own tree walk
    tree = _tree(Cfb(base))
    tree["MsoDataStore"] = store
    return cfb(tree)


def _tree(c, index: int = 0) -> dict:
    from operating import msg_reader
    out: dict = {}
    for child in c.children(index):
        name, kind = c.entries[child][0], c.entries[child][1]
        out[name] = _tree(c, child) if kind == msg_reader.STORAGE else c.stream(child, 1 << 24)
    return out


class MsoDataStore(unittest.TestCase):
    def walk(self, store, name="m.msg"):
        return carried_mail.carried_people(message_with(store), name)

    def test_xml_streams_are_no_hiding_place_and_the_addresses_they_name_are_decided(self):
        mails, whole = self.walk({"GUID1==": {"Item": ITEM, "Properties": PROPS},
                                  "GUID2==": {"Item": MSO_PI}})
        self.assertTrue(whole)
        self.assertEqual([m.path for m in mails], ["."])      # names nobody: nothing added
        named = ITEM.replace(b"</ds:datastoreItem>",
                             b"<who mail='Ex.Cluded@Corp.example'/></ds:datastoreItem>")
        mails, whole = self.walk({"G==": {"Item": named}})
        self.assertTrue(whole)
        self.assertEqual([(m.path, sorted(m.addresses), m.complete) for m in mails[1:]],
                         [("MsoDataStore", ["ex.cluded@corp.example"], True)])

    def test_a_named_address_the_exclusion_holds_withholds_the_message(self):
        class Guard:
            unavailable = False

            def known_domains(self):
                return {"corp.example"}

            def excluded(self, address):
                return address == "ex.cluded@corp.example"
        named = ITEM.replace(b"</ds:datastoreItem>",
                             b"<who mail='ex.cluded@corp.example'/></ds:datastoreItem>")
        verdict = carried_mail.decided(self.walk({"G==": {"Item": named}}), Guard())
        self.assertNotEqual(verdict.state, carried_mail.CLEAR)

    def test_anything_but_xml_text_stays_a_hiding_gap(self):
        cases = {
            "binary stream": {"G==": {"Item": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 40}},
            "unknown stream name": {"G==": {"Item": ITEM, "Hidden": ITEM}},
            "nested storage": {"G==": {"Item": ITEM, "Deeper": {"Item": ITEM}}},
            "stream where a store belongs": {"Item": ITEM},
            "xml with a NUL": {"G==": {"Item": b"<?xml version='1.0'?>\x00<a/>"}},
            "not an xml head": {"G==": {"Item": b"MIME-Version: 1.0\r\nFrom: a@b.c\r\n\r\nhi"}},
            "bad utf-8": {"G==": {"Item": b"<?xml version='1.0'?><a>\xff\xfe\xfa</a>"}},
            "over the stream cap": {"G==": {"Item": b"<?xml " + b"a" * (1 << 20)}},
            "empty": {},
        }
        for label, store in cases.items():
            with self.subTest(label):
                self.assertFalse(self.walk(store)[1])

    def test_a_store_inside_a_carried_message_is_judged_by_its_own_bytes(self):
        import io
        import zipfile
        for store, expected in (({"G==": {"Item": ITEM}}, True),
                                ({"G==": {"Item": b"\xd0\xcf\x11\xe0" + b"x" * 40}}, False)):
            body = io.BytesIO()
            with zipfile.ZipFile(body, "w") as z:
                z.writestr("in/inner.msg", message_with(store))
            with self.subTest(expected=expected):
                self.assertEqual(carried_mail.carried_people(body.getvalue(), "z.zip")[1],
                                 expected)

    def test_the_store_is_trusted_only_at_the_top_of_the_message(self):
        from operating.msg_reader import Cfb
        base = _tree(Cfb(outlook_message()))
        base["__attach_version1.0_#00000000"]["MsoDataStore"] = {"G==": {"Item": ITEM}}
        self.assertFalse(carried_mail.carried_people(cfb(base), "m.msg")[1])


if __name__ == "__main__":
    unittest.main()
