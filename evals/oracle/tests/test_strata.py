"""Strata coverage over a world shaped like the served replica (injected transport; not live).

The first live dev run (main 250eae4) read 0 candidates with status ``read`` for five of
seven strata: the root rules assumed shapes the replica does not have (legacy FM sales
orders without custom_fields, FM status text, PO closure only in line quantities, an empty
``app_receipt``, ``#`` in PO numbers). ``build_live_shaped_world`` carries those shapes.
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from evals.oracle import BcReads, PortalReads, Sources
from evals.oracle import cross_sample as cs
from evals.oracle import keys as K
from evals.oracle.fixture_world import bc_record, build_live_shaped_world
from evals.oracle.fixture_world import LIVE_SHAPED_SIZE, World, build_world
from evals.oracle.generator import (DOCUMENT_STRATA, MAX_CITES, MAX_ROOT_SEARCHES,
                                    PORTAL_STRATA, QUOTAS, STRATA, _with_closed_cite,
                                    bc_candidates, candidate_split, closed,
                                    document_candidates, generate, identifier_spans, lineage,
                                    portal_candidates, search_roots, unexplained_strata,
                                    unread_status)

NOW = "2099-01-02T04:00:00Z"
ALL = 10_000   # roots searched by a direct document_candidates call: every one


def documents_of(mail=(), sharepoint=()):
    """The fixture search over these documents (one text list each)."""
    return World({}, documents={
        "mail": [{"handle": f"graph-mail://fx-box/m{n}@ck", "texts": list(t)}
                 for n, t in enumerate(mail)],
        "sharepoint": [{"handle": f"sharepoint://fx-drive/f{n}@e", "texts": list(t)}
                       for n, t in enumerate(sharepoint)]}).documents()


def _generate(world=None, run_id: str = "live-shaped-a", **kwargs):
    world = world or build_live_shaped_world()
    sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
    kwargs.setdefault("documents", world.documents())
    return generate(sources, run_id=run_id, now=NOW, **kwargs), sources, world


class LiveShapedStrataTest(unittest.TestCase):
    def test_every_stratum_has_candidates_and_items_across_f1_to_f4(self):
        for run_id in ("live-shaped-a", "live-shaped-b", "live-2099-01-02T03"):
            generated, _, _ = _generate(run_id=run_id)
            for stratum in STRATA:
                entry = generated.strata[stratum]
                self.assertEqual(entry["status"], "read", (run_id, stratum, entry))
                self.assertGreater(entry["candidates"], 0, (run_id, stratum))
                self.assertGreaterEqual(entry["items"], 1, (run_id, stratum))
                self.assertEqual(sum(entry["items_by_family"].values()), entry["items"])
            families = [i["family"] for i in generated.items]
            self.assertEqual({f: families.count(f) for f in set(families)}, QUOTAS["dev"])
            self.assertEqual(set(generated.item_strata.values()), set(STRATA))
            self.assertEqual(unexplained_strata(generated.strata), [])
            self.assertEqual(generated.failures, {})
            for item in generated.items:  # closed_transaction names the rule behind it
                self.assertIn(K.CLOSED_CITE[item["root"]["relation"]],
                              item["oracle"]["derived_from"], item["id"])

    def test_each_family_draws_on_the_stratum_that_roots_it(self):
        generated, _, _ = _generate()
        by_family = {s: generated.strata[s]["candidates_by_family"] for s in STRATA}
        self.assertTrue({"F1", "F2", "F3"} <= set(by_family["sales_order"]))
        self.assertTrue({"F1", "F4"} <= set(by_family["purchase_order"]))
        for stratum in ("assembly_order", "receipt", "bc_invoice", "mail", "sharepoint"):
            self.assertEqual(set(by_family[stratum]), {"F1"}, stratum)

    def test_receipts_root_in_fm_lab_ids_when_app_receipt_is_empty(self):
        generated, sources, world = _generate()
        self.assertEqual(world.relations["public.app_receipt"], [])
        receipts = [i for i in generated.items if generated.item_strata[i["id"]] == "receipt"]
        self.assertTrue(receipts)
        for item in receipts:
            self.assertEqual(item["root"]["relation"], K.FM_RECEIPT_RAW)
            self.assertEqual(item["root"]["identifier_kind"], "receipt_number")
            self.assertEqual(item["lineage_stratum"], "fm_bronze")
            fks = {e.get("fk") for e in item["known_outcome"]["chain"]}
            # The F1 chain walks lab id -> FM PO item -> FM PO -> gold PO.
            self.assertTrue({"fm_raw_receipt.po_item", "fm_po_item.order", "fm_po.po"} <= fks,
                            fks)

    def test_root_rules_mirror_the_portal_reads(self):
        _, sources, world = _generate()
        portal = sources.portal
        legacy = next(r for r in world.relations[K.SO] if r["id"].startswith("fm-so-"))
        self.assertIsNone(legacy["custom_fields"])
        self.assertEqual(lineage(K.SO, legacy, portal), "gold_etl_from_fm")
        self.assertEqual(lineage(K.SO, {"id": "fm-so-900001"}, portal), "gold_native_staging")
        self.assertEqual(lineage(K.SO, {"id": "cuid-1"}, portal), "gold_native_staging")
        fm = {"fmGeneratedPoId": 6001, "fmPoNumber": "PO#90001"}
        for custom, origin in (   # keys.PO_LINEAGE_CITE: only an FM loader's stamp is FM
                ({**fm, "source": "etl-silver-to-gold"}, "gold_etl_from_fm"),
                ({**fm, "source": "mirror-fm-pos-to-gold"}, "gold_etl_from_fm"),
                (fm, "gold_native_staging"),
                ({"fmPoNumber": "PO#90001"}, "gold_native_staging"),
                ({**fm, "source": "portal-generate-po"}, "gold_native_staging"),
                ({**fm, "source": "etl-silver-to-gold", "qaSmoke": True},
                 "gold_native_staging"),
                ({"demoTag": "fx-demo"}, "gold_native_staging"), (None, "gold_native_staging")):
            self.assertEqual(lineage(K.PO, {"custom_fields": custom}, portal), origin, custom)
        for status, finished in (("Completed", True), ("Closed", True), ("COMPLETE", True),
                                 ("SHIPPED", True), ("Open", False), ("Cancelled", False),
                                 ("CONFIRMED", False), (None, False)):
            self.assertEqual(closed(K.SO, {"status": status}, portal), finished, status)
        sage = [r for r in world.relations[K.SO] if r["custom_fields"]]
        flags = {h["orduniq"]: h["complete"] for h in world.relations[K.SAGE_ORDER]}
        self.assertTrue({"0", "1"} <= set(flags.values()))
        for row in sage:
            self.assertEqual(row["status"], "READY_TO_SHIP")
            self.assertEqual(closed(K.SO, row, portal),
                             flags[row["custom_fields"]["sageOrdUniq"]] == "1")
        # A PO is closed by its fulfillment code over its lines; FM header text ("Pending")
        # does not decide, and a PO without lines is never closed by its header alone.
        ten = Decimal(10)
        self.assertEqual(K.po_fulfillment_code("Pending", Decimal(15), Decimal(15)), "RECEIVED")
        self.assertEqual(K.po_fulfillment_code("Complete", ten, Decimal(9)), "PARTIAL")
        self.assertEqual(K.po_fulfillment_code("Complete", Decimal(3), Decimal(0)), "SUBMITTED")
        self.assertEqual(K.po_fulfillment_code("cancel", ten, ten), "CANCELLED")
        self.assertEqual(K.po_fulfillment_code("RECEIVED", Decimal(0), Decimal(0)), "SUBMITTED")
        po = next(r for r in world.relations[K.PO] if r["status"] == "Pending")
        self.assertIsNone(po["closed_at"])
        received = [r for r in world.relations[K.PO_LINE] if r["purchase_order_id"] == po["id"]]
        self.assertEqual(closed(K.PO, po, portal),
                         all(r["quantity"] == r["quantity_received"] for r in received))
        self.assertTrue(closed(K.FM_RECEIPT_RAW, {"status_c": "Released"}, portal))
        self.assertFalse(closed(K.FM_RECEIPT_RAW, {"status_c": "Quarantine"}, portal))
        self.assertTrue(closed(K.RECEIPT, {"status": "REJECTED"}, portal))

    def test_document_identifiers_are_exact_separator_bounded_spans(self):
        spans = identifier_spans("RE: PO FX#2004 confirmation")
        self.assertIn("FX#2004", spans)
        self.assertIn("FX98003-1", identifier_spans("FX98003-1_CoA.pdf"))
        self.assertIn("AO-98009A", identifier_spans("AO-98009A batch record.pdf"))
        self.assertNotIn("PO9200009", identifier_spans("PO92000099 is not a known order"))
        # Spans keep their text; how a span is compared with a number is document_key's.
        self.assertEqual(identifier_spans("po9200004 lower case") & {"PO9200004"}, set())
        self.assertNotIn("FX#2004 confirmation", spans)
        generated, _, _ = _generate()
        mail = [i["root"]["identifier"] for i in generated.items
                if generated.item_strata[i["id"]] == "mail"]
        self.assertTrue(mail)


class PurchaseOrderIdentityTest(unittest.TestCase):
    """A purchase order is named the way the Portal looks one up (keys.PO_DOCUMENT_CITE):
    its gold number, BC document No. or FM number, trimmed and case-folded; its FM twin is
    staging when the Portal wrote its gold row (keys.PO_LINEAGE_CITE)."""

    def test_a_document_names_a_po_by_its_bc_or_fm_number_exactly_never_a_substring(self):
        world = build_live_shaped_world(documents=False)
        pool = portal_candidates(Sources(PortalReads(world.read_relation), None),
                                 "purchase_order")
        roots = sorted({dict(c.root_key)["id"]: c.identifier for c in pool
                        if c.family == "F1"}.items())
        self.assertGreaterEqual(len(roots), 4)
        rows = {r["id"]: r for r in world.relations[K.PO]}
        (a, a_no), (b, b_no), (c, _), (d, _) = roots[:4]
        rows[a]["custom_fields"].update(fmPoNumber="FXM#7001", renumberedTo="FXRN7001")
        rows[b]["custom_fields"].update(fmPoNumber="FXM#7002")
        rows[c]["custom_fields"].update(fmPoNumber=a_no)        # FM tier below a's gold No.
        rows[d]["custom_fields"].update(fmPoNumber="FXM#7002")  # shared FM No.: names neither
        sources = Sources(PortalReads(world.read_relation), None)
        pool = portal_candidates(sources, "purchase_order")

        def named(*texts):
            return sorted(c.identifier for c in document_candidates(
                sources, documents_of([[t] for t in texts]), "mail", pool, ALL))

        self.assertEqual(named("Re: fxm#7001 shipped"), [a_no])   # FM No., TRIM ILIKE
        self.assertEqual(named("FXRN7001_invoice.pdf"), [a_no])  # BC document No.
        self.assertEqual(named("XFXM#70011 and FXM#700"), [])     # never a substring
        # the search for FXM#7001 finds that document; its spans never confirm it
        funnel: dict = {}
        document_candidates(sources, documents_of([["XFXM#70011 and FXM#700"]]), "mail",
                            pool, ALL, funnel)
        self.assertEqual((funnel["documents"], funnel["documents_naming_one"]), (1, 0))
        self.assertEqual(named("FXM#7002"), [])                    # two POs: neither
        self.assertEqual(named(a_no), [a_no])                      # gold beats FM tier
        self.assertEqual(named(b_no), [b_no])                      # the question's number

    def test_a_shared_name_goes_to_the_portal_precedence_gold_then_bc_then_fm(self):
        # Review em-1: the Portal readers order gold po_number > BC No. > FM fmPoNumber
        # (keys.PO_DOCUMENT_CITE); a name that is one PO's gold number and another's BC
        # No. names the gold one, and a BC No. beats an FM number.
        world = build_live_shaped_world(documents=False)
        pool = portal_candidates(Sources(PortalReads(world.read_relation), None),
                                 "purchase_order")
        roots = sorted({dict(c.root_key)["id"]: c.identifier for c in pool
                        if c.family == "F1"}.items())
        rows = {r["id"]: r for r in world.relations[K.PO]}
        (a, a_no), (b, b_no), (c, _) = roots[:3]
        rows[a]["custom_fields"].update(renumberedTo=b_no)        # a's BC No. = b's gold No.
        rows[b]["custom_fields"].update(renumberedTo="FXRN7101")
        rows[c]["custom_fields"].update(fmPoNumber="FXRN7101")    # c's FM No. = b's BC No.
        sources = Sources(PortalReads(world.read_relation), None)
        pool = portal_candidates(sources, "purchase_order")

        def named(text):
            return sorted(c.identifier for c in document_candidates(
                sources, documents_of([[text]]), "mail", pool, ALL))

        self.assertEqual(named(b_no), [b_no])          # gold beats BC
        self.assertEqual(named("fxrn7101"), [b_no])    # BC beats FM
        self.assertNotIn(a_no, named(b_no))

    def test_the_fm_twin_of_a_qa_smoke_or_generate_po_row_is_staging_like_its_root(self):
        world = build_world()
        qa = next(r for r in world.relations[K.PO]
                  if (r["custom_fields"] or {}).get("qaSmoke") is True)
        twin_id = qa["custom_fields"]["fmGeneratedPoId"]
        world.relations[K.FM_PO_ITEM].append({"id": 96990, "idpogen": twin_id,
                                              "audittimestampmodified": NOW})
        world.relations[K.FM_RECEIPT_RAW].append({
            "id": 96991, "idlabid": "FX99901-1", "idpogenitem": 96990,
            "status_c": "Released", "audittimestampmodified": NOW})
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        portal = sources.portal
        twin = portal.lookup(K.FM_PO, "id", twin_id)[0]
        self.assertEqual(lineage(K.FM_PO, twin, portal), "gold_native_staging")
        self.assertEqual(lineage(K.FM_PO_ITEM, portal.lookup(K.FM_PO_ITEM, "id", 96990)[0],
                                 portal), "gold_native_staging")
        raw = portal.lookup(K.FM_RECEIPT_RAW, "id", 96991)[0]
        self.assertEqual(lineage(K.FM_RECEIPT_RAW, raw, portal), "gold_native_staging")
        # Its closed raw receipt is never a receipt root (10-DECISION #2: staging rows stay
        # out of the benchmark), in the dev-set strata and in the cross-system sample.
        self.assertNotIn("FX99901-1", {c.identifier for c in
                                       portal_candidates(sources, "receipt")})
        self.assertNotIn("FX99901-1", {r.identifier for r in cs.portal_roots(sources,
                                                                             "receipt")})
        for row in world.relations[K.PO]:
            custom = row.get("custom_fields") or {}
            fm = portal.lookup(K.FM_PO, "id", custom.get("fmGeneratedPoId"))
            if not fm:
                continue
            staged = custom.get("qaSmoke") is True or custom.get("source") == "portal-generate-po"
            self.assertEqual(lineage(K.FM_PO, fm[0], portal),
                             "gold_native_staging" if staged else "fm_bronze", custom)
        self.assertEqual(lineage(K.FM_PO, {"id": 1}, portal), "fm_bronze")   # no gold twin


class EmptyStratumTest(unittest.TestCase):
    """A stratum without candidates says why; an unreadable one says so."""

    def test_unreadable_source_reports_unavailable_with_its_reason(self):
        world = build_live_shaped_world()
        world.fail.add(K.FM_RECEIPT_RAW)
        generated, _, _ = _generate(world)
        entry = generated.strata["receipt"]
        self.assertEqual(entry["status"], "unavailable")
        self.assertEqual(entry["reason"], "RuntimeError")
        self.assertEqual(generated.failures, {K.FM_RECEIPT_RAW: "RuntimeError"})
        self.assertEqual(unexplained_strata(generated.strata), [])
        self.assertEqual(len(generated.items), 30)

    def test_an_upstream_failure_is_blocked_with_its_http_status_and_code(self):
        world = build_live_shaped_world()

        class PortalDown(RuntimeError):
            status, error_code = 503, "replica_unavailable"

        def read(name, columns):
            if name == K.MO:
                raise PortalDown("not kept")
            return world.read_relation(name, columns)

        sources = Sources(PortalReads(read), BcReads(world.bc_port()))
        generated = generate(sources, run_id="live-shaped-a", now=NOW,
                             documents=world.documents())
        entry = generated.strata["assembly_order"]
        self.assertEqual({k: entry[k] for k in ("status", "reason", "blocked", "http_status",
                                                "error_code")},
                         {"status": "unavailable", "reason": "PortalDown",
                          "blocked": "upstream", "http_status": 503,
                          "error_code": "replica_unavailable"})
        drawn = cs.sample(sources, run_id="seed-a", count=28, documents=world.documents())
        kind = drawn.kinds["assembly_order"]
        self.assertEqual((kind["status"], kind["blocked"], kind["http_status"]),
                         ("unavailable", "upstream", 503))
        self.assertFalse([i for i in drawn.items if i.relation == K.MO])

    def test_empty_relation_and_no_closed_rows_are_named(self):
        world = build_live_shaped_world()
        world.relations[K.PO] = []
        for row in world.relations[K.SO]:
            row["status"] = "Open"
        for header in world.relations[K.SAGE_ORDER]:
            header["complete"] = "0"
        generated, _, _ = _generate(world)
        po, so = generated.strata["purchase_order"], generated.strata["sales_order"]
        self.assertEqual((po["status"], po["reason"], po["candidates"], po["tried"]),
                         ("empty", "relation_empty", 0, ["public.app_purchase_order.po_number"]))
        self.assertEqual((so["status"], so["reason"]), ("empty", "none_closed"))
        self.assertEqual(so["funnel"]["not_closed"], so["funnel"]["rows"])
        self.assertEqual(unexplained_strata(generated.strata), [])

    def test_documents_naming_no_searched_root_are_a_sample_with_the_roots_searched(self):
        # Root first: no document, or none naming a root, is no candidate in the sample
        # of what the searches found, beside how many roots were searched; never empty.
        world = build_live_shaped_world()
        for kind in ("mail", "sharepoint"):
            world.docs[kind] = [{"handle": f"{kind}://fx/1@x", "texts": ["Lunch on Friday?"]}]
        for docs in (build_live_shaped_world(documents=False), world):
            generated, _, _ = _generate(docs)
            for kind in ("mail", "sharepoint"):
                entry = generated.strata[kind]
                self.assertEqual((entry["status"], entry["reason"], entry["tried"],
                                  entry["complete"], entry["funnel"]["documents"]),
                                 ("no_candidate_in_sample", "no_document_names_root",
                                  [f"fixture:{kind}"], True, 0))
                self.assertEqual(entry["funnel"]["roots_searched"], entry["funnel"]["roots"])
                self.assertGreater(entry["funnel"]["roots_searched"], 20)
            self.assertEqual(unexplained_strata(generated.strata), [])

    def test_unconfigured_sources_carry_a_reason(self):
        world = build_live_shaped_world()
        sources = Sources(PortalReads(world.read_relation), None)
        generated = generate(sources, run_id="live-shaped-a", now=NOW)
        self.assertEqual(generated.strata["bc_invoice"]["reason"], "bc_not_configured")
        self.assertEqual(generated.strata["mail"]["reason"], "documents_not_configured")
        self.assertEqual(unexplained_strata(generated.strata), [])

    def test_a_silent_zero_is_flagged(self):
        self.assertEqual(unexplained_strata({"a": {"status": "read", "candidates": 0},
                                             "b": {"status": "unavailable"},
                                             "c": {"status": "empty", "reason": "x",
                                                   "tried": ["public.t.c"]},
                                             "d": {"status": "read", "candidates": 3},
                                             "e": {"status": "empty", "reason": "x"},
                                             "f": {"status": "unreadable",
                                                   "reason": "withheld"}}),
                         ["a", "b", "e"])

    def test_unreadable_is_not_empty(self):
        full = {"tried": ["x"], "complete": True, "withheld": 0}
        self.assertIsNone(unread_status(full, 0))
        self.assertIsNone(unread_status(None, 0))
        self.assertEqual(unread_status({**full, "withheld": 2}, 0), ("unreadable", "withheld"))
        self.assertEqual(unread_status({**full, "complete": False}, 0),
                         ("unreadable", "truncated"))
        self.assertEqual(unread_status({**full, "complete": False}, 3),
                         ("incomplete", "truncated"))
        self.assertEqual(unread_status({**full, "not_configured": "no_reader"}, 0),
                         ("not_configured", "no_reader"))
        # A document search that did not happen, failed, was refused or found only
        # withheld documents is never a sample of what names the roots.
        for answer, status, reason in (
                (lambda q: {"query": q, "failed": "unavailable"}, "unreadable", "search_failed"),
                (lambda q: {"query": q, "handles": [], "complete": True, "withheld": 2},
                 "unreadable", "withheld"),
                (lambda q: {"query": q, "refused": "tenant"}, "unavailable", "search_refused"),
                (lambda q: {"query": q}, "unreadable", "search_failed"),  # shape unknown
                (None, "not_configured", "no_drive")):
            class Port:
                def search(self, kind, queries, *, hits, seed="", answer=answer):
                    if answer is None:
                        return {"results": [], "records": [], "source": ["fx:drive"],
                                "not_configured": "no_drive"}
                    return {"results": [answer(q) for q in queries], "records": [],
                            "source": ["fx:drive"]}
            generated, _, _ = _generate(documents=Port())
            entry = generated.strata["sharepoint"]
            self.assertEqual((entry["status"], entry["reason"], entry["candidates"]),
                             (status, reason, 0), answer)
            self.assertEqual(unexplained_strata(generated.strata), [])
        # a port that cannot search (the cross-system sample's sample only) is not one
        class Listing:
            def sample(self, kind, limit, **_):
                return {"records": [], "complete": True, "source": ["fx:drive"]}
        entry = _generate(documents=Listing())[0].strata["mail"]
        self.assertEqual((entry["status"], entry["reason"]),
                         ("not_configured", "document_search_not_configured"))

    def test_a_po_closed_only_by_its_header_is_a_lead_never_a_root(self):
        self.assertTrue(K.po_closed_by_header_only(" RECEIVED"))
        self.assertTrue(K.po_closed_by_header_only("received"))
        self.assertTrue(K.po_closed_by_header_only("Complete"))
        for header in ("COMPLETE", "Pending", "PARTIAL", "Cancel", "DRAFT", "", None):
            self.assertFalse(K.po_closed_by_header_only(header), header)
        world = build_live_shaped_world()
        for n, status in enumerate(("RECEIVED", "Complete", "Pending")):
            world.relations[K.PO].append({
                "id": f"fx-po-nolines-{n}", "po_number": f"FX#NL{n}", "status": status,
                "supplier_id": "fm-supp-1", "closed_at": None,
                "custom_fields": {"fmGeneratedPoId": 7700 + n, "fmPoNumber": f"FX#NL{n}",
                                  "source": "etl-silver-to-gold"}, "updated_at": NOW})
        before, _, _ = _generate()
        generated, _, _ = _generate(world)
        po, was = generated.strata["purchase_order"], before.strata["purchase_order"]
        self.assertEqual(po["funnel"]["closed_by_header_only"],
                         was["funnel"]["closed_by_header_only"] + 2)
        self.assertEqual(po["funnel"]["not_closed"], was["funnel"]["not_closed"] + 1)
        self.assertEqual(po["funnel"]["roots"], was["funnel"]["roots"])
        self.assertFalse(any(i["root"]["identifier"].startswith("FX#NL")
                             for i in generated.items))
        only = build_live_shaped_world()
        only.relations[K.PO] = [r for r in world.relations[K.PO]
                                if r["id"].startswith("fx-po-nolines-")][:2]
        generated, _, _ = _generate(only)
        self.assertEqual(generated.strata["purchase_order"]["reason"], "only_closed_by_header")

    def test_the_closure_cite_never_pushes_out_a_derivation_cite_silently(self):
        cite = K.CLOSED_CITE[K.PO]
        many = [f"rule {i}" for i in range(45)]
        out = _with_closed_cite(many, K.PO)
        self.assertEqual(len(out), MAX_CITES)
        self.assertEqual(out[-1], cite)
        self.assertEqual(out[:38], many[:38])
        self.assertEqual(out[38], "7 more derivation cites not listed")
        exact = [f"rule {i}" for i in range(39)]
        self.assertEqual(_with_closed_cite(exact, K.PO), exact + [cite])
        full = [f"rule {i}" for i in range(39)] + [cite]
        self.assertEqual(_with_closed_cite(full, K.PO), full)
        self.assertEqual(_with_closed_cite(many[:40], "public.unknown"), many[:40])


def _documents_like_live(world, mail, sharepoint):
    """Live 2026-10-07 21:22Z shape: 200 messages, 70 file names; the numbers appear in
    other shapes than the records carry."""
    world.docs = {"mail": [{"handle": f"graph-mail://fx-box/m{n}@ck", "texts": t}
                           for n, t in enumerate(mail)],
                  "sharepoint": [{"handle": f"sharepoint://fx-drive/f{n}@e", "texts": t}
                                 for n, t in enumerate(sharepoint)]}
    return world.documents()


class DocumentNumberVariantTest(unittest.TestCase):
    """A document writes a record's number in another shape (prefix, zero padding;
    keys.DOCUMENT_VARIANTS): a lead the oracle's own exact root lookup confirms."""

    def world(self, size=LIVE_SHAPED_SIZE):
        world = build_live_shaped_world(size, documents=False)
        for n, (entity_set, number) in enumerate((   # five BC number shapes, like live
                ("salesOrders", "FXSO1001"), ("purchaseOrders", "FXPO-10001"),
                ("purchaseReceipts", "FXPR00001"))):
            world.bc[entity_set].append(bc_record(entity_set, 0, 990 + n, number=number))
        return world

    def po_numbers(self, world):
        return [r["po_number"] for r in world.relations[K.PO]]

    def test_prefix_and_zero_padded_shapes_give_mail_and_sharepoint_candidates(self):
        world = self.world()
        digits = [n.split("#")[1] for n in self.po_numbers(world)]
        noise = ["Lunch on Friday?", "Delivery 2026-10-07 confirmed", "Qty 1200 cases",
                 "Ticket TK-88812 opened", f"Shipped {digits[0]} cases"]  # bare: never a lead
        mail = [[f"Re: PO{d} shipped", "Tracking to follow."] for d in digits]
        mail += [[f"UAT-PO#0{d} receiving"] for d in digits]
        mail += [[noise[n % len(noise)]] for n in range(200 - len(mail))]
        sharepoint = [[f"PO{d}_packing-slip.pdf"] for d in digits[:35]]
        sharepoint += [[f"Scan {n:04d}.pdf"] for n in range(70 - len(sharepoint))]
        generated, sources, _ = _generate(world, documents=_documents_like_live(
            world, mail, sharepoint))
        known = set(self.po_numbers(world))
        for kind in ("mail", "sharepoint"):
            entry = generated.strata[kind]
            funnel = entry["funnel"]
            self.assertEqual(entry["status"], "read", kind)
            self.assertGreaterEqual(entry["candidates"], 1, kind)
            # the numbers as written found nothing; without their prefix they did
            self.assertGreater(funnel["fallback_searches"], 0, kind)
            self.assertLessEqual(funnel["fallback_searches"], funnel["roots_searched"], kind)
            self.assertGreater(funnel["documents"], 0, kind)
            self.assertNotIn("span_exact", funnel, kind)   # nothing names a record verbatim
            self.assertNotIn("span_shared", funnel, kind)
            self.assertGreater(funnel["span_prefix"], 0, kind)
            self.assertGreaterEqual(funnel["number_spans"], funnel["span_prefix"], kind)
            self.assertNotIn(digits[0], json.dumps(funnel))   # counts, never values
        self.assertGreater(generated.strata["mail"]["funnel"]["span_zeros"], 0)
        self.assertNotIn("span_zeros", generated.strata["sharepoint"]["funnel"])
        items = [i for i in generated.items if generated.item_strata[i["id"]] in DOCUMENT_STRATA]
        self.assertTrue(items)
        for item in items:   # asked from the record's own number, which resolves to it alone
            self.assertIn(item["root"]["identifier"], known)
            self.assertEqual(item["root"]["relation"], K.PO)
        self.assertEqual(unexplained_strata(generated.strata), [])

    def test_a_variant_lead_names_what_the_exact_number_names_at_any_cardinality(self):
        world = self.world(size=160)
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        pool = portal_candidates(sources, "purchase_order")
        numbers = sorted({c.identifier for c in pool if c.family == "F1"})
        self.assertGreaterEqual(len(numbers), 60)

        def named(texts):
            documents = _documents_like_live(world, [[t] for t in texts], [])
            return sorted(c.identifier for c in document_candidates(
                sources, documents, "mail", pool, ALL))

        for size in (1, 10, 60):
            chosen = numbers[:size]
            exact = named(chosen)
            self.assertEqual(exact, chosen, size)
            self.assertEqual(named([f"Re: PO{n.split('#')[1]} shipped" for n in chosen]),
                             exact, size)
            self.assertEqual(named([f"UAT-PO#00{n.split('#')[1]}" for n in chosen]), exact,
                             size)
            # DPO is another series (its own counter): its spans never key these records.
            self.assertEqual(named([f"DPO{n.split('#')[1]} shipped" for n in chosen]), [],
                             size)

    def test_a_variant_two_records_share_or_a_bare_number_names_neither(self):
        world = self.world()
        sources = Sources(PortalReads(world.read_relation), None)
        pool = portal_candidates(sources, "purchase_order")
        (a, a_no), (b, b_no), (_, c_no) = sorted({dict(c.root_key)["id"]: c.identifier
                                                  for c in pool if c.family == "F1"}.items())[:3]
        rows = {r["id"]: r for r in world.relations[K.PO]}
        a_digits = a_no.split("#")[1]
        rows[b]["custom_fields"].update(fmPoNumber=f"QX-0{a_digits}")   # same variant as a
        pool = portal_candidates(sources, "purchase_order")
        funnel = {}

        def named(*texts, **kwargs):
            documents = _documents_like_live(world, [[t] for t in texts], [])
            return sorted(c.identifier for c in document_candidates(
                sources, documents, "mail", pool, ALL, funnel=funnel, **kwargs))

        self.assertEqual(named(f"Re: PO{a_digits} shipped"), [])
        self.assertEqual(funnel["span_shared"], 1)
        self.assertEqual(named(a_digits, f"0{a_digits}"), [])   # no prefix: never a lead
        self.assertEqual(named(a_no), [a_no])                    # the exact number still names
        # Another series' prefix on a PO's digits is not that PO; one the relation's known
        # numbers carry ("FX" of FX#n) or its numbering declares (PO, UAT-PO) is.
        c_digits = c_no.split("#")[1]
        self.assertEqual(named(f"INV-{c_digits}", f"ISO{c_digits} audit",
                               f"FY{c_digits} budget", f"Lot L{c_digits}",
                               f"Ticket TK-{c_digits}"), [])
        for text in (f"FX-0{c_digits}", f"PO{c_digits}", f"UAT-PO#0{c_digits}"):
            self.assertEqual(named(text), [c_no], text)
        # A document naming a record exactly takes no variant lead for another one.
        self.assertEqual(named(f"{b_no} and PO{b_no.split('#')[1]}0"), [b_no])
        # The lead is checked like a named record: a failed root check names nothing.
        self.assertEqual(named(f"PO{b_no.split('#')[1]}", verify=lambda c: False), [])
        self.assertEqual(funnel["not_single_root"], 1)


    def test_a_posted_receipt_number_a_document_names_is_a_candidate_only_while_one_record_holds_it(self):
        # A BC-only root (no Portal row) is checked by the oracle's own lookup of its No.:
        # a mail naming a number one receipt holds is a candidate; a number a second record
        # of another company holds too names neither (``span_shared``), never a candidate.
        def named(extra):
            world = build_live_shaped_world(documents=False)
            for company, number, k in ((0, "FXPR7001", 1), *extra):
                world.bc["purchaseReceipts"].append(bc_record(
                    "purchaseReceipts", company, 940 + k, number=number,
                    postingDate="2098-12-01"))
            world.docs["mail"] = [{"handle": "graph-mail://fx-box/n@x",
                                   "texts": ["Re: FXPR7001 delivered"]}]
            generated, _, _ = _generate(world, bc_sample_limit=200)
            return generated.strata["mail"]

        one = named(())
        self.assertGreaterEqual(one["candidates"], 1)
        self.assertEqual(one["funnel"]["documents_naming_one"], one["candidates"])
        two = named(((1, "FXPR7001", 2),))
        self.assertEqual((two["candidates"], two["status"]), (0, "no_candidate_in_sample"))
        self.assertGreater(two["funnel"]["span_shared"], 0)
        self.assertEqual(two["funnel"]["documents_naming_one"], 0)

    def test_a_dpo_span_never_keys_a_po_record_of_the_same_digits(self):
        # PO and DPO number series run separate counters (P@b08ffc3 po-number-series.ts:
        # 22-47), so the same digits under the other prefix are another record.
        world = self.world()
        pool = portal_candidates(Sources(PortalReads(world.read_relation), None),
                                 "purchase_order")
        a, b = sorted({dict(c.root_key)["id"] for c in pool if c.family == "F1"})[:2]
        rows = {r["id"]: r for r in world.relations[K.PO]}
        rows[a]["po_number"], rows[b]["po_number"] = "PO100123", "DPO100124"
        sources = Sources(PortalReads(world.read_relation), None)
        pool = portal_candidates(sources, "purchase_order")

        def named(text):
            documents = _documents_like_live(world, [[text]], [])
            return sorted(c.identifier for c in document_candidates(
                sources, documents, "mail", pool, ALL))

        for text, want in (("Re: DPO100123 shipped", []), ("DPO-0100123 invoice", []),
                           ("Re: PO-0100123 shipped", ["PO100123"]),
                           ("UAT-PO#100123 receiving", ["PO100123"]),
                           ("Re: PO100124 shipped", []), ("UAT-PO#100124", []),
                           ("DPO-0100124 invoice", ["DPO100124"])):
            self.assertEqual(named(text), want, text)


