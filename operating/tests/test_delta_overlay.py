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
    s.conn.executemany("INSERT INTO delta_owned VALUES (?)", [(NEW,), (fx.PO,), (BC_NEW,)])
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
              fmt: str = delta_overlay.FORMAT, layers: list | None = None) -> None:
        pointer = self.delta.parent / "CURRENT.json"
        temp = pointer.with_suffix(".tmp")
        body = {"format": fmt, "base_digest": digest, "store": str(store or self.delta),
                "owned": 3, "built_at": "2099-02-01T11:30:00Z"}
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


if __name__ == "__main__":
    unittest.main()
