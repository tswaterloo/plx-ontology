"""Search is complete over the schema: every text property of every served type is indexed
by the rule (``operating/chunking/record_text.json``), carried by a document chunker, or
deny-listed as an id, hash or secret. A new type is covered without a config entry; a
marking on a text field goes into its chunk; an excluded person's unit and a document's
derived records are never record-chunked; SharePoint list items have their own kind."""
import json
import tempfile
import unittest
from pathlib import Path

from operating.chunking import AUTHORITY
from operating.chunking import headers as hd
from operating.chunking.projection import Counts, open_ro
from operating.chunking.records import (DENY_CATEGORIES, WORDS_KIND, RecordChunker, classify,
                                        load_config, masked, schema_types, text_plan)
from operating.chunking.normalise import normalise
from operating.embed.store import WORDS_ONLY_KINDS
from tests.test_chunks_embed import EXCLUDED, Fixture, exclusion

TEXT_TYPES = {"text", "string"}


def _served(types):
    return {k: t for k, t in types.items()
            if t["availability"]["state"] in ("served", "partial")}


class Completeness(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.types = schema_types()

    def test_every_served_text_property_is_indexed_or_deny_listed(self):
        plan = text_plan(self.config, self.types)
        unlisted, unread = [], []
        for type_id, t in sorted(_served(self.types).items()):
            for p in t.get("properties") or []:
                if p["value_type"] not in TEXT_TYPES:
                    continue
                how = classify(self.config, type_id, p["name"])
                if how == "unlisted":
                    unlisted.append(f"{type_id}.{p['name']}")
                elif how in ("label", "free") and p["name"] not in plan[type_id][how]:
                    unread.append(f"{type_id}.{p['name']}")
                self.assertTrue(how in ("label", "free", "fields_only", "unlisted")
                                or how.startswith(("deny:", "document:")), how)
        self.assertEqual(unlisted, [], "text of a document type nobody indexes or lists")
        self.assertEqual(unread, [], "rule-indexed text the record chunker does not read")

    def test_deny_list_holds_only_ids_hashes_and_secrets(self):
        self.assertLessEqual(set(self.config["deny"]), set(DENY_CATEGORIES))
        for name in ("notes", "description", "name", "company_name", "subject", "title",
                     "comments", "textdesc", "vendor_name", "reason"):
            self.assertFalse(classify(self.config, "AnyType", name).startswith("deny:"), name)
        for name, category in (("api_token", "secrets"), ("content_sha256", "hashes"),
                               ("customer_id", "ids"), ("idcust", "ids"),
                               ("client_secret", "secrets"), ("password", "secrets")):
            self.assertEqual(classify(self.config, "AnyType", name), f"deny:{category}")

    def test_real_contact_bank_and_id_names_are_denied(self):
        """Names found in the schema and SharePoint list columns."""
        for name in ("email_any", "emailrep", "repemail", "shiptoemail", "sourceemail",
                     "phonerep", "mobilephone", "shiptophone", "tollfeephone", "userid",
                     "iban", "bank_account_number", "tax_registration_number", "sin",
                     "SubmittedByEmail", "Email", "VendorEmail", "PageURL", "ScreenshotURL",
                     "TaskID", "ProjectID", "user_password", "refresh_token", "faxnumber"):
            self.assertTrue(classify(self.config, "AnyType", name).startswith("deny:"), name)
        for type_id, t in _served(self.types).items():
            for p in t.get("properties") or []:
                if any(w in p["name"].lower() for w in ("email", "phone", "iban", "url")):
                    self.assertTrue(classify(self.config, type_id, p["name"])
                                    .startswith("deny:"), f"{type_id}.{p['name']}")

    def test_contact_details_in_any_value_are_masked(self):
        text = masked("Call Jane on (416) 555-0199 or +1 905 555 0123, mail jane.roe@x.example"
                      " about PO 100040 of 2026-11-05, qty 1200")
        self.assertNotIn("555", text)
        self.assertNotIn("jane.roe", text)
        self.assertIn("PO 100040 of 2026-11-05, qty 1200", text)

    def test_codes_stay_intact_and_only_real_phones_are_masked(self):
        """The phone mask mangled order, lot and item codes."""
        for code in ("CG-139-1000-677", "SDA-001-100-100-1503", "SO-2026-0001-0002",
                     "PO 4500-1234-5678", "Lot 2024 1105 0042", "12 345 678",
                     "Invoice 100-200-3000-4", "AO-416-555-0199"):
            self.assertEqual(masked(normalise(f"See {code} today")), f"See {code} today")
        for phone, want in (("Call (416) 555-0199 now", "Call [phone] now"),
                            ("Call 416-555-0199 now", "Call [phone] now"),
                            ("tel: 905.555.0123", "tel: [phone]"),
                            ("mobile 4165550199", "mobile [phone]"),
                            ("ring +44 20 7946 0958", "ring [phone]"),
                            ("fax #905 555 0123", "fax #[phone]")):
            self.assertEqual(masked(normalise(phone)), want, phone)

    def test_card_masking_leaves_barcodes_orders_lots_and_phones_and_no_snippet_shows_a_card(self):
        from operating.chunking.normalise import mask_cards
        from operating.search.core import _snippet

        def ean13(d12):
            total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d12))
            return d12 + str((10 - total % 10) % 10)
        # every EAN-13 of a 4-prefix country (one in ten passes Luhn in the Visa range) and
        # a GTIN-14 in the Diners range stay readable product codes
        codes = [ean13(f"40{n:010d}") for n in range(0, 10_000_000_000, 37_000_011)]
        codes += [ean13(f"4006381{n:05d}") for n in range(300)] + ["30012345678905"]
        for code in codes:
            self.assertEqual(mask_cards(f"item {code} each"), f"item {code} each", code)
        for text in ("PO 104523", "PO#5447", "DPO-100234", "Lot 2026110912345",
                     "LOT 241109-4512", "Lot No. 4511 2309 8812", "lot 45112309881234",
                     "call 416-555-0199", "+1 (416) 555-0199", "1-800-555-0199 ext 4411",
                     "+44 20 7946 0958", "tel 905.555.0123 / 905.555.0124",
                     "Canada Post 7023210039414604", "FedEx 771234567890",
                     "UPS 1Z999AA10123456784", "GTIN 10012345678902"):
            self.assertEqual(mask_cards(text), text, text)
        # a card in each network layout is still masked, an unbroken 15-19 digit one too
        for card in ("4111 1111 1111 1111", "4111111111111111", "378282246310005",
                     "3782 822463 10005", "3056 930902 5904", "6011-1111-1111-1117",
                     "5555555555554444", "4111111111111111110"[:16]):
            self.assertEqual(mask_cards(f"pay {card} now"), "pay [card] now", card)
        # a snippet never shows a card, wherever its window cuts the text
        for lead in range(0, 400, 23):
            body = "x " * lead + "Visa 4111 1111 1111 1111 exp 09/27 for the soap order. " * 3
            for terms in ((), ("soap",), ("visa",), ("1111",)):
                snippet = _snippet(body, terms)
                self.assertNotIn("4111", snippet, (lead, terms))
                self.assertNotIn("1111 1111", snippet, (lead, terms))

    def test_a_card_split_by_any_blank_or_entity_is_masked_before_and_after_normalise(self):
        from operating.chunking import make_chunk
        from operating.chunking.normalise import mask_cards
        for card in ("4111\u00a01111\u00a01111\u00a01111", "4111\t1111\t1111\t1111",
                     "4111  1111  1111  1111", "4111&nbsp;1111&nbsp;1111&nbsp;1111",
                     "4111 \u00a01111\t1111  1111", "4111&#160;1111 1111\u202f1111",
                     "3782\u00a0822463\u00a010005", "5500\t0000\t0000\t0004"):
            text = f"Visa {card} exp 09/27"
            self.assertEqual(mask_cards(text), "Visa [card] exp 09/27", repr(card))
            self.assertEqual(normalise(text), "Visa [card] exp 09/27", repr(card))
            chunk = make_chunk(kind="mail", parts=("a",), source_handle="h", object_id=None,
                               title=text, author_role=None, occurred_at=None, header=text,
                               body=text, linked_ids=(), audience_key="mail:x", markings=())
            for field in (chunk.title, chunk.header, chunk.body, normalise(chunk.body)):
                self.assertNotIn("1111", field.replace("[card]", ""), repr(card))
                self.assertNotIn("0004", field, repr(card))
        # blanks mixed with hyphens stay a figure, as do runs of years and repeated groups
        for figure in ("4111\u00a01111-1111\u00a01111", "2024\t2025\t2026\t2027",
                       "9999  9999  9999  9999"):
            self.assertEqual(mask_cards(f"see {figure} here"), f"see {figure} here")

    def test_card_numbers_are_masked_in_every_chunker(self):
        self.assertEqual(normalise("paid by 4111 1111 1111 1111 exp 09/27"),
                         "paid by [card] exp 09/27")
        self.assertEqual(normalise("card 5500-0000-0000-0004 on file"), "card [card] on file")
        self.assertEqual(normalise("ref 4111111111111112 not a card"),
                         "ref 4111111111111112 not a card")   # fails the Luhn check
        self.assertEqual(normalise("PO 4500-1234-5678"), "PO 4500-1234-5678")
        self.assertEqual(normalise("GTIN 00012345600014"), "GTIN 00012345600014")  # Luhn, no range
        self.assertEqual(normalise("Amex 3782 822463 10005"), "Amex [card]")
        for text, want in (
                ("card 4512 1089 0356 8149 05/15", "card [card] 05/15"),
                ("4111111111111111 12/25", "[card] 12/25"),
                ("4111 1111 1111 1111 123 thanks", "[card] 123 thanks"),
                ("ref #12, 4111-1111-1111-1111 77", "ref #12, [card] 77"),
                ("(4111111111111111)", "([card])"),
                ("card:4111 1111 1111 1111.", "card:[card]."),
                ("visa 4111111111111111, mc 5500 0000 0000 0004;", "visa [card], mc [card];"),
                ("PO 4500-1234-5678", "PO 4500-1234-5678"),
                ("a,4111111111111111,b", "a,[card],b"), ("No.4111111111111111", "No.[card]"),
                ("4111111111111111.00", "4111111111111111.00"),
                ("17,4111111111111111,05/27", "17,[card],05/27"),        # a CSV row
                ("3 4111 1111 1111 1111 Jane", "3 [card] Jane"),          # a numbered row
                ("3 378282246310005 ok", "3 [card] ok"),                  # 15-digit Amex
                ("12 0012345678905 2.99", "12 0012345678905 2.99"),       # barcode after a row
                ("12 4222222222222 x", "12 4222222222222 x"),
                ("12 4111 1111 1111 1111 2222 3333", "12 4111 1111 1111 1111 2222 3333")):            # 13 digits: not here
            self.assertEqual(normalise(text), want, text)
        # real figures are never cards: only card layouts with one separator are read
        for figure in ("4111 1111-1111 1111", "4111-1111 4111-1111", "2019-2020 2021-2022",
                       "9999-9999 9999-9999", "9999 9999-9999 9999",
                       "999-999 999-999 999-999", "0012345678905 12.99 4.50",
                       "3.14159265358979323", "1234567890123.45", "4111.1111.1111.1111",
                       "2024 2025 2026 2027 2028"):
            self.assertEqual(normalise(f"see {figure} here"), f"see {figure} here", figure)
        from operating.chunking import make_chunk
        chunk = make_chunk(kind="mail", parts=("a",), source_handle="h", object_id=None,
                           title="Card 4111 1111 1111 1111", author_role=None, occurred_at=None,
                           header="Title: Card 4111111111111111 05/25", body="body text here",
                           linked_ids=(), audience_key="mail:x", markings=())
        self.assertEqual((chunk.title, chunk.header), ("Card [card]", "Title: Card [card] 05/25"))
        for name in ("RiskID", "DocuSignTemplateId"):
            self.assertTrue(classify(self.config, "AnyType", name).startswith("deny:"), name)

    def test_document_entries_name_real_text_properties_and_known_kinds(self):
        for type_id, doc in self.config["documents"].items():
            self.assertIn(type_id, self.types, type_id)
            self.assertIn(doc["kind"], AUTHORITY, type_id)
            props = {p["name"]: p for p in self.types[type_id].get("properties", [])}
            for name in [*doc.get("text", ()), *doc.get("fields_only", ())]:
                self.assertIn(name, props, f"{type_id}.{name}")
                self.assertIn(props[name]["value_type"], TEXT_TYPES, f"{type_id}.{name}")

    def test_named_coverage_is_read(self):
        plan = text_plan(self.config, self.types)
        for type_id, names in self.config["fields"].items():
            self.assertNotEqual(self.types[type_id]["availability"]["state"], "not_served")
            for name in names:
                self.assertIn(name, plan[type_id]["label"] + plan[type_id]["free"],
                              f"{type_id}.{name}")
        for type_id, name in (("Contact", "company_name"), ("SalesInvoiceLine", "description"),
                              ("Customer", "name"), ("Supplier", "name"), ("Product", "name")):
            self.assertIn(name, plan[type_id]["label"], f"{type_id}.{name}")
        self.assertEqual(plan["SharePointListItem"]["json"], ["fields"])
        self.assertIn("list_item", hd.SYSTEMS)


