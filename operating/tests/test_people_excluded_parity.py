"""Parity of the people exclusion's precomputed set with the gates it replaces for a full
reader (``operating.pg.people_excluded``, business rule 2026-11-17).

One mail capture and one served Postgres store: the set is built by the real build
(``people_excluded.build``) over them, then for every served version handle (each message,
attachment and a page of each) three answers must agree row for row:

* the old item gate (``item_gate_from_case_runtime``: ``CaseRuntime.resolve_citation``) of a
  reader every mailbox is open to, which the people exclusion alone can deny, as a full
  reader;
* the host's item gate for a full reader with the set in use (no capture read);
* ``ontology.sq_item_gate`` in the database for the full reader's key ``full:<tag>``.

A staff reader's item gate answers are identical with the set in use and without; the set's
object-level filter (``document_marks``, a ``NOT EXISTS`` inside the reader's statement)
hides from a full reader only objects that the old full-reader gate also withheld, and from
a staff reader nothing, so no reader loses an object it could see; an undo (TRUNCATE) or a
set of another list takes effect on the next statement.

Fixture: the orders mailbox holds 60 cleared messages (one with an attachment), one copied to a
tenant address nothing can decide and one copied to the finance mailbox (with an
attachment); the finance mailbox, whose owner is excluded, holds one more; a Teams version
the capture holds no row of is served too.

The set is a snapshot up to one served version number (``covered_ver``): mail recorded
after it goes through the old gate until the incremental update decides it, and a failed
update leaves the set as it was (``PeopleExcludedFreshnessTests``).

Evidence label: injected-transport (the mail capture over the capture tests' in-process
FakeGraph behind the real GET-only client; a throwaway Postgres database; made-up ids).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, READER_A, SCOPE,
                                                   UPN_A, UPN_B, Clock, FakeGraph,
                                                   capture_audience, file_attachment)
from ontology.core import Actor
from operating.case_mail_scope import MailScope
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaseCaptures
from operating.ontology_tools_results import item_gate_from_case_runtime
from operating.pg import people_excluded
from operating.search import document_marks
from operating.tests import pg_testdb

FULL = Actor("plx", frozenset({"staff:every-mailbox"}), "restricted")
UNDECIDED = "pat.unlisted@example-plx.com"
EXCLUDED_B = {**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_B}
MIGRATIONS = [Path(people_excluded.__file__).parent / "migrations" / name
              for name in ("0006_ontology.sql", "0007_ontology_compact.sql",
                           "0008_ontology_read.sql", "0009_schema_migrations.sql",
                           "0027_item_audiences.sql", "0040_people_excluded_objects.sql")]
MORE = 59   # 60 cleared messages in all
TEAMS = "teams-chat://chat/19:abc/m1@g-none-1"


def _reader_audience() -> LiveSourceAudience:
    return LiveSourceAudience({f"mail:{UPN_A}": ["staff:orders", "staff:every-mailbox"],
                               f"mail:{UPN_B}": ["staff:finance", "staff:every-mailbox"]},
                              tenant="plx", classifications={"mail:*": "restricted"})


class _Bound:
    def __init__(self):
        self.memo, self.search = {}, False


def _host(runtime_gate, runtime):
    from operating.case_tools_host_gates import _GatesMixin

    class Host(_GatesMixin):
        mail_capture_dir = None          # the runtime gate decides mail (the old answer)
        sharepoint_capture_dir = None

    host = Host()
    host.runtime, host._call_bound = runtime, _Bound()
    return host._item_gate(runtime_gate)


def _serve(pg, served, first_ver: int) -> None:
    """Insert ``served`` ((handle, object id, audience key)) as objects and versions
    numbered from ``first_ver``."""
    for n, (handle, object_id, key) in enumerate(served, first_ver):
        pg.execute("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                   "native_key, audience_key, first_recorded_at) VALUES "
                   "(%s, %s, 'b', %s, %s, '2026-01-01') ON CONFLICT DO NOTHING",
                   (object_id, object_id.partition(":")[0], handle, key))
        pg.execute("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, "
                   "digest, lineage, markings, recorded_at) VALUES "
                   "(%s, %s, %s, 'd', 'graph_mail', '[]', '2026-01-01')",
                   (n, object_id, handle))


def _rows(dsn: str) -> list[tuple[str, str, str]]:
    """The set's rows (handle, object id, reason) as Postgres holds them."""
    import psycopg
    with psycopg.connect(dsn) as pg:
        return pg.execute("SELECT l1_handle, object_id, reason FROM "
                          "ontology.people_excluded_objects").fetchall()


