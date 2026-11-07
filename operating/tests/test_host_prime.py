"""A host primed before it knows its reader (``ontology_host.prime_unbound``, run by a warm
host before its ticket binds it) answers every reader exactly as a cold host does, and its
first plx.around / plx.timeline reads no store-wide fact: the served store's and every delta
layer's facts are kept beside their files and loaded. Evidence label: ``isolated-fixture``
(the fixture projection of tests/test_ontology_tools and the delta layers of
test_delta_overlay; no network)."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from operating import delta_overlay, host_peers, mcp_server, ontology_host
from operating import ontology_tools_traverse as traverse
from operating.case_runtime import CaseRuntime
from operating.tests import test_delta_overlay as dx
from tests import test_ontology_tools as fx

HOP = {"link_type": "SalesInvoice.invoices_order", "direction": "forward"}
# Layer-owned roots (the facts read on a first call five minutes long on the real store)
# and served ones, a reader's audience-gated find and an item-gated mail.
CALLS = [("plx.around", {"from": dx.INV2, "path": [HOP]}),
         ("plx.timeline", {"from": dx.SO2}),
         ("plx.object", {"object_id": dx.NEW}),
         ("plx.find", {"query": "PO7777"}),
         ("plx.around", {"from": fx.INV, "path": [HOP]}),
         ("plx.timeline", {"from": fx.SO}),
         ("plx.find", {"query": "blue widgets"}),
         ("plx.object", {"object_id": fx.MAIL}),
         ("plx.object", {"object_id": fx.DOC})]
FACT_READS = ("audience_keys", "audiences", "event_types", "slices.object", "slices.link",
              "slices.event")


# A set handle names one session's registry entry (random per session, as a cold host's
# are among themselves): compared as the same placeholder.
SET_HANDLE = re.compile(r"set:[A-Za-z0-9_-]{16,}")


def _excluding(handle: str | None):
    """An item gate withholding one person's mail from every reader (as the people
    exclusion withholds an excluded person's records)."""
    return lambda actor, item: item != handle


class HostPrimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        base = fx.build()
        base.conn.execute("INSERT INTO store_meta VALUES ('content_digest', ?)", (dx.DIGEST,))
        base.conn.commit()
        cls.store = root / "served" / "store.sqlite"
        cls.store.parent.mkdir()
        dx._save(base, cls.store)
        cls.layers = [root / "served" / "delta" / "delta-0002.sqlite",
                      root / "served" / "delta" / "delta-0001.sqlite"]
        cls.layers[0].parent.mkdir()
        dx._save(dx._delta_store(), cls.layers[1])
        dx._save(dx._delta_store(), cls.layers[0])
        pointer = cls.layers[0].parent / "CURRENT.json"
        pointer.write_text(json.dumps({
            "format": delta_overlay.FORMAT, "base_digest": dx.DIGEST,
            "store": str(cls.layers[0]), "owned": 5, "built_at": "2099-02-01T11:30:00Z",
            "layers": [{"store": str(p), "owned": 5} for p in cls.layers]}),
            encoding="utf-8")
        audience = Path(__file__).resolve().parents[2] / "examples" / \
            "source_mirror_audience.example.json"
        cls.runtime = CaseRuntime(root / "state", client=None, audience=audience, tenant="plx")

    @classmethod
    def tearDownClass(cls):
        cls.runtime.close()
        cls.temp.cleanup()

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != delta_overlay.DIR_ENV}
        env["PLX_FACTS_WORKERS"] = "1"
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.forget()

    def forget(self) -> None:
        """A fresh process: nothing kept in memory, nothing kept beside the stores."""
        for path in (self.store, *self.layers):
            for kept in path.parent.glob(traverse.FACTS_PREFIX + "*"):
                kept.unlink()
        traverse._LOADED.clear()
        traverse._BACKGROUND.clear()
        delta_overlay._LAYER_FACTS.clear()
        delta_overlay._OWNERSHIP.clear()

    def answers(self, actor, gate=None) -> tuple[list[str], dict]:
        """Every call's result as bytes for ``actor``, on a new host, and what the calls read
        store-wide (``phase_meter``'s ``store`` misses)."""
        host = dx.Host(self.runtime, actor, store_path=self.store, schema=fx.SCHEMA,
                       grammar=fx.FixtureGrammar())
        try:
            host.tools.clock = lambda: fx.NOW
            if gate is not None:
                host.tools.item_gate = gate
            with traverse.phase_meter() as meter:
                out = [SET_HANDLE.sub("set:<session>", json.dumps(
                    host.call(name, dict(arguments), actor), sort_keys=True))
                    for name, arguments in CALLS]
            return out, dict(meter.get("store") or {})
        finally:
            host.close()

    def test_a_primed_host_answers_every_reader_as_a_cold_one_and_reads_no_store_fact(self):
        # every item admitted by the item gate but, for the exclusion, one person's mail
        readers = {"full": (fx.FULL, _excluding(None)),
                   "restricted": (fx.SALES, _excluding(None)),
                   "excluded": (fx.FULL, _excluding(fx.MAIL_H))}
        cold = {}
        for name, (actor, gate) in readers.items():
            self.forget()
            cold[name], read = self.answers(actor, gate)
            # a cold host reads the layer's facts inside its first plx.around
            self.assertTrue(set(FACT_READS) & set(read), (name, read))
        # the readers' answers differ, so equality below is not vacuous
        self.assertNotEqual(cold["full"], cold["restricted"])
        self.assertNotEqual(cold["full"], cold["excluded"])
        # the excluded person's mail is withheld from the reader who otherwise reads it
        mail = CALLS.index(("plx.object", {"object_id": fx.MAIL}))
        self.assertNotIn('"error"', cold["full"][mail])
        self.assertIn('"object_not_found"', cold["excluded"][mail])

        self.forget()
        primed = ontology_host.prime_unbound(self.store, tenant="plx")
        self.assertEqual(primed, {"store": "kept", "layers": 2, "layers_kept": 2})
        for path in (self.store, *self.layers):
            self.assertTrue(traverse.facts_path(path).is_file(), path)
        for name, (actor, gate) in readers.items():
            with self.subTest(reader=name):
                warm, read = self.answers(actor, gate)
                self.assertEqual(warm, cold[name])
                self.assertFalse(set(FACT_READS) & set(read), read)

    def test_a_layer_read_in_the_background_is_kept_once_for_every_process(self):
        # a store above the lazy floor: its facts are read off the request path and kept
        with mock.patch.object(traverse, "LAZY_MIN_BYTES", 0), \
                mock.patch.object(traverse, "_prime_store_facts",
                                  wraps=traverse._prime_store_facts) as reads:
            primed = ontology_host.prime_unbound(self.store, tenant="plx")
            self.assertEqual(primed["layers_kept"], 2)
            self.assertEqual(reads.call_count, 3)   # the store and two layers, once each
            # another process opening the same files reads none of them again
            self.forget_memory()
            ontology_host.prime_unbound(self.store, tenant="plx")
            self.assertEqual(reads.call_count, 3)
        self.assertEqual(self.answers(fx.SALES)[1].keys() & set(FACT_READS), set())

    def test_a_stuck_facts_lock_times_out_into_a_whole_read_never_a_partial_one(self):
        import fcntl
        store = Path(self.temp.name) / "locked.sqlite"
        store.write_bytes(b"x")
        lock = traverse.facts_path(store).with_suffix(".lock")
        holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            with traverse._facts_lock(str(store), wait=0.2):
                reached = True   # waited its limit, then went on to read the whole set
        finally:
            os.close(holder)
        self.assertTrue(reached)
        with traverse._facts_lock(str(store), wait=0.2):    # a free lock is taken at once
            pass

    def test_facts_and_lock_files_of_layers_that_no_longer_exist_are_removed(self):
        from ontology import delta
        directory = Path(self.temp.name) / "pruned"
        directory.mkdir()
        gone = directory / "delta-gone.sqlite"
        kept = directory / "delta-kept.sqlite"
        gone.write_bytes(b"a")
        kept.write_bytes(b"b")
        for layer in (gone, kept):
            facts = traverse.facts_path(layer)
            facts.write_text("{}")
            facts.with_suffix(".lock").write_text("")
        names = {traverse.facts_path(gone).name, traverse.facts_path(gone).with_suffix(
            ".lock").name}
        gone.unlink()
        delta.prune(directory, keep={kept.resolve()})
        left = {e.name for e in directory.iterdir()}
        self.assertFalse(names & left, left)
        self.assertTrue({traverse.facts_path(kept).name,
                         traverse.facts_path(kept).with_suffix(".lock").name} <= left)

    def forget_memory(self) -> None:
        traverse._LOADED.clear()
        traverse._BACKGROUND.clear()
        delta_overlay._LAYER_FACTS.clear()
        delta_overlay._OWNERSHIP.clear()

    def test_unreadable_or_missing_stores_prime_nothing_and_never_raise(self):
        self.assertEqual(ontology_host.prime_unbound(Path(self.temp.name) / "absent.sqlite"),
                         {"store": "absent", "layers": 0, "layers_kept": 0})
        junk = Path(self.temp.name) / "junk.sqlite"
        junk.write_bytes(b"not a database" * 10)
        result = ontology_host.prime_unbound(junk)
        self.assertEqual((result["layers"], result["store"]), (0, "unprimed"))
        self.assertFalse(traverse.facts_path(junk).exists())

    def test_a_warm_host_primes_before_its_ticket_names_a_reader(self):
        seen: dict = {}
        primed = threading.Event()

        def record(args):
            seen["ticket"] = os.environ.get(dx_ticket)
            seen["store"] = args.ontology_store
            primed.set()
            return {}

        dx_ticket = "PLX_TEST_TICKET"
        args = argparse.Namespace(session_ticket_env=dx_ticket, ontology_store=self.store)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ticket"

            def give(*_a, **_k):
                self.assertTrue(primed.wait(10))   # prime ran before any ticket existed
                return "bound-ticket"

            with mock.patch.dict(os.environ, {host_peers.TICKET_FILE_ENV: str(path)}), \
                    mock.patch.object(mcp_server, "prime_unbound", record), \
                    mock.patch.object(host_peers, "await_ticket", give):
                os.environ.pop(dx_ticket, None)
                mcp_server._bind_warm_ticket(argparse.ArgumentParser(), args)
                self.assertEqual(os.environ[dx_ticket], "bound-ticket")
        self.assertEqual(seen, {"ticket": None, "store": self.store})


if __name__ == "__main__":
    unittest.main()
