"""Entity resolution (operating/entity_resolution.py): a name or code resolves to every
record of that thing across systems, scored and explained, permission-gated by the store,
and the Ask's first read carries it to the agent. isolated-fixture."""

from __future__ import annotations

import unittest
from unittest import mock

from operating import entity_resolution as er
from operating import objective_runner
from operating.query_understanding import EntityRow, identifier_leads


SAGE = 'Product:portal.sage_ic_item:{"itemno":"AB-CD-12X"}'
FM = 'Product:portal.fm_finished_product:{"id":7}'
APP = 'Product:portal.app_product:{"id":"p7"}'
BC = 'Product:bc.items:{"id":"b7"}'
HIDDEN = 'Product:bc.items:{"id":"hidden"}'
C_SAGE = 'Customer:portal.sage_ar_customer:{"idcust":"ACME01"}'
C_BC = 'Customer:bc.customers:{"id":"c1"}'
C_FM = 'Customer:portal.fm_customer:{"id":3}'
P_BRAND = 'Product:portal.fm_finished_product:{"id":9}'
PO = 'PurchaseOrder:bc.purchase_orders:{"id":"po1"}'
SPRAY = 'Product:portal.fm_finished_product:{"id":12}'
CPO_A = 'CustomerPurchaseOrder:portal.sage_oe_order_header.ponumber:{"orduniq":1}'
CPO_B = 'CustomerPurchaseOrder:portal.sage_oe_order_header.ponumber:{"orduniq":2}'
MAIL = 'Email:graph_mail.messages:{"id":"m1","mailbox":"b1"}'


def row(oid, type_, name, aliases, matched="", strict=1.0):
    return EntityRow("e:" + oid, oid, type_, name, name.lower(), tuple(aliases), matched,
                     1.0, strict)


ROWS = [row(SAGE, "item", "Acme Vanilla 75g", ["AB-CD-12X"]),
        row(FM, "item", "Acme Vanilla 75g", ["AB-CD-12X", "ACM-001-75G"]),
        row(APP, "item", "Acme Vanilla 75g", ["AB-CD-12X", "ACM-001-75G"]),
        row(BC, "item", "Acme Vanilla 75g", ["ACM-001-75G"]),
        row(HIDDEN, "item", "Acme Vanilla 75g", ["ACM-001-75G"]),
        row(C_SAGE, "customer", "ACME LLC", ["ACME01", "currency USD"]),
        row(C_BC, "customer", "ACME LLC", ["ACME01"]),
        row(C_FM, "customer", "ACME LLC", []),
        row(P_BRAND, "item", "Acme (Brand) Deodorant Cherry", ["ACM-030"]),
        row(SPRAY, "item", "Acme Smooth Coat Spray 250 mL", ["SC-250"]),
        row(CPO_A, "customer_po", "Customer PO XY0101004/ 0103440", ["XY0101004/ 0103440"]),
        row(CPO_B, "customer_po", "Customer PO XY0101004-1", ["XY0101004-1"]),
        # an order carrying the supplier's code is linked to it, never the supplier
        row(PO, "purchase_order", "PO100", ["PO100", "ACME01"]),
        # a mail participant named like a word of a question, held on the message
        row(MAIL, "person", "Abcd", [])]


class FakeIndex:
    def scope(self, policy, actor):
        return "scope"

    def by_identifier(self, forms, scope, types=None):
        forms = {f.upper() for f in forms}
        return [EntityRow(r.entity_id, r.object_id, r.type, r.name, r.normalized, r.alias,
                          next(a for a in r.alias if a.upper() in forms))
                for r in ROWS if forms & {a.upper() for a in r.alias}], set()

    def by_terms(self, terms, scope, types=None):
        out = []
        for term in terms:
            for r in ROWS:
                if term in r.normalized:
                    out.append(r)
        return out, {}


    def named_words(self, words, scope):
        return {w for w in words if 0 < sum(f" {w} " in f" {r.normalized} " for r in ROWS) <= 5}


    def split_candidates(self, digits, scope):
        return [r for r in ROWS if any(digits in a for a in r.alias)]