def _loaded(dsn: str) -> people_excluded.Excluded | None:
    """The set as a serving process loads it (``_Kept._read``) from ``dsn``."""
    with mock.patch.dict(os.environ, {"PLX_PG_DSN": dsn}):
        return people_excluded._Kept._read()


class _Answers:
    """The three answers for a full reader (module docstring) and a staff reader's."""

    def runtime(self) -> CaseRuntime:
        root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        audience = root / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {}}), encoding="utf-8")
        runtime = CaseRuntime(root / "state", client=None, audience=audience, tenant="plx",
                              live=LiveReaders(audience=_reader_audience()),
                              mail_scope=MailScope([UPN_A, UPN_B]),
                              people_exclusion=PeopleExclusion.from_env(EXCLUDED_B))
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=self.state)
        return runtime

    def answers(self, *, full: bool, use_set: bool, actor: Actor, handles=None,
                got=None) -> dict[str, bool]:
        runtime = self.runtime()
        gate = _host(item_gate_from_case_runtime(runtime), runtime)
        resolve, reads = runtime.resolve_citation, []

        def counted(handle, reader):
            reads.append(handle)
            return resolve(handle, reader)

        runtime.resolve_citation = counted
        people_excluded.forget()
        self.addCleanup(people_excluded.forget)
        with mock.patch("operating.employee_permissions.reader_unrestricted",
                        return_value=full), \
                mock.patch.dict(os.environ, {"PLX_PG_DSN": self.dsn}), \
                mock.patch.object(people_excluded, "excluded",
                                  return_value=(got or self.set) if use_set else None):
            got = {h: gate(actor, h) for h in (self.handles if handles is None else handles)}
        self.reads = reads
        return got

    def sql_answers(self, handles, exclusion=None) -> dict[str, bool]:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            pg.execute("CREATE TEMP TABLE sq_gates (fn text PRIMARY KEY, keys text[], "
                       "rosters text[])")
            pg.execute("INSERT INTO sq_gates VALUES ('g', ARRAY[%s], ARRAY['full'])",
                       (people_excluded.reader_key(("people-excluded",
                                                    exclusion or self.set.exclusion)),))
            return {h: pg.execute("SELECT ontology.sq_item_gate('g', %s)", (h,)).fetchone()[0]
                    for h in handles}

    def sql_left_to_python(self, handle, exclusion=None) -> bool:
        """Whether ``sq_item_gate`` hands ``handle`` to the Python gate (``sq_call``; none
        in this session: the call fails, nothing is admitted)."""
        import psycopg
        try:
            self.sql_answers([handle], exclusion)
        except psycopg.Error:
            return True
        return False


