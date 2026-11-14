"""A person's templates learned from what they sent, and a new document made in one
(``operating.doc_style``, ``operating.doc_gather``). Evidence: ``isolated-fixture``."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import tempfile
from pathlib import Path
from typing import Optional
import unittest
from unittest import mock
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from operating import doc_fill as df
from operating import doc_gather as dg
from operating import doc_style as ds


def _col(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def xlsx(cells: dict[tuple[int, int], str]) -> bytes:
    rows: dict[int, list[str]] = {}
    for (r, c), v in sorted(cells.items()):
        rows.setdefault(r, []).append(
            f'<c r="{_col(c)}{r}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
    sheet = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.'
             'openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(
                 f'<row r="{r}">{"".join(cs)}</row>' for r, cs in rows.items())
             + "</sheetData></worksheet>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"><Override PartName="/xl/'
                   'workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.sheet.main+xml"/></Types>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.'
                   'openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet '
                   'name="Quote" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns='
                   '"http://schemas.openxmlformats.org/package/2006/relationships"><Relationship'
                   ' Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return out.getvalue()


def quote(customer: str, day: str, items: list[tuple[str, list[str]]],
          breaks: tuple[str, ...] = ("25 kg", "100 kg", "500 kg")) -> bytes:
    """A tiered raw-material quote as one fictional author lays it out."""
    cells = {(1, 1): "QUOTATION", (2, 1): f"Customer: {customer}", (3, 1): f"Date: {day}",
             (5, 1): "Item", (5, 2): "Qty", (5, 3): "Price (USD)",
             (5, 4): f"{customer} code"}       # a header cell that changes per customer
    r = 6
    for name, prices in items:
        for q, p in zip(breaks, prices):
            cells[(r, 1)], cells[(r, 2)], cells[(r, 3)] = name, q, p
            r += 1
    cells[(r + 1, 1)] = "Terms: Net 30, FOB origin"
    cells[(r + 2, 1)] = "Prices valid for 30 days"
    cells[(r + 3, 1)] = "Kind regards,"
    cells[(r + 4, 1)] = "Pat Example, Sales"
    return xlsx(cells)


SENT = [
    quote("Northwind Labs", "Jan 5, 2026", [("Oat Oil", ["$12.40", "$11.10", "$9.80"]),
                                            ("Clay Blend", ["$4.20", "$3.90", "$3.50"])]),
    quote("Bluefin Goods", "Feb 9, 2026", [("Rose Water", ["$7.00", "$6.40", "$5.90"])]),
    quote("Kestrel Co", "Mar 2, 2026", [("Aloe Gel", ["$5.50", "$5.10", "$4.60"]),
                                        ("Shea Melt", ["$8.30", "$7.70", "$7.10"])]),
]
OTHER = xlsx({(1, 1): "PACKING LIST", (2, 1): "Ship to: Somewhere", (4, 1): "Carton",
              (4, 2): "Weight", (5, 1): "1", (5, 2): "20 kg"})


def members(docs: list[bytes], start: int = 1, parties: Optional[list[str]] = None
            ) -> list[ds.Member]:
    """Sent documents, each to its own customer unless ``parties`` names who got each."""
    out = []
    for n, data in enumerate(docs, start):
        r = ds.read(data)
        sent = (dt.datetime(2026, 1, 1) + dt.timedelta(days=n)).isoformat() + "Z"
        to = parties[n - start] if parties else f"buyer@customer{n}.test"
        out.append(ds.Member(f"graph-mail://m/{n}", sent,
                             hashlib.sha256(data).hexdigest(), r.units, r.format,
                             ds.party_of([to], ["example.test"]), (to,)))
    return out


def fact(key: str, value: str, source: str = "bc://customer/7") -> df.Fact:
    return df.Fact(key, value, df.Citation("Customer:7", key.split(".", 1)[1], source))


class LearnAndMakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.terms = df.load_terms()
        self.template = ds.learn("pat@example.test", "quote", members(SENT))

    def test_template_holds_layout_tiers_conventions_and_signoff(self) -> None:
        t = self.template
        self.assertEqual(t["support"], 3)
        fixed = {e["text"] for e in t["units"] if e["role"] == "fixed"}
        self.assertTrue({"QUOTATION", "Terms: Net 30, FOB origin", "Kind regards,",
                         "Pat Example, Sales"} <= fixed)
        # another customer's names are never fixed text
        self.assertFalse(any("Kestrel" in x or "Aloe" in x for x in fixed))
        self.assertEqual(t["tiers"]["usual"], {"axis": "row", "breaks": ["25", "100", "500"],
                                               "unit": "kg"})
        conv = t["conventions"]
        self.assertEqual((conv["sign"], conv["decimals"], conv["rounding"],
                          conv["date_format"]), ("$", 2, "0.1", "%B %d, %Y"))
        signoff = [e["text"] for e in t["units"] if e["uid"] in t["signoff"]]
        self.assertEqual(signoff[-2:], ["Kind regards,", "Pat Example, Sales"])
        self.assertTrue(any("Net 30" in e["text"] for e in t["units"]
                            if e["uid"] in t["terms"]))

    def test_new_quote_fills_cited_facts_keeps_their_text_and_flags_the_rest(self) -> None:
        mail = "graph-mail://m/quoted"
        facts = [fact("Customer.name", "Harbor Naturals")] + dg.quoted_prices(
            "Oat Oil 25 kg $12.43 USD\n100 kg $11.27 USD", mail)
        made = ds.generate(self.template, SENT[-1], facts, self.terms,
                           as_of=dt.date(2026, 4, 7))
        text = {u.uid: u.text for u in ds.read(made.data).units}
        words = " ".join(text.values())
        self.assertIn("Customer: Harbor Naturals", words)
        self.assertIn("Date: April 7, 2026", words)
        self.assertIn("Kind regards,", words)
        self.assertIn("Terms: Net 30, FOB origin", words)
        # the exemplar's customer, items and prices never reach the new document
        for gone in ("Kestrel", "Aloe", "Shea", "$5.50", "$8.30", "Mar 2"):
            self.assertNotIn(gone, words)
        # one item's tier rows kept with their breaks; quoted prices rounded the template's
        # way (0.1) and cited to the mail they were quoted in; the untiered price is blank
        prices = [u for u in made.units if u.status == "filled" and u.field
                  and u.field.startswith("price@")]
        self.assertEqual(sorted(u.text for u in prices), ["$11.30", "$12.40"])
        self.assertTrue(all(u.citation.source == mail for u in prices))
        self.assertIn("25 kg", words)
        self.assertIn("500 kg", words)
        blanks = [u for u in made.units if u.status == "blank"]
        self.assertTrue(any("tier" in u.reason for u in blanks))
        self.assertEqual(sum(1 for u in made.units if u.status == "removed"), 9)  # 3 rows
        customer = next(u for u in made.units if u.label == "Customer")
        self.assertEqual(customer.citation.source, "bc://customer/7")
        score = ds.compare(self.template, made.units, members([SENT[0]])[0].units)
        self.assertGreater(score["structure"], 0.5)
        self.assertTrue(score["tiers_same"])

    def test_two_values_or_none_leave_the_slot_blank(self) -> None:
        facts = [fact("Customer.name", "Harbor Naturals"),
                 fact("Customer.name", "Harbour Naturals Inc", "bc://customer/8")]
        facts += dg.quoted_prices("Oat Oil 25 kg $12.43\nClay 25 kg $4.00", "graph-mail://x")
        made = ds.generate(self.template, SENT[-1], facts, self.terms,
                           as_of=dt.date(2026, 4, 7))
        customer = next(u for u in made.units if u.label == "Customer")
        self.assertEqual((customer.status, customer.text), ("blank", "Customer: ____"))
        first = next(u for u in made.units if u.field is None and u.status == "blank"
                     and "different values" in u.reason)
        self.assertIsNotNone(first)

    def test_a_label_reads_mail_that_words_it_differently(self) -> None:
        facts = dg.text_facts("Customer Name: Harbor Naturals", "graph-mail://m/said")
        made = ds.generate(self.template, SENT[-1], facts, self.terms,
                           as_of=dt.date(2026, 4, 7))
        customer = next(u for u in made.units if u.label == "Customer")
        self.assertEqual((customer.status, customer.text, customer.citation.source),
                         ("filled", "Customer: Harbor Naturals", "graph-mail://m/said"))

    def test_unknown_input_fails_closed(self) -> None:
        with self.assertRaises(ds.StyleRefused) as changed:
            ds.generate(self.template, SENT[0], [], self.terms, as_of=dt.date(2026, 4, 7))
        self.assertEqual(changed.exception.code, "exemplar_changed")
        with self.assertRaises(ds.StyleRefused):
            ds.read(b"not a document")
        self.assertIsNone(ds.learn("pat@example.test", "quote", members(SENT[:1])))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "templates.json"
            path.write_text(json.dumps({"schema": "plx-doc-style/v0", "templates": []}))
            with self.assertRaises(ds.StyleRefused) as bad:
                ds.load(path)
            self.assertEqual(bad.exception.code, "templates_invalid")
            ds.save(path, [self.template], "2026-04-07T00:00:00Z")
            self.assertEqual(ds.load(path)[0]["id"], self.template["id"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_groups_split_by_structure_never_across_authors(self) -> None:
        mixed = members(SENT + [OTHER, OTHER])
        self.assertEqual(sorted(len(g) for g in ds.cluster(mixed)), [2, 3])
        got = ds.learn_all([("pat@example.test", "quote", m) for m in mixed[:2]]
                           + [("lee@example.test", "quote", m) for m in mixed[2:3]])
        self.assertEqual([t["author"] for t in got], ["pat@example.test"])

    def test_cardinality(self) -> None:
        for n in (1, 10, 60):
            docs = [quote(f"Cust {i}", "Jan 5, 2026", [(f"Item {j}", ["$1.00", "$0.90",
                                                                       "$0.80"])
                                                         for j in range(1 + i % 3)])
                    for i in range(n)]
            groups = ds.cluster(members(docs))
            self.assertEqual(sum(len(g) for g in groups), n)
            self.assertEqual(len(groups), 1)


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def letter_docx(customer: str, body: str) -> bytes:
    """A one-page letter: a heading, a paragraph holding a text box, a footnote and a
    review comment that each name the customer, and a sign-off."""
    def p(text: str, extra: str = "") -> str:
        return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>{extra}</w:p>'
    box = ('<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><w:txbxContent>'
           + p(f"Prepared for {customer}") + '</w:txbxContent></w:drawing></mc:Choice>'
           '<mc:Fallback><w:pict><w:txbxContent>' + p(f"Prepared for {customer}")
           + '</w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:r>')
    doc = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}" xmlns:mc='
           '"http://schemas.openxmlformats.org/markup-compatibility/2006"><w:body>'
           + p("PRICE LETTER", box) + p(body)
           + '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>Terms: Net 30</w:t></w:r>'
           '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r>'
           '<w:r><w:footnoteReference w:id="1"/></w:r></w:p>'
           + p("Kind regards,") + "</w:body></w:document>")
    notes = (f'<?xml version="1.0" encoding="UTF-8"?><w:footnotes xmlns:w="{W_NS}">'
             f'<w:footnote w:id="1">{p(f"Account of {customer}")}</w:footnote></w:footnotes>')
    comments = (f'<?xml version="1.0" encoding="UTF-8"?><w:comments xmlns:w="{W_NS}">'
                f'<w:comment w:id="0">{p(f"Check this with {customer} first")}</w:comment>'
                '</w:comments>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://'
                   'schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", doc)
        z.writestr("word/footnotes.xml", notes)
        z.writestr("word/comments.xml", comments)
    return out.getvalue()


class CounterpartyTest(unittest.TestCase):
    """A customer's own text repeated across documents to that customer is never a
    template's fixed text, so it never reaches a draft for someone else."""

    def setUp(self) -> None:
        self.terms = df.load_terms()
        self.facts = [fact("Customer.name", "Harbor Naturals")]

    def _words(self, data: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return " ".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())

    def test_requotes_to_one_customer_never_carry_their_name_to_another(self) -> None:
        north = [quote("Northwind Labs", "Jan 5, 2026", [("Oat Oil", ["$12.40", "$11.10",
                                                                      "$9.80"])]),
                 quote("Northwind Labs", "Feb 5, 2026", [("Oat Oil", ["$12.60", "$11.30",
                                                                      "$9.90"])])]
        cases = {
            "two to them": (north, ["ana@northwind.example", "raj@northwind.example"]),
            "two to them, one to another": (
                [SENT[1]] + north, ["kim@bluefin.example", "ana@northwind.example",
                                    "raj@northwind.example"]),
            # their name spans two parties (a broker), yet names one of the recipients
            "via a broker": (north, ["ana@northwind.example", "ops@broker.example"]),
        }
        for case, (docs, parties) in cases.items():
            with self.subTest(case):
                t = ds.learn("pat@example.test", "quote", members(docs, parties=parties))
                fixed = [e["text"] for e in t["units"] if e["role"] == "fixed"]
                self.assertFalse([x for x in fixed if "Northwind" in x], fixed)
                made = ds.generate(t, docs[-1], self.facts, self.terms,
                                   as_of=dt.date(2026, 4, 7))
                words = self._words(made.data)
                self.assertNotIn("Northwind", words)
                self.assertIn("Harbor Naturals", words)
        # across customers the layout's own text stays fixed
        t = ds.learn("pat@example.test", "quote", members(SENT))
        self.assertIn("Kind regards,", [e["text"] for e in t["units"] if e["role"] == "fixed"])

    def test_unknown_counterparty_keeps_nothing_fixed(self) -> None:
        unknown = [ds.Member(m.source, m.sent, m.digest, m.units, m.format)
                   for m in members(SENT)]
        t = ds.learn("pat@example.test", "quote", unknown)
        self.assertEqual([e for e in t["units"] if e["role"] == "fixed"], [])

    def test_footnotes_comments_and_text_boxes_never_carry_their_text(self) -> None:
        docs = [letter_docx("Northwind Labs", "Our prices for Northwind Labs follow."),
                letter_docx("Bluefin Goods", "Our prices for Bluefin Goods follow.")]
        t = ds.learn("pat@example.test", "letter", members(
            docs, parties=["ana@northwind.example", "kim@bluefin.example"]))
        made = ds.generate(t, docs[-1], self.facts, self.terms, as_of=dt.date(2026, 4, 7))
        words = self._words(made.data)
        self.assertNotIn("Bluefin", words)
        self.assertIn("PRICE LETTER", words)
        self.assertIn("Terms: Net 30", words)
        self.assertNotIn("commentReference", words)
        self.assertNotIn("<w:comment ", words)
        self.assertTrue(ET.fromstring(zipfile.ZipFile(io.BytesIO(made.data)).read(
            "word/comments.xml")) is not None)


