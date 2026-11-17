"""The read-side guard on PLX's own sales documents held as supplier documents
(``operating.pg.own_issued``): which documents it hides, the table it reads, and that every
read path (record pages, ``plx.object``/``around``/``documents``/``investigate``, the
autopilot event feed, the confirmed-date drafts, suggestions) withholds them for every
reader while the supplier's own documents beside them stay.

Evidence label: isolated-fixture (synthetic text and rows, never live PLX data).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

from operating.pg import own_issued
from operating.search import document_marks

ROOT = Path(__file__).resolve().parents[2]
SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
# Synthetic Sage OE forms (names, numbers and amounts invented).
OWN_INVOICE = ("Invoice\nInvoice Number IN1000000001\nSold To: Example Retail Co\n"
               "PB#INFO [OEINV],[],[IN1000000001],[1,234.50],[EXRE01],[ ],[ap@example.com]\n"
               "Petra Hygienic Systems International Limited\nHST 000000000\n")
OWN_CONFIRMATION = ("Order Confirmation\nOrder Number OR1000000002\nSold To: Example Spa\n"
                    "PB#INFO [OECONF], [],[OR1000000002],[99.00],[Example Spa],[],[]\n")
SUPPLIER_INVOICE = ("Acme Chemicals Inc.\nInvoice\nInvoice No: 4711\nBill To:\n"
                    "Petra Hygienic Systems\n90 Example Road\nTotal 812.00 CAD\n")
RETURNED_PO = ("Invoice\nInvoice No: 99\nYour PO attached\n"
               "PB#INFO [PETPO],[],[PO100001],[10.00],[ACME01]\n")


class DecisionTests(unittest.TestCase):
    def test_a_sage_sales_form_on_every_title_page_is_plx_issued(self):
        got = own_issued.own_issued("SupplierInvoice", [(1, OWN_INVOICE)])
        self.assertEqual(got, {"rule": "own_record_line", "forms": ["OEINV"],
                               "cue_pages": [1]})
        got = own_issued.own_issued("SupplierConfirmation", [(1, OWN_CONFIRMATION)])
        self.assertEqual(got["forms"], ["OECONF"])
        # several pages, each with its record line (a multi-page invoice)
        self.assertIsNotNone(own_issued.own_issued(
            "SupplierInvoice", [(1, OWN_INVOICE), (2, OWN_INVOICE)]))

    def test_with_no_title_cue_every_page_must_carry_the_record_line(self):
        """A heading this code does not read as the class ('Order -Confirmation' as Sage
        prints it, which the store's older reading took for a supplier confirmation): the
        document is PLX's own when every page with text carries a record line."""
        heading = "Order -Confirmation\nOrder Number OR1000000002\n"
        pages = [(1, heading + "PB#INFO [OECONF],[],[OR1000000002],[99.00]\n"),
                 (2, "PB#INFO [OECONF],[],[OR1000000002],[99.00]\nTerms\n"), (3, "")]
        for object_type in ("SupplierConfirmation", "SupplierInvoice"):
            with self.subTest(object_type):
                self.assertEqual(own_issued.own_issued(object_type, pages),
                                 {"rule": "own_record_line", "forms": ["OECONF"],
                                  "cue_pages": [], "every_page": True})

    def test_a_supplier_document_is_never_plx_issued(self):
        cases = {
            # PLX in the Bill To block: the recipient, never the issuer
            "plx_billed": [(1, SUPPLIER_INVOICE)],
            # a supplier returning PLX's own purchase order (PETPO marks nothing)
            "petpo": [(1, RETURNED_PO)],
            # a supplier's invoice page sent with one of PLX's own confirmation pages
            "mixed": [(1, SUPPLIER_INVOICE), (2, OWN_CONFIRMATION + "\nInvoice\n")],
            # the marker inside another word, or no bracketed form after it
            "glued": [(1, "Invoice\nXPB#INFO [OEINV],[],[IN1]\n")],
            "bare": [(1, "Invoice\nPB#INFO OEINV IN1\n")],
            "empty_form": [(1, "Invoice\nPB#INFO [],[OEINV]\n")],
            # no title cue this code reads, and a page without the record line: the
            # page may be the supplier's (a carrier's bill of lading in our export pack)
            "no_cue_mixed": [(1, "PB#INFO [OECONF],[],[OR1]\nOrder -Confirmation\n"),
                             (2, "Straight Bill of Lading\nCarrier: Example Freight\n")],
            "no_cue_petpo": [(1, "PB#INFO [PETPO],[],[PO1]\nOrder -Confirmation\n")],
            "no_pages": [],
            "blank_pages": [(1, ""), (2, "  \n")],
        }
        for name, pages in cases.items():
            with self.subTest(name):
                self.assertIsNone(own_issued.own_issued("SupplierInvoice", pages))

    def test_an_unknown_type_is_never_decided(self):
        for object_type in ("PurchaseOrder", "SalesInvoice", "supplierinvoice", "", None):
            with self.subTest(object_type):
                self.assertIsNone(own_issued.own_issued(object_type, [(1, OWN_INVOICE)]))

    def test_the_document_digest_comes_from_its_handle_only(self):
        self.assertEqual(own_issued.handle_sha(
            f"graph-mail://m/x@y#attachment=z&sha256={SHA_A}"), SHA_A)
        for handle in (None, 5, "graph-mail://m/x", f"x?sha256={SHA_A}0", "sha256=" + SHA_A):
            with self.subTest(handle):
                self.assertIsNone(own_issued.handle_sha(handle))


class ReadSideTests(unittest.TestCase):
    """``document_marks`` withholds the hidden ids from every reader, the owner too."""

    def setUp(self):
        document_marks.forget()
        self.addCleanup(document_marks.forget)

    def test_every_reader_is_kept_from_the_hidden_ids(self):
        from ontology.store_ddl import MARKINGS
        hidden = frozenset({"SupplierInvoice:b:1", "SupplierConfirmation:b:2"})
        with mock.patch.object(own_issued, "hidden_ids", return_value=hidden):
            for held in ((), sorted(MARKINGS)):   # a plain reader, the owner
                with self.subTest(held=held):
                    params = document_marks.params(held)
                    self.assertEqual(hidden, set(json.loads(params["dm_own"])))
                    self.assertEqual(document_marks.withheld(
                        ["SupplierInvoice:b:1", "SupplierInvoice:b:3"], held),
                        {"SupplierInvoice:b:1"})
            # one text for one set: a Postgres session keeps a long list once
            self.assertIs(document_marks.params(())["dm_own"],
                          document_marks.params(sorted(MARKINGS))["dm_own"])
        with mock.patch.object(own_issued, "hidden_ids", side_effect=RuntimeError("down")):
            self.assertEqual(document_marks.params(sorted(MARKINGS))["dm_own"], "[]")

    def test_nothing_is_hidden_unless_postgres_serves(self):
        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": ""}), \
                mock.patch.object(own_issued._HIDDEN, "_read",
                                  side_effect=AssertionError("no read")):
            self.assertEqual(own_issued.hidden_ids(), frozenset())

    def test_suggestions_drop_the_hidden_ids(self):
        from operator_api.backend_serving import own_issued_kept
        rows = [(1, "SupplierInvoice:b:1", "x"), (1, "Supplier:s:1", "y")]
        with mock.patch.object(own_issued, "hidden_ids",
                               return_value=frozenset({"SupplierInvoice:b:1"})):
            self.assertEqual(own_issued_kept(rows), [rows[1]])


