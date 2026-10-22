"""The cross-system sample's document kinds take a reference from a document's text, not
only its name, and count every text read or unread (gap-04f9b49ebae9, gap-f713876847b6,
gap-fdd76b96f8f6).

Live c89b2bbd: the SharePoint search found 14 documents and read the text of none; 13 were
never counted. Here a search finds documents whose names say nothing and whose text names
a closed root: mail and SharePoint each yield a reference, and a text the port could not
read is counted with its cause, never a silent 0. A kind its slice cut is searched again;
the port reads each hit's text once a run, so the first search's counts must stay.

Injected port over the live-shaped fixture world (the port's own reads are tested in
tests/test_document_text_reads.py); not live.
"""

from __future__ import annotations

import random
import unittest

from evals.oracle import BcReads, PortalReads, Sources
from evals.oracle import cross_sample as cs
from evals.oracle import keys as K
from evals.oracle.fixture_world import bc_record, build_live_shaped_world, build_world
from evals.oracle.generator import (MAX_LISTED_DOCUMENTS, candidate_split,
                                    portal_candidates)

RUN = "live-2099-01-02T03"
NOW = "2099-01-02T04:00:00Z"


class TextPort:
    """A document port as LiveDocuments answers: a hit's record carries its name in
    ``texts`` and, once read, its text in ``contents``; each hit new to the run is read
    once and counted (``texts_read``, ``texts_unread_by`` cause), a hit the run already
    read is returned with its contents and counted no more. ``docs``: kind -> handle ->
    (name, text, None when read, else the cause it is unread). ``tick``: called on each
    search (a clock to move)."""

    max_roots = 10_000   # every root searched

    def __init__(self, docs, tick=None):
        self.docs, self.tick, self.hits, self.reads = docs, tick, {}, []

    def search(self, kind, queries, *, hits, seed=""):
        if self.tick is not None:
            self.tick(kind)
        docs = self.docs.get(kind, {})
        results, records, unread = [], {}, {}
        read = 0
        for query in queries:
            found = sorted(h for h, (_, text, _) in docs.items() if query in text)[:hits]
            results.append({"query": query, "handles": found, "complete": True,
                            "withheld": 0})
            for handle in found:
                if handle not in self.hits:
                    name, text, cause = docs[handle]
                    self.reads.append(handle)
                    record = {"handle": handle, "texts": [name]}
                    if cause is None:
                        record["contents"] = [text]
                        read += 1
                    else:
                        unread[cause] = unread.get(cause, 0) + 1
                    self.hits[handle] = record
                records[handle] = self.hits[handle]
        return {"results": results, "records": list(records.values()),
                "source": [f"fake:{kind}"], "texts_read": read,
                "texts_unread": sum(unread.values()), "texts_unread_by": unread}


def accounted(search):
    return search.get("texts_read", 0) + search.get("texts_unread", 0)


