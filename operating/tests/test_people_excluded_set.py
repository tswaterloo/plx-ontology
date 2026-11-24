"""The people exclusion as one precomputed set for full readers (``operating.pg.
people_excluded``, business rule 2026-11-17): the item gate of mail, Teams and meetings decides a
full reader by the set alone (no capture read), every other reader and scheme exactly as
before; a set computed for another exclusion list, or none, is never used (fail closed); a
full reader (only) is kept from the set's objects at object level (``document_marks``),
inside the statement, never by a list sent with it; the build decides what the full
reader's policy denies.

Evidence label: isolated-fixture (synthetic handles and ids, never live PLX data).
"""
from __future__ import annotations

import os
import types
from pathlib import Path
import unittest
from unittest import mock

from operating.pg import people_excluded
from operating.search import document_marks

OID_X = "0000000a-0000-4000-8000-00000000000a"   # the excluded person (synthetic)
OID_OK = "0000000b-0000-4000-8000-00000000000b"
MAIL_X = "graph-mail://box-x/msg-1"
MAIL_OK = "graph-mail://box-ok/msg-2"
TEAMS_X = "teams-chat://chat-x/m1@g1-7"
FILE = "sharepoint://drive/item@etag"
SET = people_excluded.Excluded("tag-1", 100)
HELD = frozenset({MAIL_X, TEAMS_X})   # the set's handles (in Postgres; looked up by index)


def _held(self, names):
    """``Excluded.withheld`` over ``HELD`` (the index lookup's answer)."""
    return frozenset(n for n in names if n in HELD)


def _served(handles):
    """Every handle served at a version the set covers (its message too)."""
    return {h: 50 for h in handles}


class _Bound:
    def __init__(self):
        self.memo, self.search = {}, False


class _Runtime:
    employee_gate = None

    def __init__(self):
        self.employee_gate = mock.Mock(side_effect=lambda actor: f"gated:{actor}")


def _host():
    from operating.case_tools_host_gates import _GatesMixin

    class Host(_GatesMixin):
        mail_capture_dir = None
        sharepoint_capture_dir = None

    host = Host()
    host.runtime, host._call_bound = _Runtime(), _Bound()
    return host


