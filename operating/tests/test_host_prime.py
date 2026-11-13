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


class KeptFactsInMemory(unittest.TestCase):
    """The facts held in memory per process: of each store, only its two newest
    identities (over Postgres every delta layer moves it); other stores keep theirs."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        traverse._LOADED.clear()
        self.addCleanup(traverse._LOADED.clear)

    def store(self, name: str) -> str:
        path = self.dir / name
        path.touch()
        return str(path)

    def keep(self, store: str, version: int) -> dict:
        identity = {"postgres": "db", "data_version": str(version), "code": "c"}
        traverse._save_facts(store, identity, {"audience_keys": (f"k{version}",)})
        return identity

    def test_each_store_holds_only_its_current_identity_over_many_versions(self):
        served, layer = self.store("pg-marker"), self.store("other.sqlite")
        other = self.keep(layer, 0)
        self.assertEqual(traverse._load_facts(layer, other)["audience_keys"], ("k0",))
        for count in (1, 10, 60):
            first = None
            for version in range(1, count + 1):
                identity = self.keep(served, version)
                first = first or identity
                got = traverse._load_facts(served, identity)
                self.assertEqual(got["audience_keys"], (f"k{version}",))
            held = [key for key in traverse._LOADED if key[0] == served]
            self.assertEqual(len(held), min(count, 2), count)
            self.assertIn((layer, json.dumps(other, sort_keys=True)), traverse._LOADED)
            self.assertEqual(traverse._load_facts(served, identity)["audience_keys"],
                             (f"k{count}",))
            if count > 2:   # an identity two back is not served from memory or disk
                self.assertIsNone(traverse._load_facts(served, first))
            self.assertEqual(len(traverse._LOADED), 1 + min(count, 2))


class FactsReadApart(unittest.TestCase):
    """A host's read of Postgres facts in full runs in a process of its own: no
    credential-shaped variable in its environment, the DSN never on its command line, a
    time limit, and a read that fails keeps nothing."""

    UNREACHABLE = "host=/nonexistent-plx-socket-dir port=1 dbname=none connect_timeout=2"

    def test_the_process_gets_no_credentials_and_the_dsn_only_on_its_input(self):
        import subprocess
        seen = {}
        real = subprocess.Popen

        def spy(args, **kwargs):
            seen["args"], seen["env"] = args, kwargs.get("env")
            return real(args, **kwargs)
        with mock.patch.dict(os.environ, {"AZURE_CLIENT_SECRET": "x", "PLX_WORKBENCH_API_KEY": "y",
                                          "SOME_TOKEN": "z", "PGOPTIONS": "-c jit=off"}), \
                mock.patch.object(subprocess, "Popen", spy):
            self.assertFalse(traverse._keep_pg_facts_apart(self.UNREACHABLE, ()))
        self.assertTrue({"AZURE_CLIENT_SECRET", "PLX_WORKBENCH_API_KEY",
                         "SOME_TOKEN"}.isdisjoint(seen["env"]))
        self.assertEqual(seen["env"]["PGOPTIONS"], "-c jit=off")
        self.assertNotIn("nonexistent-plx-socket-dir", " ".join(seen["args"]))

    def test_the_read_ends_with_its_parent_and_leaves_no_half_written_file(self):
        # a parent stopped without its exit handlers (SIGKILL): its facts read stops
        # within seconds and removes its half-written file, never scans on for half an hour
        import signal
        import subprocess
        import sys
        import time as clock
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        hang = ("import os, pathlib, time; from operating import ontology_tools_traverse as t\n"
                "def keep(*a, **k):\n"
                "    pathlib.Path(os.environ['PLX_PG_FACTS_DIR'], "
                "f'..store-facts-1-2.json.{os.getpid()}.ab12.tmp').write_text('half')\n"
                "    print(os.getpid(), flush=True); time.sleep(60)\n"
                "t.keep_pg_facts = keep; t._keep_pg_facts_child()")
        parent_code = (
            "import subprocess, sys; from operating import ontology_tools_traverse as t\n"
            "real = subprocess.Popen\n"
            f"subprocess.Popen = lambda args, **kw: real([args[0], '-c', {hang!r}], "
            "**{**kw, 'stdout': None})\n"
            "t.keep_pg_facts = lambda *a, **k: False\n"
            f"t._keep_pg_facts_apart({self.UNREACHABLE!r}, ())")
        root = str(Path(traverse.__file__).resolve().parents[1])
        env = {**os.environ, "PLX_PG_FACTS_DIR": str(folder), "PYTHONPATH": root}
        parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE,
                                  env=env, cwd=root, text=True)
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        child = int(parent.stdout.readline())
        self.assertEqual(len(list(folder.iterdir())), 1)    # the read is under way
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()

        def alive() -> bool:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return False
            # reaped by whoever adopted it, or a zombie awaiting that: ended either way
            try:
                with open(f"/proc/{child}/stat") as stat:
                    return stat.read().split(") ", 1)[1][:1] != "Z"
            except OSError:
                return True
        deadline = clock.monotonic() + 15
        while alive() and clock.monotonic() < deadline:
            clock.sleep(0.2)
        if alive():
            os.kill(child, signal.SIGKILL)
            self.fail("the facts read outlived its parent")
        self.assertEqual(list(folder.iterdir()), [])

    def test_a_read_past_its_time_is_stopped_and_keeps_nothing(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        # the process starts writing its facts file, then hangs past its time
        hang = ("import os, pathlib, time; pathlib.Path(os.environ['PLX_PG_FACTS_DIR'], "
                "f'..store-facts-1-2.json.{os.getpid()}.ab12.tmp').write_text('half'); "
                "time.sleep(30)")
        with mock.patch.object(traverse, "FACTS_APART_SECONDS", 3), \
                mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": str(folder)}), \
                mock.patch.object(traverse, "keep_pg_facts", return_value=False):
            import subprocess
            real = subprocess.Popen
            with mock.patch.object(subprocess, "Popen", lambda args, **kw: real(
                    [args[0], "-c", hang], **kw)):
                import time as clock
                started = clock.monotonic()
                self.assertFalse(traverse._keep_pg_facts_apart(self.UNREACHABLE, ()))
                self.assertLess(clock.monotonic() - started, 20)
        self.assertEqual(list(folder.iterdir()), [])          # no half-written file left