class DocumentTextTest(unittest.TestCase):
    def setUp(self) -> None:
        world = build_live_shaped_world(documents=False)
        self.sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        closed = [c for c in portal_candidates(self.sources, "sales_order")
                  if c.family == "F1" and candidate_split(c) == "dev"][:2]
        self.assertEqual(len(closed), 2)
        self.one, self.two = (c.identifier for c in closed)

    def test_a_text_naming_a_root_is_a_reference_and_an_unread_text_says_why(self):
        port = TextPort({
            "mail": {"m-a": ("Weekly update", f"Please ship order {self.one} today.", None),
                     "m-b": ("Re: delivery", f"Order {self.two} delivered.", "unavailable")},
            "sharepoint": {
                "slip": ("packing-slip.txt", f"Packing slip\nOrder {self.one}", None),
                "fwd": ("forwarded.eml", f"Customer order {self.two}", None),
                "photo": ("pallet.jpg", f"Order {self.one}", "not_text_kind"),
                "gone": ("notes.txt", f"Order {self.two}", "unavailable")}})
        kinds = cs.sample(self.sources, run_id=RUN, documents=port, now=NOW).kinds
        for kind, naming, read, causes in (
                ("mail", 1, 1, {"texts_unread_unavailable": 1}),
                ("sharepoint", 2, 2, {"texts_unread_not_text_kind": 1,
                                      "texts_unread_unavailable": 1})):
            entry = kinds[kind]
            search = entry["search"]
            self.assertEqual(entry["status"], "read", entry)
            self.assertGreaterEqual(entry["references"], 1, entry)
            # only a text read names a root: the unread ones name none
            self.assertEqual(search["documents_naming_one"], naming, search)
            self.assertEqual(search["texts_read"], read, search)
            self.assertEqual({k: v for k, v in search.items()
                              if k.startswith("texts_unread_")}, causes, search)
            self.assertEqual(accounted(search), len(port.docs[kind]), search)
        self.assertEqual(sorted(port.reads), sorted(set(port.reads)))   # once a run

    def test_a_document_naming_two_roots_is_no_candidate_one_naming_one_is(self):
        both = f"Orders {self.one} and {self.two} shipped."
        port = TextPort({
            "mail": {"m-one": ("Re: one", f"Please ship order {self.one}.", None),
                     "m-two": ("Re: both", both, None)},
            "sharepoint": {"slip": ("slip.txt", f"Packing slip\nOrder {self.two}", None),
                           "pair": ("pair.txt", both, None)}})
        kinds = cs.sample(self.sources, run_id=RUN, documents=port, now=NOW).kinds
        for kind in ("mail", "sharepoint"):
            entry = kinds[kind]
            self.assertEqual(entry["status"], "read", entry)
            self.assertEqual(entry["search"]["documents_naming_one"], 1, entry)
            self.assertGreaterEqual(entry["candidates"], 1, entry)
            self.assertEqual(entry["references"], 1, entry)

    def test_scanned_and_scan_failed_searches_stay_in_the_funnel(self):
        class Scanned(TextPort):
            def search(self, kind, queries, **kw):
                answer = super().search(kind, queries, **kw)
                for i, r in enumerate(answer["results"]):
                    if kind == "mail":
                        answer["results"][i] = ({**r, "scan": True, "excluded": 2} if i % 2
                                                else {"query": r["query"], "refused": "tenant",
                                                      "scan_failed": True})
                return answer
        port = Scanned({"mail": {"m": ("s", f"Order {self.one}", None)}})
        search = cs.sample(self.sources, run_id=RUN, documents=port, now=NOW).kinds["mail"]["search"]
        self.assertGreater(search["search_scanned"], 0, search)
        self.assertGreater(search["search_scan_failed"], 0, search)
        self.assertEqual(search["search_scan_failed"], search["search_refused_tenant"], search)
        self.assertGreater(search["documents_excluded"], 0, search)

    def test_a_search_again_keeps_the_texts_the_first_read(self):
        bc = cs._sorted_roots(cs.bc_roots(self.sources, 50, {}))
        self.assertTrue(bc)
        number = cs._candidate(bc[0]).identifier
        now = [0.0]
        searched = []

        def tick(kind):   # the first SharePoint search outlasts its slice (at most 1/8)
            if kind == "sharepoint":
                if not searched:
                    now[0] += 50_000.0
                searched.append(now[0])

        port = TextPort({"sharepoint": {
            "inv": ("statement.txt", f"Statement\nInvoice {number}", None),
            "scan": ("scan.pdf", f"Invoice {number}", "ocr_unavailable")}}, tick)
        clock = cs.KindClock(lambda: 100_000.0 - now[0], lambda: now[0])
        shared = cs.sample(self.sources, run_id=RUN, documents=port, now=NOW,
                           clock=clock).kinds["sharepoint"]
        search = shared["search"]
        # searched twice: the first search left the BC root it found unchecked
        # (kind_budget), the second, its slice not spent, checked it
        self.assertEqual(len(searched), 4)   # the roots' numbers and their fallbacks, twice
        self.assertEqual((shared["status"], shared["references"]), ("read", 1), shared)
        self.assertEqual(sorted(port.reads), ["inv", "scan"])
        self.assertEqual((search["texts_read"], search["texts_unread"],
                          search["texts_unread_ocr_unavailable"]), (1, 1, 1), search)


