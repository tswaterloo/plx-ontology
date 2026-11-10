"""The row-delta overlay a case host reads over its served projection (operating/delta_overlay.py).

A delta store of the same DDL names the objects it owns (``delta_owned``); the host reads
those from it and every other object from the served store, under the call's own policy.
Evidence label: ``isolated-fixture`` (the fixture projection of tests/test_ontology_tools).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology.json_contract import validate
from ontology.types import Actor
from operating import delta_overlay
from operating.case_runtime import CaseRuntime
from operating.ontology_host import CaseHostTools
from operating.ontology_tools import load_contracts
from tests import test_ontology_tools as fx

CONTRACTS = load_contracts()
DIGEST = "sha256:" + "ab" * 32
NEW = "PurchaseOrder:portal.app_purchase_order:po-9"
NEW_H = "api://public.app_purchase_order/%7B%22id%22%3A%22po-9%22%7D@0000000000000002-1"
BC_NEW = "PurchaseOrder:bc.purchase_orders:bcpo-9"
BC_NEW_H = "bc://company_a/purchaseOrders/bcpo-9@cap-9"
PO_H2 = "api://public.app_purchase_order/%7B%22id%22%3A%22po-1%22%7D@0000000000000002-2"
PO_H3 = "api://public.app_purchase_order/%7B%22id%22%3A%22po-1%22%7D@0000000000000003-1"
INV2 = "SalesInvoice:bc.sales_invoices:inv-2"
INV2_H = "bc://company_a/salesInvoices/inv-2@cap-9"
SO2 = "SalesOrder:portal.app_sales_order:so-2"
SO2_H = "api://public.app_sales_order/%7B%22id%22%3A%22so-2%22%7D@0000000000000002-4"
SO_H3 = "api://public.app_sales_order/%7B%22id%22%3A%22so-1%22%7D@0000000000000002-3"


class Host(CaseHostTools):
    """The fixture's read policy, decided over the keys the host decides it over."""

    decided: list = []

    def _decide_policy(self, actor, bc_lookup):
        self.tools  # noqa: B018
        self.decided.append(tuple(self._reader_keys))
        return fx.POLICY


def _delta_store() -> fx.Store:
    s = fx.Store()
    s.conn.execute("INSERT INTO store_meta VALUES ('content_digest', ?)", (DIGEST,))
    s.conn.execute("CREATE TABLE delta_owned(object_id TEXT PRIMARY KEY)")
    # (a) new: only in the delta
    s.obj(NEW, "api:public.app_purchase_order", NEW_H, "gold_etl_from_fm")
    s.ident(NEW, "petra_po", "PO5555", "PO5555", "po_number", NEW_H, "row.po_number",
            "gold_etl_from_fm")
    # (b) changed: its alias PO4821 is now PO4822
    s.obj(fx.PO, "api:public.app_purchase_order", PO_H2, "gold_etl_from_fm")
    s.val(fx.PO, "po_number", "identifier", "PO100012", PO_H2, "row.po_number",
          "gold_etl_from_fm")
    s.ident(fx.PO, "petra_po", "PO100012", "PO100012", "po_number", PO_H2, "row.po_number",
            "gold_etl_from_fm")
    s.ident(fx.PO, "petra_po", "PO4822", "PO#4822", "po_number", PO_H2,
            "row.custom_fields.fmPoNumber", "gold_etl_from_fm", role="alias")
    # (d) new, in an audience only finance readers hold
    s.obj(BC_NEW, "bc:company_a/purchaseOrders", BC_NEW_H, "bc_company_a")
    s.ident(BC_NEW, "petra_po", "PO7777", "PO7777", "po_number", BC_NEW_H, "row.number",
            "bc_company_a")
    # (c) a link partner as context: never served from the delta
    s.obj(fx.SO, "api:public.app_sales_order", SO_H3, "gold_etl_from_fm")
    s.val(fx.SO, "customer_po", "text", "context copy", SO_H3, "row.po_reference",
          "gold_etl_from_fm")
    s.ident(fx.SO, "so_number", "SO-2099-0077", "SO-2099-0077", "order_number", SO_H3,
            "row.order_number", "gold_etl_from_fm")
    # a new invoice of the served order, and a new order with its required date
    s.obj(INV2, "bc:company_a/salesInvoices", INV2_H, "bc_company_a")
    s.link("lnk-inv-2", "SalesInvoice.invoices_order", INV2, fx.SO, "verified", "fk",
           [INV2_H, SO_H3])
    s.obj(SO2, "api:public.app_sales_order", SO2_H, "gold_etl_from_fm")
    s.conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("E:so-2-required", "SalesOrder.required", "planned",
                    "2099-03-01T00:00:00Z", None, None, SO2_H, "row.required_date",
                    fx._h(SO2_H), "gold_etl_from_fm", "[]", "2099-01-01T00:00:00Z", None))
    s.conn.execute("INSERT INTO event_objects VALUES (?,?,?)", ("E:so-2-required", SO2,
                                                              "order"))
    s.conn.executemany("INSERT INTO delta_owned VALUES (?)",
                       [(NEW,), (fx.PO,), (BC_NEW,), (INV2,), (SO2,)])
    s.conn.commit()
    return s


