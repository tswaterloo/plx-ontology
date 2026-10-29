"""Weighted identity resolution (``ontology.identity_scoring``): a supplier spelt two ways
in the Portal and BC is offered for review, never merged; a person's ``plx.review``
accept becomes a ``reviewed`` ``same_record_as`` at the next build and an identity of the
next score; a mail sender is offered and linked to its Supplier the same way.

Evidence label: ``isolated-fixture`` (synthetic Portal, BC and mail captures through the
real projector, the compiled repository schema, ``plx.review`` and the reviews link step;
no source is read); the live check's own argv (``devloop/live-checks/
ontology.identity_scoring.json``) runs ``injected-transport`` over the live-wiring fixture
world and is checked by the verifier's ``check_expectations``.
"""

from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ontology import identifiers, reviews, store_ddl
from ontology import identity_scoring as ids
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from operating import ontology_tools as ot
from operating import ontology_tools_review as rt
from tests.test_ontology_tools_review import (CONTRACTS, OID_A, OID_B, employee_session,
                                              identity_reviewer)

T = "2026-10-01T00:00:00+00:00"
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
BOX = "orders@plx.test"
OID_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"   # mail audience, but no mailbox item grant
FIRST = ["Amber", "Birch", "Cobalt", "Delta", "Ember", "Falcon", "Granite", "Harbor", "Indigo",
         "Juniper", "Kestrel", "Lumen", "Meridian", "Nimbus", "Onyx", "Pioneer", "Quartz",
         "Raven", "Sierra", "Tundra"]
SECOND = ["Polymers", "Packaging", "Botanicals", "Laboratories", "Fragrances", "Resins"]
# The Portal and BC spellings of one party, with different codes: no key joins them.
PORTAL_NW = {"id": "s-nw", "code": "NWC01", "name": "Northwind Chemical Co.", "currency": "CAD",
             "website": "https://www.northwindchem.test"}
BC_NW = {"id": "bc-v-nw", "number": "V9001", "displayName": "NORTHWIND CHEMICALS INC",
         "currencyCode": "CAD"}
DECOY = {"id": "s-nl", "code": "NWL01", "name": "Northwind Logistics Ltd.", "currency": "CAD"}


def party_name(i: int) -> str:
    return f"{FIRST[i % len(FIRST)]} {SECOND[i // len(FIRST)]}"


def world(customers: int) -> dict[str, dict[str, list[dict]]]:
    """``customers`` Portal/Sage customer pairs on one ERP code (verified links: a Portal
    to BC pair is never more than a lead, the Portal not serving the BC company), a sixth
    of them spelt a little differently in Sage; suppliers on equal codes (lead links) and
    the Northwind pair; mail from Northwind's domain, from inside and from free mail."""
    portal = {"public.app_customer": [], "sage.ar_customer": [],
              "public.app_supplier": [PORTAL_NW, DECOY]}
    bc = {"vendors": [BC_NW], "contacts": [
        {"id": "bc-k-1", "number": "K0001", "displayName": "Dana Reyes", "type": "Person",
         "companyName": "Northwind Chemicals", "email": "Sales@NorthwindChem.test"}]}
    for i in range(customers):
        name = party_name(i)
        portal["public.app_customer"].append({"id": f"c-{i}", "erp_code": f"C{i:04d}",
                                              "company_name": name, "currency": "CAD"})
        portal["sage.ar_customer"].append({"idcust": f"C{i:04d}", "codecurn": "CAD",
                                           "namecust": (name.upper() + " INC" if i % 6
                                                        else name[:-1])})
    for i in range(8):
        portal["public.app_supplier"].append({"id": f"s-{i}", "code": f"V{i:04d}",
                                              "name": party_name(i + 40), "currency": "USD"})
        bc["vendors"].append({"id": f"bc-v-{i}", "number": f"V{i:04d}", "currencyCode": "USD",
                              "displayName": party_name(i + 40)})
    mails = [("MSG-1", "Dana Reyes <sales@northwindchem.test>", "2026-09-01T09:00:00Z"),
             ("MSG-2", "buyer@plx.test", "2026-09-01T10:00:00Z"),
             ("MSG-3", "someone@gmail.com", "2026-09-01T11:00:00Z")]
    return {"portal_api": portal, "bc": bc, "graph_mail": {"messages": [
        {"mailbox": BOX, "id": key, "subject": "Price list", "from": sender, "received_at": at}
        for key, sender, at in mails]}}


