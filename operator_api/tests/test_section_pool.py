"""The section pool (``operator_api.section_pool``): a page's sections read at once, each
slot its own runtime; fair across readers; a slot that is gone hands its sections back to
the read worker; the page's quick read starts its sections ahead. The real slot child over
the console's fixture world serves the reader the same section the read worker does and a
reader who may not see the record nothing. isolated-fixture; no network."""

from __future__ import annotations

import contextlib
import io
import multiprocessing
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from operator_api import backend as be
from operator_api import models as m
from operator_api import section_pool as sp
from operator_api.auth import Reader
from operator_api.server import build_parser
from operating import session_identity


def thread_start(serve):
    """A ``SectionPool`` start whose child is ``serve(conn, number)`` on a thread."""
    def start(number):
        parent, child = multiprocessing.Pipe()
        thread = threading.Thread(target=serve, args=(child, number), daemon=True)
        thread.start()
        return parent, SimpleNamespace(join=thread.join, is_alive=thread.is_alive,
                                       terminate=lambda: None)
    return start


def reader(*principals):
    return Reader(session_identity.fixture_session(
        "plx", principals, channel=session_identity.CHANNEL_CONSOLE), "dev", "")


class PoolTest(unittest.TestCase):
    def pool(self, serve, size=1):
        pool = sp.SectionPool(size, thread_start(serve))
        self.addCleanup(pool.stop)
        for slot in pool.slots:
            self.assertTrue(slot.ready.wait(10))
        return pool

    def test_a_reader_with_sections_queued_never_holds_up_another_reader(self):
        order, release = [], threading.Event()

        def serve(conn, _):
            conn.send(("ready",))
            while (message := conn.recv()) is not None:
                order.append(message)
                if message == ("a", 1):
                    release.wait(10)
                conn.send(("error", KeyError("object_not_found")) if message[1] == 0
                          else ("ok", message))
        pool = self.pool(serve)
        slot = pool.slot_for("late", m.PAGE_SECTIONS)
        first = pool.submit(slot, "a", ("a", 1))
        while not order:
            threading.Event().wait(0.01)
        queued = [pool.submit(slot, "a", ("a", n)) for n in (2, 3)] + [
            pool.submit(slot, "b", ("b", 1))]
        release.set()
        self.assertEqual([f.result(10) for f in (first, *queued)],
                         [("a", 1), ("a", 2), ("a", 3), ("b", 1)])
        self.assertEqual(order, [("a", 1), ("b", 1), ("a", 2), ("a", 3)])
        with self.assertRaises(KeyError):   # the child's refusal, as the route expects
            pool.submit(slot, "b", ("b", 0)).result(10)
        self.assertEqual(pool._load, {})

    def test_a_section_that_never_answers_frees_its_slot_and_queued_sections(self):
        stopped = threading.Event()

        def serve(conn, _):
            conn.send(("ready",))
            conn.recv()          # the hung read: never answers
            stopped.wait(10)
        with mock.patch.object(sp, "SECTION_SECONDS", 0.2):
            pool = self.pool(serve)
            slot = pool.slot_for("late", m.PAGE_SECTIONS)
            hung = pool.submit(slot, "a", ("a", 1))
            queued = pool.submit(slot, "b", ("b", 1))
            for future in (hung, queued):
                with self.assertRaises(sp.SlotGone):
                    future.result(10)
        stopped.set()
        self.assertTrue(slot.gone.is_set())
        self.assertIsNone(pool.slot_for("late", m.PAGE_SECTIONS))   # read worker reads it

    def test_a_slot_that_is_gone_hands_its_sections_to_the_read_worker(self):
        def serve(conn, _):
            conn.send(("ready",))
            conn.recv()
            conn.close()   # the process ended mid-section
        pool = self.pool(serve, size=len(m.PAGE_SECTIONS))
        views = []
        backend = be.WorkspaceBackend(lambda session: views.append(session) or SimpleNamespace(bind=lambda s: None, close=lambda: None),
                                      data_label="test")
        backend.sections = pool
        threading.Thread(target=backend.reads.loop, daemon=True).start()
        self.addCleanup(backend.reads.stop)
        someone = reader("staff:operations")
        with mock.patch.object(be, "object_section", lambda b, view, t, i, s: (
                m.ObjectSection(object_id=i, section=s))):
            for _ in range(2):   # the first in the slot, the next with the slot gone
                got = backend.object_section(someone, "PurchaseOrder", "PO-1", "people")
                self.assertEqual((got.object_id, got.section), ("PO-1", "people"))
        self.assertIsNone(pool.slot_for("people", m.PAGE_SECTIONS))
        self.assertIsNotNone(pool.slot_for("late", m.PAGE_SECTIONS))
        with self.assertRaises(ValueError):
            backend.object_section(someone, "PurchaseOrder", "PO-1", "secrets")

    def test_the_quick_read_starts_every_section_of_the_page_on_its_slot(self):
        seen, lock = [], threading.Lock()

        def serve(conn, number):
            conn.send(("ready",))
            while (message := conn.recv()) is not None:
                with lock:
                    seen.append((number, message[0].key, message[3]))
                conn.send(("ok", None))
        pool = self.pool(serve, size=len(m.PAGE_SECTIONS))
        backend = be.WorkspaceBackend(lambda session: SimpleNamespace(bind=lambda s: None, close=lambda: None), data_label="test")
        backend.sections = pool
        threading.Thread(target=backend.reads.loop, daemon=True).start()
        self.addCleanup(backend.reads.stop)
        someone = reader("staff:operations")
        page = m.ObjectPage.model_construct(type="PurchaseOrder", object_id="PO-1")
        with mock.patch.object(be, "object_page", lambda *a, **k: page), \
                mock.patch.object(backend, "_labelled", lambda r, p, path: p):
            backend.object_page(someone, "PurchaseOrder", "PO-1", quick=True)
            backend.object_page(someone, "PurchaseOrder", "PO-1")   # the full page: none
        deadline = threading.Event()
        while len(seen) < len(m.PAGE_SECTIONS) and not deadline.wait(0.02):
            pass
        self.assertEqual(sorted(seen), sorted(
            (n, someone.key, s) for n, s in enumerate(m.PAGE_SECTIONS)))


