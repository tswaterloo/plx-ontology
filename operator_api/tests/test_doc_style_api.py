"""Make a quote for Harbor like Pat does (the Ask): a draft in Pat's learned template, every
changing part with its source or left blank, kept for the reader alone; a template is
used only by a reader who opens every document it was learned from. Evidence:
isolated-fixture (the Ask's instruct harness)."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from operating import doc_fill as df
from operating import doc_gather as dg
from operating import doc_style as ds
from operating.tests.test_doc_style import SENT
from operator_api.tests import test_instruct as ti
from operator_api.tests.test_instruct import ANA, BEN_MAIL, FILE_H, MAIL, World

CUSTOMER_H = "bc://Production/c/customers/7@a"


def template_of(sources, docs, author="pat.lee@plx.example"):
    members = []
    for n, (source, data) in enumerate(zip(sources, docs)):
        r = ds.read(data)
        members.append(ds.Member(source, f"2026-0{n + 1}-01T10:00:00Z",
                                 hashlib.sha256(data).hexdigest(), r.units, r.format,
                                 f"customer{n}.test", (f"buyer@customer{n}.test",)))
    return ds.learn(author, "quote", members)


class StyleWorld(World):
    def style_facts(self, reader, words):
        if "harbor" not in words.lower():
            return [], ["nothing matched"], None
        facts = [df.Fact("Customer.name", "Harbor Naturals",
                         df.Citation("Customer:7", "name", CUSTOMER_H)),
                 df.Fact("Customer.name", ti.EXCLUDED,
                         df.Citation("Customer:7", "contact", CUSTOMER_H))]
        facts = facts[:1] if not self.excluded_value else facts[1:]
        notes = ["1 record not read", dg.PARTIAL] if self.partial else []
        return facts + dg.quoted_prices("Oat Oil 25 kg $12.43", MAIL), notes, None


class DocStyleApi(ti.InstructApi):
    def setUp(self):
        super().setUp()
        self.backend.__class__ = StyleWorld
        StyleWorld.excluded_value = False
        StyleWorld.partial = False
        self.model = self.backend.instruct_call = ti.Model()     # never called for a draft
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        mine = template_of([MAIL, FILE_H], SENT[:2])                 # Ana opens both
        wider = template_of([MAIL, FILE_H, BEN_MAIL], SENT)          # Ben's mail among them
        (root / "exemplars").mkdir()
        for data in SENT:
            (root / "exemplars" / hashlib.sha256(data).hexdigest()).write_bytes(data)
        ds.save(root / "templates.json", [mine, wider], "2026-04-01T00:00:00Z")
        self.backend.style_store = lambda: root

    def test_a_draft_in_their_template_comes_back_cited_and_for_the_reader_alone(self):
        out = self.instruct(self.ana, "make a quote for Harbor Naturals like Pat does")
        self.assertEqual(out.status_code, 200, out.text)
        made = out.json()["fills"][0]
        self.assertEqual(self.model.prompts, [])
        got = {(f["label"], f["status"]): f for f in made["fields"]}
        customer = got[("Customer", "filled")]
        self.assertEqual((customer["after"], customer["citation"]["source"]),
                         ("Customer: Harbor Naturals", CUSTOMER_H))
        price = next(f for f in made["fields"] if f["after"] == "$12.40")
        self.assertEqual(price["citation"]["source"], MAIL)
        self.assertTrue(any(f["status"] == "no_fact" for f in made["fields"]))
        # the template Ana may use is the one learned only from what she opens: its
        # exemplar is the second quote, never the one beside Ben's mail
        self.assertEqual(made["digest_before"], hashlib.sha256(SENT[1]).hexdigest())
        data = self.ana.get(made["download_url"])
        self.assertEqual(data.status_code, 200)
        self.assertEqual(self.ben.get(made["download_url"]).status_code, 404)
        words = " ".join(u.text for u in ds.read(data.content).units)
        self.assertIn("Kind regards,", words)
        self.assertNotIn("Bluefin", words)
        self.assertNotIn(dg.PARTIAL, made["notes"])
        # a gather the time limit cut short still gives the draft, labelled partial
        StyleWorld.partial = True
        made = self.instruct(self.ana, "make a quote for Harbor Naturals like Pat does"
                             ).json()["fills"][0]
        self.assertEqual(made["notes"][0], dg.PARTIAL)
        self.assertTrue(made["download_url"])

    def test_who_may_not_use_it_or_an_unclear_ask_makes_nothing(self):
        # Ben opens none of Pat's documents: no template is his to use
        out = self.instruct(self.ben, "make a quote for Harbor Naturals like Pat does")
        self.assertIn("No one by that name has a template", out.text)
        self.assertEqual(self.backend._uploads()._rows(), [])
        # nobody by that name, no subject: refused, never another person's template
        self.assertIn("No one by that name", self.instruct(
            self.ana, "make a quote for Harbor Naturals like Quinn does").text)
        self.assertIn("Say who or what it is for", self.instruct(
            self.ana, "make a quote like Pat does").text)
        # a value naming an excluded person never enters the document
        StyleWorld.excluded_value = True
        made = self.instruct(self.ana, "make a quote for Harbor like Pat does").json()
        data = self.ana.get(made["fills"][0]["download_url"]).content
        self.assertNotIn(ti.EXCLUDED, " ".join(u.text for u in ds.read(data).units))
        # learned templates that cannot be read pause it, nothing guessed
        (self.backend.style_store() / "templates.json").write_text("{}")
        self.assertIn("Templates are paused", self.instruct(
            self.ana, "make a quote for Harbor like Pat does").text)
        self.assertEqual(self.model.prompts, [])

    def test_listing_opens_each_document_once_and_a_session_with_two_addresses_names_no_one(self):
        from types import SimpleNamespace
        from operator_api.doc_style_api import _own_address
        root = self.backend.style_store()
        kinds = [template_of([MAIL, FILE_H], SENT[:2], author=ANA[0])]
        for kind in ("invoice", "packing_list"):
            t = template_of([MAIL, FILE_H], SENT[:2], author=ANA[0])
            kinds.append(t.__class__(**{**t.__dict__, "kind": kind}) if hasattr(t, "__dict__")
                         and not isinstance(t, dict) else {**t, "kind": kind})
        ds.save(root / "templates.json", kinds, "2026-11-14T06:00:00Z")
        calls, real = [], self.backend.source
        self.backend.source = lambda reader, source, n: (calls.append(source),
                                                         real(reader, source, n))[1]
        body = self.ana.get("/api/templates/learned").json()
        self.assertEqual(len(body["templates"]), 3)
        self.assertEqual(sorted(calls), sorted({str(MAIL), str(FILE_H)}))  # once each
        one = SimpleNamespace(email=None, session=SimpleNamespace(identities=frozenset(
            {"ana@plx.example", "00000000-0000-4000-8000-0000000000a1"})))
        two = SimpleNamespace(email=None, session=SimpleNamespace(identities=frozenset(
            {"ana@plx.example", "shared-orders@plx.example"})))
        self.assertEqual(_own_address(one), "ana@plx.example")
        self.assertIsNone(_own_address(two))
        self.assertEqual(_own_address(SimpleNamespace(email="Ana@plx.example", session=two.session)),
                         "Ana@plx.example")

    def test_your_learned_templates_are_listed_and_their_words_make_one(self):
        from operator_api.doc_style_api import KIND_WORDS, style_request
        root = self.backend.style_store()
        own = template_of([MAIL, FILE_H], SENT[:2], author=ANA[0])
        beside_ben = template_of([MAIL, FILE_H, BEN_MAIL], SENT, author=ANA[0])
        pats = template_of([MAIL, FILE_H], SENT[:2])
        ds.save(root / "templates.json", [own, beside_ben, pats], "2026-11-14T06:00:00Z")
        got = self.ana.get("/api/templates/learned")
        self.assertEqual(got.status_code, 200, got.text)
        body = got.json()
        self.assertEqual((body["status"], body["learned_at"]), ("read", "2026-11-14T06:00:00Z"))
        # one quote template: the one Ana opens every document of (never the one beside
        # Ben's mail, never Pat's)
        (quote,) = body["templates"]
        self.assertEqual((quote["kind"], quote["label"], quote["documents"]), ("quote", "Quote", 2))
        self.assertEqual(quote["last_sent"], "2026-02-01")
        # the listed words ask for exactly that: Ask makes it from there
        made = self.instruct(self.ana, quote["ask"] + "Harbor Naturals")
        self.assertEqual(made.status_code, 200, made.text)
        self.assertEqual(made.json()["fills"][0]["digest_before"],
                         hashlib.sha256(SENT[1]).hexdigest())
        for kind, words in KIND_WORDS.items():   # every kind's words read back to it
            with self.subTest(kind=kind):
                asked = style_request(f"make a {words} in my usual template for Harbor")
                self.assertEqual((asked["kind"], asked["author"], asked["subject"]),
                                 (kind, "", "Harbor"))
        # Ben authored none: nothing of Ana's or Pat's is listed for him
        self.assertEqual(self.ben.get("/api/templates/learned").json()["templates"], [])
        # the people exclusion unreadable: nothing listed (fail closed)
        self.backend._instruct_guard = lambda: None
        blind = self.ana.get("/api/templates/learned").json()
        self.assertEqual((blind["status"], blind["templates"]), ("unavailable", []))
        # nothing learned, or a file this code cannot read: none, never a guess
        del self.backend._instruct_guard
        (root / "templates.json").write_text("{}")
        self.assertEqual(self.ana.get("/api/templates/learned").json()["status"], "none")

    def test_the_learned_file_is_parsed_once_per_version(self):
        """The learned templates file (tens of MB live) is parsed once per version, not on
        every Templates visit; a re-learned file (``save`` replaces it) is read anew."""
        from unittest import mock
        root = self.backend.style_store()
        own = template_of([MAIL, FILE_H], SENT[:2], author=ANA[0])
        ds.save(root / "templates.json", [own], "2026-11-14T06:00:00Z")
        parse = mock.patch.object(ds, "_read", wraps=ds._read)
        with parse as parsed:
            for _ in range(3):
                body = self.ana.get("/api/templates/learned").json()
                self.assertEqual(len(body["templates"]), 1)
            self.assertEqual(parsed.call_count, 1)
            ds.save(root / "templates.json", [own, {**own, "id": own["id"] + "-i",
                                                     "kind": "invoice"}],
                    "2026-11-15T06:00:00Z")
            body = self.ana.get("/api/templates/learned").json()
            self.assertEqual((len(body["templates"]), body["learned_at"]),
                             (2, "2026-11-15T06:00:00Z"))
            self.assertEqual(parsed.call_count, 2)


def load_tests(loader, tests, pattern):
    """This module's own tests (the instruct harness's run in its module)."""
    import unittest
    return unittest.TestSuite(DocStyleApi(name) for name in loader.getTestCaseNames(DocStyleApi)
                              if name in DocStyleApi.__dict__)
