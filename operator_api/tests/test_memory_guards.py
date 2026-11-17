"""Guards of each person's memory (``operator_api.memory``) without Postgres: pay and
finance never kept, the people exclusion (ids, addresses and the excluded people's names in
any case or spacing) failing closed on write and read, per-person isolation and view-as,
the plx.memory tool's arguments and its never being evidence, errors that never fail or
leak, the turn's time wall, the held decisions and core memory under concurrency, working
context compaction, and the learning hand-off (``operator_api.learning_memory``).

The store runs over ``FakeDB``: an in-process stand-in for the ``memory.log`` and
``memory.item`` statements ``Memory`` issues, honouring each statement's ``reader = %s``
scope. SYNTHETIC fixtures only; evidence label: isolated-fixture.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from operator_api import memory as mem

OID = "11111111-2222-4333-8444-555555555555"
EXCLUDED = "excluded.person@plx.example"
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


class Guard:
    unavailable = None
    count = 1

    def __init__(self):
        self.excluded = {OID, EXCLUDED}

    def is_excluded(self, value):
        return str(value).lower() in self.excluded

    def decides(self, address):
        return address.endswith("@plx.example")

    def known_domains(self):
        return {"plx.example"}


def reader(who, oid=None, view_as=False, canary=False):
    key = f"plx:staff:{who}" + (f",entra:{oid}" if oid else "")
    return type("R", (), {"key": key, "display_name": who.title(), "email": f"{who}@plx.example",
                          "session": type("S", (), {"identities": frozenset(),
                                                    "subject_oid": oid,
                                                    "view_as": view_as,
                                                    "canary": canary})()})()


def answer(n, question, said, *, conversation="c1", facts=(), subjects=(), outcome="answered",
           **extra):
    return {"answer_id": f"a{n}", "question": question, "direct_answer": said,
            "outcome": outcome, "conversation_id": conversation,
            "facts": [{"text": t, "check": "passed",
                       "sources": [{"handle": h, "label": label}]}
                      for t, h, label in ((f + ("BC purchase order",))[:3] for f in facts)],
            "subjects": [{"object_id": s} for s in subjects], **extra}


# ---- an in-process stand-in for the memory tables ------------------------------------------

class _Result:
    def __init__(self, rows, rowcount=None):
        self.rows = list(rows)
        self.rowcount = len(self.rows) if rowcount is None else rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


ITEMS = " ".join(mem.Memory._ITEM_COLUMNS)
LOGS = " ".join(mem.Memory._LOG_COLUMNS)


class FakeDB:
    """The statements ``Memory`` issues over ``memory.log`` and ``memory.item``, answered
    from two lists. A read is scoped to a reader only where its statement says
    ``reader = %s`` (so dropping the scope from the product's SQL shows here). ``before``
    and ``after`` (sql, args): hooks around each statement (raise, block); ``after`` runs
    once the rows are read, so a blocked read returns what was there when it started."""

    def __init__(self):
        self.log, self.item = [], []
        self.statements = []
        self.before = self.after = None
        self._ids = {"log": 0, "item": 1000}
        self._lock = threading.Lock()

    @contextmanager
    def connect(self):
        yield _Conn(self)

    def table(self, name):
        return self.log if name == "log" else self.item

    def add(self, table, **row):
        with self._lock:
            self._ids[table] += 1
            base = {"id": self._ids[table], "conversation": None, "at": NOW, "records": [],
                    "citations": [], "people": [], "source_ref": None}
            if table == "item":
                base.update(kind="fact", source="ask", subject=None, log_ids=[],
                            importance=0.4, confidence=0.8, superseded_by=None,
                            deleted_at=None)
            base.update(row)
            self.table(table).append(base)
            return base["id"]

    def texts(self):
        return " ".join(r["text"] for r in self.log + self.item)

    def memory_statements(self):
        return [(s, a) for s, a in self.statements if "memory." in s]


class _Conn:
    def __init__(self, db):
        self.db = db

    def commit(self):
        pass

    def rollback(self):
        pass

    def execute(self, sql, args=()):
        db = self.db
        s = " ".join(sql.split())
        with db._lock:
            db.statements.append((s, args))
        if db.before:
            db.before(s, args)
        with db._lock:
            out = self._answer(s, args)
        if db.after:
            db.after(s, args)
        return out

    @staticmethod
    def _cols(row, columns):
        return tuple(row.get(c) for c in columns)

    def _answer(self, s, args):
        db = self.db
        active = lambda r: r.get("superseded_by") is None and r.get("deleted_at") is None  # noqa: E731
        if s.startswith("SELECT set_config"):
            return _Result([])
        if s.startswith("INSERT INTO memory.log"):
            who, conv, at, kind, ref, text, recs, cites, people = args
            if ref and any((r["reader"], r["kind"], r["source_ref"]) == (who, kind, ref)
                           for r in db.log):
                return _Result([])
            db._ids["log"] += 1
            db.log.append({"id": db._ids["log"], "reader": who, "conversation": conv,
                           "at": at, "kind": kind, "source_ref": ref, "text": text,
                           "records": list(recs), "citations": json.loads(cites),
                           "people": list(people)})
            return _Result([(db._ids["log"],)])
        if s.startswith("INSERT INTO memory.item"):
            (who, kind, source, ref, conv, at, subject, text, recs, cites, people, ids,
             importance, confidence) = args
            if ref and any((r["reader"], r["kind"], r["source"], r["source_ref"])
                           == (who, kind, source, ref) for r in db.item):
                return _Result([])
            db._ids["item"] += 1
            db.item.append({"id": db._ids["item"], "reader": who, "kind": kind,
                            "source": source, "source_ref": ref, "conversation": conv,
                            "at": at, "subject": subject, "text": text, "records": list(recs),
                            "citations": json.loads(cites), "people": list(people),
                            "log_ids": list(ids), "importance": importance,
                            "confidence": confidence, "superseded_by": None,
                            "deleted_at": None})
            return _Result([(db._ids["item"],)])
        if s.startswith("SELECT 1 FROM memory.item"):
            who, kind, subject, source = args
            return _Result([(1,)] if any(
                (r["reader"], r["kind"], r["subject"], r["source"]) == (who, kind, subject, source)
                and r["deleted_at"] is not None for r in db.item) else [])
        if s.startswith("SELECT id, text FROM memory.item"):
            who, kind, subject = args
            rows = [r for r in db.item if (r["reader"], r["kind"], r["subject"])
                    == (who, kind, subject) and active(r)]
            return _Result([(r["id"], r["text"]) for r in sorted(rows, key=lambda r: -r["id"])])
        if s.startswith("UPDATE memory.item SET at = greatest"):
            when, _importance, ids, item_id = args
            for r in db.item:
                if r["id"] == item_id:
                    r["at"] = max(r["at"], when)
                    r["log_ids"] = sorted(set(r["log_ids"]) | set(ids))
            return _Result([], 1)
        if s.startswith("UPDATE memory.item SET superseded_by"):
            new, who, kind, subject, _new = args
            for r in db.item:
                if (r["reader"], r["kind"], r["subject"]) == (who, kind, subject) \
                        and r["id"] != new and r["superseded_by"] is None:
                    r["superseded_by"] = new
            return _Result([], 1)
        if s.startswith("UPDATE memory.item SET deleted_at"):
            item_id, who = args
            done = 0
            for r in db.item:
                if r["id"] == item_id and r["reader"] == who and r["deleted_at"] is None:
                    r["deleted_at"], done = NOW, done + 1
            return _Result([], done)
        if s.startswith("WITH found AS"):
            item = "FROM memory.item t" in s
            columns = mem.Memory._ITEM_COLUMNS if item else mem.Memory._LOG_COLUMNS
            rows = [r for r in (db.item if item else db.log) if r["reader"] == args["reader"]
                    and (active(r) or not item)]
            return _Result([self._cols(r, columns) + (
                0.5, 0.0, len(set(r["records"]) & set(args["records"]))) for r in rows])
        if s.startswith("SELECT DISTINCT unnest(log_ids)"):
            return _Result([(i,) for r in db.item if r["reader"] == args[0]
                            and r["superseded_by"] is not None and r["deleted_at"] is None
                            for i in r["log_ids"]])
        for name, columns in (("item", mem.Memory._ITEM_COLUMNS),
                              ("log", mem.Memory._LOG_COLUMNS)):
            head = "SELECT " + ", ".join(columns) + f" FROM memory.{name} WHERE "
            if not s.startswith(head):
                continue
            rows, rest = list(db.table(name)), list(args)
            if "WHERE NOT (reader = ANY(%s))" in s:   # the purge
                rows = [r for r in rows if r["reader"] not in rest[0]]
                return _Result([self._cols(r, columns) for r in rows])
            if "reader = %s" in s:
                who = rest.pop(0)
                rows = [r for r in rows if r["reader"] == who]
            if "kind = ANY(%s)" in s:
                kinds = rest.pop(0)
                rows = [r for r in rows if r["kind"] in kinds and active(r)]
            if "conversation = %s" in s:
                conv = rest.pop(0)
                rows = [r for r in rows if r["conversation"] == conv and r["kind"] != "tool_call"]
                rows.sort(key=lambda r: -r["id"])
            if "id = ANY(%s)" in s:
                ids = rest.pop(0)
                rows = sorted((r for r in rows if r["id"] in ids), key=lambda r: r["id"])
            if "ORDER BY importance DESC" in s:
                rows.sort(key=lambda r: (-r["importance"], -r["at"].timestamp()))
            elif "ORDER BY at DESC, id DESC" in s:
                rows.sort(key=lambda r: (-r["at"].timestamp(), -r["id"]))
            return _Result([self._cols(r, columns) for r in rows])
        if s.startswith("SELECT conversation FROM memory.log"):
            who, conv = args
            rows = [r for r in sorted(db.log, key=lambda r: -r["id"]) if r["reader"] == who
                    and r["conversation"] is not None and r["conversation"] != conv]
            return _Result([(rows[0]["conversation"],)] if rows else [])
        if s.startswith("SELECT DISTINCT reader FROM memory."):
            table = s.split("memory.", 1)[1].split()[0]
            return _Result([(k,) for k in dict.fromkeys(r["reader"] for r in db.table(table))])
        if s.startswith("DELETE FROM memory."):
            table = s.split("memory.", 1)[1].split()[0]
            gone, ids = args
            rows = db.table(table)
            keep = [r for r in rows if r["reader"] not in gone and r["id"] not in ids]
            count = len(rows) - len(keep)
            rows[:] = keep
            return _Result([], count)
        raise AssertionError(f"FakeDB: unexpected statement {s[:120]}")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.guard = Guard()
        self.names = frozenset()
        self.store = self.make()

    def make(self, **options):
        options.setdefault("sync", True)
        return mem.Memory(self.db.connect, guard=lambda: self.guard,
                          names=lambda: self.names, clock=lambda: NOW, **options)

    def everything_read(self, store, who, query="lot K-15", conversation="c1"):
        """Every read path's output for ``who`` as one string (and the tool's)."""
        out = [store.search(who, query), store.core(who), store.core_held(who),
               store.items(who), store.items(who, kinds=mem.ITEM_KINDS),
               store.turns(who, [r["id"] for r in self.db.log]),
               store.working(who, conversation), store.last_session(who, "other"),
               store.context(who, query, conversation=conversation, budget_ms=2000),
               store.tool(who).call({"query": query,
                                     "ids": [r["id"] for r in self.db.log] or [1]})]
        return json.dumps(out, default=str)


# ---- pay and finance ---------------------------------------------------------------------------

class FinanceText(unittest.TestCase):
    def test_an_amount_in_any_written_form_is_finance(self):
        for said in ("Ana got $1,200 yesterday", "it is 1200.00 USD", "it is USD 1200",
                     "€40 for the crate", "£5 each", "it was CAD $300",
                     "Ana got 4,250.00 yesterday", "about 32.50 then", "she got 95k",
                     "roughly 1.2m in March"):
            with self.subTest(said):
                self.assertTrue(mem.finance_text(said))
                self.assertEqual(mem.finance_free(said), "")

    def test_a_pay_word_alone_is_finance(self):
        for said in ("Her salary changes.", "The weekly wages run.", "His bonus is set.",
                     "The payroll run is late.", "Her paycheque came.",
                     "The compensation review.", "He asked for a raise.",
                     "Overtime for the crew.", "His severance letter.", "Her pension plan.",
                     "Net pay shows first."):
            with self.subTest(said):
                self.assertTrue(mem.finance_text(said))

    def test_codes_counts_and_dates_are_not_finance(self):
        for said in ("Lot K-883 is on hold.", "Ship 1,200 cases to dock 4.",
                     "Order SO-1204 ships 2026-11-20.", "Lot L-12.50 retest",
                     "Version v2.10 ships", "Bergen depot ships lot K-1"):
            with self.subTest(said):
                self.assertFalse(mem.finance_text(said))
                self.assertEqual(mem.finance_free(said), said)

    def test_finance_free_drops_only_the_sentences_that_carry_it(self):
        said = ("PO 1 ships Monday. Its total is $5; dock 4 is open.\n"
                "Her salary is set. The crew starts at six")
        self.assertEqual(mem.finance_free(said),
                         "PO 1 ships Monday. dock 4 is open. The crew starts at six")
        self.assertEqual(mem.finance_free(None), "")

    def test_personal_finance_that_cannot_be_decided_counts_as_finance(self):
        long = "The crew at dock four starts the retest of lot K-883 at six."
        self.assertFalse(mem.finance_text(long))
        with mock.patch.dict(sys.modules, {"operating.chunking.sensitive": None}):
            self.assertTrue(mem.finance_text(long))         # undecidable: never kept
            self.assertFalse(mem.finance_text("Lot K-1 waits."))   # short: not asked


# ---- the excluded people's names ---------------------------------------------------------------

class ExcludedNamePattern(unittest.TestCase):
    def test_a_name_matches_in_any_case_or_spacing_and_never_inside_a_word(self):
        pattern = mem._name_pattern(frozenset({"quinn harrow", "q", " "}))
        for said in ("QUINN HARROW approved it", "quinn   harrow", "Quinn\nHarrow",
                     "Ask Quinn Harrow's team", "(quinn harrow)"):
            with self.subTest(said):
                self.assertTrue(pattern.search(said))
        for said in ("Quinn approved it", "Quinn Harrowgate", "aquinn harrow", "q depot"):
            with self.subTest(said):
                self.assertFalse(pattern.search(said))
        self.assertIsNone(mem._name_pattern(frozenset({"x", ""})))

    def test_names_one_reads_strings_only_and_no_pattern_names_nobody(self):
        named = mem._Named(Guard(), mem._name_pattern(frozenset({"ola berg"})))
        self.assertTrue(named.names_one(None, 5, "met Ola Berg"))
        self.assertFalse(named.names_one(None, ["Ola Berg"], "Bergen"))
        self.assertTrue(named.is_excluded(OID))   # the guard answers the rest
        self.assertFalse(mem._Named(Guard(), None).names_one("Ola Berg"))


def _join(prefix):
    for thread in threading.enumerate():
        if thread.name.startswith(prefix):
            thread.join(5)


class ExcludedNamesHeld(unittest.TestCase):
    def test_closed_until_read_and_closed_again_when_a_read_fails(self):
        reads = iter([frozenset({"ola berg"}), RuntimeError("directory down")])

        def read():
            got = next(reads)
            if isinstance(got, Exception):
                raise got
            return got
        names = mem.ExcludedNames(read, seconds=3600)
        self.assertEqual(names.refresh(), frozenset({"ola berg"}))
        self.assertEqual(names(), frozenset({"ola berg"}))
        self.assertIsNone(names.refresh())   # never the names read before
        self.assertIsNone(names())

    def test_callers_never_wait_and_start_one_read_between_them(self):
        release, calls = threading.Event(), []

        def read():
            calls.append(1)
            release.wait(5)
            return frozenset({"ola berg"})
        names = mem.ExcludedNames(read, seconds=3600)
        seen = []
        callers = [threading.Thread(target=lambda: seen.append(names())) for _ in range(12)]
        for t in callers:
            t.start()
        for t in callers:
            t.join(5)
        self.assertEqual(seen, [None] * 12)   # before the first read: closed, no wait
        release.set()
        _join("plx-memory-names")
        self.assertEqual(len(calls), 1)
        self.assertEqual(names(), frozenset({"ola berg"}))


class ListedNames(unittest.TestCase):
    class Api:
        def __init__(self, users):
            self.users = users

        def get_pages(self, url, **_):
            return self.users, {"complete": True}

    def test_one_letter_names_are_no_names(self):
        them = {"id": OID, "displayName": "  Quinn Harrow ", "givenName": "Q", "surname": " "}
        self.assertEqual(mem.listed_names(self.Api([them]), Guard()),
                         frozenset({"quinn harrow"}))
        initials = {"id": OID, "displayName": "Q", "givenName": "Q", "surname": "H"}
        self.assertIsNone(mem.listed_names(self.Api([initials]), Guard()))

    def test_every_excluded_person_must_be_named(self):
        guard = Guard()
        guard.count = 2
        them = {"id": OID, "displayName": "Quinn Harrow"}
        self.assertIsNone(mem.listed_names(self.Api([them]), guard))


# ---- the exclusion fails closed ----------------------------------------------------------------

class FailClosed(StoreCase):
    def seed(self):
        self.store.from_answer(reader("ana"), answer(1, "Where is lot K-15?", "With dock four.",
                                                     facts=[("Lot K-15 is at dock four.",
                                                             "api://lot/K-15#row.dock")]))
        self.assertIn("dock four", self.db.texts())

    def test_nothing_is_written_or_read_while_the_exclusion_cannot_decide(self):
        ana = reader("ana")
        self.seed()

        class NoState:   # a guard that does not say whether it is usable
            def is_excluded(self, value):
                return False
        unavailable = Guard()
        unavailable.unavailable = "excluded_people_oids_absent"

        def boom():
            raise RuntimeError("names unreadable")
        for label, guard, names in (("no guard", None, frozenset()),
                                    ("unavailable", unavailable, frozenset()),
                                    ("no state", NoState(), frozenset()),
                                    ("names unknown", Guard(), None),
                                    ("names raise", Guard(), boom)):
            with self.subTest(label):
                before = len(self.db.log) + len(self.db.item)
                store = mem.Memory(self.db.connect, guard=lambda g=guard: g,
                                   names=names if callable(names) else (lambda n=names: n),
                                   clock=lambda: NOW, sync=True)
                self.db.statements.clear()
                self.assertEqual(store.from_answer(ana, answer(
                    2, "Always show lot K-16 first.", "Lot K-16 waits.")), [])
                self.assertIsNone(store.remember(ana, kind="fact", source="learn",
                                                 text="Lot K-17 waits."))
                self.assertIsNone(store.log_event(ana, kind="note", text="lot K-18"))
                self.assertNotIn("dock four", self.everything_read(store, ana))
                self.assertIsNone(store.purge_excluded())
                self.assertEqual(len(self.db.log) + len(self.db.item), before)
                self.assertEqual(self.db.memory_statements(), [])   # not even a read

    def test_a_guard_that_raises_stores_and_shows_nothing(self):
        ana = reader("ana")
        self.seed()

        def boom():
            raise RuntimeError("exclusion unreadable")
        store = mem.Memory(self.db.connect, guard=boom, names=lambda: frozenset(),
                           clock=lambda: NOW, sync=False)
        store.observe("answer", ana, answer(2, "Where is lot K-16?", "Lot K-16 waits."))
        store.flush(5)
        self.assertNotIn("K-16", self.db.texts())
        self.assertEqual(store.context(ana, "lot K-15", conversation="c1"), "")


class ExcludedByName(StoreCase):
    def test_a_text_naming_them_in_any_case_or_spacing_is_never_kept(self):
        ana = reader("ana")
        self.names = frozenset({"quinn harrow"})
        for n, said in enumerate(("QUINN HARROW approved lot K-11.",
                                  "quinn   harrow approved lot K-12.",
                                  "Sent to Quinn\nHarrow for lot K-13.",
                                  "Ask Quinn Harrow's team about lot K-14.")):
            with self.subTest(said):
                self.assertIsNone(self.store.log_event(ana, kind="note", text=said))
                self.store.from_answer(ana, answer(n, f"Where is lot K-1{n}?", said))
        # in the subject alone, the choice of a question asked back, an action's title
        self.assertIsNone(self.store.remember(ana, kind="preference", source="learn",
                                              subject="quinn harrow | hold notices",
                                              text="Copy them on hold notices."))
        self.assertEqual(self.store.from_clarify(ana, "Which lot?", "Which one?",
                                                 "The one Quinn Harrow holds", "a9"), [])
        self.assertEqual(self.store.from_action(ana, "send_email", "sent",
                                                "Lot K-19 for QUINN HARROW", [], [], "r1"), [])
        self.assertNotRegex(self.db.texts().lower(), r"quinn|harrow")
        self.assertFalse([i for i in self.db.item if "harrow" in str(i["subject"]).lower()])
        # the questions themselves were kept (only what names them is not)
        self.assertIn("Where is lot K-10?", self.db.texts())

    def test_a_label_naming_them_is_blanked_and_a_word_containing_a_name_is_kept(self):
        ana = reader("ana")
        self.names = frozenset({"quinn harrow", "berg"})
        got = self.store.from_answer(ana, answer(
            1, "Where is lot K-21?", "At the Bergen depot.",
            facts=[("Lot K-21 is at the Bergen depot.", "api://lot/K-21#row.site",
                    "Mail from Quinn Harrow")]))
        self.assertTrue(got)
        self.assertIn("Bergen depot", self.db.texts())
        labels = [c["label"] for r in self.db.log + self.db.item for c in r["citations"]]
        self.assertTrue(labels)
        self.assertEqual(set(labels), {""})

    def test_someone_named_after_the_write_is_hidden_from_every_read(self):
        ana = reader("ana")
        self.store.from_answer(ana, answer(1, "Where is lot K-15?", "With Ola Berg.",
                                           facts=[("Ola Berg holds lot K-15.",
                                                   "api://lot/K-15#row.holder")]))
        self.store.remember(ana, kind="preference", source="learn", text="Lot K-15 first.",
                            subject="ola berg | lots")
        self.assertIn("Ola Berg", self.everything_read(self.store, ana))
        self.names = frozenset({"OLA BERG"})   # the listing's case is not the text's
        self.store._cores.clear()
        said = self.everything_read(self.store, ana, query="lot K-15 Ola Berg")
        self.assertNotIn("Berg", said)
        self.assertNotIn("Lot K-15 first", said)
        # the question that never named them still is
        self.assertIn("Where is lot K-15?", said)


class ExcludedActor(StoreCase):
    def test_an_excluded_reader_writes_nothing_and_reads_nothing_kept_before(self):
        for who in (reader("x", oid=OID), mem.KeyReader(f"plx:staff:x,entra:{OID}"),
                    mem.KeyReader(f"plx:staff:x,mail:{EXCLUDED}")):
            with self.subTest(who.key):
                self.assertEqual(self.store.from_answer(who, answer(
                    1, "Where is lot K-1?", "On hold.")), [])
                self.db.add("log", reader=who.key, kind="answer", conversation="c1",
                            text="Lot K-15 is on hold.")
                self.db.add("item", reader=who.key, kind="instruction",
                            text="Always show lot K-15.")
                self.assertNotIn("K-15", self.everything_read(self.store, who))
        self.assertNotIn("K-1?", self.db.texts())

    def test_a_row_about_an_excluded_person_is_never_kept(self):
        ana = reader("ana")
        self.assertIsNone(self.store.log_event(ana, kind="action", text="Sent lot K-4",
                                               people=[EXCLUDED.upper()]))
        self.assertIsNone(self.store.log_event(ana, kind="note", text="lot K-3", citations=[
            {"handle": "graph-mail://excluded.person%40plx.example/m1"}]))
        self.assertIsNone(self.store.log_event(ana, kind="note", text=f"lot K-5 for {OID}"))
        self.assertEqual(self.db.log, [])


# ---- each person's own ---------------------------------------------------------------------

class PerPerson(StoreCase):
    def test_nothing_of_one_person_reaches_another(self):
        ana, ben = reader("ana"), reader("ben")
        got = self.store.from_answer(ana, answer(1, "Always answer about lot K-15 first.",
                                                 "Lot K-15 waits at dock four.",
                                                 facts=[("Lot K-15 is at dock four.",
                                                         "api://lot/K-15#row.dock")]))
        self.assertEqual(len(got), 4)   # question, answer, fact, standing instruction
        self.assertIn("dock four", self.everything_read(self.store, ana))
        self.db.statements.clear()
        self.assertNotIn("K-15", self.everything_read(self.store, ben))
        self.assertFalse(any(self.store.forget(ben, i) for i in got))
        # every statement Ben's reads issued was scoped to him alone
        for sql, args in self.db.memory_statements():
            with self.subTest(sql[:60]):
                values = list(args.values()) if isinstance(args, dict) else list(args)
                self.assertIn(ben.key, values)
                self.assertNotIn(ana.key, json.dumps(values, default=str))

    def test_core_memory_held_for_one_person_is_never_served_to_another(self):
        ana, ben = reader("ana"), reader("ben")
        self.store.remember(ana, kind="instruction", source="learn",
                            text="Always show lot K-15 first.", subject="lots")
        self.assertTrue(self.store.core_held(ana))
        self.assertEqual(self.store.core_held(ben), [])

    def test_a_view_as_or_canary_session_writes_nothing_and_reads_nothing(self):
        # the Ask canary's service session reads as the owner's claims (her key) but is
        # nobody's own: it neither feeds her memory nor is fed by it (nor by its own runs)
        for kind in ("view_as", "canary"):
            with self.subTest(kind):
                self.setUp()
                self._writes_nothing_and_reads_nothing(reader("ana", **{kind: True}))

    def _writes_nothing_and_reads_nothing(self, viewing):
        ana = reader("ana")
        item = self.store.remember(ana, kind="instruction", source="learn",
                                   text="Always show lot K-15 first.", subject="lots")
        self.store.log_event(ana, kind="note", text="lot K-15 noted", conversation="c1")
        before = json.dumps([self.db.log, self.db.item], default=str)
        self.assertEqual(self.store.from_answer(viewing, answer(
            1, "Where is lot K-16?", "Lot K-16 waits.")), [])
        self.assertIsNone(self.store.remember(viewing, kind="fact", source="learn",
                                              text="Lot K-17 waits."))
        self.assertIsNone(self.store.log_event(viewing, kind="note", text="lot K-18"))
        self.assertFalse(self.store.forget(viewing, item))
        async_store = self.make(sync=False)
        with mock.patch.object(async_store, "submit", wraps=async_store.submit) as queued:
            async_store.observe("answer", viewing, answer(2, "Lot K-19?", "Lot K-19 waits."))
            async_store.flush(5)
        queued.assert_not_called()   # not even a job that its own checks would refuse
        self.assertEqual(json.dumps([self.db.log, self.db.item], default=str), before)
        self.assertNotIn("K-15", self.everything_read(self.store, viewing))
        self.assertIn("K-15", self.everything_read(self.store, ana))

    def test_the_ask_turn_offers_no_memory_to_a_view_as_session(self):
        from operator_api.backend import WorkspaceBackend
        calls = []
        memory = SimpleNamespace(context=lambda *a, **k: calls.append("context") or "",
                                 tool=lambda *a, **k: calls.append("tool"),
                                 submit=lambda job: calls.append("submit"))
        backend = SimpleNamespace(memory=memory, state_dir=None,
                                  _memory_decide=lambda who, wait=False: (lambda h: {}))
        request = SimpleNamespace(question="Where is lot K-15?")
        for kind in ("view_as", "canary"):
            self.assertIsNone(WorkspaceBackend._memory_turn(
                backend, reader("ana", **{kind: True}), request, "c1", []), kind)
        self.assertEqual(calls, [])
        self.assertIsNotNone(WorkspaceBackend._memory_turn(backend, reader("ana"), request,
                                                           "c1", []))
        self.assertIn("context", calls)


class Purge(StoreCase):
    def test_purge_deletes_what_a_later_exclusion_covers_and_nothing_else(self):
        ana = reader("ana")
        self.store.log_event(ana, kind="note", text="Lot K-1 waits.")
        self.store.log_event(ana, kind="note", text="Ola Berg has lot K-2.")
        self.store.log_event(ana, kind="action", text="Sent lot K-3", people=["later@plx.example"])
        self.store.remember(ana, kind="fact", source="learn", text="OLA  BERG owns lot K-4.")
        self.db.add("log", reader=f"plx:staff:x,entra:{OID}", kind="note", text="Lot K-5.")
        self.names = frozenset({"ola berg"})
        self.guard.excluded.add("later@plx.example")
        self.assertEqual(self.store.purge_excluded(), {"log": 3, "item": 1})
        self.assertEqual(self.db.texts(), "Lot K-1 waits.")


# ---- pay and finance through the store ---------------------------------------------------------

class FinanceNeverKept(StoreCase):
    def test_no_path_keeps_pay_or_finance_and_the_rest_is_kept(self):
        ana = reader("ana")
        self.store.from_answer(ana, answer(
            1, "Where is PO 90412?",
            "PO 90412 ships from Leeds on Monday. Freight came to $1,200. "
            "Her salary is unchanged.\nThe duty was 1200.00 USD.",
            facts=[("PO 90412 ships on Monday.", "api://po/90412#row.ship", "Unit price sheet"),
                   ("PO 90412 freight is 4.20 USD.", "api://po/90412#row.freight")],
            not_found=[{"what": "the invoice total of $8,900"}, {"what": "the carrier"}]))
        self.assertEqual(self.store.from_answer(ana, answer(
            2, "What did Ana earn last year?", "95k.")), [])
        self.assertIsNone(self.store.remember(ana, kind="preference", source="learn",
                                              text="Prefers net pay shown before tax."))
        self.assertIsNone(self.store.remember(ana, kind="fact", source="learn",
                                              text="Dock four first.", subject="bonus | dock"))
        self.store.from_feedback(ana, 1, "Good. The rebate was $40.", "Where is lot K-1?", "f1")
        self.store.from_note(ana, "Check dock four. Payroll is late.", [], "n1")
        self.store.from_autopilot(ana, {"item_id": "i1", "title": "Lot K-2 hold",
                                        "decision": {"decision": "edit",
                                                     "note": "Fine. Costs 5 USD.",
                                                     "text": "Hold it. Wages are due."}})
        self.store.from_action(ana, "send_email", "sent", "Invoice INV-9 for $300", [], [], "r1")
        self.assertEqual(self.store.from_clarify(ana, "Which lot?", "Which?", "the 32.50 one",
                                                 "c1"), [])
        kept = self.db.texts()
        for wanted in ("ships from Leeds", "the carrier", "Rated the answer", "Check dock four",
                       "Hold it", "Lot K-2 hold"):
            self.assertIn(wanted, kept)
        for leaked in ("1,200", "1200", "salary", "4.20", "8,900", "earn", "95k", "net pay",
                       "bonus", "rebate", "Payroll", "Costs", "Wages", "INV-9", "32.50",
                       "price"):
            self.assertNotIn(leaked.lower(), (kept + json.dumps(
                [r["citations"] for r in self.db.log + self.db.item])).lower(), leaked)


# ---- the plx.memory tool ----------------------------------------------------------------------

class MemoryTool(StoreCase):
    def test_the_memory_tool_is_never_evidence_and_never_logged_as_a_tool_call(self):
        from operating import agent_session, answer_gate
        self.assertIn(mem.TOOL_NAME, agent_session.NOT_EVIDENCE_TOOLS)
        got = self.store.from_tools(reader("ana"), "c1", "a1", [
            {"name": mem.TOOL_NAME, "arguments": {"query": "Kestrel"}},
            {"name": "plx.find", "arguments": {"query": "lot K-1"}}])
        self.assertEqual(len(got), 1)
        self.assertNotIn(mem.TOOL_NAME, self.db.texts())
        # the final reply's names: what plx.memory returned backs nothing
        skipped, real = [], answer_gate.returned_names
        events = [{"type": "tool_call", "name": mem.TOOL_NAME,
                   "result": {"items": [{"text": "PO-7781 from earlier"}]}},
                  {"type": "tool_call", "name": "plx.find",
                   "result": {"items": [{"name": "PO-4471"}]}}]
        with mock.patch.object(answer_gate, "returned_names", side_effect=lambda e, skip_tools=(): (
                skipped.extend(skip_tools), real(e, skip_tools=skip_tools))[1]):
            agent_session.gate_final_reply("PO-7781 has it.", events)
        self.assertIn(mem.TOOL_NAME, skipped)
        names = " ".join(real(events, skip_tools=skipped))
        self.assertNotIn("PO-7781", names)
        self.assertIn("PO-4471", names)

    def test_an_argument_outside_the_schema_is_refused_before_any_read(self):
        tool = self.store.tool(reader("ana"))
        for bad in ({"query": "x", "who": "ben"}, {"query": ""}, {"query": "   "},
                    {"query": "x" * 401}, {}, {"query": 5}, {"ids": [True]}, {"ids": [0]},
                    {"ids": ["1"]}, {"ids": 3}, {"query": "x", "limit": True},
                    {"query": "x", "limit": 21}, {"query": "x", "limit": 2.0},
                    {"query": "x", "records": "K-1"}, {"query": "x", "records": [1]}):
            with self.subTest(bad):
                self.db.statements.clear()
                self.assertEqual(tool.call(bad), {"ok": False, "error": "invalid_arguments"})
                self.assertEqual(self.db.memory_statements(), [])
        self.assertTrue(tool.call({"query": "lot", "ids": [1], "limit": 20})["ok"])


# ---- errors and time ---------------------------------------------------------------------------

class Errors(StoreCase):
    SECRET = "Kestrel lot K-883 for ana@plx.example"

    def test_a_store_error_never_fails_the_turn_nor_reaches_the_log(self):
        ana = reader("ana")

        def down(sql, args):
            raise RuntimeError(self.SECRET)
        self.db.before = down
        with self.assertLogs("operator_api.memory", logging.WARNING) as logged:
            self.assertEqual(self.store.search(ana, "Kestrel lot"), [])
            self.assertEqual(self.store.context(ana, "Kestrel lot", conversation="c1"), "")
            self.store.submit(lambda: self.store.log_event(ana, kind="note", text=self.SECRET))
            background = self.make(sync=False)
            background.observe("answer", ana, answer(1, "Kestrel lot?", "On hold."))
            background.flush(5)
        self.assertTrue(any("RuntimeError" in line for line in logged.output))
        for line in logged.output:
            self.assertNotIn("Kestrel", line)
            self.assertNotIn("ana@", line)

    def test_a_search_over_its_budget_returns_nothing(self):
        ana = reader("ana")
        self.store.log_event(ana, kind="note", text="Lot K-15 waits at dock four.")
        self.assertTrue(self.store.search(ana, "lot K-15"))
        self.db.statements.clear()
        self.assertEqual(self.store.search(ana, "lot K-15", budget_ms=1), [])
        self.assertFalse([s for s, _ in self.db.statements if "FROM memory.log t" in s])

    def test_a_stalled_read_is_cut_at_the_turns_wall_and_the_rest_still_arrives(self):
        self.assertLessEqual(mem.CONTEXT_MS + mem.CONTEXT_GRACE_MS, 140)
        ana = reader("ana")
        self.store.remember(ana, kind="instruction", source="learn",
                            text="Always show lot K-15 first.", subject="lots")
        release = threading.Event()

        def stall(sql, args):
            if sql.startswith("WITH found AS"):
                release.wait(3)
        self.db.after = stall
        try:
            started = time.perf_counter()
            said = self.store.context(ana, "lot K-15", conversation="c1")
            took = (time.perf_counter() - started) * 1000
        finally:
            release.set()
            _join("plx-memory-found")
        self.assertLess(took, 1000)   # the stall lasts 3 s
        self.assertIn("Core memory:", said)
        self.assertIn("Always show lot K-15 first.", said)
        self.assertNotIn("Relevant from earlier", said)

    def test_an_undecidable_access_check_withholds_every_cited_row(self):
        rows = [{"id": 1, "citations": [{"handle": "api://lot/K-1"}]},
                {"id": 2, "citations": []},
                {"id": 3, "citations": [{"handle": "api://lot/K-3"}]}]

        def boom(handles):
            raise RuntimeError("policy down")
        self.assertEqual([r["id"] for r in mem.Memory._openable(rows, boom)], [2])
        partly = lambda handles: {"api://lot/K-1": None}   # noqa: E731 - K-3 undecided
        self.assertEqual([r["id"] for r in mem.Memory._openable(rows, partly)], [1, 2])
        closed = lambda handles: {h: {"reason": "item_refused"} for h in handles}  # noqa: E731
        self.assertEqual([r["id"] for r in mem.Memory._openable(rows, closed)], [2])


# ---- concurrency ------------------------------------------------------------------------------

class HeldDecisions(unittest.TestCase):
    def test_parallel_turns_ask_once_per_person_and_never_share_a_decision(self):
        release, calls = threading.Event(), []

        def ask(who, handles):
            calls.append((who.key, tuple(handles)))
            release.wait(5)
            return {h: None for h in handles}
        held = mem.Decisions(ask)
        ana, ben = reader("ana"), reader("ben")
        seen = []
        turns = [threading.Thread(target=lambda: seen.append(held.decider(ana)(["h1", "h2"])))
                 for _ in range(16)]
        for t in turns:
            t.start()
        for t in turns:
            t.join(5)
        self.assertEqual(seen, [{}] * 16)   # undecided: withheld, nobody waits
        release.set()
        _join("plx-memory-decide")
        self.assertEqual(calls, [(ana.key, ("h1", "h2"))])
        self.assertEqual(held.decider(ana)(["h1", "h2"]), {"h1": None, "h2": None})
        self.assertEqual(held.decider(ben)(["h1"]), {})   # Ana's decision is not Ben's
        _join("plx-memory-decide")
        self.assertEqual(calls[-1], (ben.key, ("h1",)))

    def test_a_failed_check_is_withheld_and_asked_again(self):
        answers = iter([RuntimeError("down"), {"h1": None}])

        def ask(who, handles):
            got = next(answers)
            if isinstance(got, Exception):
                raise got
            return got
        held = mem.Decisions(ask)
        self.assertEqual(held.decider(reader("ana"), wait=True)(["h1"]), {})
        self.assertEqual(held.decider(reader("ana"), wait=True)(["h1"]), {"h1": None})


class HeldCore(StoreCase):
    def test_a_write_during_a_core_read_is_never_hidden_by_the_held_core(self):
        ana = reader("ana")
        self.store.remember(ana, kind="instruction", source="learn",
                            text="Always show lot K-15 first.", subject="lots")
        reading, release = threading.Event(), threading.Event()

        def hold(sql, args):
            if "ORDER BY importance DESC" in sql and not release.is_set():
                reading.set()
                release.wait(5)
        self.db.after = hold
        first = []
        turn = threading.Thread(target=lambda: first.append(self.store.core_held(ana)))
        turn.start()
        self.assertTrue(reading.wait(5))
        self.store.remember(ana, kind="preference", source="learn",
                            text="Prefers lot codes in bold.", subject="format")
        release.set()
        turn.join(5)
        self.assertNotIn("bold", json.dumps(first))   # read before the write
        self.assertIn("bold", json.dumps(self.store.core_held(ana)))

    def test_parallel_writes_and_reads_of_many_people_stay_their_own(self):
        store = self.make(sync=False)
        people = [reader(f"p{n}") for n in range(8)]

        def work(who):
            for n in range(5):
                store.observe("answer", who, answer(n, f"Where is lot {who.key[-2:]}-{n}?",
                                                    f"Lot {who.key[-2:]}-{n} waits.",
                                                    conversation=who.key))
                self.store.core_held(who)
        threads = [threading.Thread(target=work, args=(who,)) for who in people]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        store.flush(10)
        self.assertEqual(len(self.db.log), 8 * 5 * 2)
        for who in people:
            mine = [r for r in self.db.log if r["reader"] == who.key]
            self.assertEqual(len(mine), 10)
            self.assertTrue(all(who.key[-2:] in r["text"] for r in mine))
            said = self.store.context(who, "lot waits", conversation=who.key, budget_ms=2000)
            others = {p.key[-2:] for p in people} - {who.key[-2:]}
            self.assertFalse([o for o in others if f"{o}-" in said], said)


# ---- working context compaction ----------------------------------------------------------------

class Compaction(StoreCase):
    def turns(self, ana, sizes):
        ids = []
        for n, size in enumerate(sizes):
            text = (f"Lot K-{n} waits at dock four. " + "The crew retests it again. " * size)
            ids.append(self.store.log_event(
                ana, kind="question" if n % 2 == 0 else "answer", text=text.strip(),
                conversation="c1", records=[f"Lot:K-{n}"],
                citations=[{"handle": f"api://lot/K-{n}#row.dock", "label": "Lot"}]
                if n % 2 else ()))
        return ids

    def test_a_short_conversation_stays_verbatim(self):
        ana = reader("ana")
        ids = self.turns(ana, [30] * 6)
        here = self.store.working(ana, "c1", chars=8000)
        # past what stays verbatim after a fold, short of the fold itself: nothing folds
        size = sum(len(t) + 1 for t in here["turns"])
        self.assertGreater(size, mem.RECENT_SHARE * 8000)
        self.assertLessEqual(size, mem.FOLD_AT * 8000)
        self.assertIsNone(here["summary"])
        self.assertEqual(here["folded"], [])
        self.assertEqual([int(t.split(" ", 1)[0][1:]) for t in here["turns"]], ids)

    def test_older_turns_fold_into_a_summary_and_the_last_two_stay_verbatim(self):
        ana = reader("ana")
        ids = self.turns(ana, [2] * 8 + [30, 30])   # the last two each past the recent share
        here = self.store.working(ana, "c1", chars=1000, defer=True)
        kept = [int(t.split(" ", 1)[0][1:]) for t in here["turns"]]
        self.assertEqual(here["folded"] + kept, ids)   # every turn once, in order
        self.assertEqual(len(kept), 2)
        self.assertLessEqual(len(here["summary"]), int(mem.SUMMARY_SHARE * 1000))
        self.assertIn(f"#{ids[0]}-#{ids[7]}", here["summary"])
        # the fold is kept off the turn, and only when submitted
        self.assertEqual(self.db.item, [])
        here["keep"]()
        (summary,) = self.db.item
        self.assertEqual(summary["kind"], "summary")
        self.assertEqual(summary["subject"], "conversation:c1")
        self.assertEqual(summary["log_ids"], here["folded"])
        self.assertEqual({c["handle"] for c in summary["citations"]},
                         {f"api://lot/K-{n}#row.dock" for n in (1, 3, 5, 7)})
        # a folded source closed to the person withholds the summary itself
        closed = lambda handles: {h: ({"reason": "item_refused"} if h.endswith("K-3#row.dock")  # noqa: E731
                                      else None) for h in handles}
        self.assertEqual(self.store.items(ana, kinds=("summary",), decide=closed), [])
        self.assertTrue(self.store.items(ana, kinds=("summary",)))

    def test_fold_stays_within_its_budget(self):
        rows = [{"id": n, "kind": "question" if n % 2 else "answer", "records": [f"R{n}"],
                 "text": f"Lot K-{n} " + "waits at dock four " * 40} for n in range(1, 60)]
        for chars in (200, 400, 1500):
            with self.subTest(chars):
                said = mem.Memory.fold(rows, chars)
                self.assertLessEqual(len(said), chars)
                self.assertTrue(said.startswith("Summary of 59 earlier entries (log ids #1-#59"))


# ---- the learning hand-off ---------------------------------------------------------------------

class LearningHandOff(StoreCase):
    def test_a_lesson_reaches_only_a_known_unexcluded_persons_memory(self):
        from operator_api import learning_memory as lm
        ana_oid = "22222222-2222-4333-8444-555555555555"
        lesson = {"kind": "preference", "text": "Prefers lot codes in bold.", "source": "learn",
                  "subject": "format"}

        def down(oid):
            raise RuntimeError("memory unreadable")
        writer = lm.OfflineWriter(self.store, keys=down)
        self.assertIsNone(writer.remember_for(ana_oid, **lesson))
        self.assertEqual(writer.skipped, {"no_reader_key": 1})
        writer = lm.OfflineWriter(self.store, keys=lambda oid: f"group:g1,t:employee:{oid}")
        self.assertIsNone(writer.remember_for("group:g1,other", **lesson))
        self.assertEqual(writer.skipped, {"not_a_person_id": 1})
        self.assertIsNone(writer.remember_for(OID, **lesson))   # excluded: the store refuses
        self.assertEqual(self.db.item, [])
        self.assertIsNotNone(writer.remember_for(ana_oid, **lesson))
        self.assertEqual([r["reader"] for r in self.db.item], [f"group:g1,t:employee:{ana_oid}"])


if __name__ == "__main__":
    unittest.main()