def main_serve(conn, argv, settings, fixtures):
    """The read worker's own read of a section (``read_section`` over the reader's
    workspace on the runtime that built it, nothing kept), answering the slot child's
    messages: what a section read in a slot must equal."""
    from operator_api.workspace import case_console

    def serve(ctx):
        backend = be.WorkspaceBackend(ctx["objects_for"], **settings)
        worker = backend.reads
        conn.send(("ready",))
        while (message := conn.recv()) is not None:
            who, object_type, object_id, section = message
            try:
                view = worker._workspace(who.session)
                with be.page_scope(), view._request():
                    oid = be.page_oid(backend, view, object_type, object_id)
                    page = be.read_page(backend, view, oid)
                    conn.send(("ok", be.read_section(backend, view, page, section)))
            except Exception as error:  # noqa: BLE001 - compared with the slot's
                conn.send(("error", error))
        return 0
    with contextlib.redirect_stderr(io.StringIO()):
        return case_console.main(argv, serve_with=serve, fixtures=fixtures)


class FixtureChildTest(unittest.TestCase):
    """The real slot child (``section_pool.child``) over the fixture world, on a thread,
    and the read worker's own read of each section beside it."""

    MARKINGS: tuple = ()   # the fixture reader's markings (``case_console --marking``)

    @classmethod
    def setUpClass(cls):
        from operator_api.workspace import case_console
        from operator_api.workspace import object_workspace as objects
        from operator_api.tests import case_fixtures
        cls.fixtures = case_fixtures
        cls.tmp = Path(tempfile.mkdtemp(prefix="section-pool-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, store)
        cls.ops = [case_fixtures.OPERATIONS, case_fixtures.PURCHASING, case_fixtures.QUALITY]
        parser = build_parser()
        args = parser.parse_args(
            ["--dev", "--state-dir", str(cls.state), "--pg-dsn", "",
             "--console-arg=--source-mirror-audience",
             f"--console-arg={cls.state / case_fixtures.MIRROR_AUDIENCE_FILE}"]
            + [a for p in cls.ops for a in ("--dev-fixture-principal", p)]
            + [f"--console-arg={a}" for mk in cls.MARKINGS for a in ("--marking", mk)])
        argv = be.console_argv(parser, args, "dev")
        settings = {"data_label": "fixture", "state_dir": cls.state, "store_path": store,
                    "pg_dsn": "", "ask_served": False, "data_dir": cls.tmp / "app"}

        def serve(conn, _):
            with contextlib.redirect_stderr(io.StringIO()):
                sp.child(conn, argv, settings, fixtures=case_fixtures)
        cls.pool = sp.SectionPool(len(m.PAGE_SECTIONS), thread_start(serve))
        for slot in cls.pool.slots:
            assert slot.ready.wait(120), "a slot child did not start"
        cls.backend = be.WorkspaceBackend(lambda session: None, data_label="fixture")
        main_settings = dict(settings, data_dir=cls.tmp / "main")
        cls.main = sp.SectionPool(1, thread_start(
            lambda conn, _: main_serve(conn, argv, main_settings, case_fixtures)))
        assert cls.main.slots[0].ready.wait(120), "the main read did not start"

    @classmethod
    def tearDownClass(cls):
        cls.pool.stop()
        cls.main.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def section(self, who, name):
        slot = self.pool.slot_for(name, m.PAGE_SECTIONS)
        return self.pool.submit(slot, who.key, (who, "PurchaseOrder",
                                                self.fixtures.PO_NUMBER, name)).result(120)

    def test_the_reader_gets_each_section_and_an_outsider_none(self):
        ops, outsider = reader(*self.ops), reader("staff:outsider")
        for name in m.PAGE_SECTIONS:
            got = self.section(ops, name)
            self.assertIsInstance(got, m.ObjectSection)
            self.assertEqual(got.section, name)
            self.assertTrue(got.object_id.startswith("PurchaseOrder:"), got.object_id)
            # kept in its slot for this reader: the same section again
            self.assertEqual(self.section(ops, name), got)
            with self.assertRaises(KeyError):   # never read for a reader who may not see it
                self.section(outsider, name)
        trail = self.section(ops, "trail")
        self.assertTrue(trail.trail, trail)

    def test_a_section_read_in_a_slot_is_the_main_read_for_the_reader(self):
        ops = reader(*self.ops)
        for name in m.PAGE_SECTIONS:
            slot = self.section(ops, name)
            main = self.main.submit(self.main.slots[0], ops.key, (
                ops, "PurchaseOrder", self.fixtures.PO_NUMBER, name)).result(120)
            self.assertEqual(slot.model_dump(mode="json"), main.model_dump(mode="json"),
                             name)


class FinanceFixtureChildTest(FixtureChildTest):
    """The same with the reader holding the finance marking: its finance-marked parts
    (landed cost, margin) read in a slot exactly as the main read reads them."""

    MARKINGS = ("finance",)


class PlansOnceTest(unittest.TestCase):
    """A page read runs each plan over the record once; its later sections reuse it."""

    def test_each_plan_runs_once_per_page_read(self):
        calls = []
        view = SimpleNamespace(investigate=lambda family, oid: calls.append(family)
                               or {"investigation": {"family": family}})
        families = ("late_po", "people_on_record", "landed_cost")
        with mock.patch.object(be, "plan_families", return_value=set(families)):
            with be.plans_once():
                be.plan_runs(view, "PurchaseOrder", "po", families[:1])
                be.plan_runs(view, "PurchaseOrder", "po", families[:2])
                ran = be.plan_runs(view, "PurchaseOrder", "po", families)
            self.assertEqual(calls, list(families))
            self.assertEqual(set(ran), set(families))
            be.plan_runs(view, "PurchaseOrder", "po", families[:1])   # a new read
            self.assertEqual(calls, [*families, "late_po"])


if __name__ == "__main__":
    unittest.main()