class PeopleExcludedParityTests(_Answers, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.state = Path(cls.tmp.name) / "mail"
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-OK", "CK-OK", subject="Cleared",
                  attachments=[file_attachment("ATT-OK")])
        graph.put(BOX_A, "MSG-UND", "CK-UND", subject="Undecided")
        graph.put(BOX_A, "MSG-EXC", "CK-EXC", subject="Copied to finance",
                  attachments=[file_attachment("ATT-EXC", content=b"%PDF-1.4 other\n" * 40)])
        graph.put(BOX_B, "MSG-B", "CK-B", subject="In the finance mailbox")
        for n in range(MORE):   # cardinality: no cleared row lost across the build's slices
            graph.put(BOX_A, f"MSG-N{n}", f"CK-N{n}", subject=f"Cleared {n}")
        graph.messages[BOX_A]["MSG-UND"]["ccRecipients"] = [
            {"emailAddress": {"address": UNDECIDED}}]
        graph.messages[BOX_A]["MSG-EXC"]["ccRecipients"] = [
            {"emailAddress": {"address": UPN_B}}]
        store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
        messages = list(store.db.execute("SELECT l1_handle, mailbox_id FROM messages"))
        attachments = [r[0] for r in store.db.execute("SELECT l1_handle FROM attachments")]
        # without the capture's record of the tenant's uncleared addresses (a store
        # recorded before it, or one aged out), the tenant address stays undecided
        # (``tests.test_mail_participants_cleared.TenantRecordTests`` covers the record)
        with store.db:
            store.db.execute("DELETE FROM meta WHERE key='tenant_withheld_at'")
        store.close()
        assert len(messages) == 4 + MORE and len(attachments) == 2, (messages, attachments)
        box_of = dict(messages)
        # served versions: each message, each attachment (its message's mailbox unit), and
        # a Teams version no capture holds
        cls.served = []
        for handle, box in messages:
            cls.served.append((handle, f"Email:{handle}", f"mail:{box}"))
        for handle in attachments:
            box = box_of[handle.partition("#")[0]]
            cls.served.append((handle, f"EmailAttachment:{handle}", f"mail:{box}"))
        cls.served.append((TEAMS, "TeamsMessage:m1", "teams:chat:19:abc"))
        # a person citation (no captured message: its gate decides it, never the set) and a
        # second object backed by the withheld copy's attachment (both objects are the set's)
        from ontology.people_directory import address_key
        cls.person = f"graph-mail://people/person/{address_key('supplier@outside.example')}"
        cls.served.append((cls.person, "Person:supplier", f"mail:{BOX_A}"))
        shared = next(h for h, _o, _k in cls.served if "#attachment=ATT-EXC" in h)
        cls.served.append((shared, "Document:carried-copy", f"mail:{BOX_A}"))
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
            _serve(pg, cls.served, 1)
        with psycopg.connect(cls.dsn) as pg, \
                mock.patch.object(people_excluded, "internal_domains",
                                  return_value=tuple(SCOPE.internal_domains)):
            cls.result = people_excluded.build(pg, cls.state, environ=EXCLUDED_B, workers=2)
            pg.commit()
        cls.set = _loaded(cls.dsn)
        assert cls.set is not None and cls.set.covered_ver == len(cls.served), cls.set
        cls.handles = list(dict.fromkeys(h for h, _o, _k in cls.served))
        cls.handles += [h + "#page=2" if "#" not in h else h + "&page=2"
                        for h, _o, _k in cls.served if h.startswith("graph-mail://")]

    def test_the_build_decides_each_kind_of_version(self):
        # the excluded mailbox's message (own unit, by its unit and by its mailbox), the
        # undecided copy, the copy to finance and its attachment, the uncaptured Teams one
        self.assertEqual((self.result["handles"], set(self.result["by_reason"])),
                         (5, {"own_unit", "participant", "uncaptured"}), self.result)
        self.assertEqual(self.result["objects"], 6)   # the attachment backs two objects
        rows = _rows(self.dsn)
        self.assertIn("Document:carried-copy", {o for _h, o, _r in rows})
        self.assertNotIn(self.person, {h for h, _o, _r in rows})
        self.assertFalse(people_excluded.decides(self.person))
        self.assertEqual(self.set.exclusion,
                         PeopleExclusion.from_env(EXCLUDED_B).members_checked_digest())

    def test_a_full_reader_reads_everything_but_the_set_row_for_row(self):
        old = self.answers(full=False, use_set=False, actor=FULL)
        # the fixture holds both answers (no vacuous parity)
        self.assertTrue(any(old.values()) and not all(old.values()), old)
        self.assertEqual(sum(1 for h, ok in old.items()
                             if ok and "#" not in h and h.startswith("graph-mail://1")), 1 + MORE)
        new = self.answers(full=True, use_set=True, actor=FULL)
        self.maxDiff = None
        self.assertEqual(new, old)
        # the set alone decided every form it decides (not one citation resolved for
        # them); a page of a message is no form it decides: the gate denies it, as before
        decided = [h for h in self.handles if people_excluded.decides(h)]
        undecided = set(self.handles) - set(decided)
        self.assertEqual({h.rpartition("#")[2] for h in undecided - {self.person}},
                         {"page=2"})
        self.assertIn(self.person, undecided)
        self.assertEqual(len(self.reads), len(undecided))
        with mock.patch.dict(os.environ, {"PLX_PG_DSN": self.dsn}):
            self.assertEqual({h: not self.set.withholds(h) for h in decided},
                             {h: old[h] for h in decided})
        self.assertEqual(self.sql_answers(decided), {h: old[h] for h in decided})

    def test_a_staff_reader_is_decided_as_before(self):
        before = self.answers(full=False, use_set=False, actor=READER_A)
        after = self.answers(full=False, use_set=True, actor=READER_A)
        self.assertEqual(after, before)
        self.assertGreater(len(self.reads), 0)   # still through the gate
        self.assertTrue(any(before.values()), before)

    def hidden(self, people: str, *, explain: bool = False):
        """The served objects ``document_marks.sql`` hides on Postgres (through the
        reader's own connection, ``PgCompatConnection``) for ``people``
        (``document_marks.params``), or the statement's plan."""
        from operating.pg.sqlite_compat import PgCompatConnection
        document_marks.forget()
        self.addCleanup(document_marks.forget)
        with mock.patch.dict(os.environ, {"PLX_PG_DSN": self.dsn, "PLX_ONTOLOGY_PG": "only"}), \
                mock.patch.object(people_excluded._KEPT, "get", return_value=self.set):
            text = document_marks.sql("o.object_id")
            params = document_marks.params((), people)
        self.assertNotIn("json_each(:dm_people)", text)   # never a list with the statement
        if explain:   # the statement as the compat layer sends it, planned by Postgres
            import psycopg
            from operating.pg.sqlite_compat import translate
            with psycopg.connect(self.dsn) as raw:
                raw.execute("SET search_path = ontology, public")
                return "\n".join(r[0] for r in raw.execute("EXPLAIN " + translate(
                    "SELECT o.object_id FROM objects o WHERE o.object_id = :one AND "
                    + text), {**params, "one": "Email:x", **{f"{k}__list_text": json.loads(params[k])
                                               for k in ("dm_ids", "dm_own", "dm_types")}}
                ).fetchall())
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        return {r[0] for r in pg.execute(
            f"SELECT o.object_id FROM objects o WHERE NOT {text}", params).fetchall()}

    def test_the_object_level_filter_hides_nothing_a_full_reader_could_see(self):
        import psycopg
        old = self.answers(full=False, use_set=False, actor=FULL)
        self.assertIn(self.person, old)
        by_object: dict[str, set[bool]] = {}
        for h, o, _k in self.served:
            by_object.setdefault(o, set()).add(old[h])
        withheld = {o for o, seen in by_object.items() if False in seen} - {"Person:supplier"}
        self.assertTrue(withheld and set(by_object) - withheld)   # both kinds: not vacuous
        self.assertEqual(self.hidden(self.set.exclusion), withheld)
        # staff (no tag), another list's tag: nothing hidden by the set
        self.assertEqual(self.hidden(""), set())
        self.assertEqual(self.hidden("another"), set())
        # one index probe by object id (the planner may take it at any table size)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("ALTER DATABASE " + self.name + " SET enable_seqscan = off")
        try:
            plan = self.hidden(self.set.exclusion, explain=True)
        finally:
            with psycopg.connect(self.dsn, autocommit=True) as pg:
                pg.execute("ALTER DATABASE " + self.name + " RESET enable_seqscan")
        self.assertIn("people_excluded_objects_by_object", plan)
        self.assertNotIn("Seq Scan on people_excluded_objects", plan)
        # an undo (TRUNCATE) or a rebuild for another list: no filter on the very next
        # statement and the Python lookup decides nothing (the gates decide), with the
        # process's state row still the old one
        with psycopg.connect(self.dsn) as pg:
            rows = pg.execute("SELECT l1_handle, object_id, reason FROM "
                              "ontology.people_excluded_objects").fetchall()
            state = pg.execute("SELECT exclusion, built_at, objects, handles, covered_ver "
                               "FROM ontology.people_excluded_state").fetchone()
        try:
            for undo in ("UPDATE ontology.people_excluded_state SET exclusion = 'another'",
                         "TRUNCATE ontology.people_excluded_objects, "
                         "ontology.people_excluded_state"):
                with self.subTest(undo=undo):
                    with psycopg.connect(self.dsn, autocommit=True) as pg:
                        pg.execute(undo)
                    self.assertEqual(self.hidden(self.set.exclusion), set())
                    with mock.patch.dict(os.environ, {"PLX_PG_DSN": self.dsn}):
                        self.assertIsNone(self.set.withholds(self.served[0][0]))
                    new = self.answers(full=True, use_set=True, actor=FULL)
                    self.assertEqual(new, old)   # the old gate, row for row
                    self.assertGreater(len(self.reads), 0)
        finally:
            with psycopg.connect(self.dsn) as pg:
                pg.execute("TRUNCATE ontology.people_excluded_objects, "
                           "ontology.people_excluded_state")
                with pg.cursor().copy("COPY ontology.people_excluded_objects (l1_handle, "
                                      "object_id, reason) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
                pg.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, "
                           "objects, handles, covered_ver) VALUES (%s, %s, %s, %s, %s)", state)

    def test_a_carried_attachment_takes_its_members_and_pages_by_index_alone(self):
        import psycopg
        head = "graph-mail://box/M@C#attachment=A&sha256=ab"
        mine = [head, head + "&page=2", head + "&member=x.pdf", head + "&member=x.pdf&page=1"]
        others = [head + "c", head[:-1], "graph-mail://box/M@C#attachment=B&sha256=ab",
                  head.replace("&sha256", "%sha256"), "graph-mail://box/M@C"]
        with psycopg.connect(self.dsn) as pg:
            self.assertTrue(people_excluded.byte_ordered(pg))
            for n, handle in enumerate(mine + others, 10_000):
                pg.execute("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, "
                           "digest, lineage, markings, recorded_at) VALUES (%s, %s, %s, 'd', "
                           "'graph_mail', '[]', '2026-01-01')", (n, f"O:{n}", handle))
            with mock.patch.object(people_excluded, "CARRIED_BATCH", 1):
                got = people_excluded.carried_versions(pg.cursor(), [head, "graph-mail://none"])
            pg.execute("SET enable_seqscan = off")   # usable index: the planner takes it
            plan = "\n".join(r[0] for r in pg.execute(
                "EXPLAIN " + people_excluded._CARRIED_SQL.replace("%s", "ARRAY['x']"),
            ).fetchall())
            pg.rollback()
        self.assertEqual(sorted(h for h, _o in got), sorted(mine))
        self.assertNotIn("Seq Scan on object_versions", plan)

    def test_another_list_or_another_form_is_left_to_the_python_gate(self):
        import psycopg
        tag = people_excluded.reader_key(("people-excluded", self.set.exclusion))
        page = next(h for h in self.handles if h.endswith("#page=2"))
        for key, handle in (("full:another", self.handles[0]), (tag, page), (tag, self.person)):
            with self.subTest(key=key, handle=handle), psycopg.connect(self.dsn) as pg:
                pg.execute("CREATE TEMP TABLE sq_gates (fn text PRIMARY KEY, keys text[], "
                           "rosters text[])")
                pg.execute("INSERT INTO sq_gates VALUES ('g', ARRAY[%s], ARRAY['full'])",
                           (key,))
                # not this list's set, or no form it decides: the database calls the
                # Python gate (none in this session: the call fails, nothing is admitted)
                with self.assertRaises(psycopg.Error):
                    pg.execute("SELECT ontology.sq_item_gate('g', %s)", (handle,)).fetchone()