class FakeStore:
    def links_touching(self, scope, ids, types):
        links = [{"link_type": "Customer.same_record_as", "from_object": C_FM,
                  "to_object": C_SAGE, "tier": "stored_by_text_join"}]
        return [link for link in links if link["link_type"] in types
                and (link["from_object"] in ids or link["to_object"] in ids)]


class FakeSchema:
    link_types = {"Customer.same_record_as": {}, "Product.same_record_as": {},
                  "Product.bc_posted_as": {}, "Product.owned_by": {}}


class FakeBase:
    policy = object()
    schema = FakeSchema()
    store = FakeStore()

    def _visible_versions(self, scope, ids):
        return {oid: {"lineage": "silver_view"} for oid in ids if oid != HIDDEN}

    def _displays(self, scope, ids):
        return {oid: {"untrusted_text": oid[-6:], "origin": {"citation": "h"}} for oid in ids}


def resolve(text, object_type=None):
    entities, gaps = er.Resolver(FakeBase(), FakeIndex()).resolve("scope", "actor", text,
                                                                   object_type)
    return entities, gaps


class Mentions(unittest.TestCase):
    def test_a_multi_part_code_is_one_token(self):
        leads = identifier_leads("If I take the item BZ-DEO-21F, where is the rest?")
        self.assertEqual([lead.text for lead in leads], ["BZ-DEO-21F"])

    def test_question_mentions_are_codes_and_name_phrases_never_numbers(self):
        got = er.mentions("If I take the item AB-CD-12X we bought 10032 from Acme Labs on "
                          "Oct 5, where did the rest go?")
        self.assertEqual([(m["text"], m["kind"]) for m in got],
                         [("AB-CD-12X", "code"), ("Acme Labs", "name")])

    def test_the_system_a_question_counts_in_is_no_mention(self):
        # dev 11-10 (G-0110/0112): "sales orders in Sage" read the vendor Sage Software
        # first, as the question's subject; the place records are held is no record
        for text, names in (
                ("As of October 19, 2026, how many sales orders in Sage had order date "
                 "on or before that day?", []),
                ("How many approvals in Mission Control are open for Acme Labs?",
                 ["Acme Labs"]),
                ("How much did we pay Sage Software in BC last year?", ["Sage Software"]),
                ("How many invoices from Sage are open?", ["Sage"]),
                ("Which orders of Sage Holdings are open?", ["Sage Holdings"]),
                ("How many orders are in Sage Holdings' name?", ["Sage Holdings"])):
            with self.subTest(text=text):
                self.assertEqual([m["text"] for m in er.mentions(text)], names)

    def test_a_system_set_apart_or_named_with_the_is_no_mention(self):
        # novel dev 11-11: "apart from Sage" prefetched the vendor Sage Software, and every
        # "in the Portal" / "outside the Portal" read "Portal" as a record's name (a gate
        # relevance anchor); a name that only starts with a system word is still one
        for text, names in (
                ("Where else does the sales order OR1000151212 show up, apart from Sage?",
                 ["sales order OR1000151212"]),
                ("Is the product PA-RC-60 in the Portal connected to anything in another "
                 "system? What?", ["PA-RC-60"]),
                ("What do we have on the customer Acme Labs outside the Portal?",
                 ["Acme Labs"]),
                ("Anything on Acme Labs other than BC, aside from Teams or besides MC?",
                 ["Acme Labs"]),
                ("Which suppliers other than BC Hydro are late?", ["BC Hydro"]),
                ("How many invoices from Sage are open?", ["Sage"]),
                ("Is the Portal Group a customer?", ["Portal Group"])):
            with self.subTest(text=text):
                self.assertEqual([m["text"] for m in er.mentions(text)], names)

    def test_a_short_text_is_its_own_mention(self):
        self.assertEqual([(m["text"], m["kind"]) for m in er.mentions("acme")],
                         [("acme", "name")])


    def test_a_short_code_with_a_space_or_a_label_word_is_the_code(self):
        got = er.mentions("PO 100")
        self.assertEqual([(m["text"], m["kind"]) for m in got], [("PO 100", "code")])
        self.assertIn("PO100", got[0]["forms"])
        got = er.mentions("invoice XY1000123")
        self.assertEqual([(m["text"], m["kind"]) for m in got], [("XY1000123", "code")])
        entities, _ = resolve("PO 100")
        self.assertEqual(entities[0]["status"], "match")
        self.assertEqual(entities[0]["records"][0]["object"]["object_id"], PO)