class GatherTest(unittest.TestCase):
    def test_facts_come_from_mail_before_the_day_and_never_one_record_picked(self) -> None:
        later, earlier = "graph-mail://m/later", "graph-mail://m/earlier"
        texts = {earlier: "Contact: Dana Reyes\nOat Oil 25 kg $12.43 USD\n100 kg $11.27 USD",
                 later: "Contact: Someone Else"}
        calls = []

        def call(name, args):
            calls.append(name)
            if name == "plx.find":
                return {"candidates": [
                    {"tier": "lead", "object": {"object_id": "Customer:1",
                                                "object_type": "Customer"}},
                    {"tier": "lead", "object": {"object_id": "Customer:2",
                                                "object_type": "Customer"}},
                    {"tier": "strong", "object": {"object_id": "Email:9",
                                                  "object_type": "Email"}}]}
            if name == "plx.documents":
                return {"documents": [{"citation": later, "date": "2026-04-09"},
                                      {"citation": earlier, "date": "2026-03-01"}]}
            if args["citation"] == earlier:
                return {"status": "resolved", "fields": {"body": texts[earlier]}}
            if args["citation"] == later:
                return {"status": "resolved", "fields": {"body": texts[later]}}
            raise KeyError("refused")

        facts, notes = dg.gather(call, "Harbor raw materials", df.load_terms(), repo=object(),
                                 before="2026-04-01")
        self.assertIn("2 records loosely match: none used", notes)
        self.assertNotIn("Customer:1", {f.citation.record for f in facts})
        self.assertEqual(dg.text_answer("Contact", facts)[1].citation.source, earlier)
        prices = {f.key: f.value for f in facts if f.key.startswith("Quoted.")}
        self.assertEqual(prices, {"Quoted.price@25 kg": "12.43 USD",
                                  "Quoted.price@100 kg": "11.27 USD"})
        self.assertEqual(calls.count("plx.read"), 1)        # the later mail is never read

    def test_the_time_limit_gives_a_labelled_partial_result(self) -> None:
        clock = [100.0]
        mail = "graph-mail://m/one"

        def call(name, args):
            clock[0] += 30                                  # each read takes 30 s
            if name == "plx.find":
                return {"candidates": []}
            if name == "plx.documents":
                return {"documents": [{"citation": mail}, {"citation": mail + "2"}]}
            return {"status": "resolved", "fields": {"body": "Contact: Dana Reyes"}}
        with mock.patch.object(dg.time, "monotonic", lambda: clock[0]):
            facts, notes = dg.gather(call, "Harbor", df.load_terms(), deadline=185.0)
            self.assertEqual(notes[0], dg.PARTIAL)
            # the first mail read in time is used; the second never started
            self.assertEqual([f.citation.source for f in facts], [mail])
            clock[0] = 100.0
            _facts, notes = dg.gather(call, "Harbor", df.load_terms(), deadline=1000.0)
            self.assertNotIn(dg.PARTIAL, notes)


if __name__ == "__main__":
    unittest.main()
