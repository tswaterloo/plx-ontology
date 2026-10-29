"""Root-first mail and SharePoint strata over live-shaped readers find their candidates
(gap-04f9b49ebae9, gap-f713876847b6; a108c9b752a2). Fixture world
and fake Graph readers behind the real ``LiveDocuments``; injected, not live.

Live 2bf15f5 and 2d141d7 searched 23-24 closed roots per source and found 0-1 mail and
16-17 SharePoint files, none naming a root. The readers here hold what the live sources
showed: documents for the newest transactions only, and a purchase-order number written
apart from its label ("FX 2231" for FX#2231, as a synthetic "PO 1234" for PO#1234). The
whole mailbox is searched, its drafts, deleted items and junk left out.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import unittest

from evals.oracle import BcReads, PortalReads, Sources
from evals.oracle import keys as K
from evals.oracle.fixture_world import World, bc_record, build_live_shaped_world
from evals.oracle.generator import (BC_DOCUMENT_ROOT_SETS, MAX_ROOT_SEARCHES, PORTAL_STRATA,
                                    RECENT_WINDOW, bc_root, candidate_split,
                                    document_candidates, generate, portal_candidates,
                                    root_queries, root_when, search_roots, search_status)
from integrations.live_source_guard import NotVisible
from integrations.tests.test_bc_purchase import BUYER
from scripts import eval_oracle_dev as dev

ROOT = Path(__file__).resolve().parents[2]
RUN = "live-2099-01-02T03"
SIZE = 240
RECENT = 100   # the documents name the transactions of the newest RECENT numbers only
FOLDERS = {"inbox": "fid-inbox", "sentitems": "fid-sent", "drafts": "fid-drafts",
           "deleteditems": "fid-deleted", "junkemail": "fid-junk"}
# the folder of the n-th message: every third one sits where nothing is sent or received
MESSAGE_FOLDERS = ("sentitems", "inbox", "junkemail", "sentitems", "drafts", "inbox",
                   "deleteditems", "sentitems", "inbox")
LEFT_OUT = {"drafts", "deleteditems", "junkemail"}


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z]+", text.casefold()) if t]


def _found(query: str, text: str) -> bool:
    """Graph search as a phrase of the query's word tokens ("FX#2231" finds "FX 2231")."""
    want, have = _tokens(query), _tokens(text)
    return bool(want) and any(have[i:i + len(want)] == want for i in range(len(have)))


class Mail:
    """A mailbox with well-known folders; ``list_messages`` searches one folder or all."""

    def __init__(self, messages):
        self.messages = messages

    @contextlib.contextmanager
    def directory_cache(self):
        yield

    def resolve_folder(self, actor, mailbox, folder):
        if folder not in FOLDERS:
            raise NotVisible()
        return FOLDERS[folder]

    def list_messages(self, actor, mailbox, *, search, max_items, folder=None, **kwargs):
        items = [m for m in self.messages if _found(search, m["subject"] + " " + m["body"])
                 and (folder is None or m["parent_folder_id"] == FOLDERS[folder])]
        return {"items": [{k: v for k, v in m.items() if k != "body"}
                          for m in items[:max_items]],
                "completeness": {"truncated": len(items) > max_items}}

    def get_message(self, actor, mailbox, message_id, *, include_body, expected_folder_id):
        message = next(m for m in self.messages if m["id"] == message_id)
        return {"change_key": message["change_key"], "body_text": message["body"]}


class Drive:
    def __init__(self, files):
        self.files = files

    def search(self, actor, query, *, max_items):
        items = [f for f in self.files if _found(query, f["name"])]
        return {"items": items[:max_items], "completeness": {"truncated": len(items) > max_items}}


