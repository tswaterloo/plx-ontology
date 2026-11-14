"""A form filled from where its answers are (``operating.doc_gather`` form sources,
``operating.doc_fill_copies``): its own thread, the records its identifiers name exactly,
and the filler's earlier copies of the same form; a label matched by what it means;
ambiguous, one-party and unreadable sources fill nothing. Evidence: isolated-fixture."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from operating import doc_fill as df
from operating import doc_fill_copies as dc
from operating import doc_gather as dg
from operating import instruct_files as files

CONV = "AAQkConversation1"
FORM_MSG = "graph-mail://box/form@v1"
EARLIER = "graph-mail://box/earlier@v1"
LATER = "graph-mail://box/later@v1"
SHEET = "graph-mail://box/sheet"


def text(value):
    return {"untrusted_text": value}


class Mail:
    """The reader's tools over one thread: the form's message, an earlier message with a
    workbook attached and one sent after the form; ``refused`` handles fail as a refused
    read does."""

    def __init__(self, bodies=None, refused=(), finds=None, attached=None, sent=None,
                 people=None):
        # what each message's own read lists as attached: (citation, file name)
        self.attached = attached or {EARLIER: [(SHEET, "COC.xlsx"),
                                               ("graph-mail://box/logo", "logo.png")]}
        self.bodies = {EARLIER: "Hi, for this one:\nLot Number: B-1207\nPhone: 555-0100\n"
                                "Thanks",
                       LATER: "Lot Number: Z-9", FORM_MSG: "see attached"}
        self.bodies.update(bodies or {})
        self.refused = set(refused)
        self.finds = finds or {}
        self.calls = []
        self.sent = {FORM_MSG: "2026-05-02T10:00:00Z", EARLIER: "2026-05-01T09:00:00Z",
                     LATER: "2026-05-03T09:00:00Z"}
        self.sent.update(sent or {})
        # who each message came from and went to: the earlier one with a customer
        self.people = {EARLIER: ("buyer@maple.example", '["pat@x.test"]'),
                       FORM_MSG: ("pat@x.test", '["lee@x.test"]')}
        self.people.update(people or {})

    def __call__(self, name, args):
        self.calls.append((name, args.get("query") or args.get("citation")))
        if name == "plx.read":
            handle = args["citation"]
            if handle in self.refused:
                raise PermissionError("refused")
            if handle == SHEET:
                return {"status": "resolved", "content": text(
                    "[Sheet: COC]\nA1=Sample Code | B1=Lot # | C1=Tel\n"
                    "A2=SC-1 | B2=B-1207 | C2=555-0100")}
            sent = self.sent[handle]
            frm, to = self.people.get(handle, ("", ""))
            return {"status": "resolved", "content": text(self.bodies[handle]),
                    "fields": {"row.conversation_id": text(CONV),
                               **({"row.sent_at": text(sent)} if sent else {}),
                               **({"row.from": text(frm), "row.to": text(to)} if frm else {})},
                    "attachments": [{"citation": c, "name": text(n)}
                                    for c, n in self.attached.get(handle, ())]}
        if name == "plx.find":
            q = args["query"]
            if q in self.finds:
                return self.finds[q]
            if args.get("object_type") == "Email" and q == CONV:
                cands = [{"matched": {"raw": text(CONV)}, "object": {"display": {
                    "untrusted_text": "subject", "origin": {"citation": h}}}}
                    for h in (FORM_MSG, EARLIER, LATER)]
                # a near match is another conversation: never part of the thread
                cands.append({"matched": {"raw": text(CONV + "x")}, "object": {"display": {
                    "origin": {"citation": "graph-mail://box/other@v1"}}}})
                return {"candidates": cands}
            return {"candidates": []}
        raise AssertionError(name)


def fields(*labels):
    return [df.FormField(f"f{n}", label) for n, label in enumerate(labels)]


def by_label(planned):
    return {p.label: p for p in planned}


def found(object_id, object_type, ident):
    return {"candidates": [{"tier": "exact", "reason": "exact_identifier", "object": {
        "object_id": object_id, "object_type": object_type},
        "matched": {"raw": ident, "normalized": ident}}]}


class FormSourcesTest(unittest.TestCase):
    def setUp(self):
        self.terms = df.load_terms()

    def test_the_thread_answers_by_meaning_cited_before_the_form_only(self):
        mail = Mail()
        form = fields("Lot No.", "Tel", "Sample Code", "Favourite colour")
        facts, notes, records = dg.form_facts(
            mail, form, self.terms, message=FORM_MSG, before="2026-05-02T10:00:00Z",
            skip=[FORM_MSG])
        got = by_label(dg.plan_form(form, facts, self.terms))
        # "Lot No." reads the mail's "Lot Number:"; the message sent after the form ("Z-9")
        # and the form's own message are never read
        self.assertEqual((got["Lot No."].status, got["Lot No."].after), ("filled", "B-1207"))
        self.assertEqual(got["Lot No."].citation.source, EARLIER)
        self.assertIn("thread", got["Lot No."].reason)
        self.assertNotIn("Z-9", [f.value for f in facts])
        self.assertNotIn("graph-mail://box/other@v1", [c[1] for c in mail.calls])
        # a one-word label is the filler's own: another party's mail never answers it
        self.assertEqual(got["Tel"].status, "unknown_field")
        # a label the workbook writes but the terms file does not list is never taken from
        # mail (held-out: 2 of 4 such fills wrong); only an earlier copy of the form answers it
        self.assertEqual(got["Sample Code"].status, "unknown_field")
        self.assertEqual(got["Favourite colour"].status, "unknown_field")
        self.assertEqual(records, [])
        # another document attached in the thread (a workbook of its own lines) is never
        # read as an answer, only as a version of the same form
        mail = Mail(bodies={EARLIER: "see the sheet"})
        facts, _n, _r = dg.form_facts(
            mail, form, self.terms, message=FORM_MSG, before="2026-05-02T10:00:00Z",
            skip=[FORM_MSG], blob=lambda d: b"")
        self.assertEqual(by_label(dg.plan_form(form, facts, self.terms))["Lot No."].status,
                         "no_fact")
        self.assertFalse([f for f in facts if f.citation.source == SHEET])

    def test_mail_never_fills_a_line_level_field_or_a_company_name(self):
        mail = Mail(bodies={EARLIER: "Item #: A-77\nNet weight: 12 kg\nIncoterms: FCA\nCustomer: Maple Leaf Ltd"})
        form = fields("Item #", "Net weight", "Incoterms", "Customer")
        facts, _n, _r = dg.form_facts(mail, form, self.terms, message=FORM_MSG,
                                      before="2026-05-02T10:00:00Z", skip=[FORM_MSG])
        got = by_label(dg.plan_form(form, facts, self.terms))
        self.assertEqual(got["Incoterms"].after, "FCA")
        for line in ("Item #", "Net weight", "Customer"):
            self.assertNotEqual(got[line].status, "filled", line)
        # a terms file naming a field it does not list is refused
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.loads(Path(df.TERMS_FILE).read_text())
            raw["not_from_mail"] = ["no_such_field"]
            path = Path(tmp) / "terms.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(df.FillRefused):
                df.load_terms(path)

    def test_two_values_or_a_record_that_disagrees_leave_it_blank(self):
        mail = Mail(bodies={EARLIER: "Lot Number: B-1207\nLot Number: B-1300"})
        form = fields("Lot #")
        facts, _n, _r = dg.form_facts(mail, form, self.terms, message=FORM_MSG,
                                      before="2026-05-02T10:00:00Z", skip=[FORM_MSG])
        self.assertEqual(dg.plan_form(form, facts, self.terms)[0].status, "ambiguous")
        record = df.Fact("Lot.lot_number", "B-9999", df.Citation("Lot:1", "lot_number",
                                                                 "bc://lots/1@a"))
        thread = [f for f in facts if f.value == "B-1207"]
        planned = dg.plan_form(form, [record] + thread, self.terms)[0]
        self.assertEqual((planned.status, planned.after), ("ambiguous", ""))
        self.assertIn("disagree", planned.reason)

    def test_the_record_is_the_one_its_strongest_identifier_names_or_none(self):
        form = [df.FormField("po", "PO #", "4471"), df.FormField("f1", "Carrier")]
        named = dg.identifiers(form, "re: samples for lot L-77", [], self.terms)
        self.assertEqual([(n, i) for n, i, _f in named],
                         [("po_number", "4471"), ("lot_number", "L-77")])
        mail = Mail(finds={"4471": found("PurchaseOrder:bc:4471", "PurchaseOrder", "4471"),
                           "L-77": found("Lot:bc:L-77", "Lot", "L-77")})
        records, notes, key = dg.subject_records(mail, named, self.terms)
        self.assertEqual(sorted(records), ["Lot:bc:L-77", "PurchaseOrder:bc:4471"])
        self.assertEqual(key, "po")
        # one identifier naming two records, or two naming two of one type: none used
        two = {"candidates": found("PurchaseOrder:a", "PurchaseOrder", "4471")["candidates"]
               + found("PurchaseOrder:b", "PurchaseOrder", "4471")["candidates"]}
        records, notes, _k = dg.subject_records(Mail(finds={"4471": two}), named[:1],
                                                self.terms)
        self.assertEqual(records, [])
        self.assertTrue(any("more than one record" in n for n in notes))
        other = [("po_number", "4471", None), ("po_number", "5500", None)]
        records, notes, _k = dg.subject_records(Mail(finds={
            "4471": found("PurchaseOrder:a", "PurchaseOrder", "4471"),
            "5500": found("PurchaseOrder:b", "PurchaseOrder", "5500")}), other, self.terms)
        self.assertEqual(records, [])
        # a loose (lead) match is never the record
        lead = {"candidates": [{"tier": "lead", "reason": "text", "object": {
            "object_id": "PurchaseOrder:a", "object_type": "PurchaseOrder"},
            "matched": {"raw": "PO 4471 and more", "identifier_kind": "display_text",
                        "property": "notes"}}]}
        self.assertEqual(dg.subject_records(Mail(finds={"4471": lead}), named[:1],
                                            self.terms)[0], [])

    def test_one_po_held_in_two_systems_is_read_as_one_record_only_when_verified(self):
        named = [("po_number", "4471", "po")]
        two = {"candidates": found("PurchaseOrder:gold:1", "PurchaseOrder", "4471")["candidates"]
               + found("PurchaseOrder:fm:1", "PurchaseOrder", "4471")["candidates"]}

        def joined(tier, refused=False):
            class Around(Mail):
                def __call__(self, name, args):
                    if name != "plx.around":
                        return super().__call__(name, args)
                    self.calls.append((name, args["from"]))
                    if refused:
                        return {"error": {"code": "refused"}}
                    assert args["path"][0]["link_type"] == "PurchaseOrder.same_record_as"
                    return {"objects": [], "edges": [{"from": "PurchaseOrder:gold:1",
                                                      "to": "PurchaseOrder:fm:1",
                                                      "tier": tier}]}
            return Around(finds={"4471": two})
        records, notes, key = dg.subject_records(joined("verified"), named, self.terms)
        self.assertEqual(records, ["PurchaseOrder:fm:1", "PurchaseOrder:gold:1"])
        self.assertEqual((notes, key), ([], "po"))
        # a lead (the PO number text alone) or a link not read: two records, none used
        for mail in (joined("lead"), joined("verified", refused=True)):
            records, notes, _k = dg.subject_records(mail, named, self.terms)
            self.assertEqual(records, [], notes)
        # a third record the links do not reach: none used
        three = {"candidates": two["candidates"] + found(
            "PurchaseOrder:bc:9", "PurchaseOrder", "4471")["candidates"]}
        mail = joined("verified")
        mail.finds = {"4471": three}
        self.assertEqual(dg.subject_records(mail, named, self.terms)[0], [])
        # read as one, a value the two records disagree on is left blank
        facts = [df.Fact("PurchaseOrder.incoterms", "FCA", df.Citation("PurchaseOrder:gold:1",
                 "incoterms", "portal://po/1")),
                 df.Fact("PurchaseOrder.incoterms", "DAP", df.Citation("PurchaseOrder:fm:1",
                         "incoterms", "fm://po/1"))]
        self.assertEqual(dg.plan_form(fields("Incoterms"), facts, self.terms)[0].status,
                         "ambiguous")

    def test_earlier_copies_answer_only_what_they_agree_on_and_only_if_opened(self):
        def sheet(phone, lot):
            return files.make("xlsx", f"Chain of custody\nTel:|{phone}\nLot #:|{lot}\n"
                                      "Date of order:|2026-01-05\n"
                                      "Remarks:|Blended and packed at our own plant under "
                                      "the usual quality plan\n")
        copies = [("graph-mail://box/c4", sheet("555-0100", "L-4"), "a.test", "2026-05-01"),
                  ("graph-mail://box/c3", sheet("555-0100", "L-3"), "b.test", "2026-04-01"),
                  ("graph-mail://box/c2", sheet("555-0100", "L-2"), "a.test", "2026-03-01"),
                  ("graph-mail://box/c1", sheet("555-0199", "L-1"), "c.test", "2026-02-01")]
        form = df.open_document(sheet("", ""), self.terms).fields()
        blank = [df.FormField(f.field_id, f.label, "") for f in form]
        got = by_label(dg.plan_form(blank, dc.copy_facts(copies[:3], self.terms),
                                    self.terms))
        # the same in the three newest copies: the filler's own; changing per copy, or a
        # date however often it repeats: that copy's own, nothing
        self.assertEqual((got["Tel"].status, got["Tel"].after,
                          got["Tel"].citation.source),
                         ("filled", "555-0100", "graph-mail://box/c4"))
        self.assertIn("earlier copies", got["Tel"].reason)
        self.assertNotEqual(got["Lot #"].status, "filled")
        self.assertNotEqual(got["Date of order"].status, "filled")
        # a line of prose the same in every copy is reworded form by form: nothing
        self.assertNotEqual(got["Remarks"].status, "filled")
        # one copy among those read that differs, or fewer than three: nothing
        for some in (copies, copies[:2]):
            got = by_label(dg.plan_form(blank, dc.copy_facts(some, self.terms), self.terms))
            self.assertNotEqual(got["Tel"].status, "filled")
        # the same in three copies that all went to one customer (their consignee, their
        # account) is theirs, not the filler's own; a copy whose counterparty is unknown
        # does not count toward two
        for parties in (("a.test",) * 3, ("a.test", "a.test", ""), ("", "", "")):
            one = [(h, b, p, w) for (h, b, _x, w), p in zip(copies[:3], parties)]
            got = by_label(dg.plan_form(blank, dc.copy_facts(one, self.terms), self.terms))
            self.assertNotEqual(got["Tel"].status, "filled", parties)
        # unless the form is for that customer (its email came from them) and the copies
        # span a month or more: copies within days are one job's
        one = [(h, b, "a.test", w) for h, b, _x, w in copies[:3]]
        for target, filled in (("a.test", True), ("b.test", False), ("", False)):
            got = by_label(dg.plan_form(blank, dc.copy_facts(one, self.terms, target),
                                        self.terms))
            self.assertEqual(got["Tel"].status == "filled", filled, target)
        batch = [(h, b, "a.test", f"2026-05-0{n}") for n, (h, b, _x, _w) in
                 enumerate(copies[:3], 1)]
        got = by_label(dg.plan_form(blank, dc.copy_facts(batch, self.terms, "a.test"),
                                    self.terms))
        self.assertNotEqual(got["Tel"].status, "filled")
        # the reader's own read decides: a copy refused, or changed since, is not used
        rows = [{"source": h, "digest": f"d{n}", "party": p, "sent": w}
                for n, (h, _b, p, w) in enumerate(copies)]
        data = {f"d{n}": b for n, (_h, b, _p, _w) in enumerate(copies)}

        def call(name, args):
            if args["citation"] == "graph-mail://box/c4":
                raise PermissionError("refused")
            changed = "x" if args["citation"] == "graph-mail://box/c1" else None
            return {"status": "resolved", "fields": {"row.content_sha256": text(
                changed or rows[[r["source"] for r in rows].index(args["citation"])]
                ["digest"])}}
        self.assertEqual([(h, p) for h, _b, p, _w in dc.opened(call, rows, data.__getitem__)],
                         [("graph-mail://box/c3", "b.test"), ("graph-mail://box/c2", "a.test")])

    def test_the_forms_earlier_versions_in_its_thread_answer_what_two_agree_on(self):
        def sheet(account, ref, sample, extra=""):
            return files.make("xlsx", f"Sample request\nAccount:|{account}\n"
                                      f"Reference:|{ref}\nSample:|{sample}\n"
                                      f"Requested on:|2026-04-0{len(ref)}\n{extra}")
        other = files.make("xlsx", "Freight quote\nCarrier:|Northline\nPieces:|4\n"
                                   "Weight:|80\nAccount:|A-1\n")
        blobs = {"d1": sheet("A-1", "R-1", "S-1"), "d2": sheet("A-1", "R-2", "S-1"),
                 "d3": sheet("A-1", "R-3", "S-9"), "dx": other}
        atts = {EARLIER: [("graph-mail://box/v1", "d1"), ("graph-mail://box/v2", "d2"),
                          ("graph-mail://box/v1b", "d1"), ("graph-mail://box/x", "dx")],
                LATER: [("graph-mail://box/v3", "d3")]}
        cites = {c: d for got in atts.values() for c, d in got}

        class Thread(Mail):
            def __call__(self, name, args):
                q = args.get("query") or args.get("citation")
                if name == "plx.read" and q in cites:
                    self.calls.append((name, q))
                    if q in self.refused:
                        raise PermissionError("refused")
                    return {"status": "resolved", "content": text("(form)"),
                            "fields": {"row.content_sha256": text(cites[q])}}
                return super().__call__(name, args)
        blank = [df.FormField(f.field_id, f.label, "") for f in
                 df.open_document(sheet("", "", ""), self.terms).fields()]
        listed = {h: [(c, "Sample request.xlsx") for c, _d in got] for h, got in atts.items()}

        def fill(mail, blob=blobs.__getitem__, before="2026-05-02T10:00:00Z", own="pat@x.test"):
            mail.attached = listed
            facts, _n, _r = dg.form_facts(mail, blank, self.terms, message=FORM_MSG,
                                          before=before, skip=[FORM_MSG], blob=blob, own=own)
            return by_label(dg.plan_form(blank, facts, self.terms))
        mail = Thread()
        got = fill(mail)
        # two versions sent before the form agree on the account: taken, cited to one of
        # them; a value they differ on, or a date, is that version's own; the same file
        # attached twice is one version; another form in the thread is not a version
        self.assertEqual((got["Account"].status, got["Account"].after), ("suggested", "A-1"))
        self.assertIn(got["Account"].citation.source, ("graph-mail://box/v1",
                                                       "graph-mail://box/v2"))
        self.assertIn("earlier versions", got["Account"].reason)
        self.assertIn("check", got["Account"].reason)
        # a suggested value is shown, never written: the filled copy holds none of them
        made = df.fill(sheet("", "", ""), dg.form_facts(
            Thread(attached=listed), blank, self.terms, message=FORM_MSG,
            before="2026-05-02T10:00:00Z", skip=[FORM_MSG], blob=blobs.__getitem__,
            own="pat@x.test")[0], self.terms, planner=dg.plan_form)
        self.assertEqual(made.counts()["suggested"], 2)
        self.assertEqual((made.counts()["filled"], made.data), (0, None))
        self.assertNotIn(got["Reference"].status, ("filled", "suggested"))
        self.assertNotIn(got["Requested on"].status, ("filled", "suggested"))
        self.assertEqual((got["Sample"].status, got["Sample"].after), ("suggested", "S-1"))
        # a version written after the form (the third, sample S-9) is never read
        self.assertNotIn("graph-mail://box/v3", [c[1] for c in mail.calls])
        self.assertIn("graph-mail://box/v2", [c[1] for c in mail.calls])
        # one version alone (the other refused to this reader, or the same file twice)
        # fills nothing; neither does a store that cannot give the bytes
        for mail, blob in ((Thread(refused={"graph-mail://box/v2"}), blobs.__getitem__),
                           (Thread(), lambda d: (_ for _ in ()).throw(FileNotFoundError(d))),
                           (Thread(), None)):
            got = fill(mail, blob)
            self.assertNotIn(got["Account"].status, ("filled", "suggested"))
            self.assertNotIn(got["Sample"].status, ("filled", "suggested"))
        # versions passed only among the reader's colleagues are drafts: nothing taken.
        # Neither when the reader's address is unknown or odd, nor when the message names
        # no address or only unreadable ones (fails closed)
        for people, own in (({EARLIER: ("lee@X.test", '["pat@x.test", "kim@mail.x.test"]')},
                             "pat@x.test"),
                            ({}, ""), ({}, "not-an-address"), ({}, "a@b@x.test"),
                            ({EARLIER: ("", "")}, "pat@x.test"),
                            ({EARLIER: ("someone", '["nobody", "@", "x@"]')}, "pat@x.test")):
            got = fill(Thread(people=people), own=own)
            self.assertNotIn(got["Account"].status, ("filled", "suggested"), (people, own))
            self.assertNotIn(got["Sample"].status, ("filled", "suggested"), (people, own))
        # one outside recipient on a colleague's message is enough
        got = fill(Thread(people={EARLIER: ("lee@x.test", '["kim@x.test", "ap@maple.example"]')}))
        self.assertEqual((got["Account"].status, got["Account"].after), ("suggested", "A-1"))
        # two versions disagreeing (the third is read when the form is sent later)
        got = fill(Thread(), before="2026-05-03T12:00:00Z")
        self.assertEqual(got["Account"].after, "A-1")
        self.assertEqual(got["Sample"].status, "ambiguous")
        # a form whose time is unknown (no measure's, its own message gives none), or
        # versions whose own message gives no time: no version is read
        for mail, before in ((Thread(sent={EARLIER: None}), "2026-05-02T10:00:00Z"),
                             (Thread(sent={FORM_MSG: None}), None)):
            got = fill(mail, before=before)
            self.assertNotIn(got["Account"].status, ("filled", "suggested"))
            self.assertNotIn(got["Sample"].status, ("filled", "suggested"))
            self.assertNotIn("graph-mail://box/v1", [c[1] for c in mail.calls])
        # the form's own time when no measure gives one is its message's: a version sent
        # after the form (v3) is never read, the two before it still answer
        mail = Thread()
        got = fill(mail, before=None)
        self.assertEqual((got["Account"].status, got["Sample"].after), ("suggested", "S-1"))
        self.assertNotIn("graph-mail://box/v3", [c[1] for c in mail.calls])
        # versions agreeing on a lot the thread's mail states otherwise: neither is taken
        lots = {d: sheet("A-1", f"R-{n}", "S-1", "Lot #:|L-5\n")
                for n, d in enumerate(("d1", "d2", "d3"), 1)}
        lot_form = [df.FormField(f.field_id, f.label, "") for f in
                    df.open_document(lots["d1"], self.terms).fields()]
        for body, want in (("Lot Number: B-1207", ("ambiguous", "")),
                           ("see the sheets", ("suggested", "L-5"))):
            mail = Thread(bodies={EARLIER: body})
            mail.attached = listed
            facts, _n, _r = dg.form_facts(mail, lot_form, self.terms, message=FORM_MSG,
                                          before="2026-05-02T10:00:00Z", skip=[FORM_MSG],
                                          blob=lots.__getitem__, own="pat@x.test")
            got = by_label(dg.plan_form(lot_form, facts, self.terms))
            self.assertEqual((got["Lot #"].status, got["Lot #"].after), want, body)

    def test_the_party_a_form_came_from_is_read_as_the_reader(self):
        def call(name, args):
            if args["citation"] == "graph-mail://box/in":
                return {"status": "resolved", "fields": {
                    "row.from": text("Dana Reyes <dana@Maple.example>")}}
            if args["citation"] == "graph-mail://box/own":
                return {"status": "resolved", "fields": {"row.from": text("pat@x.test")}}
            raise PermissionError("refused")
        for message, party in (("graph-mail://box/in", "maple.example"),
                               ("graph-mail://box/own", ""), ("graph-mail://box/no", ""),
                               (None, "")):
            self.assertEqual(dg.sender_party(call, message, "pat@x.test"), party, message)

    def test_the_copies_index_and_its_matching_fail_closed(self):
        index = {"pat@x.test": [
            {"source": "s1", "sent": "2026-04-01", "digest": "d1", "labels": ["lot #", "tel"]},
            {"source": "s2", "sent": "2026-05-01", "digest": "d2", "labels": ["lot #", "tel"]},
            {"source": "s3", "sent": "2026-05-09", "digest": "d3", "labels": ["lot #", "tel"]},
            {"source": "s4", "sent": "2026-04-02", "digest": "d4",
             "labels": ["carrier", "tel", "weight", "pieces"]}]}
        form = fields("Lot #", "Tel")
        got = dc.earlier(index, "PAT@x.test", form, before="2026-05-02", not_source="s2")
        self.assertEqual([r["source"] for r in got], ["s1"])
        self.assertEqual(dc.earlier(index, "lee@x.test", form), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "copies.json"
            self.assertEqual(dc.load(path), {})
            path.write_text(json.dumps({"schema": "plx-doc-fill-copies/v0", "authors": {}}))
            with self.assertRaises(df.FillRefused) as bad:
                dc.load(path)
            self.assertEqual(bad.exception.code, "copies_invalid")
            dc.save(path, index, "2026-05-10T00:00:00Z")
            self.assertEqual(dc.load(path)["pat@x.test"][0]["source"], "s1")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_an_unreadable_thread_or_odd_answers_fill_nothing(self):
        form = fields("Lot #")
        for mail in (Mail(refused={FORM_MSG}), Mail(finds={CONV: {"candidates": "x"}}),
                     Mail(finds={CONV: None})):
            facts, notes, _r = dg.form_facts(mail, form, self.terms, message=FORM_MSG,
                                             skip=[FORM_MSG])
            self.assertFalse(facts)
            self.assertEqual(dg.plan_form(form, facts, self.terms)[0].status, "no_fact")
        self.assertEqual(dg.said("Lot #:\n| --- |\nA1= | B1=", "h",
                                 {dg.meaning("Lot #", self.terms)}, self.terms, 1), [])

    def test_the_time_limit_stops_the_reads_and_labels_the_result(self):
        clock = [0.0]
        mail = Mail()

        def slow(name, args):
            clock[0] += 10
            return mail(name, args)
        with mock.patch.object(dg.time, "monotonic", lambda: clock[0]):
            facts, notes, _r = dg.form_facts(slow, fields("Lot #"), self.terms,
                                             message=FORM_MSG, deadline=25.0)
        self.assertEqual(notes[0], dg.PARTIAL)
        self.assertLessEqual(len(mail.calls), 3)

    def test_one_ten_and_sixty_fields_plan_once_each_within_budget(self):
        for n in (1, 10, 60):
            form = [df.FormField(f"f{i}", "Lot No.") for i in range(n)]
            body = "Lot Number: S-7"
            facts = dg.said(body, EARLIER, {dg.meaning(f.label, self.terms) for f in form},
                            self.terms, dg.THREAD)
            started = time.monotonic()
            planned = dg.plan_form(form, facts, self.terms)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual([p.after for p in planned], ["S-7"] * n)


if __name__ == "__main__":
    unittest.main()