class RootFirstTest(unittest.TestCase):
    """Root first (architect-20261013T1224Z, gap-04f9b49ebae9): the document strata search
    the documents for the numbers of the split's closed roots instead of reading documents
    and hoping one names a closed record (three live runs: 4/4/3 of ~800 spans named a
    Portal row, all open)."""

    def setUp(self):
        self.world = build_live_shaped_world(documents=False)
        sources = Sources(PortalReads(self.world.read_relation), None)
        self.closed = next(c for c in portal_candidates(sources, "sales_order")
                           if c.family == "F1" and candidate_split(c) == "dev")

    def test_a_closed_number_only_outside_the_listing_is_found_by_search(self):
        # The newest 1,000 documents (all a listing reads) name nothing closed; the one
        # naming a closed root is older. The search finds it, the listing never did.
        for kind in DOCUMENT_STRATA:
            self.world.docs[kind] = [{"handle": f"{kind}://fx/new-{n}@x",
                                      "texts": [f"Weekly note {n}"]} for n in range(1000)]
            self.world.docs[kind].append({"handle": f"{kind}://fx/old@x", "texts": [
                f"RE: order {self.closed.identifier} shipped"]})
        generated, _, _ = _generate(self.world)
        for kind in DOCUMENT_STRATA:
            entry = generated.strata[kind]
            self.assertEqual((entry["status"], entry["candidates"],
                              entry["funnel"]["documents"]), ("read", 1, 1), kind)
        named = {i["root"]["identifier"] for i in generated.items
                 if generated.item_strata[i["id"]] in DOCUMENT_STRATA}
        self.assertEqual(named, {self.closed.identifier})

    def test_a_refused_search_is_typed_and_the_number_without_its_prefix_is_searched(self):
        # The tenant refuses a query carrying '#' (a search syntax it will not parse): the
        # number without its leading letters is searched and the hit still has to name the
        # root by its spans.
        pool = portal_candidates(Sources(PortalReads(self.world.read_relation), None),
                                 "purchase_order")
        root = next(c for c in pool if c.family == "F1" and "#" in c.identifier)
        inner = documents_of([[f"{root.identifier} packing slip"], ["Lunch on Friday?"]])

        class Refusing:
            max_roots = ALL

            def search(self, kind, queries, *, hits, seed=""):
                answer = inner.search(kind, [q for q in queries if "#" not in q], hits=hits)
                answer["results"] += [{"query": q, "refused": "tenant"}
                                      for q in queries if "#" in q]
                return answer

        funnel, read = {}, {}
        sources = Sources(PortalReads(self.world.read_relation), None)
        found = document_candidates(sources, Refusing(), "mail", pool, ALL, funnel, read)
        self.assertIn(root.identifier, [c.identifier for c in found])
        self.assertGreater(funnel["search_refused_tenant"], 0)
        self.assertGreater(funnel["fallback_searches"], 0)
        self.assertEqual((read["complete"], "capped" in read), (False, False))
        # every search refused: unavailable, never a sample and never empty
        everything = documents_of()
        everything.search = lambda kind, queries, **_: {
            "results": [{"query": q, "refused": "guard"} for q in queries], "records": [],
            "source": ["fixture:mail"]}
        entry = _generate(self.world, documents=everything)[0].strata["mail"]
        self.assertEqual((entry["status"], entry["reason"]), ("unavailable", "search_refused"))
        self.assertEqual(entry["funnel"]["search_refused_guard"],
                         entry["funnel"]["searches"] + entry["funnel"].get("fallback_searches", 0))

    def test_a_live_port_searches_a_bounded_round_robin_of_roots(self):
        # A port without its own bound keeps MAX_ROOT_SEARCHES roots per source, taken in
        # turn from each record stratum, and at most as many searches again without a prefix.
        asked = []
        inner = documents_of()

        class Live:
            def search(self, kind, queries, *, hits, seed=""):
                asked.append((kind, list(queries)))
                return inner.search(kind, queries, hits=hits)

        sources = Sources(PortalReads(self.world.read_relation), BcReads(self.world.bc_port()))
        generated = generate(sources, run_id="live-shaped-a", now=NOW, documents=Live())
        for kind in DOCUMENT_STRATA:
            entry = generated.strata[kind]
            self.assertGreater(entry["funnel"]["roots"], MAX_ROOT_SEARCHES)
            self.assertEqual(entry["funnel"]["roots_searched"], MAX_ROOT_SEARCHES)
            self.assertEqual((entry["status"], entry["reason"], entry["capped"]),
                             ("no_candidate_in_sample", "no_document_names_root", True))
            exact, *fallback = [q for k, q in asked if k == kind]
            self.assertLessEqual(len(exact), 3 * MAX_ROOT_SEARCHES)   # names per root
            self.assertLessEqual(sum(map(len, fallback)), MAX_ROOT_SEARCHES)
        pool = [c for s in PORTAL_STRATA for c in portal_candidates(sources, s)]
        roots = search_roots(pool + bc_candidates(sources, 10, "live-shaped-a", 6), "x")
        self.assertEqual(len({c.stratum for c in roots[:5]}), 5)   # one per stratum first