class Source:
    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        digest = hashlib.sha256(json.dumps([rel, row], sort_keys=True).encode()).hexdigest()
        if self.source == "bc":
            return CapturedRecord("bc", rel, f"bc_manufacturing/{row['id']}", row,
                                  f"bc://production/bc_manufacturing/{rel}/{row['id']}@X", "row",
                                  digest, T, None, T, f"bc:production:bc_manufacturing:{rel}",
                                  capture_lineage="bc_manufacturing")
        if self.source == "graph_mail":
            return CapturedRecord("graph_mail", rel, json.dumps([row["id"], BOX]), row,
                                  f"graph-mail://{BOX}/{row['id']}@CK-1", "row", digest, T, None,
                                  T, f"mail:{BOX}")
        field = {"sage.ar_customer": "idcust", "sage.oe_invoice_header": "invuniq"}.get(rel, "id")
        key = json.dumps({field: row[field]})
        return CapturedRecord("portal_api", rel, key, row, f"api://{rel}/{row[field]}@X", "row",
                              digest, T, None, T, f"api:{rel}")


def build(path: Path, rows: dict, review_store: reviews.ReviewStore | None = None) -> None:
    steps = None if review_store is None else {"reviews": reviews.link_step(review_store, "plx")}
    build_store(path, SCHEMA, {name: Source(name, rel) for name, rel in rows.items()},
                normalizer=identifiers.normalize, clock=lambda: NOW, post_project=steps)


def tools(path: Path, review_store: reviews.ReviewStore) -> ot.OntologyTools:
    a, b, c = (f"employee:{oid}" for oid in (OID_A, OID_B, OID_C))
    policy = ot.ReadPolicy(tenant="plx", audiences={"api:*": {a, b, c}, "bc:*": {a, c},
                                                    "mail:*": {a, c}},
                           markings={m: {a, b, c} for m in store_ddl.MARKINGS})
    return ot.OntologyTools(
        ot.StoreReader.open(path), policy, schema=ot.SchemaView.from_compiled(SCHEMA),
        grammar=identifiers, contracts=CONTRACTS, clock=lambda: NOW,
        item_gate=lambda actor, handle: (f"employee:{OID_A}" in actor.principals
                                         and handle.startswith(f"graph-mail://{BOX}/")))


SCHEMA = load_schema()


class IdentityScoringTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "projection.sqlite"
        self.reviews = reviews.ReviewStore(reviews.store_path(self.dir / "state"),
                                           clock=lambda: NOW)
        self.rows = world(12)
        build(self.store, self.rows)

    def score(self, oid=OID_A, store=None, schema=None, **kwargs):
        base = tools(store or self.store, self.reviews)
        self.addCleanup(base.store.conn.close)
        actor = employee_session(oid).actor
        conn = sqlite3.connect(str(store or self.store))
        self.addCleanup(conn.close)
        before = conn.total_changes
        out = ids.score(conn, schema or SCHEMA, ids.Scope.of_tools(base, actor), **kwargs)
        self.assertEqual(conn.total_changes, before)
        self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 0)
        for kind in out["kinds"].values():
            self.assertEqual(sum(kind["bands"].values()), kind["pairs_compared"], kind)
            pairs = [(c["subject"], c["target"]) for c in kind["candidates"]]
            self.assertEqual(len(pairs), len(set(pairs)))
            self.assertEqual(kind["evaluation"]["train_eval_overlap"], 0)
            # the per-source-pair split partitions the kind's pairs and identity pairs
            split = kind["evaluation"].get("by_sources", {})
            if kind["object_type"] != "Email":
                self.assertEqual(sum(s["pairs_compared"] for s in split.values()),
                                 kind["pairs_compared"])
                self.assertEqual(sum(s["identity_pairs"] for s in split.values()),
                                 kind["evaluation"]["identity_pairs"])
        return out

    def candidate(self, kind, subject_id, target_id):
        found = [c for c in kind["candidates"] if subject_id in c["subject"] + c["target"]
                 and target_id in c["subject"] + c["target"]]
        return found[0] if found else None

    def accept(self, candidate):
        base = tools(self.store, self.reviews)
        self.addCleanup(base.store.conn.close)
        session = employee_session(OID_A)
        review = rt.ReviewTools(base, self.reviews, session=session, gate=identity_reviewer)
        out = review.call("plx.review", dict(candidate["review"], mode="record"), session.actor)
        self.assertNotIn("error", out, json.dumps(out)[:1500])
        self.assertEqual(out["business_system_writes"], 0)
        build(self.store, self.rows, self.reviews)   # the next build projects the decision

    def links(self, link_type):
        with sqlite3.connect(str(self.store)) as conn:
            return conn.execute("SELECT from_object, to_object, tier FROM links WHERE "
                                "link_type = ? AND superseded_at IS NULL AND binding_kind = "
                                "'reviewed'", (link_type,)).fetchall()

    def test_a_supplier_spelt_two_ways_is_offered_for_review_and_a_reviewed_merge_is_identity(self):
        supplier = self.score(kinds=["supplier"])["kinds"]["supplier"]
        offered = self.candidate(supplier, '"s-nw"', '"bc-v-nw"')
        self.assertIsNotNone(offered, json.dumps(supplier["candidates"])[:1500])
        self.assertEqual(offered["band"], "review")
        self.assertGreaterEqual(offered["probability"], ids.REVIEW_PROBABILITY)
        self.assertIn({"feature": "name", "level": "close"},
                      [{k: f[k] for k in ("feature", "level")} for f in offered["features"]])
        # The other Northwind shares a word, never the party: below review, never offered.
        self.assertIsNone(self.candidate(supplier, '"s-nl"', '"bc-v-nw"'))
        self.assertGreater(supplier["bands"]["below_review"], 0)
        # Equal codes are a lead in the schema, never an identity m may learn from.
        self.assertEqual((supplier["identity_links"], supplier["m_basis"]["identity_pairs"]),
                         ({}, 0))
        self.assertEqual(self.links("Supplier.same_record_as"), [])

        self.accept(offered)
        self.assertEqual(self.links("Supplier.same_record_as"),
                         [(offered["subject"], offered["target"], "reviewed")])
        again = self.score(kinds=["supplier"])["kinds"]["supplier"]
        self.assertIsNone(self.candidate(again, '"s-nw"', '"bc-v-nw"'))
        self.assertEqual((again["identity_links"], again["bands"]["identity"],
                          again["m_basis"]["identity_pairs"]), ({"reviewed": 1}, 1, 1))

    def test_a_mail_sender_is_offered_for_its_supplier_and_linked_once_reviewed(self):
        mail = self.score(kinds=["mail_sender"])["kinds"]["mail_sender"]
        self.assertEqual(mail["senders_skipped"], {"internal_sender": 1})
        # Both of Northwind's cards (one party), never the other Northwind; the Portal
        # card first: its website is the sender's domain.
        offered, other = mail["candidates"]
        self.assertEqual([c["target"] for c in mail["candidates"]],
                         ['Supplier:portal.app_supplier:{"id":"s-nw"}',
                          'Supplier:bc.vendors:{"id":"bc-v-nw"}'])
        self.assertEqual({c["link_type"] for c in mail["candidates"]}, {"Email.sent_by_supplier"})
        self.assertIn('"MSG-1"', offered["subject"])
        self.assertEqual({f["feature"] for f in offered["features"] if f["weight"] > 0},
                         {"domain", "domain_name", "contact"})
        # The sender, the supplier's name and the BC contact holding the address.
        self.assertEqual(len(offered["review"]["evidence"]), 3)

        self.accept(offered)
        self.assertEqual(self.links("Email.sent_by_supplier"),
                         [(offered["subject"], offered["target"], "reviewed")])
        again = self.score(kinds=["mail_sender"])["kinds"]["mail_sender"]
        self.assertEqual(([c["target"] for c in again["candidates"]], again["bands"]["identity"],
                          again["identity_links"]), ([other["target"]], 1, {"reviewed": 1}))

    def test_m_is_learned_from_verified_pairs_and_evaluated_on_held_out_folds(self):
        customer = self.score(kinds=["customer"])["kinds"]["customer"]
        self.assertEqual(customer["identity_links"], {"verified": 12})
        self.assertEqual(customer["m_basis"]["identity_pairs"], 12)
        # 10 of 12 names agree exactly: m moves from its 0.70 prior toward 10/12.
        self.assertAlmostEqual(customer["m"]["name"]["exact"], (10 + 4 * 0.7) / 16, places=3)
        self.assertEqual(customer["bands"]["identity"], 12)
        self.assertEqual(customer["candidates"], [])
        evaluation = customer["evaluation"]
        self.assertEqual((evaluation["identity_pairs"], evaluation["blocking_recall"]), (12, 1.0))
        # Held out, the exact names score above review; a one-letter variant with only
        # its currency beside it may not, and is not covered.
        self.assertGreaterEqual(evaluation["true_positives"], 10)
        self.assertEqual(evaluation["coverage"], round(evaluation["true_positives"] / 12, 4))
        self.assertEqual(evaluation["precision_with_closure"], 1.0)

    def test_a_restricted_reader_is_scored_over_what_it_may_read_only(self):
        full = self.score()["kinds"]
        portal_only = self.score(OID_B)["kinds"]
        no_mailbox = self.score(OID_C)["kinds"]
        self.assertEqual(portal_only["supplier"]["records"], 10)
        self.assertEqual(full["supplier"]["records"], 19)
        self.assertEqual(portal_only["customer"]["records"], 24)
        for kinds in (portal_only, no_mailbox):
            self.assertEqual(kinds["mail_sender"]["records"], 0)
            self.assertEqual(kinds["mail_sender"]["candidates"], [])
        for name, kind in portal_only.items():
            self.assertEqual(kind["pairs_compared"], 0 if name != "customer" else
                             full["customer"]["pairs_compared"])
            self.assertNotIn("bc.", json.dumps(kind))
        self.assertIsNotNone(self.candidate(no_mailbox["supplier"], '"s-nw"', '"bc-v-nw"'))

    def test_an_unknown_kind_fails_closed(self):
        for kinds in (["supplier", "vendor"], []):
            with self.assertRaisesRegex(ids.IdentityScoringError, "identity_kind_unknown"):
                self.score(kinds=kinds)

    def test_every_record_is_counted_once_at_1_10_and_60_pairs(self):
        for n in (1, 10, 60):
            store = self.dir / f"n{n}.sqlite"
            build(store, world(n))
            started = time.monotonic()
            customer = self.score(store=store, kinds=["customer"])["kinds"]["customer"]
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual((customer["records"], customer["identity_links"],
                              customer["bands"]["identity"]), (2 * n, {"verified": n}, n))

    def compared(self, **kwargs):
        """The score and the comparison levels of every pair it compared."""
        seen, real = {}, ids._Groups.levels

        def levels(groups, a, b):
            seen[frozenset((a, b))] = out = real(groups, a, b)
            return out
        with mock.patch.object(ids._Groups, "levels", levels):
            return self.score(**kwargs), seen

    def test_a_customer_po_two_customers_share_is_no_agreement_and_no_candidate(self):
        """One PO text on two customers' invoices (Sage for one, BC for the other) names two
        POs: the Portal matches a PO only within its customer. The same text on one
        customer's two invoices agrees once a person has reviewed the customer."""
        po, day = "4500-12", {"invdate": 20260901, "invoiceDate": "2026-09-01"}
        self.rows = {"portal_api": {
            "sage.ar_customer": [{"idcust": "C0001", "namecust": "AMBER POLYMERS",
                                  "codecurn": "CAD"},
                                 {"idcust": "C0002", "namecust": "BIRCH RESINS",
                                  "codecurn": "CAD"}],
            "sage.oe_invoice_header": [{"invuniq": 1, "invnumber": "IN-1", "customer": "C0001",
                                        "ponumber": po, "invdate": day["invdate"]}]},
            "bc": {"customers": [{"id": "bc-c-1", "number": "C0001",
                                  "displayName": "AMBER POLYMERS", "currencyCode": "CAD"},
                                 {"id": "bc-c-2", "number": "C0002",
                                  "displayName": "BIRCH RESINS", "currencyCode": "CAD"}],
                   "salesInvoices": [
                       {"id": f"bc-i-{n}", "number": f"SI-{n}", "customerNumber": f"C000{n}",
                        "orderNumber": f"SO-{n}", "externalDocumentNumber": po,
                        "invoiceDate": day["invoiceDate"]} for n in (1, 2)]}}
        build(self.store, self.rows)
        sage = 'SalesInvoice:portal.sage_oe_invoice_header:{"invuniq":1}'
        other, own = (f'SalesInvoice:bc.sales_invoices:{{"id":"bc-i-{n}"}}' for n in (2, 1))
        # The scorer compares a customer PO only as the schema's scoped identifier; the
        # same domain schema with SalesInvoice.customer_po declaring that kind (an open
        # decision) exercises the scope, the shipped schema compares no customer PO.
        scoped = load_schema()
        prop = next(p for p in scoped.object_types["SalesInvoice"]["properties"]
                    if p["name"] == "customer_po")
        prop.update(value_type="identifier", identifier_kind="customer_po")
        for schema in (SCHEMA, scoped):
            out, levels = self.compared(kinds=["invoice"], schema=schema)
            invoice = out["kinds"]["invoice"]
            self.assertIn(frozenset((sage, other)), levels, schema is scoped)
            self.assertNotIn("exact", levels[frozenset((sage, other))].values())
            self.assertIsNone(self.candidate(invoice, sage, other))
        self.assertEqual(levels[frozenset((sage, other))].get("customer_po"), "differ")
        # Customers are a lead across Sage and BC (the Portal does not serve the BC
        # company), so even the own customer's PO is no agreement until reviewed.
        self.assertEqual(levels[frozenset((sage, own))].get("customer_po"), "differ")

        customer = self.score(kinds=["customer"])["kinds"]["customer"]
        self.accept(self.candidate(customer, '"C0001"', '"bc-c-1"'))
        out, levels = self.compared(kinds=["invoice"], schema=scoped)
        invoice = out["kinds"]["invoice"]
        self.assertEqual(levels[frozenset((sage, own))].get("customer_po"), "exact")
        self.assertEqual(levels[frozenset((sage, other))].get("customer_po"), "differ")
        self.assertIsNone(self.candidate(invoice, sage, other))
        offered = self.candidate(invoice, sage, own)
        self.assertIsNotNone(offered, json.dumps(invoice["candidates"])[:1500])
        self.assertIn({"feature": "customer_po", "level": "exact"},
                      [{k: f[k] for k in ("feature", "level")} for f in offered["features"]])
        # A reader who may read both invoices but not the BC customer has no customer for
        # the BC invoice: no scope, no customer PO key, no agreement on it.
        seen, real = {}, ids._Groups.levels

        def levels_of(groups, a, b):
            seen[frozenset((a, b))] = found = real(groups, a, b)
            return found
        with closing(sqlite3.connect(str(self.store))) as conn, \
                mock.patch.object(ids._Groups, "levels", levels_of):
            audiences = tuple(a for (a,) in conn.execute(
                "SELECT DISTINCT audience_key FROM objects")
                if a != "bc:production:bc_manufacturing:customers")
            restricted = ids.score(conn, scoped, ids.Scope(audiences, store_ddl.MARKING_SETS),
                                   kinds=["invoice"])["kinds"]["invoice"]
        self.assertEqual(restricted["records"], 3)
        self.assertNotIn("customer_po", seen.get(frozenset((sage, own)), {}))
        self.assertFalse([c for c in restricted["candidates"]
                          if "customer_po" in {f["feature"] for f in c["features"]}])

    def test_a_petra_po_and_a_bc_document_number_never_meet_in_one_feature(self):
        """A Portal PO renumbered to a BC number and a BC PO whose own number is that text:
        a bc_document_no is no Petra PO, so no feature compares the two."""
        self.rows = {"portal_api": {"public.app_purchase_order": [
            {"id": "po-1", "po_number": "PO-2001", "date_generated": "2026-09-01",
             "custom_fields": {"renumberedTo": "PO-3001"}}]},
            "bc": {"purchaseOrders": [{"id": "bc-po-1", "number": "PO-3001",
                                       "orderDate": "2026-09-01"}]}}
        build(self.store, self.rows)
        portal = 'PurchaseOrder:portal.app_purchase_order:{"id":"po-1"}'
        bc = 'PurchaseOrder:bc.purchase_orders:{"id":"bc-po-1"}'
        out, levels = self.compared(kinds=["purchase_order"])
        self.assertEqual(levels[frozenset((portal, bc))].get("petra_po"), "differ")
        self.assertNotIn("bc_document_no", levels[frozenset((portal, bc))])
        self.assertEqual(out["kinds"]["purchase_order"]["candidates"], [])


