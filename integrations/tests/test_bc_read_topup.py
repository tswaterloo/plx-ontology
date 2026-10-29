"""A completed BC capture is a starting point for the next invocation, never proof of
freshness.

The bug: a consumer (the projection probe, the order-margin eval) topped up any store file
it found, so a second process over a store an earlier one had finished made no entity GET
and reported ``all_complete`` while serving rows the source had since changed or deleted.
Now a unit is reused only when this verifier session completed it (``fresh_since``, the
session's start the verifier passes as ``bc_read.FRESH_SINCE_ENV``); any other unit is read
again, and every complete unit reports its capture age. Injected transport (the BC fake of
``integrations/tests/test_bc_capture.py``); the store clock is the harness's.
"""

from __future__ import annotations

from datetime import timedelta
import os
import tempfile
import unittest

from integrations import bc_read
from integrations.capture_store import CaptureStore, bc_unit
from integrations.live_source_guard import LiveSourceAudience
from integrations.tests import test_bc_capture as tb
from ontology.types import Actor
from operating.host import HostConfig
from scripts import ontology_projection_probe as probe

ORDERS = "purchaseOrders"


def iso(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_probe(tmp: str, fake: tb.FakeBc, environ=tb.ENV) -> tuple[dict, set[str]]:
    """One projection probe process over ``tmp``'s state; the entity sets it read."""
    before = len(fake.calls)
    result = probe.projection_probe(
        None, f"{tmp}/mirror.sqlite", f"{tmp}/store.sqlite", claim_sample=10,
        principal="staff:reader", bc={"environ": environ, "sender": fake, "books": tb.BOOKS},
        monotonic=lambda: float(len(fake.calls)))
    reads = {url.split("?")[0].rsplit("/", 1)[-1].split("(")[0]
             for method, url, _ in fake.calls[before:] if method == "GET"}
    return result, reads


def current_orders(tmp: str) -> dict[str, str]:
    """The probe's BC capture as its served reader sees it: current order id -> amount."""
    store = f"{tmp}/store.sqlite"
    config = HostConfig.from_file(os.path.join(probe.served_state_path(store),
                                               probe.SERVED_CONFIG))
    held = CaptureStore.open_read_only(probe.bc_capture_path(store),
                                       LiveSourceAudience.from_file(config.live_audience))
    try:
        items = held.records(Actor(config.tenant, frozenset({config.principal}), "restricted"),
                             "bc", bc_unit(tb.CO[0], ORDERS), limit=100)["items"]
    finally:
        held.close()
    return {i["row"]["id"]: str(i["row"]["amount"]) for i in items}


class CrossInvocationTest(unittest.TestCase):
    def test_a_second_invocation_reads_a_completed_store_again(self) -> None:
        """Reproduction: the source changes an amount and deletes an order
        between two probe processes over the same state."""
        fake = tb.FakeBc(companies=tb.CO)
        with tempfile.TemporaryDirectory() as tmp:
            first, _ = run_probe(tmp, fake)
            rows = fake.data[(tb.CO[0], ORDERS)]
            changed, deleted = rows[1]["id"], rows.pop(2)["id"]
            rows[1]["amount"] = "#987.50"
            second, reads = run_probe(tmp, fake)
            orders = current_orders(tmp)
        self.assertTrue(first["bc_capture"]["all_complete"])
        self.assertIn(ORDERS, reads)  # entity GETs, not only the company listing
        self.assertNotIn(deleted, orders)
        self.assertEqual(orders[changed], "987.50")
        capture = second["bc_capture"]
        self.assertEqual((capture["all_complete"], capture["topped_up"],
                          capture["units_reused"]), (True, False, 0))
        self.assertEqual(capture["units_complete"], capture["units_total"])
        self.assertIsInstance(capture["capture_age_seconds"], float)

    def test_a_session_reuses_only_what_it_completed_and_reports_its_age(self) -> None:
        h = tb.Harness(self, tb.FakeBc(companies=tb.CO))
        with self.assertRaises(ValueError):  # fails closed, before any GET
            h.run(fresh_since="yesterday")
        self.assertEqual(h.fake.bc_gets(), [])
        h.run()  # an earlier invocation
        session = tb.T0 + timedelta(days=1)
        h.clock.now = session
        produced = h.run(budget_seconds=150)  # the session's producer, cut by its budget
        self.assertEqual(produced["stopped_reason"], "budget")
        reached = {u["unit"]: u for c in produced["companies"] for u in c["units"]}
        done = {u for u, r in reached.items() if r["complete"]}
        self.assertTrue(done)
        # A unit the producer did not reach still holds the earlier invocation's capture.
        old = next(u for u in (bc_unit(c, s) for c in tb.CO for s in bc_read.CAPTURE_ENTITY_SETS)
                   if u not in reached)
        company, entity_set = old.split(":", 1)[1].split("/")
        rows = h.fake.data[(company, entity_set)]
        deleted = rows.pop()[bc_read.grammar.BC_RECORD_KEY_FIELD.get(entity_set, "id")]
        h.clock.now = session + timedelta(hours=2)
        before = len(h.fake.calls)
        consumer = h.run(fresh_since=iso(session))
        units = {u["unit"]: u for c in consumer["companies"] for u in c["units"]}
        reused = {u for u, r in units.items() if r.get("reused")}
        self.assertTrue(consumer["all_complete"])
        self.assertEqual(reused, done - {bc_unit(c, bc_read.COMPANY_SET) for c in tb.CO})
        self.assertEqual(consumer["units_reused"], len(reused))
        # Ages run from each run's start: the reused units' (and a unit the producer paused
        # and the consumer resumed) are the producer's, the old unit's is this read's.
        self.assertEqual({units[u]["capture_age_seconds"] for u in reused}, {7200.0})
        self.assertEqual(units[old]["capture_age_seconds"], 0.0)
        self.assertEqual(consumer["capture_age_seconds"], 7200.0)
        read = {url for _m, url, _h in h.fake.calls[before:]}
        self.assertTrue(any(f"companies({company})/{entity_set}" in url for url in read))
        self.assertNotIn(deleted, [i["row"].get("id") for i in h.store.records(
            tb.SERVICE, "bc", old, limit=100)["items"]])
        # Outside a session nothing is reused: every unit is read again.
        again = h.run()
        self.assertEqual((again["units_reused"], again["all_complete"]), (0, True))


if __name__ == "__main__":
    unittest.main()