class PeopleExcludedFreshnessTests(_Answers, unittest.TestCase):
    """R1 (review 2026-11-17): the set is a snapshot. A message captured and served after
    the build is decided by the old gate (never the set) until the incremental update
    decides it; a failed update changes nothing."""

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.state = Path(cls.tmp.name) / "mail"
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-OK", "CK-OK", subject="Cleared")
        graph.put(BOX_B, "MSG-B", "CK-B", subject="In the finance mailbox")
        store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        capture = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                              sleep=lambda s: None)
        assert capture.run(SCOPE)["complete"]
        old = cls._messages(store)
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
            _serve(pg, old, 1)
        with psycopg.connect(cls.dsn) as pg, \
                mock.patch.object(people_excluded, "internal_domains",
                                  return_value=tuple(SCOPE.internal_domains)):
            people_excluded.build(pg, cls.state, environ=EXCLUDED_B, workers=2)
            pg.commit()
            cls.built = (pg.execute("SELECT l1_handle, object_id, reason FROM "
                                    "ontology.people_excluded_objects").fetchall(),
                         pg.execute("SELECT exclusion, built_at, objects, handles, covered_ver "
                                    "FROM ontology.people_excluded_state").fetchone())
        cls.covered = cls.built[1][4]
        assert cls.covered == len(old), cls.built
        # after the build: the delta captures and serves new mail, one copied to the
        # excluded finance mailbox (with an attachment), one cleared; and a newer version
        # of a message the set already decided as cleared
        graph.put(BOX_A, "MSG-NEW-EXC", "CK-NEW-EXC", subject="New, copied to finance",
                  attachments=[file_attachment("ATT-NEW")])
        graph.messages[BOX_A]["MSG-NEW-EXC"]["ccRecipients"] = [
            {"emailAddress": {"address": UPN_B}}]
        graph.put(BOX_A, "MSG-NEW-OK", "CK-NEW-OK", subject="New, cleared")
        assert capture.run(SCOPE)["complete"]
        known = {h for h, _o, _k in old}
        cls.new = [r for r in cls._messages(store) if r[0] not in known]
        store.close()
        assert len(cls.new) == 3, cls.new   # two messages and the attachment
        cls.ok = graph_read.mail_citation(BOX_A, "MSG-OK", "CK-OK")
        assert cls.ok in known, (cls.ok, known)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            _serve(pg, cls.new, cls.covered + 1)
            # the cleared message re-recorded (a newer version of the same handle)
            pg.execute("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, "
                       "digest, lineage, markings, recorded_at) VALUES (%s, %s, %s, 'd2', "
                       "'graph_mail', '[]', '2026-01-02')",
                       (cls.covered + 10, f"Email:{cls.ok}", cls.ok))
        cls.new_handles = [h for h, _o, _k in cls.new]
        cls.handles = [h for h, _o, _k in old] + cls.new_handles

    @staticmethod
    def _messages(store):
        box_of = dict(store.db.execute("SELECT l1_handle, mailbox_id FROM messages"))
        out = [(h, f"Email:{h}", f"mail:{b}") for h, b in box_of.items()]
        out += [(h, f"EmailAttachment:{h}", f"mail:{box_of[h.partition('#')[0]]}")
                for (h,) in store.db.execute("SELECT l1_handle FROM attachments")]
        return out

    def setUp(self):
        import psycopg
        rows, state = self.built   # each test starts from the build's set
        with psycopg.connect(self.dsn) as pg:
            pg.execute("TRUNCATE ontology.people_excluded_objects, "
                       "ontology.people_excluded_state")
            with pg.cursor().copy("COPY ontology.people_excluded_objects (l1_handle, "
                                  "object_id, reason) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            pg.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, "
                       "objects, handles, covered_ver) VALUES (%s, %s, %s, %s, %s)", state)
        self.set = _loaded(self.dsn)

    def update(self):
        import psycopg
        with psycopg.connect(self.dsn) as pg, \
                mock.patch.object(people_excluded, "internal_domains",
                                  return_value=tuple(SCOPE.internal_domains)):
            return people_excluded.update(pg, self.state, environ=EXCLUDED_B)

    def state_now(self):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return (sorted(pg.execute("SELECT l1_handle, object_id, reason FROM "
                                      "ontology.people_excluded_objects").fetchall()),
                    pg.execute("SELECT covered_ver, objects, handles FROM "
                               "ontology.people_excluded_state").fetchone())

    def test_mail_served_after_the_build_is_decided_by_the_old_gate(self):
        self.assertEqual(self.set.covered_ver, self.covered)
        old = self.answers(full=False, use_set=False, actor=FULL)
        # the new copy to finance and its attachment are withheld, the new cleared one
        # is not (no vacuous parity)
        self.assertEqual({h: old[h] for h in self.new_handles},
                         {h: "MSG-NEW-OK" in h for h in self.new_handles})
        new = self.answers(full=True, use_set=True, actor=FULL)
        self.assertEqual(new, old)
        # the set holds none of them and decided none of them: the old gate read each
        # new handle and the re-recorded cleared message
        self.assertFalse(set(self.new_handles) & {h for h, _o, _r in _rows(self.dsn)})
        self.assertEqual(set(self.reads), set(self.new_handles) | {self.ok})
        for handle in self.new_handles + [self.ok]:
            self.assertFalse(self.set.covered(handle))
            self.assertTrue(self.sql_left_to_python(handle), handle)
        # a message the set covers is still the set's alone (by the database too)
        covered = [h for h in self.handles if h not in self.reads]
        self.assertTrue(covered)
        self.assertEqual(self.sql_answers(covered), {h: old[h] for h in covered})

    def test_the_update_decides_the_new_versions(self):
        old = self.answers(full=False, use_set=False, actor=FULL)
        with mock.patch.object(people_excluded, "CARRIED_BATCH", 1):   # every batch edge
            got = self.update()
        top = self.covered + 10
        self.assertEqual(got["covered_ver"], top, got)
        rows, state = self.state_now()
        self.assertEqual(state[0], top)
        exc = {h for h in self.new_handles if "MSG-NEW-EXC" in h}
        self.assertEqual(len(exc), 2)   # the copy to finance and its attachment
        self.assertEqual({h for h, _o, r in rows if r == "participant"}, exc)
        self.assertEqual(state[2], len({h for h, _o, _r in rows}))
        self.set = _loaded(self.dsn)
        new = self.answers(full=True, use_set=True, actor=FULL)
        self.assertEqual(new, old)
        self.assertEqual(self.reads, [])   # the set decided every handle now
        self.assertEqual(self.sql_answers(self.handles), old)
        # nothing new: the update is a no-op
        again = self.update()
        self.assertEqual((again["covered_ver"], again["versions"]), (top, 0))
        self.assertEqual(self.state_now(), (rows, state))

    def test_a_set_of_another_list_is_rebuilt_whole(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("UPDATE ontology.people_excluded_state SET exclusion = 'another'")
        got = self.update()
        self.assertTrue(got.get("full"), got)
        rows, state = self.state_now()
        self.assertEqual(state[0], self.covered + 10)
        self.assertEqual(_loaded(self.dsn).exclusion, self.set.exclusion)
        self.assertTrue({h for h in self.new_handles if "MSG-NEW-EXC" in h}
                        <= {h for h, _o, _r in rows})

    def test_a_failed_update_changes_nothing(self):
        before = self.state_now()
        old = self.answers(full=False, use_set=False, actor=FULL)

        def bump(conn):   # a build in between: the state is no longer the one read
            import psycopg
            with psycopg.connect(self.dsn, autocommit=True) as other:
                other.execute("UPDATE ontology.people_excluded_state SET covered_ver = %s",
                              (self.covered - 1,))
            return real(conn)

        real = people_excluded.watermark
        for name, patch in (
                ("decision", mock.patch.object(people_excluded, "_mail_messages",
                                               side_effect=RuntimeError("capture"))),
                ("lookup", mock.patch.object(people_excluded, "_UNDER_SQL", "SELECT nope")),
                ("raced", mock.patch.object(people_excluded, "watermark", side_effect=bump))):
            with self.subTest(name), patch:
                import psycopg
                with self.assertRaises((RuntimeError, psycopg.Error)) as raised:
                    self.update()
                if name == "raced":
                    # the update wrote nothing over the other build's state
                    self.assertEqual(str(raised.exception), "set_changed")
                    self.assertEqual(self.state_now(),
                                     (before[0], (self.covered - 1, *before[1][1:])))
                    self.setUp()   # back to the build's set for the checks below
                self.assertEqual(self.state_now(), before)
                self.set = _loaded(self.dsn)
                self.assertEqual(self.set.covered_ver, self.covered)
                # the new mail stays with the old gate, row for row
                self.assertEqual(self.answers(full=True, use_set=True, actor=FULL), old)
                self.assertTrue(set(self.new_handles) <= set(self.reads))

    def test_the_watermark_waits_for_a_delta_in_flight(self):
        import psycopg
        with psycopg.connect(self.dsn) as delta, psycopg.connect(self.dsn) as pg:
            delta.execute(people_excluded.DELTA_LOCK_SQL)   # a delta mid-write
            pauses = []
            with mock.patch.object(people_excluded, "WATERMARK_LOCK_TIMEOUT", "200ms"), \
                    mock.patch.object(people_excluded, "_pause", pauses.append), \
                    self.assertRaises(psycopg.errors.LockNotAvailable):
                people_excluded.watermark(pg)
            # every try waited, with a pause between tries and none after the last
            self.assertEqual(len(pauses), people_excluded.WATERMARK_TRIES - 1)
            pg.rollback()
            delta.rollback()
            self.assertEqual(people_excluded.watermark(pg), self.covered + 10)

    def test_the_watermark_is_read_once_a_long_delta_ends(self):
        """A delta writing past one try's wait: the next try after the pause reads the
        watermark (the update is not failed on a lock timeout)."""
        import psycopg
        with psycopg.connect(self.dsn) as delta, psycopg.connect(self.dsn) as pg:
            delta.execute(people_excluded.DELTA_LOCK_SQL)   # a delta mid-write
            pauses = []

            def delta_ends(seconds):
                pauses.append(seconds)
                delta.rollback()

            with mock.patch.object(people_excluded, "WATERMARK_LOCK_TIMEOUT", "200ms"), \
                    mock.patch.object(people_excluded, "_pause", delta_ends):
                self.assertEqual(people_excluded.watermark(pg), self.covered + 10)
            self.assertEqual(pauses, [people_excluded.WATERMARK_PAUSE_SECONDS])


if __name__ == "__main__":
    unittest.main()