class SchemaIdentifierFeaturesTest(unittest.TestCase):
    def test_identifier_features_are_the_schema_identifier_properties_one_kind_each(self):
        """No identifier property is named in ``KINDS``: each record kind compares one
        feature per identifier kind its object type's schema properties declare, over
        exactly the properties of that kind."""
        for kind in ids.KINDS.values():
            declared = {p["name"]: p.get("identifier_kind") for p in
                        SCHEMA.object_types.get(kind.object_type, {}).get("properties", ())}
            for feature in kind.features:
                self.assertEqual([p for p in feature.properties if declared.get(p)], [],
                                 (kind.name, feature.name))
            self.assertEqual(kind.identifiers, kind.name in ids.DOCUMENT_KINDS, kind.name)
            if not kind.identifiers:
                continue
            resolved = ids.resolved(SCHEMA, kind.name)
            by_kind = {f.identifier_kind: f.properties for f in resolved.features
                       if f.identifier_kind}
            self.assertEqual(set(by_kind), set(declared.values()) - {None}, kind.name)
            for feature in resolved.features:
                self.assertEqual({declared.get(p) for p in feature.properties},
                                 {feature.identifier_kind} if feature.properties else set(),
                                 (kind.name, feature.name))
                if feature.identifier_kind:
                    self.assertEqual(set(feature.properties),
                                     {p for p, k in declared.items()
                                      if k == feature.identifier_kind})
        orders = {f.identifier_kind: set(f.properties)
                  for f in ids.resolved(SCHEMA, "purchase_order").features}
        self.assertTrue(orders["petra_po"] and orders["bc_document_no"])
        self.assertFalse(orders["petra_po"] & orders["bc_document_no"])
        with self.assertRaisesRegex(ids.IdentityScoringError, "identity_kind_unknown"):
            ids.resolved(SCHEMA, "vendor")
        # A schema property of an identifier kind the grammar does not know fails closed.
        unknown = load_schema()
        unknown.object_types["Shipment"]["properties"][0]["identifier_kind"] = "no_such_kind"
        with self.assertRaisesRegex(ids.IdentityScoringError, "identifier_kind_unknown"):
            ids.resolved(unknown, "shipment")


