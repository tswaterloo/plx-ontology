"""The questionnaire answer bank kept on disk (``operating.answer_bank_store``): built once
for the company, decided per reader, it shows each reader exactly what the per-reader read
showed (rows, counts, withheld, complete), for a full reader, a restricted one, one who
may read nothing and with the people exclusion; a bank of another exclusion, version or
age is never used; N=1/10/60 documents.

SYNTHETIC fixtures only (made-up ids, names and addresses); evidence label:
isolated-fixture.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from operating import answer_bank_store as abs_
from operating import business_documents as bd
from operating import document_tables as dt
from operating.tests.test_business_documents import AS_OF, EVERY, Fixture, Guard, sheet

BOX_A = "mail:00000000-0000-4000-8000-0000000000a1"
BOX_B = "mail:00000000-0000-4000-8000-0000000000b2"
BOX_X = "mail:00000000-0000-4000-8000-0000000000c3"   # an excluded person's mailbox
QUESTIONS = [["Section"], ["Do you hold a GMP certificate?", "Yes, co-packed since 2019"],
             ["What price will you hold for this year?", "USD 4.10 per case"],
             ["Is the site allergen free?", "Yes"]]


def original_answer_pairs(conn, gate, ans):
    """``business_documents._answer_pairs`` as it was before the bank (origin/main
    4a25a95d21), verbatim: the reference every reader is compared with."""
    customer_of = bd.CustomerOf(conn, gate, ans)
    suppliers = dt.Parties(conn, "Supplier", "Supplier.same_record_as",
                           getattr(gate.guard, "internal", ()))
    out = []
    for d in bd._docs(conn, gate, bd.QUESTIONNAIRE_WORDS, ans,
                      require=lambda n: bd._words_in(n, bd.QUESTIONNAIRE_WORDS)):
        pairs = bd.qa_pairs(d.text or "")
        if not pairs:
            continue
        customer, basis, evidence = customer_of(d)
        outside = [d.sender] + d.recipients if d.sender else d.recipients
        supplier = next((x for x in (suppliers.of(o) for o in outside
                                     if not suppliers.internal_address(o)) if x), None)
        if customer is None or supplier is not None:
            reason = "supplier_questionnaire" if supplier else "party_unknown"
            ans.counts[reason] += len(pairs)
            continue
        for q, a, cell in pairs:
            specific = bd.fact_category(q) is None or bd._specific(a, customer_of, customer)
            out.append((d, q, a, cell, customer, basis, evidence, specific))
    return out


def world(extra: int = 0) -> tuple[Fixture, dict[str, str]]:
    """Two customers by domain, two by the votes of one sender domain's messages (the one
    a reader's votes name depends on which mailboxes it may open), a supplier, and
    questionnaires in three mailboxes (one an excluded person's)."""
    f = Fixture()
    ids = {"alpha": f.customer("Alpha Buyer Co", "alpha.example"),
           "t1": f.w.obj("Customer", {"name": "Tee One Co"}),
           "t2": f.w.obj("Customer", {"name": "Tee Two Inc"})}
    f.w.obj("Supplier", {"name": "Gamma Materials", "bc_email": "qa@gamma-supply.example"})

    def tied(customer: str, box: str) -> None:
        so = f.w.obj("SalesOrder", {"order_number": f"SO{f.w.n}"})
        f.w.link("SalesOrder.ordered_by", so, customer)
        msg = f.w.obj("Email", {"subject": "order", "sender": "ops@teebuyer.example"},
                      audience=box, binding="mail")
        f.w.link("Email.about_order", msg, so)

    for _ in range(2):
        tied(ids["t1"], BOX_A)       # a reader of box A alone: Tee One's (2 of 2)
    for _ in range(10):
        tied(ids["t2"], BOX_B)       # every box: Tee Two's (10 of 12)
    tied(ids["t1"], BOX_X)
    q = sheet(QUESTIONS)
    f.mailed("Customer questionnaire.xlsx", q, sender="qa@alpha.example", audience=BOX_A)
    f.mailed("Questionnaire tee.xlsx", q, sender="planner@teebuyer.example", audience=BOX_A)
    f.mailed("Questionnaire b.xlsx", q, sender="qa@alpha.example", audience=BOX_B)
    f.mailed("Questionnaire x.xlsx", q, sender="qa@alpha.example", audience=BOX_X)
    f.mailed("Supplier questionnaire.xlsx", q, sender="qa@gamma-supply.example",
             audience=BOX_A)
    f.mailed("Questionnaire nobody.xlsx", q, sender="x@nowhere-known.example",
             audience=BOX_A)
    for n in range(extra):
        f.mailed(f"Questionnaire {n}.xlsx", sheet([["S"], [f"Do you hold GMP lot {n}?",
                                                          f"Yes {n}"]]),
                 sender="qa@alpha.example", audience=BOX_A if n % 2 else BOX_B)
    return f, ids


READERS = {"full": lambda k: True, "box_a_only": lambda k: k != BOX_B,
           "nothing": lambda k: False}


class AnswerBankStore(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.guard = Guard(blocked={BOX_X})
        self.guard.fingerprint = lambda: "exclusion-1"

    def conn(self, f: Fixture, name: str = "s.sqlite"):
        conn = f.build(self.dir / name)
        self.addCleanup(conn.close)
        return conn

    def ask(self, conn, may_read, *, bank=None, subject=None, held=EVERY):
        out = bd.answer(conn, view="answer_bank", may_read=may_read, held=held,
                        guard=self.guard, as_of=AS_OF, bank=bank,
                        args={"subject": subject} if subject else {})
        out.pop("seconds", None)
        return out

    def old(self, conn, may_read, **kw):
        with mock.patch.object(bd, "_answer_pairs", original_answer_pairs):
            return self.ask(conn, may_read, **kw)

    def test_every_reader_sees_exactly_what_the_per_reader_read_showed(self) -> None:
        f, ids = world()
        conn = self.conn(f)
        bank = json.loads(json.dumps(abs_.build(conn, self.guard)))   # as kept on disk
        seen = {}
        for name, may_read in READERS.items():
            for subject in (None, ids["alpha"], ids["t1"], ids["t2"]):
                with self.subTest(reader=name, subject=subject):
                    before = self.old(conn, may_read, subject=subject)
                    after = self.ask(conn, may_read, bank=bank, subject=subject)
                    self.assertEqual(after, before)
                    # and the per-reader fallback (collected through the reader's gate)
                    self.assertEqual(self.ask(conn, may_read, subject=subject), before)
                    seen[(name, subject)] = before
        # the probes bite: the votes name another customer for a reader of box A alone,
        # and what the people exclusion withholds is never a row for anyone
        full_t = {r["party_id"] for r in seen[("full", ids["t2"])]["rows"]
                  if r["party_basis"] == "sender_threads"}
        mine_t = {r["party_id"] for r in seen[("box_a_only", ids["t1"])]["rows"]
                  if r["party_basis"] == "sender_threads"}
        self.assertEqual((full_t, mine_t), ({ids["t2"]}, {ids["t1"]}))
        # "co" is a word of Tee One Co's own name: specific to it, generic for Tee Two
        scope = {r["party_id"]: r["scope"] for r in
                 seen[("full", ids["t2"])]["rows"] + seen[("box_a_only", ids["t1"])]["rows"]
                 if r["answer"].startswith("Yes, co-packed")
                 and r["party_basis"] == "sender_threads"}
        self.assertEqual(scope, {ids["t2"]: "generic", ids["t1"]: "customer_specific"})
        for out in seen.values():
            cited = {c["object_id"] for r in out["rows"] for c in r["cites"]}
            self.assertFalse(any(":mail:" in o and o in bank_x(f) for o in cited))
        self.assertEqual(seen[("nothing", None)]["rows"], [])
        self.assertGreater(seen[("box_a_only", None)]["withheld_counts"]
                           ["document_not_readable"], 0)
        # nothing of the excluded mailbox is in the bank but its records' ids
        x_docs = [e for e in bank["docs"] if e["id"] in bank_x(f)]
        self.assertTrue(x_docs)
        self.assertTrue(all(set(e) <= {"id", "message", "day"} for e in x_docs))
        self.assertNotIn("Yes, co-packed", json.dumps([e for e in x_docs]))

    def test_the_reader_cap_on_thread_messages_is_the_readers_own(self) -> None:
        f, ids = world()
        conn = self.conn(f)
        with mock.patch.object(bd, "THREAD_MAIL", 3):
            bank = abs_.build(conn, self.guard)
            for name, may_read in READERS.items():
                with self.subTest(reader=name):
                    self.assertEqual(self.ask(conn, may_read, bank=bank, subject=ids["t1"]),
                                     self.old(conn, may_read, subject=ids["t1"]))

    def test_cardinality_no_row_lost_or_duplicated(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                f, _ = world(extra=n)
                conn = self.conn(f, f"n{n}.sqlite")
                bank = abs_.build(conn, self.guard)
                for name, may_read in READERS.items():
                    before = self.old(conn, may_read)
                    began = time.monotonic()
                    after = self.ask(conn, may_read, bank=bank)
                    self.assertLess(time.monotonic() - began, 5.0)
                    self.assertEqual(after, before)
                full = self.ask(conn, READERS["full"], bank=bank)
                # one generic GMP answer per extra document the full reader may open
                lots = [r for r in full["rows"] if r["question"].startswith("Do you hold GMP lot")]
                self.assertEqual(len(lots), n)
                self.assertEqual(len({r["document"] for r in lots}), n)

    def test_a_bank_is_used_only_for_its_exclusion_version_and_age(self) -> None:
        f, _ = world()
        conn = self.conn(f)
        path = self.dir / "answer-bank" / "bank.json"
        abs_.write(path, abs_.build(conn, self.guard))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertIsNotNone(abs_.load(path, self.guard))
        other = Guard(blocked={BOX_X})
        other.fingerprint = lambda: "exclusion-2"
        self.assertIsNone(abs_.load(path, other))
        self.assertIsNone(abs_.load(path, Guard()))          # no fingerprint: never
        self.assertIsNone(abs_.load(path, None))
        self.assertIsNone(abs_.load(path, self.guard,
                                    now=time.time() + abs_.MAX_AGE_SECONDS + 60))
        self.assertIsNone(abs_.load(self.dir / "missing.json", self.guard))
        bad = json.loads(path.read_text())
        bad["version"] = abs_.VERSION + 1
        abs_.write(path, bad)
        self.assertIsNone(abs_.load(path, self.guard))
        path.write_text("{not json")
        self.assertIsNone(abs_.load(path, self.guard))

    def test_a_bank_that_withheld_what_the_reader_may_open_is_never_used(self) -> None:
        f, _ = world()
        conn = self.conn(f)
        # built under a wider exclusion than the reader's (as if its fingerprint lied)
        wide = Guard(blocked={BOX_X, BOX_A})
        wide.fingerprint = lambda: "exclusion-1"
        bank = abs_.build(conn, wide)
        out = self.ask(conn, READERS["full"], bank=bank)
        self.assertEqual(out["counts"].pop("answer_bank_read_per_reader"), 1)
        self.assertEqual(out, self.old(conn, READERS["full"]))

    def test_no_bank_is_built_without_the_people_exclusion(self) -> None:
        f, _ = world()
        conn = self.conn(f)
        with self.assertRaises(RuntimeError):
            abs_.build(conn, Guard(unavailable="exclusion_not_loaded"))


class StoreGuardKept(unittest.TestCase):
    """The people exclusion is loaded once a minute per store, never kept unloaded."""

    def test_an_unloaded_exclusion_is_never_kept_and_a_loaded_one_expires(self) -> None:
        loads = []
        answers = [None, Guard(unavailable="exclusion_not_loaded"), Guard(), Guard()]

        def people(_path):
            loads.append(1)
            return answers[len(loads) - 1]

        bd._GUARDS.clear()
        self.addCleanup(bd._GUARDS.clear)
        with mock.patch("operating.proactive_engine._people", people), \
                mock.patch("operating.cash_forecast.store_file", lambda _b: "/x/store"):
            self.assertIsNone(bd._store_guard(None))
            self.assertEqual(bd._store_guard(None).unavailable, "exclusion_not_loaded")
            kept = bd._store_guard(None)
            self.assertIs(bd._store_guard(None), kept)          # kept: no third load
            self.assertEqual(len(loads), 3)
            bd._GUARDS["/x/store"] = (time.monotonic() - bd.GUARD_SECONDS - 1, kept)
            self.assertIsNot(bd._store_guard(None), kept)       # expired: loaded again
            self.assertEqual(len(loads), 4)


def bank_x(f: Fixture) -> set[str]:
    return {r["object_id"] for r in f.w.rows["objects"] if r.get("audience_key") == BOX_X}


if __name__ == "__main__":
    unittest.main()
