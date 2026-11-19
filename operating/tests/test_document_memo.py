"""The reader's memo of the business_documents steps (``operating.document_memo``) at the
``business_documents.answer`` boundary: a kept step answers every subject exactly as a
fresh read does (forecasts, their accuracy, price sheets, the answer bank; N=1/10/60),
without reading the documents again; every row is still gated on the request; readers are
kept apart; a step being read answers "still reading", never an empty list; a stale step
is read again.

SYNTHETIC fixtures only (``operating.tests.test_business_documents`` worlds); evidence
label: isolated-fixture.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operating import business_documents as bd
from operating import call_limits
from operating import document_memo
from operating.tests.test_business_documents import (AS_OF, BOX, EVERY, Fixture, Guard,
                                                     forecast_world, sheet)


class Counting:
    """The store connection, counting the statements run on it."""

    def __init__(self, inner):
        self.inner, self.statements = inner, 0

    def execute(self, sql, *args):
        self.statements += 1
        return self.inner.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Serial:
    """A SQLite connection used from several threads one statement at a time, each
    statement's rows read whole under the lock (as the Postgres connection serves them)."""

    def __init__(self, inner):
        self.inner, self.lock = inner, threading.RLock()

    def execute(self, sql, *args):
        with self.lock:
            return Rows(self.inner.execute(sql, *args).fetchall())

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Rows(list):
    def fetchall(self):
        return list(self)

    def fetchone(self):
        return self[0] if self else None


class FingerGuard(Guard):
    def __init__(self, *args, stamp="g1", **kwargs):
        super().__init__(*args, **kwargs)
        self.stamp = stamp

    def fingerprint(self):
        return self.stamp


def world(n: int):
    """``n`` items forecast by one customer, a price sheet to it, invoices to it and to
    another customer, and a questionnaire it sent."""
    f, cust, items = forecast_world(n)
    other = f.customer("Other Buyer Ltd", "cust-b.example")
    f.mailed("Price list 2026.xlsx",
             sheet([["Item #", "Description", "Price"]]
                   + [[f"SYN-{i:03d}", "x", "USD 10.00"] for i in range(n)]),
             sender="rep@synth-tenant.example", to="buyer@cust-a.example")
    for item in items:
        f.invoice(cust, item, "4", "40", "2026-02-01")     # as the sheet says
        f.invoice(cust, item, "10", "60", "2026-03-01")    # 6.00: under it
        f.invoice(other, item, "1", "99", "2026-03-01")    # another customer's: never
    f.mailed("Customer questionnaire.xlsx",
             sheet([["Do you hold a GMP certificate?", "Yes, since 2019"],
                    ["Our account manager?", "Jo Synthetic at cust-a"]]),
             sender="qa@cust-a.example")
    return f, cust, other, items


def plain(out):
    out = dict(out)
    out.pop("seconds", None)
    return out