def _save(store: fx.Store, path: Path) -> None:
    target = sqlite3.connect(path)
    store.conn.backup(target)
    target.close()


class DeltaOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        base = fx.build()
        base.conn.execute("INSERT INTO store_meta VALUES ('content_digest', ?)", (DIGEST,))
        base.conn.commit()
        cls.store = root / "served" / "store.sqlite"
        cls.store.parent.mkdir()
        _save(base, cls.store)
        cls.delta = root / "served" / "delta" / "delta-0001.sqlite"
        cls.delta.parent.mkdir()
        _save(_delta_store(), cls.delta)
        # a newer layer re-owning the changed PurchaseOrder once more
        newer = fx.Store()
        newer.conn.execute("INSERT INTO store_meta VALUES ('content_digest', ?)", (DIGEST,))
        newer.conn.execute("CREATE TABLE delta_owned(object_id TEXT PRIMARY KEY)")
        newer.obj(fx.PO, "api:public.app_purchase_order", PO_H3, "gold_etl_from_fm")
        newer.val(fx.PO, "po_number", "identifier", "PO100012", PO_H3, "row.po_number",
                  "gold_etl_from_fm")
        newer.ident(fx.PO, "petra_po", "PO4823", "PO#4823", "po_number", PO_H3,
                    "row.custom_fields.fmPoNumber", "gold_etl_from_fm", role="alias")
        newer.conn.execute("INSERT INTO delta_owned VALUES (?)", (fx.PO,))
        newer.conn.commit()
        cls.newer = cls.delta.parent / "delta-0002.sqlite"
        _save(newer, cls.newer)
        audience = Path(__file__).resolve().parents[2] / "examples" / \
            "source_mirror_audience.example.json"
        cls.runtime = CaseRuntime(root / "state", client=None, audience=audience, tenant="plx")

    @classmethod
    def tearDownClass(cls):
        cls.runtime.close()
        cls.temp.cleanup()

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != delta_overlay.DIR_ENV}
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.point(DIGEST)
        self.host = Host(self.runtime, fx.FULL, store_path=self.store, schema=fx.SCHEMA,
                         grammar=fx.FixtureGrammar())
        self.host.tools.clock = lambda: fx.NOW
        self.addCleanup(self.host.close)

    def point(self, digest: str, store: Path | None = None,
              fmt: str = delta_overlay.FORMAT, layers: list | None = None,
              partial: bool | None = None) -> None:
        pointer = self.delta.parent / "CURRENT.json"
        temp = pointer.with_suffix(".tmp")
        body = {"format": fmt, "base_digest": digest, "store": str(store or self.delta),
                "owned": 3, "built_at": "2099-02-01T11:30:00Z"}
        if partial is not None:
            body.update(partial=partial, base="postgres")
        if layers is not None:
            body.update(store=str(layers[0]), layers=[{"store": str(path), "owned": 1}
                                                      for path in layers])
        temp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(temp, pointer)

    def call(self, name: str, arguments: dict, actor=fx.FULL) -> dict:
        result = self.host.call(name, arguments, actor)
        self.assertEqual(validate(result, CONTRACTS["tools"][name]["output"],
                                  root=CONTRACTS), [], result)
        return result

    def ids(self, result: dict) -> list[str]:
        return [c["object"]["object_id"] for c in result.get("candidates", ())]

    def test_an_owned_new_object_is_found_and_opened_and_the_policy_sees_its_unit(self):
        found = self.call("plx.find", {"query": "PO5555"})
        self.assertEqual((found["status"], self.ids(found)), ("found", [NEW]))
        self.assertEqual(found["completeness"]["returned"], 1)
        self.assertEqual(self.call("plx.object", {"object_id": NEW})["object"]["object_id"],
                         NEW)
        # the policy is decided over the delta's audience units too
        self.assertIn("bc:company_a/purchaseOrders", self.host.decided[-1])
        # a find matching served and delta objects keeps both, each once
        both = self.call("plx.find", {"query": "PO100012"})
        self.assertEqual(sorted(self.ids(both)), sorted({fx.PO, fx.PO2, fx.BCPO}))
        self.assertEqual([c["rank"] for c in both["candidates"]], [1, 2, 3])

    def test_a_find_resolves_the_names_it_reads_once_never_once_per_layer(self):
        """A layer's answer merges only its candidates: an entity resolution is the served
        store's alone and is never asked of a layer, and a find's layer answers resolve
        no names (each would repeat the entity index's searches)."""
        from operating import ontology_tools
        ran, resolved = [], []
        real_find, real_entities = ontology_tools.OntologyTools._find, \
            ontology_tools.OntologyTools._entities

        def finding(tools, arguments, actor):
            ran.append((id(tools), arguments.get("mode")))
            return real_find(tools, arguments, actor)

        def entities(tools, actor, text, object_type=None):
            resolved.append(id(tools))
            return real_entities(tools, actor, text, object_type)
        with mock.patch.object(ontology_tools.OntologyTools, "_find", finding), \
                mock.patch.object(ontology_tools.OntologyTools, "_entities", entities):
            self.assertEqual(self.ids(self.call("plx.find", {"query": "PO5555"})), [NEW])
            self.assertEqual(len({tools for tools, _mode in ran}), 2)   # served + layer
            self.assertEqual(len(resolved), 1)
            ran.clear()
            resolved.clear()
            self.call("plx.find", {"mode": "entities", "text": "PO5555"})
            self.assertEqual(len(ran), 1)
            self.assertEqual(len(resolved), 1)

    def _asked(self, name, arguments, *, layers_for=None, left=None):
        """(the answer, the number of stores whose plx.find ran) of one call."""
        from operating import call_limits, ontology_host, ontology_tools
        ran, real_find = [], ontology_tools.OntologyTools._find

        def finding(tools, arguments, actor):
            ran.append(id(tools))
            return real_find(tools, arguments, actor)
        patches = [mock.patch.object(ontology_tools.OntologyTools, "_find", finding)]
        if layers_for is not None:
            patches.append(mock.patch.object(ontology_host, "_layers_for", layers_for))
        if left is not None:
            patches.append(mock.patch.object(call_limits, "seconds_left", lambda: left))
        for patch in patches:
            patch.start()
        try:
            return self.call(name, arguments), len(set(ran))
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_a_filter_listing_asks_only_the_layers_owning_its_type_with_the_same_answer(self):
        """A layer owning no record of a filter listing's type cannot change it: it is not
        asked, and the answer is the one every layer gives; a query find asks them all."""
        self.point(DIGEST, layers=[self.newer, self.delta])
        every = lambda layers, arguments: list(layers)  # noqa: E731
        for arguments, stores in (
                ({"object_type": "SalesOrder",
                  "filters": [{"property": "customer_po", "op": "present"}]}, 2),
                ({"object_type": "PurchaseOrder",
                  "filters": [{"property": "po_number", "op": "eq", "value": "PO100012"}]}, 3),
                ({"query": "PO100012"}, 3)):
            got, asked = self._asked("plx.find", arguments)
            whole, _ = self._asked("plx.find", arguments, layers_for=every)
            self.assertEqual(asked, stores, arguments)
            # the same answer (a set handle is minted per call)
            self.assertEqual({k: v for k, v in got.items() if k != "set"},
                             {k: v for k, v in whole.items() if k != "set"}, arguments)
        self.assertIn(fx.PO, self.ids(self._asked("plx.find", {
            "object_type": "PurchaseOrder",
            "filters": [{"property": "po_number", "op": "eq", "value": "PO100012"}]})[0]))

    def test_a_find_short_of_time_returns_the_served_answer_with_a_gap_not_nothing(self):
        """With too little of the call's limit left to ask the layers, the served answer
        comes back saying it reads the served generation only, never a timeout."""
        arguments = {"object_type": "PurchaseOrder",
                     "filters": [{"property": "po_number", "op": "eq", "value": "PO100012"}]}
        got, asked = self._asked("plx.find", arguments, left=2.5)
        self.assertEqual(asked, 1)
        self.assertNotIn("error", got)
        self.assertTrue(self.ids(got))
        self.assertIn(delta_overlay._UNMERGED_GAP["reason"],
                      [g.get("reason") for g in got["gaps"]])
        self.assertFalse(got["completeness"]["complete"])

    def test_a_changed_object_is_read_from_the_delta_only(self):
        opened = self.call("plx.object", {"object_id": fx.PO})
        self.assertIn(PO_H2, json.dumps(opened))
        self.assertNotIn(fx.PO_H, json.dumps(opened))
        # its old alias no longer finds it; its new one does
        self.assertNotIn(fx.PO, self.ids(self.call("plx.find", {"query": "PO#4821"})))
        self.assertEqual(self.ids(self.call("plx.find", {"query": "PO#4822"})), [fx.PO])

    def test_a_context_object_of_the_delta_is_never_served_from_it(self):
        opened = json.dumps(self.call("plx.object", {"object_id": fx.SO}))
        self.assertIn(fx.SO_V2, opened)
        self.assertNotIn("context copy", opened)
        self.assertNotIn(SO_H3, opened)
        self.assertEqual(self.ids(self.call("plx.find", {"query": "SO-2099-0077"})), [])

    def test_a_reader_without_the_audience_gets_nothing_of_an_owned_object(self):
        self.assertEqual(self.ids(self.call("plx.find", {"query": "PO7777"})), [BC_NEW])
        hidden = self.call("plx.find", {"query": "PO7777"}, actor=fx.SALES)
        self.assertEqual((hidden["status"], self.ids(hidden)), ("not_found", []))
        self.assertEqual(self.call("plx.object", {"object_id": BC_NEW}, actor=fx.SALES)
                         ["error"]["code"], "object_not_found")
        self.assertNotIn(BC_NEW, self.ids(self.call("plx.find", {"query": "PO100012"},
                                                    actor=fx.SALES)))

    def test_an_overlay_of_another_generation_or_unreadable_is_ignored(self):
        for digest, store, fmt in (("sha256:" + "cd" * 32, None, delta_overlay.FORMAT),
                                   (DIGEST, self.delta.parent / "missing.sqlite",
                                    delta_overlay.FORMAT),
                                   (DIGEST, None, "plx-delta/v2")):
            self.point(digest, store, fmt)
            self.assertEqual(self.call("plx.object", {"object_id": NEW})["error"]["code"],
                             "object_not_found")
            self.assertEqual(self.ids(self.call("plx.find", {"query": "PO5555"})), [])
            self.assertIn(fx.PO_H, json.dumps(self.call("plx.object", {"object_id": fx.PO})))
        self.point(DIGEST)   # a new pointer is read on the next request
        self.assertEqual(self.ids(self.call("plx.find", {"query": "PO5555"})), [NEW])

    def test_the_newest_layer_owning_an_object_serves_it(self):
        self.point(DIGEST, layers=[self.newer, self.delta])
        opened = json.dumps(self.call("plx.object", {"object_id": fx.PO}))
        self.assertIn(PO_H3, opened)
        self.assertNotIn(PO_H2, opened)
        self.assertEqual(self.ids(self.call("plx.find", {"query": "PO#4823"})), [fx.PO])
        self.assertEqual(self.ids(self.call("plx.find", {"query": "PO#4822"})), [])
        # an object only the older layer owns is still served from it
        self.assertEqual(self.ids(self.call("plx.find", {"query": "PO5555"})), [NEW])
        self.assertEqual(self.call("plx.object", {"object_id": NEW})["object"]["object_id"],
                         NEW)
        # a layer of another generation voids the whole pointer
        self.point(DIGEST, layers=[self.newer, self.delta.parent / "missing.sqlite"])
        self.assertEqual(self.ids(self.call("plx.find", {"query": "PO5555"})), [])

    def test_identity_and_documents_read_an_owned_object_from_its_layer_for_its_readers(self):
        identity = self.call("plx.identity", {"object_ids": [NEW]})
        self.assertNotIn("error", identity)
        self.assertNotIn("error", self.call("plx.documents", {"object_id": NEW}))
        # without the delta the served store has no such object
        self.point("sha256:" + "cd" * 32)
        self.assertEqual(self.call("plx.identity", {"object_ids": [NEW]})["error"]["code"],
                         "object_not_found")
        self.point(DIGEST)
        # a reader outside the object's audience learns nothing of it; one inside does
        for name, arguments in (("plx.identity", {"object_ids": [BC_NEW]}),
                                ("plx.documents", {"object_id": BC_NEW})):
            self.assertNotIn("error", self.call(name, arguments), name)
            self.assertEqual(self.call(name, arguments, actor=fx.SALES)["error"]["code"],
                             "object_not_found", name)
        # served and layered objects in one call: the served answer, saying so
        mixed = self.call("plx.identity", {"object_ids": [NEW, fx.FM]})
        self.assertEqual(mixed["gaps"][-1]["reason"], "delta_overlay_unmerged")

    def test_around_and_timeline_from_an_owned_root_read_its_layer(self):
        hop = {"link_type": "SalesInvoice.invoices_order", "direction": "forward"}
        around = self.call("plx.around", {"from": INV2, "path": [hop]})
        self.assertIn(fx.SO, [o["object_id"] for o in around["objects"]])
        self.assertEqual([e["event_id"] for e in
                          self.call("plx.timeline", {"from": SO2})["events"]],
                         ["E:so-2-required"])
        # a deeper path: its first hop, saying the rest is read at the next generation
        deeper = self.call("plx.around", {"from": INV2, "path": [hop, {
            "link_type": "SalesOrder.same_record_as", "direction": "forward"}]})
        self.assertIn(fx.SO, [o["object_id"] for o in deeper["objects"]])
        self.assertEqual(deeper["gaps"][-1]["reason"], delta_overlay.HOPS_DEFERRED)
        self.assertFalse(deeper["completeness"]["complete"])
        # a reader outside the invoice's audience gets nothing through the layer
        self.assertEqual(self.call("plx.around", {"from": INV2, "path": [hop]},
                                   actor=fx.SALES)["error"]["code"], "object_not_found")

    def test_aggregate_and_compare_say_how_many_changed_records_they_did_not_read(self):
        def reasons(result):
            return [gap["reason"] for gap in result["gaps"]]
        tools = self.host.tools
        counted = self.call("plx.aggregate", {
            "set": tools.sets.new(fx.FULL, [NEW, BC_NEW, fx.PO2], complete=True), "measure": {"op": "count"}})
        gap = counted["gaps"][-1]
        self.assertEqual(gap["reason"], delta_overlay.NOT_AGGREGATED)
        self.assertIn("2 record(s)", gap["detail"])
        compared = self.call("plx.compare", {"left": fx.PO, "right": fx.PO2})
        self.assertIn(delta_overlay.NOT_AGGREGATED, reasons(compared))
        # only the changed records this reader may read are counted
        sales = self.call("plx.aggregate", {
            "set": tools.sets.new(fx.SALES, [NEW, BC_NEW, fx.PO2], complete=True),
            "measure": {"op": "count"}}, actor=fx.SALES)
        self.assertIn("1 record(s)", sales["gaps"][-1]["detail"])
        # nothing changed among them: no gap
        plain = self.call("plx.aggregate", {"set": tools.sets.new(fx.FULL, [fx.PO2], complete=True),
                                            "measure": {"op": "count"}})
        self.assertNotIn(delta_overlay.NOT_AGGREGATED, reasons(plain))


    def test_an_overlay_a_postgres_base_trimmed_says_so_on_every_answer_and_hides_nothing_more(self):
        """A pointer published over the Postgres base lists only the newest layers
        (``partial``): a reader of the SQLite generation and that overlay (Postgres not
        serving) gets every answer marked ``delta_overlay_partial``, within the contract,
        and still nothing it may not read."""
        def reasons(result):
            return [gap["reason"] for gap in result.get("gaps") or ()]
        self.point(DIGEST, partial=True)
        tools = self.host.tools
        answers = {
            "plx.find": self.call("plx.find", {"query": "PO5555"}),
            "plx.object": self.call("plx.object", {"object_id": NEW}),
            "plx.around": self.call("plx.around", {"from": INV2, "path": [
                {"link_type": "SalesInvoice.invoices_order", "direction": "forward"}]}),
            "plx.timeline": self.call("plx.timeline", {"from": SO2}),
            "plx.aggregate": self.call("plx.aggregate", {
                "set": tools.sets.new(fx.FULL, [fx.PO2], complete=True),
                "measure": {"op": "count"}}),
        }
        self.assertEqual(self.ids(answers["plx.find"]), [NEW])   # the layers still serve
        for name, result in answers.items():
            self.assertIn(delta_overlay.PARTIAL, reasons(result), name)
            if "completeness" in result:
                self.assertFalse(result["completeness"]["complete"], name)
        hidden = self.call("plx.find", {"query": "PO7777"}, actor=fx.SALES)
        self.assertEqual((hidden["status"], self.ids(hidden)), ("not_found", []))
        self.assertIn(delta_overlay.PARTIAL, reasons(hidden))
        self.assertNotIn(BC_NEW, json.dumps(hidden))
        self.assertEqual(self.call("plx.object", {"object_id": BC_NEW}, actor=fx.SALES)
                         ["error"]["code"], "object_not_found")
        # a whole pointer (a SQLite base, or one written before the field): no such gap
        for partial in (False, None):
            self.point(DIGEST, partial=partial)
            self.assertNotIn(delta_overlay.PARTIAL,
                             reasons(self.call("plx.find", {"query": "PO5555"})))


class MergeFindTests(unittest.TestCase):

    def test_a_layer_that_cut_its_answer_leaves_the_merged_answer_incomplete(self):
        def candidate(oid):
            return {"object": {"object_id": oid}, "rank": 1}
        base = {"status": "found", "candidates": [candidate("served")], "gaps": [],
                "completeness": {"complete": True, "truncated": False, "returned": 1,
                                 "total": 1, "next_cursor": None, "reason": None}}
        layer = {"status": "ambiguous", "candidates": [candidate("new-1"), candidate("new-2")],
                 "gaps": [], "completeness": {"complete": False, "truncated": True,
                                              "returned": 2, "total": None,
                                              "next_cursor": "c", "reason": "budget"}}
        owned = frozenset({"new-1", "new-2", "new-3"})
        merged = delta_overlay.merge_find(base, [(layer, owned)], owned, {"query": "x"})
        self.assertEqual([c["object"]["object_id"] for c in merged["candidates"]],
                         ["served", "new-1", "new-2"])
        self.assertIs(merged["completeness"]["complete"], False)
        self.assertIs(merged["completeness"]["truncated"], True)
        self.assertIsNone(merged["completeness"]["total"])


if __name__ == "__main__":
    unittest.main()