def live_shaped_sources():
    """The live-shaped world grown to SIZE transactions, each purchase order and FM receipt
    dated by its number (the newest last), and readers holding documents for the newest
    RECENT of each: a mail naming the purchase order apart from its label (in the folders
    of MESSAGE_FOLDERS in turn), a scanned CoA named by the receipt's lab id. Also the
    purchase-order numbers named only in a folder left out."""
    world = build_live_shaped_world(SIZE, documents=False)
    start = datetime(2098, 1, 1, tzinfo=timezone.utc)
    for relation, column in ((K.PO, "updated_at"), (K.FM_RECEIPT_RAW, "audittimestampmodified")):
        for n, row in enumerate(world.relations[relation]):
            row[column] = (start + timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")
    messages, files, left_out = [], [], set()
    for row in world.relations[K.PO][-RECENT:]:
        label, number = row["po_number"].split("#")
        n = len(messages)
        folder = MESSAGE_FOLDERS[n % len(MESSAGE_FOLDERS)]
        if folder in LEFT_OUT:
            left_out.add(row["po_number"])
        messages.append({"citation": f"graph-mail://fx-box/m{n}@ck{n}", "id": f"m{n}",
                         "change_key": f"ck{n}", "parent_folder_id": FOLDERS[folder],
                         "subject": "Order confirmation", "body_preview": "",
                         "body": f"We confirm your order {label} {number}. Ships Friday."})
    for row in world.relations[K.FM_RECEIPT_RAW][-RECENT:]:
        n = len(files)
        files.append({"citation": f"sharepoint://fx-drive/f{n}@e{n}", "id": f"f{n}",
                      "kind": "file", "name": f"{row['idlabid']} CoA scan.jpg"})
    sources, _, configured, _ = dev.build_fixture(world)
    sources = type(sources)(sources.portal, BcReads(world.bc_port()))
    docs = dev.LiveDocuments(BUYER, mail_reader=Mail(messages), mailbox="box",
                             drive_reader=Drive(files), drive_paths=["/Documents"])
    return sources, docs, configured, left_out


def run(run_id=RUN):
    """The dev run over fresh live-shaped readers: (report, left-out PO numbers, the
    queries of each document search, by kind)."""
    sources, docs, configured, left_out = live_shaped_sources()
    asked: list[tuple[str, tuple[str, ...]]] = []

    class Recorded:
        def search(self, kind, queries, **kwargs):
            asked.append((kind, tuple(queries)))
            return docs.search(kind, queries, **kwargs)

    report, items = dev.execute(sources, Recorded(), run_id=run_id, now=dev.FIXTURE_NOW,
                                count=30, audit=10, id_suffix="live", evidence_note=None,
                                evidence_label="injected-transport", configured=configured)
    return report, items, left_out, asked, sources


class StrataCandidatesTest(unittest.TestCase):
    def test_live_shaped_mail_and_sharepoint_name_closed_roots(self):
        report, items, left_out, _, _ = run()
        strata = report["strata"]
        # the live spec's floors for the document strata (the whole spec is checked over
        # the fixture run in tests.test_eval_oracle_dev)
        mins = json.loads((ROOT / "devloop/live-checks/eval.oracle_dev.json")
                          .read_text(encoding="utf-8"))["expect"]["json_min"]
        for kind in ("mail", "sharepoint"):
            entry = strata[kind]
            self.assertEqual(entry["status"], "read", entry)
            self.assertGreaterEqual(entry["candidates"], mins[f"strata.{kind}.candidates"])
            self.assertGreaterEqual(entry["funnel"]["documents_naming_one"], 1, kind)
            self.assertEqual(entry["funnel"]["roots_searched"], MAX_ROOT_SEARCHES, kind)
            self.assertGreaterEqual(MAX_ROOT_SEARCHES,
                                    mins[f"strata.{kind}.funnel.roots_searched"])
        # the mail names its purchase order apart from the label: a lead, never exact
        self.assertGreaterEqual(strata["mail"]["funnel"]["span_labelled"], 1)
        self.assertNotIn("span_exact", strata["mail"]["funnel"])
        self.assertEqual(report["unexplained_strata"], [])
        self.assertEqual((report["counts"]["items"], report["counts"]["schema_valid"],
                          report["counts"]["deterministic"]), (30, 30, 30))

    def test_drafts_deleted_items_and_junk_never_license_a_candidate(self):
        # Every closed dev root searched: mail in the folders left out is found and counted
        # (documents_excluded), and no purchase order named only there is a candidate.
        sources, docs, _, left_out = live_shaped_sources()
        pool = [c for s in PORTAL_STRATA for c in portal_candidates(sources, s)
                if candidate_split(c) == "dev"]
        self.assertTrue(left_out & {c.identifier for c in pool if c.family == "F1"})
        funnel, read = {}, {}
        found = document_candidates(sources, docs, "mail", pool, 10_000, funnel, read,
                                    run_id=RUN, now=dev.FIXTURE_NOW)
        named = {c.identifier for c in found}
        self.assertTrue(named)
        self.assertFalse(named & left_out)
        self.assertGreaterEqual(funnel["documents_excluded"], len(left_out & {
            c.identifier for c in pool if c.family == "F1"}))

    def test_a_run_id_draws_its_own_sample_of_the_newest_roots(self):
        # Two runs of one id search the same roots; another id searches others, all from
        # each stratum's newest window (RECENT_WINDOW dated roots), never strict newest-first.
        first = run()[3]
        again, other = run()[3], run("live-2099-01-02T04")[3]
        exact = {kind: next(q for k, q in first if k == kind) for kind in ("mail", "sharepoint")}
        self.assertEqual(first, again)
        self.assertNotEqual(exact["mail"], next(q for k, q in other if k == "mail"))
        sources = run()[4]
        ordered = {r["fields"].get("orderNumber") for entity_set in ("purchaseInvoices",
                                                                       "purchaseReceipts")
                   for r in sources.bc.sample(entity_set, 10_000)}
        for stratum, prefix in (("purchase_order", "FX#"), ("receipt", "FX9")):
            roots = [c for c in portal_candidates(sources, stratum)
                     if c.family == "F1" and candidate_split(c) == "dev"]
            dated = sorted(roots, key=lambda c: root_when(sources, c, dev.FIXTURE_NOW),
                           reverse=True)
            window = {c.identifier for c in dated[:RECENT_WINDOW]}
            # BC invoices carry the PO numbers too (orderNumber), searched for as their names
            searched = {q for q in exact["mail"] if q.startswith(prefix)} - ordered
            self.assertTrue(searched, stratum)
            self.assertLessEqual(searched, window, stratum)
            self.assertNotEqual(searched, {c.identifier for c in dated[:len(searched)]})

    def test_a_refusal_naming_no_query_never_counts_a_failed_root_refused(self):
        # 6 roots answered, 12 whose searches failed, 6 refused by refusals naming no query:
        # the failed roots are unavailable (search_failed), not refused, so the stratum is
        # not mostly refused.
        world = build_live_shaped_world()
        sources = Sources(PortalReads(world.read_relation), None)
        pool = [c for s in PORTAL_STRATA for c in portal_candidates(sources, s)]
        roots = search_roots(pool, RUN)[:MAX_ROOT_SEARCHES]
        names = [set(root_queries(sources, r)) for r in roots]
        answered, failed = set().union(*names[:6]), set().union(*names[6:18])
        empty = World({}, documents={"mail": []}).documents()

        class Port:
            def search(self, kind, queries, *, hits, seed=""):
                answer = empty.search(kind, [q for q in queries if q in answered], hits=hits)
                answer["results"] += [{"query": q, "failed": "unavailable"} if q in failed
                                      else {"refused": "tenant"}
                                      for q in queries if q not in answered]
                return answer

        funnel, read = {}, {}
        found = document_candidates(sources, Port(), "mail", pool, funnel=funnel, read=read,
                                    run_id=RUN)
        self.assertEqual(found, [])
        self.assertEqual((funnel["roots_asked"], funnel["roots_searched"],
                          funnel["roots_refused"]), (24, 6, 6))
        self.assertEqual(search_status(read, funnel), ("unreadable", "search_failed"))

    def test_closed_bc_orders_and_posted_receipts_are_roots_the_documents_may_name(self):
        # Live 2b0ee89: the purchasing mail names POs above every Portal PO row read, and
        # 5504 of 5866 PO headers carried FM labels counted out of vocabulary though the
        # lines decide closure. Here the mail names only BC documents no Portal row has:
        # each closed one in the dev split is a mail candidate once (N = 1/10/60 of each
        # set), an open order never; a set whose read fails leaves the others' roots.
        def world_of(n):
            world = build_live_shaped_world()
            closed, open_ = [], []
            for k in range(n):
                receipt = bc_record("purchaseReceipts", k % 2, 900_000 + k,
                                    number=f"FXPR{7100 + k}", postingDate="2098-12-01")
                order = bc_record("purchaseOrders", k % 2, 910_000 + k,
                                  number=f"FXPO{7200 + k}", fullyReceived=k % 2 == 0)
                world.bc["purchaseReceipts"].append(receipt)
                world.bc["purchaseOrders"].append(order)
                (closed if k % 2 == 0 else open_).append(("purchaseOrders", order))
                closed.append(("purchaseReceipts", receipt))
            world.docs["mail"] = [
                {"handle": f"graph-mail://fx-box/bc-{k}@x",
                 "texts": [f"Re: {record['fields']['number']} delivered"]}
                for k, (_, record) in enumerate(closed + open_)]
            return world, closed

        def dev_roots(closed):
            return [c for c in (bc_root(s, r, "bc_document") for s, r in closed)
                    if candidate_split(c) == "dev"]

        for n in (1, 10, 60):
            world, closed = world_of(n)
            sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
            generated = generate(sources, run_id=RUN, now=dev.FIXTURE_NOW,
                                 documents=world.documents(), bc_sample_limit=200)
            self.assertEqual(generated.strata["bc_invoice"]["document_roots"], len(closed))
            mail = generated.strata["mail"]
            self.assertEqual(mail["candidates"], len(dev_roots(closed)), (n, mail))
            self.assertEqual(mail["funnel"]["documents_naming_one"], len(dev_roots(closed)))
            # the FM labels the Portal's PO writers copy are no unknown words
            self.assertEqual(generated.strata["purchase_order"]["status_out_of_vocabulary"], 0)
        self.assertTrue(dev_roots(closed))
        inner = world.bc_port()

        class NoReceipts:
            def __getattr__(self, name):
                return getattr(inner, name)

            def sample(self, entity_set, limit):
                if entity_set == "purchaseReceipts":
                    raise ConnectionError("BC down")
                return inner.sample(entity_set, limit)

        generated = generate(Sources(PortalReads(world.read_relation), BcReads(NoReceipts())),
                             run_id=RUN, now=dev.FIXTURE_NOW, documents=world.documents(),
                             bc_sample_limit=200)
        orders = [c for c in dev_roots(closed) if c.root_relation == "bc.purchase_orders"]
        self.assertEqual(generated.failures, {"bc:purchaseReceipts.sample": "ConnectionError"})
        self.assertEqual(generated.strata["mail"]["candidates"], len(orders))
        # The live port reads those sets newest first: BC sorts posted receipts by posting
        # date and sales orders by order date; purchase orders, which the grammar sorts by
        # no field, are read one wide page and the newest by order date kept, so the mail
        # stratum sees recent roots (an oldest-first page held none the mail names).
        asked = {}

        class Reader:
            def list_entities(self, actor, company, entity_set, **kwargs):
                asked[entity_set] = (kwargs.get("orderby"), kwargs["max_items"])
                return {"items": [{"company_id": company, "entity_set": entity_set,
                                   "id": f"r{k}", "citation": f"bc://x/{k}",
                                   "fields": {"number": f"N{k}",
                                              "orderDate": f"2098-0{1 + k % 9}-01"}}
                                  for k in range(30)], "completeness": {}}

        port = dev.LiveBcPort(Reader(), BUYER)
        port._companies = [{"id": "c1"}]
        sampled = {s: port.sample(s, 10) for s in ("salesInvoices", *BC_DOCUMENT_ROOT_SETS)}
        self.assertEqual(asked, {"salesInvoices": (None, 10),
                                 "salesOrders": ("orderDate desc", 10),
                                 "purchaseOrders": (None, dev.BC_DATED_READ),
                                 "purchaseReceipts": ("postingDate desc", 10)})
        orders = sampled["purchaseOrders"]["records"]
        self.assertEqual(len(orders), 10)
        # months 9, 8 and 7 hold three records each: the tenth newest is of month 6
        self.assertEqual(sorted(r["fields"]["orderDate"] for r in orders)[:2],
                         ["2098-06-01", "2098-07-01"])
        self.assertFalse(sampled["purchaseOrders"]["complete"])   # 20 older ones left

    def test_a_portal_po_and_its_bc_twin_are_one_root_another_vendors_order_names_neither(self):
        # The Portal's BC drain numbers the BC order by the
        # Portal PO number, so a mail naming that number named "two records" and lost the PO.
        # The BC order the walk from the PO finds (its own vendor, one company) is the PO's
        # twin: the mail names the PO. A same-numbered BC order of another vendor is a record
        # of its own: the number names neither (fail closed).
        world = build_live_shaped_world()
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        portal = [c for c in portal_candidates(sources, "purchase_order") if c.family == "F1"]
        rows = {r["po_number"]: r for r in world.relations[K.PO]}
        codes = {r["id"]: r["code"] for r in world.relations[K.SUPPLIER]}
        twin_of, other_vendor = portal[0], portal[1]
        for serial, (c, same) in enumerate(((twin_of, True), (other_vendor, False))):
            vendor = codes[rows[c.identifier]["supplier_id"]]
            world.bc["purchaseOrders"].append(bc_record(
                "purchaseOrders", 0, 950_000 + serial, number=c.identifier,
                vendorNumber=vendor if same else vendor + "X", fullyReceived=True))
        world.docs["mail"] = [{"handle": f"graph-mail://fx-box/twin-{n}@x",
                               "texts": [f"Re: {c.identifier} received in full"]}
                              for n, c in enumerate((twin_of, other_vendor))]
        bc = [c for c in (bc_root("purchaseOrders", r, "bc_document")
                          for r in world.bc["purchaseOrders"]) if c is not None]
        self.assertEqual(len(bc), 2)
        funnel = {}
        found = document_candidates(sources, world.documents(), "mail", portal + bc, 10_000,
                                    funnel, {}, run_id=RUN, now=dev.FIXTURE_NOW)
        self.assertEqual([(c.root_relation, c.root_key) for c in found],
                         [(twin_of.root_relation, twin_of.root_key)])
        self.assertEqual((funnel["documents"], funnel["documents_naming_one"]), (2, 1))
        self.assertEqual(funnel["span_shared"], 1)   # the other vendor's number

    def _closed_order_pool(self, receipts, docs):
        """Sources, the pool of the posted receipts' roots and the documents naming a PO the
        BC deleted once invoiced: it survives only as the receipts' orderNumber."""
        world = build_live_shaped_world()
        pool = []
        for k, (number, order, posted, *more) in enumerate(receipts):
            company, entity_set = more or (0, "purchaseReceipts")
            record = bc_record(entity_set, company, 960_000 + k, number=number,
                               orderNumber=order, postingDate=posted, status="Paid")
            world.bc[entity_set].append(record)
            pool.append(bc_root(entity_set, record, "bc_document"))
        world.docs.update(docs)
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        return sources, pool, world.documents()

    def test_a_po_surviving_only_as_a_receipts_order_number_is_named_in_mail_and_sharepoint(self):
        # BC deletes a PO once fully invoiced; its number lives on as the posted receipt's
        # orderNumber (keys.EDGES bc_purchase_receipt.po). The mail and the SharePoint file
        # name that number only, never the receipt's own: both strata get the candidate.
        sources, pool, documents = self._closed_order_pool(
            [("FXPR8001", "FXPO8801", "2098-12-01")],
            {"mail": [{"handle": "graph-mail://fx-box/po-8801@x",
                       "texts": ["Order FXPO8801 shipped"]}],
             "sharepoint": [{"handle": "sharepoint://fx-drive/po-8801@x",
                             "texts": ["FXPO8801 packing slip.pdf"]}]})
        for kind in ("mail", "sharepoint"):
            funnel = {}
            found = document_candidates(sources, documents, kind, pool, 10_000, funnel, {},
                                        run_id=RUN, now=dev.FIXTURE_NOW)
            self.assertEqual([c.identifier for c in found], ["FXPR8001"], kind)
            # counts only: the spans equal to a posted document's orderNumber
            self.assertEqual(funnel["span_order_number"], 1, kind)

    def test_a_receipt_and_an_invoice_of_one_order_name_it_once_at_the_newest_receipt(self):
        sources, pool, documents = self._closed_order_pool(
            [("FXPR8001", "FXPO8801", "2098-12-01"), ("FXPR8002", "FXPO8801", "2098-12-02"),
             ("FXPI8003", "FXPO8801", "2098-12-09", 0, "purchaseInvoices")],
            {"mail": [{"handle": "graph-mail://fx-box/po-8801@x",
                       "texts": ["Order FXPO8801 shipped"]}]})
        funnel = {}
        found = document_candidates(sources, documents, "mail", pool, 10_000, funnel, {},
                                    run_id=RUN, now=dev.FIXTURE_NOW)
        self.assertEqual([c.identifier for c in found], ["FXPR8002"])
        self.assertFalse(funnel.get("span_shared"))

    def test_an_order_number_two_companies_share_names_neither(self):
        sources, pool, documents = self._closed_order_pool(
            [("FXPR8001", "FXPO8801", "2098-12-01"),
             ("FXPR8002", "FXPO8801", "2098-12-02", 1, "purchaseReceipts")],
            {"mail": [{"handle": "graph-mail://fx-box/po-8801@x",
                       "texts": ["Order FXPO8801 shipped"]}]})
        funnel = {}
        found = document_candidates(sources, documents, "mail", pool, 10_000, funnel, {},
                                    run_id=RUN, now=dev.FIXTURE_NOW)
        self.assertEqual(found, [])
        self.assertEqual(funnel["span_shared"], 1)

    def test_bc_roots_are_dated_by_their_posting_date_so_the_newest_are_searched_first(self):
        n = RECENT_WINDOW + 6
        sources, pool, _ = self._closed_order_pool(
            [(f"FXPR{8100 + k}", f"FXPO{8300 + k}",
              (datetime(2098, 1, 1) + timedelta(days=k)).strftime("%Y-%m-%d"))
             for k in range(n)] + [("FXPR8099", "FXPO8299", None)], {})
        when = {c: w for c in pool if (w := root_when(sources, c, dev.FIXTURE_NOW))}
        self.assertEqual(len(when), n)   # the undated one stays undated
        order = [c.identifier for c in search_roots(pool, RUN, when)]
        newest = {f"FXPR{8100 + k}" for k in range(6, n)}
        self.assertEqual(set(order[:RECENT_WINDOW]), newest)
        self.assertEqual(order[-1], "FXPR8099")
        # a date later than the run's clock dates nothing
        self.assertIsNone(root_when(sources, pool[-2], "2098-01-02T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