class Resolve(unittest.TestCase):
    def test_the_index_is_given_the_store_s_item_check(self):
        """IN-13: an item-gated index row (a mail or Teams participant, a file or chat
        name) is a candidate only when the store's gate opens its record: the scope the
        index reads carries that gate as its item check."""
        seen = []

        class Index(FakeIndex):
            def by_terms(self, terms, scope, types=None):
                seen.append(scope)
                return super().by_terms(terms, scope, types)
        er.Resolver(FakeBase(), Index()).resolve("scope", "actor", "Why is Acme late?")
        self.assertTrue(seen)
        self.assertEqual(seen[0].item_check([MAIL, HIDDEN]), {MAIL})

    def test_a_code_reaches_every_system_through_shared_codes(self):
        entities, gaps = resolve("Where is AB-CD-12X now?")
        self.assertEqual(gaps, [])
        self.assertEqual(len(entities), 1)
        item = entities[0]
        ids = [r["object"]["object_id"] for r in item["records"]]
        self.assertEqual(set(ids), {SAGE, FM, APP, BC})   # BC by the FM code it carries
        self.assertNotIn(HIDDEN, ids)                      # the store's gate decides
        self.assertEqual(item["status"], "match")
        self.assertEqual(set(item["systems"]), {"Sage", "FileMaker", "Portal",
                                                "Business Central"})
        bc = next(r for r in item["records"] if r["object"]["object_id"] == BC)
        self.assertIn("ACM-001-75G", bc["why"][0])

    def test_a_name_resolves_the_party_and_offers_its_namesakes_as_leads(self):
        entities, _ = resolve("Why does Acme stock show no value?")
        party = entities[0]
        self.assertEqual(party["object_type"], "Customer")
        self.assertEqual({r["object"]["object_id"] for r in party["records"]},
                         {C_SAGE, C_BC, C_FM})
        self.assertEqual(party["status"], "match")
        leads = [e for e in entities if e["status"] == "lead"]
        self.assertTrue(leads)
        self.assertTrue(all(r["confidence"] < er.MATCH_AT for e in leads
                            for r in e["records"]))
        # the order holding the customer's code is never the customer
        self.assertNotIn(PO, {r["object"]["object_id"] for e in entities
                              for r in e["records"]})

    def test_a_name_read_off_a_message_is_a_lead_never_the_thing(self):
        entities, _ = resolve("Did we send the documents with the different ABCD?")
        found = [r for e in entities for r in e["records"]
                 if r["object"]["object_id"] == MAIL]
        self.assertTrue(found)
        self.assertTrue(all(r["status"] == "lead" for r in found))

    def test_a_phrase_or_a_lower_case_name_resolves(self):
        """A phrase of several words whole in a name names that record; a word written in
        lower case that is in only a few names is looked up as a name too."""
        entities, _ = resolve("did they approve the new label for Smooth Coat?")
        spray = next(e for e in entities if e["mention"] == "Smooth Coat")
        self.assertEqual(spray["status"], "match")
        self.assertEqual(spray["records"][0]["object"]["object_id"], SPRAY)
        entities, _ = resolve("anything new on the cherry deodorant?")
        found = [r for e in entities for r in e["records"]
                 if r["object"]["object_id"] == P_BRAND]
        self.assertTrue(found)
        self.assertEqual(found[0]["status"], "lead")   # a lower-case word: a lead at most

    def test_a_value_inside_a_messy_reference_resolves_from_its_split(self):
        """A reference field holding several values, a continuation written as digits
        only, or a short suffix: each value finds the record, marked as split from it."""
        self.assertEqual(er.components("XY0101004/ 0103440"), ["XY0101004", "XY0103440"])
        self.assertEqual(er.components("XY0101004-1"), ["XY0101004-1", "XY0101004"])
        self.assertEqual(er.components("A123, B456 and 789"), ["A123", "B456", "A789"])
        self.assertEqual(er.components("XY0101004"), [])
        entities, _ = resolve("did we get XY0103440 and XY0101004?")
        by_mention = {}
        for entity in entities:
            # read out of a longer reference: possible, never the same thing
            self.assertEqual(entity["status"], "derived")
            for record in entity["records"]:
                by_mention.setdefault(entity["mention"], set()).add(
                    record["object"]["object_id"])
                self.assertEqual(record["status"], "derived")
                self.assertIn("possible: split from", " ".join(record["why"]))
        # a record two asked values both find is listed once
        self.assertEqual(by_mention["XY0103440"] | by_mention["XY0101004"], {CPO_A, CPO_B})
        self.assertIn(CPO_B, by_mention["XY0101004"])
        # every asked code keeps its own records past the leads' bound
        with mock.patch.object(er, "MAX_LEADS", 1):
            entities, _ = resolve("did we get XY0103440 and XY0101004?")
        self.assertEqual({e["mention"] for e in entities}, set(by_mention))

    def test_the_index_and_the_query_fold_a_name_alike(self):
        """One normaliser: the entity index stores a name as the query folds it (an
        apostrophe dropped, ``&`` as ``and``), and a row stored before that (an
        apostrophe as a space) still resolves."""
        from operating.chunking.entities import fold as index_fold
        from operating.query_understanding import fold as query_fold
        for name in ("Mapleton's Supply", "Ash & Oak Ltd.", "Café Rouge’s", "ACME, INC.",
                     "B2B-Parts/East", "o'neil  &  sons"):
            self.assertEqual(index_fold(name), query_fold(name))
        self.assertEqual(index_fold("Mapleton's Supply"), "mapletons supply")
        legacy = row('Supplier:bc.vendors:{"id":"m1"}', "supplier", "MAPLETON'S SUPPLY", [])
        legacy = EntityRow(legacy.entity_id, legacy.object_id, legacy.type, legacy.name,
                           "mapleton s supply", (), "", 1.0, 1.0)

        class Legacy(FakeIndex):
            def by_terms(self, terms, scope, types=None):
                return [legacy] if "mapleton s supply" in terms else [], {}

        entities, _ = er.Resolver(FakeBase(), Legacy()).resolve("scope", "actor",
                                                               "Mapleton's Supply")
        self.assertEqual(entities[0]["status"], "match")
        self.assertEqual(entities[0]["records"][0]["object"]["object_id"], legacy.object_id)

    def test_a_name_with_a_slip_or_words_between_is_a_lead_and_a_plural_a_match(self):
        slip = row('Supplier:bc.vendors:{"id":"t1"}', "supplier",
                   "NORVIK LOGISITICS 9TH WAREHOUSE", [])
        towel = row('Product:portal.sage_ic_item:{"itemno":"T1"}', "item",
                    "12 x 20 Copper Leaf Std Hand Towels", [])
        exact = row('Supplier:bc.vendors:{"id":"q1"}', "supplier", "Quill Works", [])
        near = row('Supplier:bc.vendors:{"id":"q2"}', "supplier", "Quills Works Inc", [])

        class Index(FakeIndex):
            def by_terms(self, terms, scope, types=None):
                return [slip, towel, exact, near], {}

        def best(text):
            entities, _ = er.Resolver(FakeBase(), Index()).resolve("scope", "actor", text)
            return {r["object"]["object_id"]: r["status"] for e in entities
                    for r in e["records"]}

        self.assertEqual(best("Norvik Logistics")[slip.object_id], "lead")
        self.assertEqual(best("Copper Leaf hand towel")[towel.object_id], "lead")
        found = best("Quill Works")
        self.assertEqual(found[exact.object_id], "match")
        self.assertEqual(found[near.object_id], "lead")   # a closer name was found

    def test_a_name_among_many_namesakes_of_its_common_word_is_found_by_its_rare_one(self):
        slip = EntityRow('e:s', 'Supplier:bc.vendors:{"id":"t1"}', "supplier",
                         "NORVIK LOGISITICS 9TH WAREHOUSE", "norvik logisitics 9th warehouse",
                         (), "norvik", 1.0, 1.0)
        word = EntityRow('e:w', 'Email:9', "person", "Norvik", "norvik", (), "norvik",
                         1.0, 1.0)

        class Index(FakeIndex):
            def by_terms(self, terms, scope, types=None):
                # the whole name's term finds only its namesakes; its rare word finds it
                return ([slip, word] if "norvik" in terms else []), {}

        entities, _ = er.Resolver(FakeBase(), Index()).resolve("scope", "actor",
                                                               "Norvik Logistics")
        found = {r["object"]["object_id"]: r["status"] for e in entities for r in e["records"]}
        self.assertEqual(found, {slip.object_id: "lead"})   # one word alone names nothing

    def test_two_firms_a_letter_apart_are_never_matched_and_one_plural_is(self):
        prater = row('Customer:bc.customers:{"id":"c1"}', "customer", "Prater Way Car Wash LP", [])
        crater = row('Customer:bc.customers:{"id":"c2"}', "customer", "Crater Car Wash", [])
        bolt = row('Supplier:bc.vendors:{"id":"s1"}', "supplier", "Bolt Fasteners", [])
        bolts = row('Supplier:bc.vendors:{"id":"s2"}', "supplier", "Bolts Fastener", [])
        rows = []

        class Index(FakeIndex):
            def by_terms(self, terms, scope, types=None):
                return rows, {}

        def found(text):
            entities, _ = er.Resolver(FakeBase(), Index()).resolve("scope", "actor", text)
            return {r["object"]["object_id"]: r["status"] for e in entities
                    for r in e["records"]}

        rows[:] = [prater, crater]
        self.assertEqual(found("Prater Car Wash"),
                         {prater.object_id: "lead", crater.object_id: "lead"})
        rows[:] = [bolt]
        self.assertEqual(found("Bolt Fastener"), {bolt.object_id: "match"})
        rows[:] = [bolt, bolts]   # two names differ from the asked only by a plural
        self.assertEqual(found("Bolt Fastener"),
                         {bolt.object_id: "lead", bolts.object_id: "lead"})

    def test_one_person_seen_in_several_mailboxes_is_one_person(self):
        people = [EntityRow(f"p:{n}", f"Email:{n}", "person", "Jo Park", "jo park",
                            ("jo.park@maple.example", "maple.example"), "", 1.0, 1.0)
                  for n in range(3)]

        class Index(FakeIndex):
            def by_terms(self, terms, scope, types=None):
                return people, {}

        entities, _ = er.Resolver(FakeBase(), Index()).resolve("scope", "actor", "Jo Park")
        self.assertEqual(len(entities), 1)
        self.assertEqual(len(entities[0]["records"]), 3)

    def test_no_index_is_a_gap_never_an_error(self):
        entities, gaps = er.Resolver(FakeBase(), None).resolve("scope", "actor", "Acme")
        self.assertEqual(entities, [])
        self.assertEqual(gaps[0]["reason"], "entity_index_unavailable")