class CrossSampleStrataTest(unittest.TestCase):
    def test_cross_sample_kinds_never_report_a_silent_zero(self):
        world = build_live_shaped_world()
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        sample = cs.sample(sources, run_id="live-shaped-a", documents=world.documents())
        for kind, entry in sample.kinds.items():
            if entry["status"] == "read":
                self.assertGreater(entry["references"], 0, kind)
            else:
                self.assertTrue(entry.get("reason"), kind)
            self.assertTrue(entry["tried"], kind)
        # FM receipts are roots; their one cross-system edge is the Portal receipt copy of
        # the lab id, and with app_receipt empty (live 1303d122: 9333 roots, none from
        # app_receipt) none is named. The funnel says so per root relation.
        receipt = sample.kinds["receipt"]
        self.assertEqual(receipt["roots"], len(world.relations[K.FM_RECEIPT_RAW]))
        self.assertEqual((receipt["status"], receipt["reason"]), ("empty", "no_reference"))
        self.assertEqual(receipt["funnel"][K.RECEIPT]["rows"], 0)
        self.assertEqual(receipt["funnel"][K.FM_RECEIPT_RAW]["no_reference"], receipt["roots"])
        for kind, entry in sample.kinds.items():   # every root is accounted for once
            for relation, counts in entry.get("funnel", {}).items():
                self.assertEqual(counts["roots"], sum(counts[s] for s in (
                    "no_reference_edge", "no_reference", "references", "untried")),
                    (kind, relation))
        self.assertGreater(sample.kinds["mail"]["references"], 0)
        self.assertEqual(sample.kinds["receipt"]["tried"],
                         ["public.app_receipt.receipt_number",
                          "public.receipt_raw_material.idlabid"])

    def test_a_receipt_root_is_named_by_the_portal_receipt_copy_of_its_lab_id(self):
        # keys.REFERENCE_EDGES receipt.fm_raw_receipt: app_receipt.receipt_number = FM
        # idlabid. The Portal rows are staging (no PO): never roots, still the reference.
        for n in (1, 10, 48):
            world = build_live_shaped_world(documents=False)
            raw = world.relations[K.FM_RECEIPT_RAW]
            for row in raw[:n]:
                world.relations[K.RECEIPT].append({
                    "id": f"fx-rc-{row['id']}", "receipt_number": row["idlabid"],
                    "purchase_order_id": None, "status": "RELEASED",
                    "vendor_invoice_number": None, "updated_at": NOW})
            if n == 10:   # a lab id two FM receipts share names neither (fail closed)
                raw[-1]["idlabid"] = raw[0]["idlabid"]
            sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
            drawn = cs.sample(sources, run_id="live-shaped-a", count=60, bc_finds_total=0)
            entry = drawn.kinds["receipt"]
            items = [i for i in drawn.items if i.kind == "receipt"]
            named = {r["idlabid"] for r in raw[:n]} - ({raw[0]["idlabid"]} if n == 10 else set())
            self.assertEqual(entry["funnel"][K.FM_RECEIPT_RAW]["references"], len(named), n)
            self.assertEqual(entry["funnel"][K.RECEIPT]["native_staging"], n)
            self.assertEqual(entry["bc_finds"], 0)
            self.assertTrue(items, n)
            self.assertEqual(len({i.key for i in items}), len(items))
            for item in items:
                self.assertEqual((item.relation, item.reference, item.reference_system),
                                 (K.FM_RECEIPT_RAW, "receipt.fm_raw_receipt", "portal"))
                self.assertIn(item.identifier, named)

    def test_a_sales_order_named_by_a_sampled_bc_invoice_spends_no_probe(self):
        world = build_live_shaped_world(documents=False)
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        drawn = cs.sample(sources, run_id="live-shaped-a", bc_finds_total=0)
        entry = drawn.kinds["sales_order"]
        items = [i for i in drawn.items if i.kind == "sales_order"]
        self.assertEqual((entry["status"], entry["bc_finds"]), ("read", 0))
        self.assertTrue(items)
        funnel = entry["funnel"][K.SO]
        self.assertEqual(funnel["from_sampled_bc"], funnel["references"])
        invoices = {r["fields"]["externalDocumentNumber"] for r in world.bc["salesInvoices"]}
        orders = [r["order_number"] for r in world.relations[K.SO]]
        for item in items:
            self.assertEqual((item.reference, item.reference_system),
                             ("so.bc_sales_invoice", "bc"))
            self.assertIn(item.identifier, invoices)
            self.assertEqual(orders.count(item.identifier), 1)

    def test_po_three_way_is_drawn_when_only_a_sampled_bc_purchase_invoice_matches(self):
        # No Portal receipt (app_receipt empty) and no FM PO number to name it: the BC
        # purchase invoice that made the PO a candidate also names it, without a probe.
        world = build_live_shaped_world(documents=False)
        world.relations[K.FM_PO] = []
        self.assertEqual(world.relations[K.RECEIPT], [])
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        drawn = cs.sample(sources, run_id="live-shaped-a", bc_finds_total=0)
        entry = drawn.kinds[cs.THREE_WAY]
        items = [i for i in drawn.items if i.kind == cs.THREE_WAY]
        self.assertEqual((entry["status"], entry["bc_finds"]), ("read", 0))
        self.assertTrue(items)
        for item in items:
            self.assertEqual((item.relation, item.reference), (K.PO, "po.bc_purchase_invoice"))
            self.assertIn(item.hash16, drawn.readings)
        # An invoice of another vendor that carries the PO number names nothing.
        for record in world.bc["purchaseInvoices"]:
            record["fields"]["vendorNumber"] = "FXOTHER"
        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        drawn = cs.sample(sources, run_id="live-shaped-a", bc_finds_total=0)
        entry = drawn.kinds[cs.THREE_WAY]
        self.assertGreater(entry["candidates"], 0)
        self.assertEqual((entry["status"], entry["reason"]), ("empty", "no_reference"))

    def test_cross_sample_names_an_unreadable_document_kind(self):
        world = build_live_shaped_world()

        class Withheld:
            def search(self, kind, queries, **_):
                return {"records": [], "source": [f"fixture:{kind}"],
                        "results": [{"query": q, "handles": [], "complete": True,
                                     "withheld": 3} for q in queries]}

        sources = Sources(PortalReads(world.read_relation), BcReads(world.bc_port()))
        sample = cs.sample(sources, run_id="live-shaped-a", documents=Withheld())
        for kind in ("mail", "sharepoint"):
            entry = sample.kinds[kind]
            self.assertEqual((entry["status"], entry["reason"], entry["tried"]),
                             ("unreadable", "withheld", [f"fixture:{kind}"]))


if __name__ == "__main__":
    unittest.main()
