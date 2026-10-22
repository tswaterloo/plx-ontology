"""Mail read/pin snapshot regression (injected transport, no live requests).

The source keeps metadata/changeKey constant but changes its body within one
plx.read call. Any offered cite_as must pin the body actually displayed, or the
reader must withhold that pin visibly (a ``cite_as_withheld`` gap). The unchanged
body still pins and cites (parity).
"""

from hashlib import sha256
import unittest

from integrations.tests.test_live_source_adapters import MSG_PATH, j
from operating.tests.test_case_live_citations import (
    BODY, MAIL_METADATA, VINCE, LiveCitationTestCase,
)


class MailPinSnapshotTests(LiveCitationTestCase):
    def test_cite_as_never_pins_a_later_body_than_the_displayed_content(self):
        later_body = BODY + "PS: the quoted quantity is superseded.\n"
        served_bodies = []

        def changing_message(_url):
            # On current main, GETs 1/2 resolve metadata/digest, GET 3 reads the
            # displayed body, and GETs 4/5 construct/resolve cite_as. A repaired
            # single-snapshot reader can use fewer GETs; no count is required.
            body = BODY if len(served_bodies) < 3 else later_body
            served_bodies.append(body)
            return j(200, dict(self.message, body={"contentType": "text", "content": body}))

        self.sender.routes[f"{MSG_PATH}/m1"] = changing_message
        server = self.server(self.runtime(self.live()), VINCE)
        read = self.ok(server, "plx.read", {"citation": MAIL_METADATA})

        self.assertEqual(read["status"], "resolved")
        displayed = read["content"]["untrusted_text"]
        self.assertEqual(displayed, BODY)
        self.assertEqual(read["content"]["origin"]["citation"], MAIL_METADATA)
        self.assertTrue(served_bodies)
        self.assertTrue(all(method == "GET" for method, _ in self.source_calls()))
        if "cite_as" in read:
            expected = sha256(displayed.encode("utf-8")).hexdigest()
            self.assertIn(
                f"#body_sha256={expected}&", read["cite_as"]["citation"],
                "plx.read must not recommend a citation for an unseen later body; "
                "pin the displayed snapshot or withhold cite_as",
            )
        else:
            # A withheld pin is visible, never silent.
            self.assertIn("cite_as_withheld", [gap["reason"] for gap in read["gaps"]])

    def test_an_unchanged_body_is_pinned_from_the_read_that_showed_it_and_cites(self):
        server = self.server(self.runtime(self.live()), VINCE)
        case_id = self.open_case(server)
        before = len(self.source_calls())
        read = self.cite(server, case_id)
        displayed = read["content"]["untrusted_text"]
        self.assertEqual(displayed, BODY)
        expected = sha256(displayed.encode("utf-8")).hexdigest()
        self.assertTrue(read["cite_as"]["citation"].startswith(
            f"{MAIL_METADATA}#body_sha256={expected}&shown_sha256="))
        self.assertNotIn("cite_as_withheld", [gap["reason"] for gap in read["gaps"]])
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        [finding] = view["findings"]["supported"]
        self.assertEqual(finding["citations"][0]["digest"], read["cite_as"]["digest"])
        self.assertEqual(finding["citations"][0]["mirror"]["status"], "current")
        self.assertTrue(len(self.source_calls()) > before)
        self.assertTrue(all(method == "GET" for method, _ in self.source_calls()))


if __name__ == "__main__":
    unittest.main()