class FewHitsPort(TextPort):
    """The live SharePoint search as daily bb20ab0f met it (2026-10-18 diagnostic: 47 of 51
    root queries found nothing, 4 found one document naming no root): each search answers
    the ``noise`` documents whatever was asked (Graph's name-and-text match), and
    ``list_documents`` lists ``listed`` (handle -> (name, text)) for SharePoint as the drive
    listing does, None for mail (``listings``: each kind asked)."""

    max_roots = 2   # few roots searched, as live (24 of 26460)

    def __init__(self, noise, listed):
        super().__init__({"sharepoint": noise})
        self.listed, self.listings = listed, []

    def search(self, kind, queries, *, hits, seed=""):
        answer = super().search(kind, [""] * len(queries), hits=hits, seed=seed)
        for result, query in zip(answer["results"], queries):
            result["query"] = query   # "" is in every text: each query finds the noise
        return answer

    def list_documents(self, kind, limit, *, seed=""):
        self.listings.append((kind, limit))
        if kind != "sharepoint":
            return None
        records = [{"handle": h, "texts": [name], "contents": [text]}
                   for h, (name, text) in sorted(self.listed.items())][:limit]
        return {"records": records, "source": [f"fake:{kind}/listing"], "complete": True,
                "withheld": 1, "texts_read": len(records)}