class TableTests(unittest.TestCase):
    """The migration's table, ``apply`` and the reader's load, on a throwaway database."""

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_testdb
        cls.pg_testdb = pg_testdb
        cls.db, cls.dsn = pg_testdb.create_db()
        import psycopg
        cls.conn = psycopg.connect(cls.dsn, autocommit=True)
        cls.conn.execute(
            "CREATE SCHEMA ontology; CREATE TABLE ontology.objects (object_id text PRIMARY KEY,"
            " object_type text NOT NULL); CREATE TABLE ontology.object_values (object_id text,"
            " property text, l1_handle text, superseded_at text)")
        cls.conn.execute((ROOT / "operating/pg/migrations/0038_own_issued_documents.sql"
                          ).read_text(encoding="utf-8"))
        rows = [("SupplierInvoice:m:1", "SupplierInvoice", SHA_A),
                ("SupplierInvoice:m:2", "SupplierInvoice", SHA_A),   # a second mailing
                ("SupplierConfirmation:m:3", "SupplierConfirmation", SHA_A),
                ("SupplierInvoice:m:4", "SupplierInvoice", SHA_B)]
        for object_id, object_type, sha in rows:
            cls.conn.execute("INSERT INTO ontology.objects VALUES (%s, %s)",
                             (object_id, object_type))
            cls.conn.execute("INSERT INTO ontology.object_values VALUES (%s, 'evidence', %s, "
                             "NULL)", (object_id, f"graph-mail://x#attachment=y&sha256={sha}"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.pg_testdb.drop_db(cls.db)

    def setUp(self):
        own_issued.forget()
        self.addCleanup(own_issued.forget)

    def decision(self, sha, out):
        return json.dumps([sha, out, None])

    def test_apply_hides_each_copy_of_a_decided_document_of_its_type_only(self):
        evidence = {"rule": "own_record_line", "forms": ["OEINV"], "cue_pages": [1]}
        got = own_issued.apply(self.conn, [
            self.decision(SHA_A, {"SupplierInvoice": evidence}),
            self.decision(SHA_B, None), self.decision(SHA_C, {"SupplierInvoice": evidence})])
        self.assertEqual(got["hidden_objects"], 2)
        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only", "PLX_PG_DSN": self.dsn}):
            self.assertEqual(own_issued.hidden_ids(),
                             {"SupplierInvoice:m:1", "SupplierInvoice:m:2"})
        self.assertEqual(own_issued.verify(self.conn),
                         {"rows": 2, "live_supplier_objects": 2})

    def test_a_malformed_decision_writes_nothing(self):
        evidence = {"rule": "own_record_line", "forms": ["OEINV"], "cue_pages": [1]}
        own_issued.apply(self.conn, [self.decision(SHA_A, {"SupplierInvoice": evidence})])
        for bad in (self.decision(SHA_A, {"PurchaseOrder": evidence}),
                    self.decision(SHA_A, {"SupplierInvoice": {"rule": "letterhead"}}),
                    self.decision("A" * 64, None), json.dumps({"sha": SHA_A}), "[1, 2, 3]"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    own_issued.apply(self.conn, [bad])
                self.assertEqual(self.conn.execute(
                    f"SELECT count(*) FROM {own_issued.TABLE}").fetchone()[0], 2)

    def test_refresh_judges_only_new_documents_and_hides_a_new_copy_of_an_own_one(self):
        """The 15-minute pass: the first run judges every document (a full build); a
        later one judges only a digest no pass judged (the delta's new document), and a
        new object of a document already judged PLX's own is hidden without a re-read."""
        evidence = {"rule": "own_record_line", "forms": ["OECONF"], "cue_pages": [0]}
        asked: list[list[str]] = []

        def judged(tasks, _workers, _decisions=None):
            asked.append([sha for sha, _types in tasks])
            for _sha, types in tasks:   # every type judged, whichever the store holds now
                self.assertEqual(types, tuple(sorted(own_issued.TYPES)))
            return ({sha: ({"SupplierInvoice": evidence, "SupplierConfirmation": evidence}
                           if sha == SHA_A else {}) for sha, _ in tasks}, {})
        self.conn.execute(f"TRUNCATE {own_issued.TABLE}, {own_issued.JUDGED}")
        added = []
        self.addCleanup(lambda: [self.conn.execute(
            f"DELETE FROM ontology.{t} WHERE object_id = ANY(%s)", (added,))
            for t in ("objects", "object_values")])
        with mock.patch.object(own_issued, "_judge_all", side_effect=judged):
            first = own_issued.refresh(self.conn, workers=2)
            self.assertEqual(first["mode"], "build")
            self.assertEqual(sorted(asked[0]), sorted({SHA_A, SHA_B}))
            hidden = {r[0] for r in self.conn.execute(f"SELECT object_id FROM {own_issued.TABLE}")}
            self.assertEqual(hidden, {"SupplierInvoice:m:1", "SupplierInvoice:m:2",
                                      "SupplierConfirmation:m:3"})
            # nothing new: nothing judged, nothing changes
            again = own_issued.refresh(self.conn, workers=2)
            self.assertEqual((again["mode"], asked[-1]), ("refresh", []))
            # the delta adds a supplier document (SHA_C) and another copy of SHA_A
            for object_id, sha in (("SupplierInvoice:m:5", SHA_C),
                                   ("SupplierConfirmation:m:6", SHA_A)):
                added.append(object_id)
                self.conn.execute("INSERT INTO ontology.objects VALUES (%s, %s)",
                                  (object_id, object_id.split(":")[0]))
                self.conn.execute("INSERT INTO ontology.object_values VALUES (%s, 'evidence', "
                                  "%s, NULL)", (object_id, f"sp://f?sha256={sha}"))
            own_issued.refresh(self.conn, workers=2)
            self.assertEqual(asked[-1], [SHA_C])
        hidden = {r[0] for r in self.conn.execute(f"SELECT object_id FROM {own_issued.TABLE}")}
        self.assertEqual(hidden, {"SupplierInvoice:m:1", "SupplierInvoice:m:2",
                                  "SupplierConfirmation:m:3", "SupplierConfirmation:m:6"})
        self.assertEqual(dict(self.conn.execute(
            f"SELECT content_sha256, own FROM {own_issued.JUDGED}").fetchall()),
            {SHA_A: True, SHA_B: False, SHA_C: False})

    def test_a_judgement_of_another_shape_is_judged_again(self):
        """A stored own judgement whose evidence is not the rule's shape decides nothing:
        the next pass reads that document again (fail closed toward judging)."""
        self.conn.execute(f"TRUNCATE {own_issued.TABLE}, {own_issued.JUDGED}")
        for sha, own, evidence in ((SHA_A, True, '{"SupplierInvoice": {"rule": "letterhead"}}'),
                                   (SHA_B, False, None)):
            self.conn.execute(f"INSERT INTO {own_issued.JUDGED} VALUES (%s, %s, %s, 'x')",
                              (sha, own, evidence))
        asked = []
        with mock.patch.object(own_issued, "_judge_all", side_effect=lambda t, *_a: (
                asked.extend(s for s, _ in t) or ({s: {} for s, _ in t}, {}))):
            own_issued.refresh(self.conn, workers=1)
        self.assertEqual(asked, [SHA_A])
        self.assertEqual(self.conn.execute(
            f"SELECT count(*) FROM {own_issued.TABLE}").fetchone()[0], 0)

    def test_a_missing_table_or_server_hides_nothing_and_a_read_set_stays(self):
        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only",
                                          "PLX_PG_DSN": "postgresql:///plx_no_such_db_o"}):
            self.assertEqual(own_issued.hidden_ids(), frozenset())
        own_issued.forget()
        evidence = {"rule": "own_record_line", "forms": ["OEINV"], "cue_pages": [1]}
        own_issued.apply(self.conn, [self.decision(SHA_B, {"SupplierInvoice": evidence})])
        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only", "PLX_PG_DSN": self.dsn}):
            self.assertEqual(own_issued.hidden_ids(), {"SupplierInvoice:m:4"})
            # a later refresh that fails keeps the set it read
            with mock.patch.object(own_issued._HIDDEN, "_read", side_effect=OSError("down")):
                self.assertIsNone(own_issued._HIDDEN._load())
            self.assertEqual(own_issued.hidden_ids(), {"SupplierInvoice:m:4"})


class AutopilotFeedTests(unittest.TestCase):
    def test_a_hidden_supplier_invoice_raises_no_ap_event_and_its_neighbour_does(self):
        from operating import autopilot_engine as autopilot
        typed = {"SupplierInvoice:b:1": ("SupplierInvoice", "mail:x"),
                 "SupplierInvoice:b:2": ("SupplierInvoice", "mail:x")}
        seen = []

        def record_events(_st, otype, _kind, oids, *_a, **_k):
            seen.extend(oids)
            return []
        registry = {"sources": {"SupplierInvoice": {"event": "document.received"}}}
        with mock.patch.object(autopilot, "_types", return_value=typed), \
                mock.patch.object(autopilot, "_people_maps", return_value=({}, {}, {})), \
                mock.patch.object(autopilot, "_record_events", side_effect=record_events), \
                mock.patch.object(autopilot, "approvals_cleared", side_effect=lambda *a: a[1]), \
                mock.patch.object(own_issued, "hidden_ids",
                                  return_value=frozenset({"SupplierInvoice:b:1"})):
            autopilot.discover(object(), list(typed), registry, people=None)
        self.assertEqual(seen, ["SupplierInvoice:b:2"])


class ProactiveStoreTests(unittest.TestCase):
    """The proactive engine's store (late orders, PO status, the alert rules, AP drafts):
    a PLX-issued confirmation counts as a removed record, so no rule reads its link or its
    promised date; the supplier's own confirmation beside it stays."""

    def test_a_hidden_confirmation_is_a_removed_record_for_every_rule(self):
        import tempfile
        from operating import proactive_engine as pe
        from operating.tests.test_proactive_engine import World
        w = World()
        po = w.obj("PurchaseOrder", {"po_number": "PO100001"})
        ours = w.obj("SupplierConfirmation", {"delivery_date": "2026-10-01"})
        theirs = w.obj("SupplierConfirmation", {"delivery_date": "2026-10-04"})
        bill = w.obj("SupplierInvoice", {"invoice_number": "4711"})
        for c in (ours, theirs):
            w.link("SupplierConfirmation.confirms", c, po)
        w.link("SupplierInvoice.for_purchase_order", bill, po)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = w.build(Path(tmp.name) / "store.sqlite")
        for hidden in (frozenset(), frozenset({ours})):
            with self.subTest(hidden=sorted(hidden)), \
                    mock.patch.object(own_issued, "hidden_ids", return_value=hidden):
                st = pe._Store(path)
                try:
                    links = st.links(["SupplierConfirmation.confirms",
                                      "SupplierInvoice.for_purchase_order"])
                    values = st.values(["SupplierConfirmation"], ["delivery_date"])
                    removed = st.removed(["SupplierConfirmation", "PurchaseOrder"])
                finally:
                    st.close()
                kept = {ours, theirs} - hidden
                self.assertEqual({l["from_object"] for l in links}, kept | {bill})
                self.assertEqual(set(values), kept)
                self.assertEqual(removed, hidden)


class EntityIndexTests(unittest.TestCase):
    """Query understanding resolves no identifier to a hidden document (every entity read
    passes ``_item_checked``), for every reader; other rows are untouched."""

    def test_entity_rows_of_a_hidden_document_are_dropped(self):
        from operating.query_understanding import PgEntityIndex
        rows = [("e1", "SupplierInvoice:b:1", "supplier_invoice", "", "in1", [], "api:a"),
                ("e2", "SupplierInvoice:b:2", "supplier_invoice", "", "in2", [], "api:a"),
                ("e3", "PurchaseOrder:p:1", "purchase_order", "", "po1", [], "api:a")]
        with mock.patch.object(own_issued, "hidden_ids",
                               return_value=frozenset({"SupplierInvoice:b:1"})), \
                mock.patch("operating.query_understanding.admitted", return_value=set()):
            got = PgEntityIndex._item_checked(object(), rows)
        self.assertEqual([r[0] for r in got], ["e2", "e3"])


class ReadPathTests(unittest.TestCase):
    """Over the PO document chain fixture (a PO, its supplier's confirmation, packing slip
    and bill of lading, injected transports): the confirmation hidden as PLX-issued leaves
    plx.investigate, plx.object, plx.around and plx.documents; the PO and the other
    documents stay."""

    from tests import test_investigate_chain_documents as chain
    setUp = chain.PoDocumentChainTest.setUp
    session = chain.PoDocumentChainTest.session

    def test_a_hidden_confirmation_leaves_every_read_path_and_its_neighbours_stay(self):
        chain = self.chain
        code, _document, _text = chain.run_spec(self.temp)
        self.assertEqual(code, 0)
        shown = self.session(chain.PURCHASING)(
            "plx.investigate", {"family": "chain", "root": chain.PO})
        counted = {e["name"]: e for e in shown["answer"] if e["kind"] == "count"}
        confirmation = counted["supplier_confirmation_count"]["basis"][0]
        self.assertTrue(confirmation.startswith("SupplierConfirmation:"), confirmation)
        with mock.patch.object(own_issued, "hidden_ids",
                               return_value=frozenset({confirmation})):
            ask = self.session(chain.PURCHASING)
            hidden = ask("plx.investigate", {"family": "chain", "root": chain.PO})
            self.assertEqual(hidden["root"]["object_id"], chain.PO)
            counted = {e["name"]: e["value"] for e in hidden["answer"] if e["kind"] == "count"}
            self.assertEqual(counted.get("supplier_confirmation_count", 0), 0, counted)
            self.assertEqual(counted.get("packing_slip_count"), 1, counted)
            self.assertEqual(counted.get("freight_bill_count"), 1, counted)
            obj = ask("plx.object", {"object_id": confirmation})
            self.assertEqual((obj.get("error") or {}).get("code"), "object_not_found", obj)
            around = ask("plx.around", {"from": chain.PO, "path": [
                {"link_type": "SupplierConfirmation.confirms", "direction": "reverse"}]})
            documents = ask("plx.documents", {"object_id": chain.PO})
            for answer in (hidden, around, documents):
                self.assertNotIn(confirmation, json.dumps(answer))


if __name__ == "__main__":
    unittest.main()