class RuleChunks(unittest.TestCase):
    """The rule at the chunker's output, over a fixture projection."""
    TYPES = {
        "WidgetOrder": {"id": "WidgetOrder", "availability": {"state": "served"},
                        "properties": [
                            {"name": "widget_remark", "value_type": "text"},
                            {"name": "name", "value_type": "text"},
                            {"name": "payroll_note", "value_type": "text",
                             "markings": ["finance", "pii"]},
                            {"name": "api_token", "value_type": "text"},
                            {"name": "status_code", "value_type": "text"},
                            {"name": "widget_id", "value_type": "text"}]},
        "SharePointListItem": {"id": "SharePointListItem", "availability": {"state": "served"},
                               "properties": [{"name": "fields", "value_type": "json"},
                                              {"name": "web_url", "value_type": "text"}]},
        "Hidden": {"id": "Hidden", "availability": {"state": "not_served"},
                   "properties": [{"name": "notes", "value_type": "text"}]},
    }

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "fx.sqlite"
        self.fx = Fixture(self.path)

    def run_chunker(self, **kw):
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        counts = Counts()
        chunker = RecordChunker(conn, exclusion(), counts, types=self.TYPES)
        return list(chunker.record_chunks(**kw)), counts

    def test_a_new_type_is_indexed_by_the_rule_and_keeps_its_markings(self):
        secret = "sk_" + "live_abcdefghijklmnopqrstuvwxyz"
        self.fx.obj("WidgetOrder:1", "WidgetOrder", "api:public.app_widget_order", values={
            "widget_remark": ("Customer wants the lavender bars wrapped separately", "[]"),
            "name": ("Acme", "[]"),
            "payroll_note": ("Bonus paid with the October payroll run for this order",
                             '["finance", "pii"]'),
            "api_token": (secret + " token value for the widget api", "[]"),
            "status_code": ("OK", "[]"),
            "widget_id": ("widget order identifier abcdefghij", "[]")})
        self.fx.obj("Hidden:1", "Hidden", "api:public.app_hidden",
                    values={"notes": ("A note on a type that is not served at all", "[]")})
        chunks, _ = self.run_chunker()
        by_field = {c.header.split("field ")[-1].split("\n")[0]: c for c in chunks}
        self.assertEqual(sorted(by_field), ["row.name", "row.payroll_note", "row.widget_remark"])
        self.assertEqual(by_field["row.payroll_note"].markings, ("finance", "pii"))
        self.assertEqual(by_field["row.widget_remark"].markings, ())
        self.assertIn("Acme", by_field["row.name"].body)
        self.assertFalse(any(secret in c.body or secret in c.header for c in chunks))
        self.assertEqual({c.kind for c in chunks}, {"portal_text"})

    def test_sage_gl_ar_ap_free_text_is_never_search_text_and_the_fields_block_is_read(self):
        from operating.chunking.records import load_config
        text = {"name": "x", "value_type": "text"}
        types = {
            "RelSageGlJec": {"availability": {"state": "served"}, "properties": [
                {**text, "name": "jecomment"}, {**text, "name": "batchid"}]},
            "RelSageGlJeDetail": {"availability": {"state": "served"}, "properties": [
                {**text, "name": n} for n in ("transdesc", "jnldtldesc", "jnldtlref",
                                              "namevend", "acctdesc")]},
            "RelSageApRpd": {"availability": {"state": "served"}, "properties": [
                {**text, "name": "comment"}]},
            "RelSageArCmm": {"availability": {"state": "served"}, "properties": [
                {**text, "name": "textcmnt1"}]},
            "WidgetOrder": {"availability": {"state": "served"}, "properties": [
                {"name": "widget_ref_text", "value_type": "identifier"},
                {"name": "widget_remark", "value_type": "text"}]}}
        pay = "Salary advance repaid by Jane Example for the October payroll"
        self.fx.obj("RelSageGlJec:1", "RelSageGlJec", "api:sage.gl_jec",
                    values={"jecomment": (pay, "[]"), "batchid": ("BATCH 42", "[]")})
        self.fx.obj("RelSageGlJeDetail:1", "RelSageGlJeDetail", "api:sage.gl_je_detail",
                    values={"transdesc": (pay + " transfer", "[]"),
                            "jnldtldesc": (pay + " detail", "[]"),
                            "jnldtlref": (pay + " reference", "[]"),
                            "namevend": ("Jane Example payroll services", "[]"),
                            "acctdesc": ("Wages and salaries expense account", "[]")})
        self.fx.obj("RelSageApRpd:1", "RelSageApRpd", "api:sage.ap_rpd",
                    values={"comment": (pay + " remitted", "[]")})
        self.fx.obj("RelSageArCmm:1", "RelSageArCmm", "api:sage.ar_cmm",
                    values={"textcmnt1": ("Customer asked to hold the lavender order", "[]")})
        self.fx.obj("WidgetOrder:9", "WidgetOrder", "api:public.app_widget_order",
                    values={"widget_ref_text": ("Lavender relaunch reference text", "[]"),
                            "widget_remark": ("Wrap the lavender bars separately", "[]")})
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        config = {**load_config(), "fields": {**load_config()["fields"],
                                              "WidgetOrder": ["widget_ref_text"],
                                              "RelSageGlJec": ["jecomment"]}}
        chunks = list(RecordChunker(conn, exclusion(), Counts(), config=config,
                                    types=types).record_chunks())
        text = "\n".join(c.title + c.header + c.body for c in chunks)
        # no reader at all (a Sage reader without finance and pii included) finds GL, AR or
        # AP pay text: it is never a chunk, never words, never an embedding
        for word in ("Salary advance", "Jane Example", "payroll"):
            self.assertNotIn(word, text)
        self.assertIn("Wages and salaries", text)                 # an account's name stays
        self.assertIn("hold the lavender order", text)            # AR customer comments stay
        self.assertIn("Lavender relaunch reference text", text)   # the fields block is read
        self.assertIn("Wrap the lavender bars", text)

    def test_codes_are_words_only_and_contact_details_never_reach_a_chunk(self):
        self.fx.obj("WidgetOrder:2", "WidgetOrder", "api:sage.widget", values={
            "widget_remark": ("AP-INV VEND01 REF ABCD-EFGH JNL-DTL", "[]"),
            "name": ("Maple Suds Inc", "[]"),
            "status_code": ("Please reach Sam at sam.lee@maple.example or 416-555-0199 today",
                            "[]")})
        chunks, _ = self.run_chunker()
        kinds = {c.header.split("field ")[-1].split("\n")[0]: c.kind for c in chunks}
        self.assertEqual(kinds, {"row.widget_remark": WORDS_KIND, "row.name": "record_text",
                                 "row.status_code": "record_text"})
        self.assertIn(WORDS_KIND, WORDS_ONLY_KINDS)

    def test_raw_relation_rows_are_words_only_however_wordy(self):
        types = {**self.TYPES, "RelSageArCmm": {
            "id": "RelSageArCmm", "availability": {"state": "served"},
            "properties": [{"name": "textcmnt1", "value_type": "text"}]}}
        self.fx.obj("RelSageArCmm:1", "RelSageArCmm", "api:sage.ar_cmm", values={
            "textcmnt1": ("Customer asked to hold every shipment until the credit note", "[]")})
        self.fx.obj("WidgetOrder:3", "WidgetOrder", "api:public.app_widget_order", values={
            "widget_remark": ("Customer asked to hold every shipment until the credit note",
                              "[]")})
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        chunks = list(RecordChunker(conn, exclusion(), Counts(), types=types).record_chunks())
        self.assertEqual({c.object_id: c.kind for c in chunks},
                         {"RelSageArCmm:1": WORDS_KIND, "WidgetOrder:3": "portal_text"})
        text = " ".join(c.body + c.header for c in chunks)
        self.assertNotIn("sam.lee", text)
        self.assertNotIn("555-0199", text)

    def test_list_items_get_their_kind_and_the_exclusion_and_document_rules_hold(self):
        site = "sharepoint-list:example.sharepoint.com,s1,w1/list-1"
        self.fx.obj("SharePointListItem:1", "SharePointListItem", site, values={
            "fields": (json.dumps({"Title": "Lavender soap relaunch", "Status": "Open",
                                   "Notes": "Waiting on the new carton artwork from design",
                                   "AssignedTo": {"LookupValue": "Name Hidden"},
                                   "SubmittedByEmail": "staff.one@example-plx.com",
                                   "PageURL": "https://example.sharepoint.com/page",
                                   "TaskID": "TASK-ABCDEFGHIJKLMNOPQRST",
                                   "Comment": "Ask staff.two@example-plx.com for the artwork",
                                   "Details": "Brief at https://example.sharepoint.com/brief "
                                              "card 4111 1111 1111 1111"}),
                        "[]"),
            "web_url": ("https://example.sharepoint.com/lists/list-1/1", "[]")})
        mine = f"sharepoint-list:example-my.sharepoint.com,personal,{EXCLUDED}/l2"
        self.fx.obj("SharePointListItem:2", "SharePointListItem", mine, values={
            "fields": (json.dumps({"Title": "Private list of an excluded person"}), "[]")})
        self.fx.obj("WidgetOrder:9", "WidgetOrder", "mail:12345678-aaaa-bbbb-cccc-00000000000a",
                    values={"widget_remark": ("Read out of a mail document attachment", "[]")})
        chunks, counts = self.run_chunker()
        self.assertEqual([c.object_id for c in chunks], ["SharePointListItem:1"])
        chunk = chunks[0]
        self.assertEqual((chunk.kind, chunk.authority), ("list_item", 3))
        self.assertIn("Title: Lavender soap relaunch", chunk.body)
        self.assertIn("Notes: Waiting on the new carton artwork", chunk.body)
        self.assertNotIn("Name Hidden", chunk.body)
        self.assertNotIn("https://", chunk.body)
        self.assertNotIn("example-plx.com", chunk.body)
        self.assertNotIn("TASK-", chunk.body)
        self.assertIn("Comment: Ask [email] for the artwork", chunk.body)
        self.assertIn("Details: Brief at [link] card [card]", chunk.body)
        self.assertEqual(counts.skipped.get("document_family"), 1)

    def test_every_record_lands_in_exactly_one_shard(self):
        for n in (1, 10, 60):
            for i in range(n):
                self.fx.obj(f"WidgetOrder:{n}-{i:03d}", "WidgetOrder", "api:sage.widget",
                            values={"widget_remark": (f"Remark number {i} of batch {n} here",
                                                      "[]")})
        self.fx.done()
        conn = open_ro(self.path)
        self.addCleanup(conn.close)
        whole = [c.chunk_id for c in RecordChunker(conn, exclusion(), Counts(),
                                                   types=self.TYPES).record_chunks()]
        self.assertEqual(len(whole), 71)
        sharded = []
        for i in range(4):
            sharded += [c.chunk_id for c in RecordChunker(
                conn, exclusion(), Counts(), types=self.TYPES).record_chunks(shard=(i, 4))]
        self.assertEqual(sorted(sharded), sorted(whole))


if __name__ == "__main__":
    unittest.main()