class SharePointFewHitsTest(unittest.TestCase):
    """gap-fdd76b96f8f6 (daily bb20ab0f: sharepoint no_candidate_in_sample, 3 documents):
    a stratum whose searches find few documents widens its candidates the general way. A
    document found (searched or listed) names a root of the whole searchable pool, not only
    the few searched, under the same span rules: each root once however many documents
    name it, a number two records share names neither, a BC twin names its Portal
    record. Injected port over the live-shaped fixture world; not live."""

    def setUp(self) -> None:
        from evals.oracle.fixture_world import drain_purchase_orders_to_bc
        world = build_live_shaped_world(documents=False)
        drained = next(r for r in world.relations["public.app_purchase_order"]
                       if r["status"] == "Complete")
        drain_purchase_orders_to_bc(world, [drained["id"]])
        world.bc["purchaseOrders"] += [   # one number, two companies' orders
            bc_record("purchaseOrders", company, 990_000 + company, number="FXPO-SHARED-7701",
                      vendorNumber="FXV001", fullyReceived=True) for company in (0, 1)]
        self.sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        so = [c for c in portal_candidates(self.sources, "sales_order") if c.family == "F1"]
        self.so = so[-1].identifier
        self.po = drained["po_number"]
        self.named = {
            "a.pdf": ("Order confirmation.pdf", f"Confirmation\nSales order {self.so}"),
            "b.pdf": (f"{self.so} packing list.pdf", "Packing list"),
            "twin.pdf": (f"PO {self.po}.pdf", f"Purchase order {self.po}"),
            "shared.pdf": ("FXPO-SHARED-7701.pdf", "Purchase order FXPO-SHARED-7701")}

    def run_kind(self, port):
        drawn = cs.sample(self.sources, run_id=RUN, documents=port, now=NOW)
        return drawn.kinds["sharepoint"], sorted(
            (i.relation, i.identifier) for i in drawn.items if i.kind == "sharepoint")

    def check(self, entry, items):
        self.assertEqual(entry["status"], "read", entry)
        # one reference per root named: the sales order (two documents) and the PO the
        # BC order twins, never the BC twin, never the shared number
        self.assertEqual(entry["references"], 2, entry)
        self.assertEqual(items, sorted([("public.app_purchase_order", self.po),
                                        ("public.app_sales_order", self.so)]), entry)
        self.assertEqual(entry["search"]["documents_naming_one"], 3, entry)
        self.assertGreaterEqual(entry["skipped"].get("not_unique", 0), 1, entry)

    def test_few_search_hits_list_the_drive_and_name_unsearched_roots(self):
        port = FewHitsPort({}, self.named)
        entry, items = self.run_kind(port)
        self.assertEqual(port.listings, [("mail", MAX_LISTED_DOCUMENTS),
                                         ("sharepoint", MAX_LISTED_DOCUMENTS)])
        self.check(entry, items)
        search = entry["search"]
        self.assertEqual(search["listed_documents"], len(self.named), search)
        self.assertEqual(entry["candidates"], len(self.named), entry)
        self.assertIn("fake:sharepoint/listing", entry["tried"], entry)
        self.assertEqual(entry["withheld"], 1, entry)

    def test_a_search_hit_names_any_searchable_root_and_no_listing_follows(self):
        port = FewHitsPort({h: (name, text, None) for h, (name, text) in self.named.items()},
                           {"other.pdf": ("x.pdf", f"Sales order {self.so}")})
        entry, items = self.run_kind(port)
        # the searches named a root: no SharePoint listing (mail named none, asked)
        self.assertEqual(port.listings, [("mail", MAX_LISTED_DOCUMENTS)])
        self.check(entry, items)
        self.assertNotIn("listed_documents", entry["search"], entry)

    def test_a_listing_naming_nothing_stays_no_candidate_in_sample_with_its_counts(self):
        port = FewHitsPort({}, {"memo.txt": ("memo 4471.txt", "Minutes 2098-11-02")})
        entry, items = self.run_kind(port)
        self.assertEqual(items, [])
        self.assertEqual((entry["status"], entry["reason"]),
                         ("no_candidate_in_sample", "no_document_names_root"), entry)
        self.assertEqual(entry["search"]["listed_documents"], 1, entry)
        self.assertEqual(entry["search"]["documents_naming_one"], 0, entry)

    def test_a_failed_listing_says_so_and_a_budget_stop_is_a_budget_stop(self):
        class Failing(FewHitsPort):
            error = OSError

            def list_documents(self, kind, limit, *, seed=""):
                super().list_documents(kind, limit, seed=seed)
                raise self.error("listing read failed")

        port = Failing({}, self.named)
        entry, items = self.run_kind(port)
        self.assertEqual(items, [])
        self.assertEqual((entry["status"], entry["reason"]), ("unreadable", "listing_failed"))
        self.assertFalse(entry["complete"], entry)

        class BudgetStop(Exception):
            pass
        BudgetStop.__name__ = cs.BUDGET_STOP
        port = Failing({}, self.named)
        port.error = BudgetStop
        entry, _ = self.run_kind(port)
        self.assertEqual((entry["status"], entry.get("blocked")), ("unavailable", "budget"))

    def test_listed_documents_naming_n_roots_keep_each_once(self):
        for n in (1, 10, 60):
            world = build_live_shaped_world(documents=False)
            orders = [bc_record("purchaseOrders", k % 2, 980_000 + k,
                                number=f"FXPO-LISTED-{5000 + k}", vendorNumber="FXV001",
                                fullyReceived=True) for k in range(n)]
            world.bc["purchaseOrders"] += orders
            sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
            listed = {f"po-{k}.pdf": (f"{o['fields']['number']}.pdf", "Purchase order")
                      for k, o in enumerate(orders)}
            listed |= {f"copy-{k}.pdf": (f"copy of {o['fields']['number']}.pdf", "")
                       for k, o in enumerate(orders)}
            port = FewHitsPort({}, listed)
            drawn = cs.sample(sources, run_id=RUN, documents=port, now=NOW, count=28,
                              bc_sample_limit=100)
            entry, search = drawn.kinds["sharepoint"], drawn.kinds["sharepoint"]["search"]
            items = [i.identifier for i in drawn.items if i.kind == "sharepoint"]
            with self.subTest(n=n):
                self.assertEqual(search["listed_documents"], 2 * n, search)
                self.assertEqual(search["documents_naming_one"], 2 * n, search)
                self.assertEqual(search.get("documents_naming_many", 0), 0, search)
                # each order a reference once, however many documents name it; the
                # items drawn are the kind's quota of them
                self.assertEqual(entry["references"], n, entry)
                self.assertEqual(len(items), len(set(items)), items)
                self.assertLessEqual(set(items), {o["fields"]["number"] for o in orders})
                self.assertLessEqual(len(items), n, items)
                self.assertGreaterEqual(len(items), 1, items)

    def test_a_listing_cut_by_no_limit_of_its_own_says_so_never_a_sample(self):
        class Cut(FewHitsPort):
            def list_documents(self, kind, limit, *, seed=""):
                listed = super().list_documents(kind, limit, seed=seed)
                return listed and {**listed, "complete": False}   # a gap, not a cap

        port = Cut({}, {"memo.txt": ("memo 4471.txt", "Minutes 2098-11-02")})
        entry, items = self.run_kind(port)
        self.assertEqual(items, [])
        self.assertEqual((entry["status"], entry["reason"]),
                         ("incomplete", "listing_incomplete"), entry)
        self.assertEqual(entry["search"]["listing_incomplete"], 1, entry)
        self.assertFalse(entry["complete"], entry)

    def test_a_document_naming_a_root_and_an_unsearched_closed_record_names_neither(self):
        """Uniqueness is keyed over every closed record read, searchable or not: a
        document naming a sales order (searchable) and a purchase order of a kind not
        drawn names neither; the sales order alone is named once."""
        po = next(c for c in portal_candidates(self.sources, "purchase_order")
                  if c.family == "F1" and c.identifier != self.po).identifier
        roots = [r for kind in ("sales_order", "purchase_order")
                 for r in cs.portal_roots(self.sources, kind)]
        for listed, expected, many in (
                ({"both.pdf": (f"{self.so} for PO {po}.pdf", "")}, [], 1),
                ({"so.pdf": (f"{self.so}.pdf", "")}, [self.so], 0)):
            skipped, funnel, read = {}, {}, {}
            pairs = cs.document_references(
                self.sources, FewHitsPort({}, listed), "sharepoint", roots, skipped,
                funnel, read, {"sales_order"}, run_id=RUN, now=NOW)
            with self.subTest(listed=sorted(listed)):
                self.assertEqual([identifier for _, identifier in pairs], expected, funnel)
                self.assertEqual(funnel["documents_naming_one"], len(expected), funnel)
                self.assertEqual(funnel.get("documents_naming_many", 0), many, funnel)

    def test_a_kind_the_port_does_not_list_has_no_listing_counts(self):
        port = FewHitsPort({}, self.named)
        mail = cs.sample(self.sources, run_id=RUN, documents=port, now=NOW).kinds["mail"]
        self.assertIn(("mail", MAX_LISTED_DOCUMENTS), port.listings)
        self.assertFalse({"listed_documents", "listing_failed"} & set(mail["search"]), mail)
        self.assertEqual((mail["status"], mail["reason"]),
                         ("no_candidate_in_sample", "no_document_names_root"), mail)