class FindRoots(unittest.TestCase):
    def test_the_asks_first_read_roots_a_resolved_entity(self):
        calls = []

        def find(arguments):
            calls.append(arguments)
            if arguments.get("mode") == "entities":
                return {"status": "resolved", "entities": [
                    {"mention": "AB-CD-12X", "object_type": "Product", "confidence": 0.97,
                     "status": "match", "systems": ["Sage", "FileMaker"],
                     "records": [{"object": {"object_id": SAGE}, "system": "Sage",
                                  "confidence": 0.97, "status": "match"},
                                 {"object": {"object_id": FM}, "system": "FileMaker",
                                  "confidence": 0.97, "status": "match"},
                                 {"object": {"object_id": BC}, "system": "Business Central",
                                  "confidence": 0.75, "status": "lead"}]}]}
            return {"status": "not_found", "candidates": []}

        found = objective_runner.find_roots("Where is the rest of AB-CD-12X?", find)
        self.assertEqual(found["roots"][0]["object_id"], SAGE)
        self.assertEqual(found["roots"][0]["same_as"], [FM])
        self.assertEqual(found["roots"][0]["found_by"], "resolver")
        self.assertEqual(found["entities"][0]["mention"], "AB-CD-12X")
        # a lead record (a text-join identity link) is never stated as the same thing
        from operator_api import preread
        told = preread.brief(None, found["roots"], entities=found["entities"])
        same_line = next(line for line in told.splitlines() if "names one" in line)
        self.assertNotIn(BC, same_line)
        self.assertIn(BC, next(line for line in told.splitlines() if "Leads only" in line))

    def test_a_derived_record_is_offered_as_possible_never_rooted(self):
        from operator_api import preread
        entity = {"mention": "XY0103510", "object_type": "SalesInvoice", "confidence": 0.8,
                  "status": "derived", "systems": ["Sage"],
                  "records": [{"object": {"object_id": CPO_A}, "system": "Sage",
                               "confidence": 0.8, "status": "derived",
                               "why": ["possible: split from XY0101006/0103510"]}]}
        found = objective_runner.find_roots("did we get XY0103510?", lambda arguments: (
            {"status": "resolved", "entities": [entity]}
            if arguments.get("mode") == "entities" else {"status": "not_found"}))
        self.assertEqual(found["roots"], [])
        told = preread.brief(None, found["roots"], entities=found["entities"])
        self.assertIn("Possible only", told)
        self.assertIn(CPO_A, told)
        # what it was read out of is said beside it
        self.assertIn("split from XY0101006/0103510", told)
        self.assertNotIn("names one", told)

    def test_systems_are_those_of_matched_records(self):
        class Lead(FakeStore):
            def links_touching(self, scope, ids, types):
                return [{"link_type": "Product.same_record_as", "from_object": SAGE,
                         "to_object": 'Product:bc.items:{"id":"lead"}', "tier": "lead"}]
        base = FakeBase()
        base.store = Lead()
        entities, _ = er.Resolver(base, FakeIndex()).resolve("scope", "actor", "AB-CD-12X")
        records = {r["object"]["object_id"]: r for e in entities for r in e["records"]}
        self.assertEqual(records['Product:bc.items:{"id":"lead"}']["status"], "lead")