class GateTests(unittest.TestCase):
    def _gate(self, *, full: bool, got=SET, versions=_served, held=_held):
        calls = []

        def runtime_gate(actor, handle):
            calls.append(handle)
            return True

        runtime_gate.key_material = lambda actor: {"actor": actor}
        host = _host()
        with mock.patch("operating.ontology_tools_evidence.RuntimeCitations"):
            gate = host._item_gate(runtime_gate)
        patches = [mock.patch("operating.employee_permissions.reader_unrestricted",
                              return_value=full),
                   mock.patch.object(people_excluded, "excluded", return_value=got),
                   mock.patch.object(people_excluded, "latest_versions",
                                     side_effect=versions),
                   mock.patch.object(people_excluded.Excluded, "withheld", held)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return gate, calls

    def test_a_full_reader_is_decided_by_the_set_alone(self):
        gate, calls = self._gate(full=True)
        self.assertFalse(gate("owner", MAIL_X))
        self.assertFalse(gate("owner", MAIL_X + "#attachment=a1"))           # its attachment
        self.assertFalse(gate("owner", MAIL_X + "#attachment=a1&page=2"))    # a page of it
        self.assertFalse(gate("owner", TEAMS_X))
        self.assertTrue(gate("owner", MAIL_OK))
        self.assertTrue(gate("owner", "calendar-event://cal/e1@g1-3"))
        self.assertEqual(calls, [])                                           # no capture read
        # a form the set does not decide (a page of a message, another fragment) keeps
        # the gate, which denies what it cannot read
        for odd in (MAIL_OK + "#page=2", MAIL_OK + "#body", TEAMS_X + "#x"):
            calls.clear()
            self.assertTrue(gate("owner", odd))
            self.assertEqual(calls, [odd])
        calls.clear()
        # a scheme the set does not cover keeps its gate
        self.assertTrue(gate("owner", FILE))
        self.assertEqual(calls, [FILE])
        self.assertEqual(gate.key_material("owner"), ("people-excluded", "tag-1"))

    def test_a_handle_the_set_does_not_cover_keeps_the_gate(self):
        # R1: recorded after the set (a newer version of it, its page form or its
        # message), not served at all, an unreadable lookup, or a set with no version
        # number: the gate decides, never the set
        newer = lambda hs: {**_served(hs), MAIL_X: 101}   # noqa: E731
        cases = (("newer", SET, newer, MAIL_X),
                 ("message newer", SET, newer, MAIL_X + "#attachment=a1"),
                 ("not served", SET, lambda hs: {}, MAIL_X),
                 ("page of an unserved attachment", SET,
                  lambda hs: {MAIL_X: 50}, MAIL_X + "#attachment=a1&page=2"),
                 ("lookup failed", SET, lambda hs: None, TEAMS_X),
                 ("no version number", people_excluded.Excluded("tag-1"), _served, MAIL_X))
        for name, got, versions, handle in cases:
            with self.subTest(name):
                gate, calls = self._gate(full=True, got=got, versions=versions)
                self.assertTrue(gate("owner", handle))   # the runtime gate's answer
                self.assertEqual(calls, [handle])
        # the set's rows cannot be read, or the state row is no longer this one's (an undo,
        # a rebuild for another list): the gate decides, at once
        for handle in (MAIL_X, TEAMS_X, MAIL_OK):
            with self.subTest("rows unreadable", handle=handle):
                gate, calls = self._gate(full=True, held=lambda self, names: None)
                self.assertTrue(gate("owner", handle))   # the runtime gate's answer
                self.assertEqual(calls, [handle])
        # served at or below the number, a page through its served attachment: the set
        gate, calls = self._gate(full=True, versions=lambda hs: {
            MAIL_X: 100, MAIL_X + "#attachment=a1": 7})
        self.assertFalse(gate("owner", MAIL_X + "#attachment=a1&page=2"))
        self.assertEqual(calls, [])

    def test_staff_and_a_full_reader_without_a_usable_set_keep_every_gate(self):
        for full, got in ((False, SET), (True, None)):
            with self.subTest(full=full, usable=got is not None):
                gate, calls = self._gate(full=full, got=got)
                self.assertTrue(gate("someone", MAIL_X))   # the runtime gate decides
                self.assertEqual(calls, [MAIL_X])
                self.assertEqual(gate.key_material("someone"), {"actor": "someone"})

    def test_a_failing_entitlement_keeps_the_gate(self):
        gate, calls = self._gate(full=True)
        with mock.patch("operating.employee_permissions.reader_unrestricted",
                        side_effect=RuntimeError("down")):
            self.assertTrue(gate("owner", MAIL_X))
        self.assertEqual(calls, [MAIL_X])

    def test_the_full_readers_key_is_the_sets_and_nothing_else(self):
        from operating.pg import item_audiences
        self.assertEqual(people_excluded.reader_key(("people-excluded", "tag-1")),
                         "full:tag-1")
        for material in (("people-excluded", ""), ("other", "tag-1"), ["people-excluded",
                                                                         "tag-1"], None, {}):
            self.assertIsNone(people_excluded.reader_key(material))

        class Base:
            item_gate_scope = ("host",)
            item_gate = mock.Mock(key_material=lambda actor: ("people-excluded", "tag-1"))

        self.assertEqual(item_audiences.reader_key(Base(), "owner"), "full:tag-1")


class AListOfHandlesIsDecidedInBatchesTest(unittest.TestCase):
    """A page build deciding N handles for one reader (Assist's list: ~1,100 matters, each
    citing mail) reads the set's version numbers and rows in the read ahead, once, and the gate
    finds them kept: before, the owner's view (built anew per handle) never matched the kept
    key and every handle did its own two lookups and its own look at the reader (0.8 s a
    list). The answers are the same ones the gate gives alone."""

    def _run(self, owner: bool, n: int):
        from operating.case_tools_host_citations import page_build
        host = _host()
        host._in_call = True
        versions = mock.Mock(side_effect=_served)
        handles = [f"graph-mail://box-ok/msg-{i}" for i in range(n)] + [MAIL_X]
        gated = mock.Mock(principals=frozenset({"p"}))
        host.runtime.employee_gate = mock.Mock(return_value=gated)
        with mock.patch("operating.ontology_tools_evidence.RuntimeCitations"), \
                mock.patch("operating.employee_permissions.reader_unrestricted", return_value=True), \
                mock.patch("operating.employee_permissions.reader_owner", return_value=owner), \
                mock.patch.object(people_excluded, "excluded", return_value=SET), \
                mock.patch.object(people_excluded, "latest_versions", versions), \
                mock.patch.object(people_excluded.Excluded, "withheld", _held), \
                page_build():
            gate = host._item_gate(None)
            gate.prefetch("who", handles)
            out = {h: gate("who", h) for h in handles}
        return out, versions, host.runtime.employee_gate

    def test_n_handles_cost_one_read_ahead_not_n(self):
        for n in (1, 10, 60):
            for owner in (False, True):
                with self.subTest(n=n, owner=owner):
                    out, versions, employee_gate = self._run(owner, n)
                    self.assertEqual(len(out), n + 1)                    # none lost or doubled
                    self.assertEqual(versions.call_count, 1)             # one batch, no per-handle look-up
                    self.assertEqual(employee_gate.call_count, 1)        # the reader resolved once
                    # the people exclusion still holds for everyone but the owner
                    self.assertEqual(out[MAIL_X], owner)
                    self.assertTrue(all(v for h, v in out.items() if h != MAIL_X))


class SetTests(unittest.TestCase):
    def setUp(self):
        people_excluded.forget()
        self.addCleanup(people_excluded.forget)

    def test_a_set_of_another_list_or_without_postgres_is_never_used(self):
        with mock.patch.object(people_excluded._KEPT, "get", return_value=SET):
            with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only"}):
                with mock.patch.object(people_excluded, "current_exclusion",
                                       return_value="tag-1"):
                    self.assertIs(people_excluded.excluded(), SET)
                for other in ("tag-2", None):
                    with mock.patch.object(people_excluded, "current_exclusion",
                                           return_value=other):
                        self.assertIsNone(people_excluded.excluded())
            with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": ""}):
                self.assertIsNone(people_excluded.excluded())

    def test_the_rows_are_read_with_the_state_row_or_not_at_all(self):
        # one statement: the handles found and the state row of that moment; any other
        # state (an undo left none, another list, an older number) or a failure: None
        names = [MAIL_X, MAIL_OK]
        for row, want in (([("tag-1", 100, [MAIL_X])], frozenset({MAIL_X})),
                          ([("tag-1", 120, None)], frozenset()),
                          ([], None), ([("tag-2", 100, [MAIL_X])], None),
                          ([("tag-1", 99, [MAIL_X])], None), ([("tag-1", None, [])], None)):
            with self.subTest(row=row), \
                    mock.patch.object(people_excluded._LOOKUP, "_query", return_value=row):
                self.assertEqual(SET.withheld(names), want)
                self.assertEqual(SET.withholds(MAIL_X),
                                 None if want is None else MAIL_X in want)
        with mock.patch.object(people_excluded._LOOKUP, "_query",
                               side_effect=RuntimeError("down")):
            self.assertIsNone(SET.withheld(names))
            self.assertIsNone(SET.withholds(MAIL_X))
        self.assertIsNone(people_excluded.Excluded("tag-1").withheld(names))

    def test_own_units_are_what_the_full_readers_policy_denies(self):
        from integrations.people_exclusion import PeopleExclusion
        guard = PeopleExclusion([OID_X])
        keys = [f"mail:{OID_X}", f"mail:{OID_OK}", "mail:not-an-oid",
                f"meetings:calendar:{OID_X}", f"meetings:organizer:{OID_OK}",
                f"meetings:plan:{OID_X}", "meetings:unknown:x", "meetings:calendar:",
                "sharepoint:site", "teams:chat"]
        self.assertEqual(people_excluded.own_unit_keys(keys, guard), sorted([
            f"mail:{OID_X}", "mail:not-an-oid", f"meetings:calendar:{OID_X}",
            "meetings:unknown:x", "meetings:calendar:"]))
        # an unavailable guard decides nobody clear: every person's unit
        closed = people_excluded.own_unit_keys(keys, PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assertIn(f"mail:{OID_OK}", closed)

    def test_teams_and_meetings_rows_name_every_identity_the_gate_reads(self):
        directory = mock.Mock(unavailable=None)
        from integrations.people_exclusion import PeopleExclusion
        guard = PeopleExclusion([OID_X])
        row = {"from": {"user": {"id": OID_OK}}, "participants": [{"id": OID_OK}]}
        acl = {"source_acl": {"identities": [OID_X]}}
        self.assertTrue(people_excluded.capture_withheld("teams-chat", row, acl, guard,
                                                         directory))
        # a channel message is not decided by the chat ACL (as the gate)
        with mock.patch("operating.excluded_people.participants_withheld",
                        return_value=False):
            self.assertFalse(people_excluded.capture_withheld("teams-channel", row, acl,
                                                              guard, directory))
            self.assertTrue(people_excluded.capture_withheld(
                "calendar-event", {"attendees": [{"emailAddress": {"address": OID_X}}]}, {},
                guard, directory))
        with mock.patch("operating.excluded_people.participants_withheld",
                        side_effect=RuntimeError("down")):
            self.assertTrue(people_excluded.capture_withheld("planner-task", {}, {}, guard,
                                                             directory))

    def test_write_refuses_an_undecided_list_or_a_bad_row(self):
        with self.assertRaises(ValueError):
            people_excluded.write(mock.Mock(), [], "", 1)
        with self.assertRaises(ValueError):
            people_excluded.write(mock.Mock(), [(MAIL_X, "Email:x:1", "other")], "tag-1", 1)
        for covered in (None, -1, "7"):   # no version number: nothing is written
            conn = mock.Mock()
            with self.assertRaises(ValueError):
                people_excluded.write(conn, [], "tag-1", covered)
            conn.transaction.assert_not_called()


class ObjectLevelTests(unittest.TestCase):
    """``document_marks``: a full reader alone is kept from the set's objects, by the
    exclusion tag (a short text), never by the set's ids sent with the statement (review
    2026-11-17: 1.37M ids bound per statement took 4-24 s). The Postgres side is
    ``test_people_excluded_parity``."""

    def setUp(self):
        document_marks.forget()
        self.addCleanup(document_marks.forget)

    def test_only_a_full_reader_carries_the_tag_and_never_a_list(self):
        from ontology.store_ddl import MARKINGS
        for held in ((), sorted(MARKINGS)):
            self.assertEqual(document_marks.params(held)["dm_people"], "")   # staff
            self.assertEqual(document_marks.params(held, "tag-1")["dm_people"], "tag-1")
        for odd in (None, ["Email:x:1"], 7):   # never a list, never anything but text
            self.assertEqual(document_marks.params((), odd)["dm_people"], "")

        def base(material):
            return mock.Mock(item_gate=mock.Mock(key_material=material))

        self.assertEqual(people_excluded.full_reader(
            base(lambda a: ("people-excluded", "tag-1")), "owner"), "tag-1")
        for material in (lambda a: {"actor": a}, lambda a: None,
                         lambda a: ("people-excluded", ""), mock.Mock(side_effect=OSError)):
            self.assertEqual(people_excluded.full_reader(base(material), "x"), "")
        self.assertEqual(people_excluded.full_reader(object(), "x"), "")
        # the gate's own hook (``case_tools_host_gates``): its text decides, and the whole
        # key material (the staff rule's baselines) is never read for it
        never = mock.Mock(side_effect=AssertionError("key material read"))
        for tag, want in (("tag-1", "tag-1"), ("", "")):
            gate = types.SimpleNamespace(full_reader_tag=lambda a, t=tag: t, key_material=never)
            self.assertEqual(people_excluded.full_reader(
                types.SimpleNamespace(item_gate=gate), "owner"), want)
        gate = types.SimpleNamespace(full_reader_tag=mock.Mock(side_effect=OSError),
                                     key_material=never)
        self.assertEqual(people_excluded.full_reader(
            types.SimpleNamespace(item_gate=gate), "x"), "")
        # a hook answering anything but text is no answer: the key material decides
        for odd in (None, ["tag-1"], ("people-excluded", "tag-1")):
            gate = types.SimpleNamespace(
                full_reader_tag=lambda a, o=odd: o,
                key_material=lambda a: ("people-excluded", "tag-2"))
            self.assertEqual(people_excluded.full_reader(
                types.SimpleNamespace(item_gate=gate), "x"), "tag-2", odd)

    def test_the_sets_tables_are_named_only_when_postgres_holds_them(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE o (object_id TEXT)")
        db.executemany("INSERT INTO o VALUES (?)", [("Email:x:1",), ("Email:ok:3",)])
        for env, kept in (({"PLX_ONTOLOGY_PG": ""}, SET), ({"PLX_ONTOLOGY_PG": "only"}, None)):
            with self.subTest(env=env), mock.patch.dict(os.environ, env), \
                    mock.patch.object(people_excluded._KEPT, "get", return_value=kept):
                text = document_marks.sql("object_id")
                self.assertNotIn("people_excluded", text)
                got = [r[0] for r in db.execute(f"SELECT object_id FROM o WHERE {text}",
                                                document_marks.params((), "tag-1"))]
                self.assertEqual(got, ["Email:x:1", "Email:ok:3"])
        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only"}), \
                mock.patch.object(people_excluded._KEPT, "get", return_value=SET):
            text = document_marks.sql("o.object_id")
        self.assertIn("people_excluded_objects peo WHERE peo.object_id = o.object_id", text)
        self.assertIn("pes.exclusion = :dm_people", text)
        self.assertNotIn("json_each(:dm_people)", text)


class MigrationTests(unittest.TestCase):
    def test_the_full_reader_branch_covers_the_same_schemes(self):
        text = (Path(people_excluded.__file__).parent / "migrations"
                / "0040_people_excluded_objects.sql").read_text()
        for scheme in people_excluded.COVERED_SCHEMES:
            self.assertIn(f"'{scheme}'", text)
        self.assertIn("'full:' || s.exclusion = reader_keys[1]", text)
        self.assertIn(f"arg ~ '{people_excluded.DECIDED_FORM}'", text)
        self.assertIn("v.ver <= covered", text)
        self.assertIn("v.ver > covered", text)
        self.assertIn("covered_ver bigint NOT NULL", text)





class UnreadableStoreTest(unittest.TestCase):
    def test_a_capture_store_this_user_cannot_open_is_not_covered_never_a_crash(self):
        """11-22 live: split raised "unable to open database file" on the Teams store (its
        live WAL unreadable to the build's user). Now that family is named as not covered
        (its served versions stay ``uncaptured``: withheld), a readable one still opens and
        a family with no store is simply absent."""
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "teams-capture").mkdir()
            (Path(tmp) / "teams-capture" / "capture.db").write_bytes(b"not a database" * 100)
            (Path(tmp) / "meetings-capture").mkdir()
            good = sqlite3.connect(Path(tmp) / "meetings-capture" / "capture.db")
            good.execute("CREATE TABLE t (x)")
            good.commit()
            good.close()
            stores, not_covered = people_excluded.open_stores(tmp)
            self.assertEqual(not_covered, ["teams"])
            self.assertEqual(list(stores), ["meetings"])
            stores["meetings"].close()
            (Path(tmp) / "meetings-capture" / "capture.db").unlink()
            self.assertEqual(people_excluded.open_stores(tmp)[1], ["teams"])
            self.assertEqual(people_excluded.open_stores(tmp)[0], {})


class CurrentTagIsReadOnceUntilTheListMoves(unittest.TestCase):
    """``current_exclusion`` (asked by every kept page's check, per request) parses the list
    again only when its settings or files moved; never serves a tag for another list."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "excluded.txt"
        self.write([OID_X])
        env = {"PLX_EXCLUDED_PEOPLE_FILE": str(self.path)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        people_excluded._TAG = None

    def write(self, ids):
        import json
        from integrations.people_exclusion import SCHEMA
        self.path.write_text(json.dumps({"schema": SCHEMA, "object_ids": ids}))

    def test_same_list_parsed_once_and_a_changed_list_gives_its_own_tag(self):
        from integrations.people_exclusion import PeopleExclusion
        real = PeopleExclusion.from_env.__func__
        calls = []
        with mock.patch.object(PeopleExclusion, "from_env", classmethod(
                lambda cls, environ, **kw: calls.append(1) or real(cls, environ, **kw))):
            first = people_excluded.current_exclusion()
            self.assertTrue(first)
            for _ in range(5):
                self.assertEqual(people_excluded.current_exclusion(), first)
            self.assertEqual(len(calls), 1)
            self.write([OID_X, OID_OK])       # the list moved
            second = people_excluded.current_exclusion()
            self.assertNotEqual(second, first)
            self.assertEqual(people_excluded.current_exclusion(), second)
            self.assertEqual(len(calls), 2)
            # a mapping that is not the process's own is read every time, never remembered
            other = dict(os.environ)
            people_excluded.current_exclusion(other)
            people_excluded.current_exclusion(other)
            self.assertEqual(len(calls), 4)

    def test_an_unreadable_list_is_none_and_is_read_again_when_it_returns(self):
        self.assertTrue(people_excluded.current_exclusion())
        self.path.unlink()
        self.assertIsNone(people_excluded.current_exclusion())   # unavailable: fail closed
        self.write([OID_X])
        self.assertTrue(people_excluded.current_exclusion())


if __name__ == "__main__":
    unittest.main()