class SalesOrderPoMappingTest(unittest.TestCase):
    """gap-b01a86b2f438: BC holds none of the Portal sales-order numbers, but its sales
    order carries the customer PO the Portal wrote (``so.bc_sales_order_po``). One declared
    edge, no per-root branch."""

    def setUp(self) -> None:
        self.world = build_world()
        self.world.bc["salesOrders"], self.world.bc["salesInvoices"] = [], []
        sources = self.sources()
        roots = cs.portal_roots(sources, "sales_order")
        by_po: dict[str, int] = {}
        for r in roots:
            po = r.node.row.get("po_reference")
            by_po[po] = by_po.get(po, 0) + 1
        self.root = next(r for r in roots if by_po[r.node.row["po_reference"]] == 1)
        self.po = self.root.node.row["po_reference"]

    def sources(self) -> Sources:
        return Sources(PortalReads(self.world.read_relation), BcReads(self.world.bc_port()))

    def reference(self, root=None):
        sources, skipped = self.sources(), {}
        root = root or self.root
        edges = cs.reference_edges(sources, root.relation)
        found = cs.record_reference(sources, root, edges, random.Random(0),
                                    cs._Budget(50), skipped, {})
        return found, skipped

    def test_the_declared_mapping_is_one_general_edge(self):
        edge = next(e for e in K.REFERENCE_EDGES if e.name == "so.bc_sales_order_po")
        self.assertEqual((edge.source, edge.source_field, edge.target, edge.target_field),
                         (K.SO, "po_reference", "bc:salesOrders", "externalDocumentNumber"))

    def test_a_number_reaching_bc_only_through_the_mapping_gets_a_reference(self):
        self.world.bc["salesOrders"] = [bc_record(
            "salesOrders", 0, 1, number="BC-ONLY-1", externalDocumentNumber=self.po)]
        found, _ = self.reference()
        self.assertEqual(found, (self.po, "so.bc_sales_order_po", "bc"))

    def test_a_number_two_bc_records_share_names_neither(self):
        self.world.bc["salesOrders"] = [
            bc_record("salesOrders", 0, 1, number="BC-ONLY-1",
                      externalDocumentNumber=self.po),
            bc_record("salesOrders", 0, 2, number="BC-ONLY-2",
                      externalDocumentNumber=self.po)]
        found, skipped = self.reference()
        self.assertIsNone(found)
        self.assertEqual(skipped.get("not_unique"), 1)
        # held in two companies: names neither either (the oracle's span check)
        self.world.bc["salesOrders"][1] = bc_record(
            "salesOrders", 1, 2, number="BC-ONLY-2", externalDocumentNumber=self.po)
        self.assertIsNone(self.reference()[0])

    def test_no_mapping_no_reference(self):
        self.world.bc["salesOrders"] = [bc_record(
            "salesOrders", 0, 1, number="BC-ONLY-1", externalDocumentNumber="UNRELATED-PO")]
        found, _ = self.reference()
        self.assertIsNone(found)

    def test_a_po_two_portal_orders_share_names_no_root(self):
        shared = next(r for r in cs.portal_roots(self.sources(), "sales_order")
                      if r.node.row["po_reference"] == "FX Person")
        self.world.bc["salesOrders"] = [bc_record(
            "salesOrders", 0, 1, number="BC-ONLY-1", externalDocumentNumber="FX Person")]
        found, skipped = self.reference(shared)
        self.assertIsNone(found)
        self.assertEqual(skipped.get("not_unique"), 1)

    def test_a_bc_sales_order_root_never_draws_through_the_shared_field(self):
        self.world.bc["salesOrders"] = [bc_record(
            "salesOrders", 0, 1, number="BC-ONLY-1", externalDocumentNumber=self.po)]
        sources = self.sources()
        root = cs._bc_set_roots("bc_sales_order", "salesOrders",
                                sources.bc.sample("salesOrders", 5),
                                cs._funnel_row(None, "bc:salesOrders"))[0]
        edges = [e for e in cs.reference_edges(sources, root.relation)
                 if e[0] == "so.bc_sales_order_po"]
        self.assertEqual(len(edges), 1)
        found = cs.record_reference(sources, root, edges, random.Random(0), cs._Budget(50),
                                    {}, {})
        self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