class SharedNumber(unittest.TestCase):
    """W-0070 (dev 11-10): "order 465" named a cancelled purchase order PO#0465 (the only
    one the entity index carries) while the sales order the question meant is known by
    its number alone; the Ask read the purchase order as the thing asked."""

    PO_IDX = 'PurchaseOrder:portal.app_purchase_order:{"id":"fm-po-488"}'
    SO_FM = 'SalesOrder:portal.fm_order_entry:{"id":465}'
    HIDDEN_SO = 'SalesOrder:portal.fm_order_entry:{"id":9465}'
    LOT = 'Lot:portal.fm_lot:{"id":465}'

    def resolver(self, store_rows, hidden=()):
        index_rows = [row(self.PO_IDX, "purchase_order", "PO465", ["PO#0465"])]

        class Index(FakeIndex):
            def by_identifier(self, forms, scope, types=None):
                forms = {f.upper() for f in forms}
                return [r for r in index_rows
                        if forms & {a.upper().replace("#0", "") for a in r.alias}], set()

            def split_candidates(self, digits, scope):
                return []

        class Store(FakeStore):
            asked = []

            def identifiers_by_value(self, scope, values, types=None, cap=None):
                # the statement's contract: the reader's scope, then the types, a set
                # order and the cap (StoreReader.identifiers_by_value)
                Store.asked.append(sorted(values))
                rows = [r for r in store_rows if r["normalized"] in values
                        and r["object_id"] not in hidden]
                if types is not None:
                    rows = [r for r in rows if r["object_id"].split(":", 1)[0] in types]
                if cap is not None:
                    rows = sorted(rows, key=lambda r: (r["object_id"].split(":", 1)[0],
                                                       r["object_id"], r["property"]))[:cap]
                return rows

        base = FakeBase()
        base.store = Store()
        return er.Resolver(base, Index()), Store

    @staticmethod
    def ident(oid, prop, value):
        return {"object_id": oid, "property": prop, "normalized": value, "raw": value,
                "identifier_kind": "fm_id"}

    def test_a_number_several_records_carry_names_none_of_them(self):
        resolver, _ = self.resolver([self.ident(self.SO_FM, "fm_id", "465"),
                                     self.ident(self.LOT, "fm_id", "465")])
        q = "Which assembly orders are making the goods for order 465?"
        entities, _ = resolver.resolve("scope", "actor", q)
        offered = {r["object"]["object_id"]: e for e in entities for r in e["records"]}
        # the sales order the index lacks is found; a lot sharing the number is no order
        self.assertEqual(set(offered), {self.PO_IDX, self.SO_FM})
        for entity in entities:
            self.assertEqual(entity["status"], "derived")
            self.assertTrue(entity["shares_number"])
            self.assertEqual(entity["records"][0]["why"][0], er.SHARED_WHY)
        found = objective_runner.find_roots(q, lambda arguments: (
            {"status": "resolved", "entities": entities}
            if arguments.get("mode") == "entities" else {"status": "not_found"}))
        self.assertEqual(found["roots"], [])   # never one picked by its number
        from operator_api import preread
        told = preread.brief(None, found["roots"], entities=found["entities"])
        self.assertIn('"order 465" is carried by several different records', told)
        self.assertIn(self.SO_FM, told)
        self.assertIn(self.PO_IDX, told)
        self.assertNotIn("Possible only", told)

    def test_many_other_records_sharing_the_number_never_crowd_out_the_order(self):
        # in the store a short number is carried by many lots, lines and entries; they
        # come back before the sales order, more of them than the resolver keeps
        others = [self.ident(f'Lot:portal.fm_lot:{{"id":{n}}}', "fm_id", "465")
                  for n in range(er.MAX_EXPAND + 5)]
        resolver, _ = self.resolver([*others, self.ident(self.SO_FM, "fm_id", "465")])
        entities, _ = resolver.resolve("scope", "actor", "what is the status of order 465?")
        offered = {r["object"]["object_id"] for e in entities for r in e["records"]}
        # never the one indexed purchase order as a silent match
        self.assertEqual(offered, {self.PO_IDX, self.SO_FM})
        self.assertEqual({e["status"] for e in entities}, {"derived"})

    def test_one_record_carrying_it_stays_the_match(self):
        resolver, _ = self.resolver([self.ident(self.HIDDEN_SO, "fm_id", "465")],
                                    hidden={self.HIDDEN_SO})
        entities, _ = resolver.resolve("scope", "actor", "what ships on order 465?")
        # a record the reader may not read neither splits the number nor is named
        self.assertEqual([e["status"] for e in entities], ["match"])
        self.assertNotIn("shares_number", entities[0])
        self.assertEqual([r["object"]["object_id"] for r in entities[0]["records"]],
                         [self.PO_IDX])

    def test_a_label_naming_one_kind_never_reads_the_store_numbers(self):
        resolver, store = self.resolver([self.ident(self.SO_FM, "fm_id", "465")])
        entities, _ = resolver.resolve("scope", "actor", "is PO 465 late?")
        self.assertEqual(store.asked, [])
        self.assertEqual([e["status"] for e in entities], ["match"])

    def test_a_quantity_is_never_looked_up_as_a_number(self):
        resolver, store = self.resolver([self.ident(self.SO_FM, "fm_id", "500")])
        resolver.resolve("scope", "actor", "Should we order 500 units of glycerin?")
        self.assertEqual(store.asked, [])

    def test_a_failing_store_read_keeps_the_index_answer(self):
        resolver, store = self.resolver([])

        def boom(scope, values, types=None, cap=None):
            raise RuntimeError("store down")
        resolver.base.store.identifiers_by_value = boom
        entities, gaps = resolver.resolve("scope", "actor", "order 465 status?")
        self.assertEqual([e["status"] for e in entities], ["match"])


if __name__ == "__main__":
    unittest.main()