class KeptSteps(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.n = 0
        bd.MEMO.clear()
        self.addCleanup(bd.MEMO.clear)

    def drained(self) -> None:
        """Every background read ended (before its connection closes)."""
        for _ in range(600):
            if not bd.MEMO.reading:
                return
            threading.Event().wait(0.05)
        self.fail("a background read never ended")

    def conn(self, f: Fixture, **kwargs) -> sqlite3.Connection:
        self.n += 1
        path = self.dir / f"store{self.n}.sqlite"
        f.build(path).close()
        conn = sqlite3.connect(path, **kwargs)
        self.addCleanup(conn.close)
        # the invoices' amounts state their currency (the store keeps it beside the value)
        conn.execute("UPDATE value_cells SET currency = 'USD' WHERE field IN (SELECT field "
                     "FROM value_fields WHERE property = 'amount_excluding_tax')")
        conn.commit()
        return conn

    def ask(self, conn, view, *, reader=None, may_read=lambda k: True, held=EVERY,
            guard=None, **args):
        return bd.answer(conn, view=view, may_read=may_read, held=held,
                         guard=FingerGuard() if guard is None else guard, as_of=AS_OF,
                         args=args, reader=reader)

    def test_kept_steps_answer_every_subject_as_a_fresh_read_n_1_10_60(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                bd.MEMO.clear()
                f, cust, other, items = world(n)
                conn = Counting(self.conn(f))
                subjects = [cust, other, items[0], None]
                for view in bd.KEPT_VIEWS:
                    for subject in subjects:
                        args = {"subject": subject} if subject else {}
                        before = conn.statements
                        fresh = self.ask(conn, view, **args)
                        cold = conn.statements - before
                        kept = self.ask(conn, view, reader="r1", **args)
                        self.assertEqual(plain(kept), plain(fresh), (view, subject))
                        before = conn.statements
                        again = self.ask(conn, view, reader="r1", **args)
                        self.assertEqual(plain(again), plain(fresh), (view, subject))
                        # kept: the documents are not found, read or resolved again
                        self.assertLess(conn.statements - before, max(cold, 1),
                                        (view, subject))
                        if view in ("forecasts", "answer_bank"):
                            self.assertLess(conn.statements - before, 20 + 2 * n, view)
                forecasts = self.ask(conn, "forecasts", reader="r1", subject=cust)
                self.assertEqual(len(forecasts["rows"]), 3 * n)
                self.assertEqual(len({(r["item"], r["period"]) for r in forecasts["rows"]}),
                                 3 * n)
                prices = self.ask(conn, "price_sheets", reader="r1", subject=cust)
                self.assertEqual(sorted(r["item"] for r in prices["rows"]), sorted(items))
                # the other customer's invoice line is never one of the sheet's
                self.assertTrue(all(r["invoice_lines"] == 2 and r["mismatched_lines"] == 1
                                    for r in prices["rows"]))
                self.assertEqual(self.ask(conn, "price_sheets", reader="r1",
                                          subject=other)["rows"], [])

    def test_a_kept_step_still_gates_every_row_on_the_request(self) -> None:
        f, cust, _, _ = world(2)
        conn = self.conn(f)
        for view in bd.KEPT_VIEWS:
            self.assertTrue(self.ask(conn, view, reader="r1", subject=cust)["rows"], view)
            # the same reader key, the mailbox no longer readable: nothing from it shows
            out = self.ask(conn, view, reader="r1", subject=cust,
                           may_read=lambda k: k != BOX)
            self.assertEqual(out["rows"], [], view)
            self.assertTrue(out["withheld_counts"], view)
        blind = self.ask(conn, "price_sheets", reader="r1", subject=cust,
                         held=frozenset({"pii"}))
        self.assertEqual((blind["rows"], blind["withheld_counts"]),
                         ([], {"withheld_finance": 1}))

    def test_readers_are_kept_apart(self) -> None:
        f, cust, _, _ = world(2)
        conn = self.conn(f)
        actor = SimpleNamespace(tenant="t", principals={"p:a"})
        keys = {bd.reader_key(actor, EVERY, FingerGuard()),
                bd.reader_key(SimpleNamespace(tenant="t", principals={"p:b"}), EVERY,
                              FingerGuard()),
                bd.reader_key(actor, frozenset({"pii"}), FingerGuard()),
                bd.reader_key(actor, EVERY, FingerGuard(stamp="g2"))}
        self.assertEqual(len(keys), 4)
        # no exact name for the reader or the exclusion: nothing kept
        self.assertIsNone(bd.reader_key(actor, EVERY, Guard()))
        self.assertIsNone(bd.reader_key(object(), EVERY, FingerGuard()))
        hidden = self.ask(conn, "forecasts", reader="restricted", subject=cust,
                          may_read=lambda k: k != BOX)
        self.assertEqual(hidden["rows"], [])
        full = self.ask(conn, "forecasts", reader="full", subject=cust)
        self.assertEqual(len(full["rows"]), 6)
        self.assertNotIn("document_not_readable", full["withheld_counts"])

    def test_a_step_being_read_answers_still_reading_never_an_empty_list(self) -> None:
        f, cust, _, _ = world(1)
        conn = Serial(self.conn(f, check_same_thread=False))
        release = threading.Event()
        real = bd._forecast_lines

        seen = []

        def slow(*args):
            seen.append(call_limits.seconds_left())
            release.wait(30)
            return real(*args)

        with mock.patch.object(bd.MEMO, "threads", lambda c: True), \
                mock.patch.object(document_memo, "MEMO_PAGE_WAIT", 0.05), \
                mock.patch.object(bd, "_forecast_lines", slow):
            with call_limits.sql_deadline(time.monotonic() + 60):
                out = self.ask(conn, "forecasts", reader="r1", subject=cust)
            self.assertEqual((out["rows"], out["complete"], out["counts"].get("still_reading")),
                             ([], False, 1))
            # the agent is told in words, so it never reports "none found"
            self.assertIn("still being read and is incomplete", out["note"])
            self.assertIn("do not say none were found", out["note"])
            release.set()
            self.drained()
            out = self.ask(conn, "forecasts", reader="r1", subject=cust)
            self.assertEqual((len(out["rows"]), out["complete"]), (3, True))
            self.assertEqual(seen, [None])   # read past the request's own deadline
            # the page's other lists were read beside it (``prefetch``): kept now
            self.assertIn(("r1", "answer_pairs"), bd.MEMO.kept)
            self.assertIn(("r1", "sheet_prices"), bd.MEMO.kept)

    def test_the_still_reading_note_passes_the_tool_contract_and_the_page_says_so(self) -> None:
        """Through ``BusinessDocumentsTool.call`` (the contract check): the note survives
        and the page's list shows "Still being read", which its section is not kept for."""
        from operator_api import record_documents as rd
        f, cust, _, _ = world(1)
        conn = Serial(self.conn(f, check_same_thread=False))
        release = threading.Event()
        real = bd._forecast_lines

        def slow(*args):
            release.wait(30)
            return real(*args)

        class Base:
            class store:
                pass

            class policy:
                may_read = staticmethod(lambda actor, key: True)
                held_markings = staticmethod(lambda actor: EVERY)
        Base.store.conn = conn
        tool = bd.BusinessDocumentsTool(Base, guard=lambda: FingerGuard())
        actor = SimpleNamespace(tenant="t", principals={"p:a"})
        self.addCleanup(release.set)
        with mock.patch.object(bd.MEMO, "threads", lambda c: True), \
                mock.patch.object(document_memo, "MEMO_PAGE_WAIT", 0.05), \
                mock.patch.object(bd, "_forecast_lines", slow):
            out = tool.call({"mode": "business_documents", "view": "forecasts",
                             "subject": cust, "as_of": AS_OF.isoformat()}, actor)
            self.assertNotIn("error", out)
            self.assertEqual((out["rows"], out["complete"]), ([], False))
            self.assertIn("do not say none were found", out["note"])
            view = SimpleNamespace(_call=lambda t, a: tool.call(a, actor))
            listed = rd.doc_list(view, "forecasts", cust, AS_OF)
            self.assertEqual((listed.error, listed.complete), (rd.STILL_READING, False))
            release.set()
            self.drained()

    def test_a_poll_while_the_bank_is_read_waits_only_what_is_left_and_names_the_read(
            self) -> None:
        """The Templates page's whole answer bank (no subject) starts its own step only;
        a request that comes while that read goes on waits only what is left of
        ``MEMO_PAGE_WAIT`` counted from the read's start (the app's poll never holds a read
        worker for the whole wait again); "still reading" names the read by an opaque
        token through the tool contract onto the list (never sent), and ``running`` says
        so until the read ends, then the list is read and gated as before."""
        from operator_api import record_documents as rd
        f, cust, _, _ = world(1)
        conn = Serial(self.conn(f, check_same_thread=False))
        release = threading.Event()
        real = bd._answer_pairs

        def slow(*args):
            release.wait(30)
            return real(*args)

        class Base:
            class store:
                pass

            class policy:
                may_read = staticmethod(lambda actor, key: True)
                held_markings = staticmethod(lambda actor: EVERY)
        Base.store.conn = conn
        tool = bd.BusinessDocumentsTool(Base, guard=lambda: FingerGuard())
        actor = SimpleNamespace(tenant="t", principals={"p:a"})
        view = SimpleNamespace(_call=lambda t, a: tool.call(a, actor))
        self.addCleanup(lambda: (release.set(), self.drained()))
        with mock.patch.object(bd.MEMO, "threads", lambda c: True), \
                mock.patch.object(document_memo, "MEMO_PAGE_WAIT", 0.6), \
                mock.patch.object(bd, "_answer_pairs", slow):
            started = time.monotonic()
            first = rd.doc_list(view, "answer_bank", None, AS_OF)
            self.assertGreaterEqual(time.monotonic() - started, 0.55)   # the first waits
            self.assertEqual((first.error, first.rows), (rd.STILL_READING, []))
            # only the bank's own step: no forecast or price sheet read beside it
            self.assertEqual([k[1] for k in bd.MEMO.reading], ["answer_pairs"])
            token = first._reading_token
            self.assertTrue(token and bd.MEMO.running(token))
            self.assertNotIn(token, first.model_dump_json())
            self.assertNotIn("_reading_token", first.model_dump())
            started = time.monotonic()
            again = rd.doc_list(view, "answer_bank", None, AS_OF)
            self.assertLess(time.monotonic() - started, 0.3)   # the wait is spent
            self.assertEqual(again.error, rd.STILL_READING)
            self.assertFalse(bd.MEMO.running("not-a-token"))
            self.assertFalse(bd.MEMO.running(None))
            release.set()
            self.drained()
            self.assertFalse(bd.MEMO.running(token))
            done = rd.doc_list(view, "answer_bank", None, AS_OF)
            self.assertIsNone(done.error)
            self.assertIsNone(done._reading_token)
            # a reader who may read nothing gets nothing from the kept step
            blind = SimpleNamespace(_call=lambda t, a: bd.answer(
                conn, view="answer_bank", may_read=lambda k: False, held=EVERY,
                guard=FingerGuard(), as_of=AS_OF, args={}, reader=None))
            self.assertEqual(rd.doc_list(blind, "answer_bank", None, AS_OF).rows, [])

    def test_a_kept_customer_lookup_holds_no_connection(self) -> None:
        f, cust, _, _ = world(1)
        conn = self.conn(f, check_same_thread=False)
        gate = SimpleNamespace(guard=Guard())
        ans = SimpleNamespace(reader="r9", gate=gate, view="forecasts", counts={},
                              withheld={}, complete=True)
        ans.counts, ans.withheld = __import__("collections").Counter(), \
            __import__("collections").Counter()
        got = bd._customers(conn, gate, ans)
        kept = bd.MEMO.kept[("r9", "customers")].value
        self.assertIsNone(kept.conn)
        conn.close()
        # the builder's connection is closed: another request binds its own and still reads
        other = self.conn(f, check_same_thread=False)
        again = bd._customers(other, gate, ans)
        self.assertIs(again.by_domain, got.by_domain)
        self.assertIsNotNone(again.conn)
        self.assertIsInstance(again.members(next(iter(again._ids))), set)

    def test_a_failed_read_is_read_on_the_request_with_its_error(self) -> None:
        f, cust, _, _ = world(1)
        conn = Serial(self.conn(f, check_same_thread=False))
        self.addCleanup(self.drained)

        def broken(*args):
            raise RuntimeError("store gone")

        with mock.patch.object(bd.MEMO, "threads", lambda c: True), \
                mock.patch.object(bd, "_forecast_lines", broken):
            with self.assertRaises(RuntimeError):
                self.ask(conn, "forecasts", reader="r1", subject=cust)
            self.assertNotIn(("r1", "forecast_lines"), bd.MEMO.kept)

    def test_a_stale_step_is_read_again(self) -> None:
        f, cust, _, items = world(1)
        conn = self.conn(f)
        now = [1000.0]
        with mock.patch.object(bd.MEMO, "clock", lambda: now[0]):
            self.assertEqual(len(self.ask(conn, "forecasts", reader="r1",
                                          subject=cust)["rows"]), 3)
            conn.execute("DELETE FROM text_chunks")   # every sheet's text gone
            conn.commit()
            now[0] += document_memo.MEMO_FRESH_SECONDS - 1
            self.assertEqual(len(self.ask(conn, "forecasts", reader="r1",
                                          subject=cust)["rows"]), 3)
            now[0] += document_memo.MEMO_STALE_SECONDS
            self.assertEqual(self.ask(conn, "forecasts", reader="r1",
                                      subject=cust)["rows"], [])


if __name__ == "__main__":
    unittest.main()