class LiveCheckSpecTest(unittest.TestCase):
    def test_spec_runs_end_to_end_and_counts_a_payload_that_is_not_its_candidates(self):
        from devloop import live_verify as lv
        from devloop.tests import test_live_wiring as wiring
        from scripts import identity_scoring_probe as probe
        from scripts import ontology_projection_probe as projection

        spec_doc = wiring.spec("ontology.identity_scoring")
        lv.validate_spec(spec_doc, stem="ontology.identity_scoring")
        fake, books = wiring.fixture_bc()
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, wiring.fixture_portal():
            os.chdir(wiring.ROOT)   # the spec names its audience file relative to the repo
            try:
                code, document = wiring.run_main(
                    probe.main, wiring.argv_of(spec_doc, tmp),
                    environ={**wiring.PORTAL_ENV, **wiring.BC_ENV}, bc_sender=fake,
                    bc_books=books)
            finally:
                os.chdir(cwd)
            host = projection.served_host(f"{tmp}/ontology.sqlite")
            score = ids.score

            def swapped(*args, **kwargs):
                """Every kind offers, under the review probability, a payload naming
                another pair than its candidate's and a candidate with neither payload
                nor reason."""
                out = score(*args, **kwargs)
                for kind in out["kinds"].values():
                    link_type = kind["link_type"]
                    link_type = link_type if isinstance(link_type, str) else link_type[0]
                    pair = {"subject": "fx-a", "target": "fx-b", "link_type": link_type,
                            "probability": 0.4}
                    kind["candidates"] += [
                        dict(pair, review={"decision": "accept_link", "subject": "fx-a",
                                           "target": "fx-a", "link_type": link_type,
                                           "evidence": []}),
                        dict(pair, probability=0.9, review=None)]
                return out

            with mock.patch.object(ids, "score", swapped):
                broken = probe.check_identity(host, budget_seconds=None)
        self.assertEqual(code, 0, document)
        # Only the live-read label and the Portal-BC party floor may miss on the fixture
        # world (its BC capture holds documents, no vendor or customer).
        failures = lv.check_expectations(spec_doc, code, document)
        self.assertLessEqual(set(failures), {"bc_capture.evidence_label != expected",
                                             "portal_bc_pairs < 1"}, failures)
        self.assertEqual(document["sources"], ["bc", "portal_api"])
        # the purchase orders' verified Portal-BC keys are evaluated end to end
        orders = document["evaluation"]["purchase_order"]
        self.assertGreater(orders["identity_pairs"], 0)
        self.assertEqual(orders["blocking_recall"], 1.0)
        for name in document["kinds_scored"]:
            self.assertEqual(sum(document["bands"][name].values()),
                             document["pairs_compared"][name], name)
        self.assertEqual(document["portal_bc_pairs"], sum(
            k["pairs_compared"] for k in document["portal_bc"].values()))
        kinds = len(ids.KINDS)
        self.assertEqual((broken["payload_failures"], broken["below_threshold_offered"],
                          document["payload_failures"]), (2 * kinds, kinds, 0))
        # Counts and names only: nothing the verifier records names a record.
        recorded = json.dumps(lv._recorded(spec_doc, document))
        for value in ("PO92", "FXBC", "fx-", "api://", "bc://", ":portal.", ":bc."):
            self.assertNotIn(value, recorded)


if __name__ == "__main__":
    unittest.main()
