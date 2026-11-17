"""The item gate answered from recorded item audiences on Postgres (AUD-01, migration 0027,
``operating.pg.item_audiences``): for every item-ACL scheme, the SQL gate reads exactly the
handles the Python gate (``OntologyTools._item_allowed`` over ``resolve_citation``) allows,
for an allowed and a denied reader, an excluded person (as item and as reader), a handle
with no recorded decision, a changed permission list (in place, or invalidated), an expired
or unkept decision; a recorded handle is answered without calling the Python gate; and only
the recordable schemes are ever recorded (the backfill, ``decide`` and ``write`` refuse the
rest, and a record without an end is never honoured)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest

from operating.tests import pg_testdb

MIGRATIONS = [Path(__file__).resolve().parents[1] / "pg" / "migrations" / name
              for name in ("0006_ontology.sql", "0007_ontology_compact.sql",
                           "0008_ontology_read.sql", "0009_schema_migrations.sql",
                           "0010_ontology_words.sql", "0011_ontology_word_order.sql",
                           "0027_item_audiences.sql")]
SCHEMES = ("graph-mail", "sharepoint", "sharepoint-list", "original", "teams-channel",
           "teams-chat", "calendar-event", "planner-task", "meeting-transcript")
TENANT = "t-test"
ANA = "00000000-0000-4000-8000-0000000000a1"
BO = "00000000-0000-4000-8000-0000000000b2"
GONE = "00000000-0000-4000-8000-0000000000ee"     # the excluded person
DATED = ("sharepoint", "sharepoint-list", "teams-channel", "teams-chat", "planner-task",
         "meeting-transcript")   # a capture-age bound
RECORDED = "sharepoint-list"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(hours_ago: float) -> str:
    return (_now() - timedelta(hours=hours_ago)).isoformat()


class FakeRuntime:
    """``resolve_citation`` with each scheme's kinds of decision: the item's permission
    list against the reader's identities, the people exclusion over the item's people, the
    capture-age bound on a dated list, and a live re-read for an original."""

    def __init__(self) -> None:
        from integrations.people_exclusion import PeopleExclusion
        self.people_exclusion = PeopleExclusion([GONE])
        self.items: dict[str, dict] = {}
        self.calls = 0
        runtime = self

        class Gate:
            def entitlement(self, actor):
                oid = next(iter(actor.principals)).partition(":")[2]
                ids = frozenset({oid})
                return SimpleNamespace(source_identities=ids, actor=actor,
                                       fingerprint=hashlib.sha256(oid.encode()).hexdigest())
        self.employee_gate = Gate()
        del runtime

    def resolve_citation(self, handle, actor):
        from integrations.live_source_guard import check_request
        from operating.capture_age import capture_decides
        self.calls += 1
        item = self.items[handle]
        if self.people_exclusion.excluded(item.get("people", ())):
            raise LookupError("withheld")
        if item.get("stamp") is not None and not capture_decides(item["stamp"], _now()):
            raise LookupError("capture_too_old")
        if item.get("live"):
            check_request("GET", "https://graph.microsoft.com/v1.0/drives/x/items/y")
        ids = self.employee_gate.entitlement(actor).source_identities
        if not ids & set(item["acl"]):
            raise LookupError("employee_acl_denied")
        return {"citation": handle}


class Base:
    """The tools' item gate seam: ``_item_allowed`` is ``OntologyTools``'s own."""

    def __init__(self, gate) -> None:
        from operating.ontology_tools import OntologyTools
        self.item_gate = gate
        self.item_gate_scope = ("test-host",)
        self._allowed = OntologyTools._item_allowed

    def _item_allowed(self, actor, handle):
        return self._allowed(self, actor, handle)


def _handles() -> dict[str, dict]:
    items = {}
    for scheme in SCHEMES:
        stamp = _stamp(1) if scheme in DATED else None
        items[f"{scheme}://u/ana-only"] = {"acl": [ANA], "stamp": stamp}
        items[f"{scheme}://u/both"] = {"acl": [ANA, BO], "stamp": stamp}
        items[f"{scheme}://u/excluded-person"] = {"acl": [ANA, BO], "people": [GONE],
                                                  "stamp": stamp}
        items[f"{scheme}://u/unrecorded"] = {"acl": [BO], "stamp": stamp}
    items["sharepoint-list://u/stale"] = {"acl": [ANA], "stamp": _stamp(60)}
    items["original://u/live"] = {"acl": [ANA], "live": True}
    return items


class ItemAudienceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        cls.items = _handles()
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
            with pg.cursor().copy("COPY ontology.object_versions (ver, object_id, l1_handle, "
                                  "digest, lineage, markings, recorded_at) FROM STDIN") as copy:
                for n, handle in enumerate(sorted(cls.items), 1):
                    copy.write_row((n, f"O:{n}", handle, "d", "portal", "[]",
                                    "2026-01-01T00:00:00Z"))

    def setUp(self):
        import psycopg
        from ontology.types import Actor
        from operating.ontology_tools_results import item_gate_from_case_runtime
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("TRUNCATE ontology.item_audiences, ontology.item_acl_state, "
                       "ontology.item_audience_rosters")
        self.runtime = FakeRuntime()
        self.runtime.items = {h: dict(v) for h, v in self.items.items()}
        self.base = Base(item_gate_from_case_runtime(self.runtime))
        self.ana = Actor(TENANT, frozenset({f"employee:{ANA}"}))
        self.bo = Actor(TENANT, frozenset({f"employee:{BO}"}))
        self.gone = Actor(TENANT, frozenset({f"employee:{GONE}"}))
        self.pg = None

    def tearDown(self):
        if self.pg is not None:
            self.pg.close()

    def record(self, handles=None, readers=None):
        """The producer over ``handles`` (default: all but the unrecorded) for the roster."""
        import psycopg
        from operating.pg import item_audiences as ia
        readers = readers or (self.ana, self.bo)
        pairs = [(ia.reader_key(self.base, a), (lambda h, a=a: self.base._item_allowed(a, h)))
                 for a in readers]
        self.assertTrue(all(k for k, _ in pairs))
        keys = [k for k, _ in pairs]
        roster = ia.roster_id(keys)
        handles = handles if handles is not None else [
            h for h in self.runtime.items if not h.endswith("/unrecorded")]
        decisions = [ia.decide(h, pairs, roster) for h in handles]
        with psycopg.connect(self.dsn, autocommit=True) as out:
            ia.write(out, keys, decisions)
        return {d.handle: d for d in decisions}

    def read(self, actor):
        """(handles the SQL gate reads for ``actor``, handles the Python gate was asked)."""
        from operating.ontology_tools_traverse import TraverseTools
        from operating.pg.sqlite_compat import PgCompatConnection
        if self.pg is None:
            self.pg = PgCompatConnection(self.dsn)
        check = TraverseTools._gate_function(SimpleNamespace(base=self.base), actor)
        asked = []
        inner = check

        def counted(handle):
            asked.append(handle)
            return inner(handle)
        counted.item_keys = getattr(check, "item_keys", None)
        self.pg.create_function("plx_gate_ia", 1, counted)
        try:
            rows = self.pg.execute("SELECT v.l1_handle FROM object_versions v "
                                   "WHERE plx_gate_ia(v.l1_handle)").fetchall()
        finally:
            self.pg.create_function("plx_gate_ia", 1, None)
        return {r[0] for r in rows}, set(asked)

    def gate(self, actor):
        return {h for h in self.runtime.items if self.base._item_allowed(actor, h)}

    def test_every_scheme_reads_as_the_gate_and_recorded_handles_skip_it(self):
        decisions = self.record()
        for scheme in SCHEMES:
            for suffix in ("ana-only", "both", "excluded-person"):
                with self.subTest(scheme=scheme, item=suffix):
                    # an excluded person's item is denied before any capture-age check:
                    # a decision with no end is never recorded (the gate decides it)
                    self.assertEqual(decisions[f"{scheme}://u/{suffix}"].kept,
                                     scheme == RECORDED and suffix != "excluded-person")
        for actor, name in ((self.ana, "ana"), (self.bo, "bo")):
            with self.subTest(reader=name):
                read, asked = self.read(actor)
                self.assertEqual(read, self.gate(actor))
                recorded = {h for h, d in decisions.items() if d.kept}
                self.assertEqual(len(recorded), 2)
                # a recorded handle is answered from its rows, never by the Python gate
                self.assertFalse(asked & recorded, sorted(asked & recorded))
                # every other handle is asked, as before
                self.assertEqual(asked, set(self.runtime.items) - recorded)
                # a handle with no recorded decision is asked, as before
                self.assertTrue({h for h in self.runtime.items
                                 if h.endswith("/unrecorded")} <= asked)
        ana, _ = self.read(self.ana)
        bo, _ = self.read(self.bo)
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme):
                self.assertIn(f"{scheme}://u/ana-only", ana)       # allowed
                self.assertNotIn(f"{scheme}://u/ana-only", bo)     # denied
                self.assertIn(f"{scheme}://u/both", bo)
                self.assertNotIn(f"{scheme}://u/excluded-person", ana | bo)
                self.assertIn(f"{scheme}://u/unrecorded", bo)      # fallback allows
                self.assertNotIn(f"{scheme}://u/unrecorded", ana)  # fallback denies

    def test_an_excluded_reader_holds_no_key_and_the_gate_decides(self):
        from operating.pg import item_audiences as ia
        self.record()
        self.assertIsNone(ia.reader_key(self.base, self.gone))
        read, asked = self.read(self.gone)
        self.assertEqual(read, set())
        self.assertEqual(read, self.gate(self.gone))
        self.assertTrue(asked)          # every gated handle went to the gate
        # no key of an excluded person is ever written
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            keys = {r[0] for r in pg.execute(
                "SELECT audience_key FROM ontology.item_audience_rosters")}
        self.assertEqual(keys, {ia.reader_key(self.base, self.ana),
                                ia.reader_key(self.base, self.bo)})

    def test_a_reader_outside_the_roster_falls_back_for_every_handle(self):
        from ontology.types import Actor
        self.record(readers=(self.ana,))
        read, asked = self.read(self.bo)
        self.assertEqual(read, self.gate(self.bo))
        self.assertEqual(asked, set(self.runtime.items))

    def test_a_changed_permission_list_is_decided_again_once_invalidated(self):
        import psycopg
        from operating.pg import item_audiences as ia
        self.record()
        for scheme in SCHEMES:
            self.runtime.items[f"{scheme}://u/ana-only"]["acl"] = [BO]   # revoked from Ana
        changed = [f"{scheme}://u/ana-only" for scheme in SCHEMES]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            self.assertEqual(ia.invalidate(pg, changed), 1)   # the one recorded
        for actor in (self.ana, self.bo):
            read, asked = self.read(actor)
            self.assertEqual(read, self.gate(actor))
            self.assertTrue(set(changed) <= asked)
        self.record(handles=changed)    # recorded again: the rows follow the new list
        for actor in (self.ana, self.bo):
            read, asked = self.read(actor)
            self.assertEqual(read, self.gate(actor))
            self.assertNotIn(f"{RECORDED}://u/ana-only", asked)

    def test_a_projected_version_change_drops_the_whole_objects_decisions(self):
        """The delta's hook (``invalidate_staged``) inside its own staging tables."""
        import psycopg
        from operating.pg import item_audiences as ia
        self.record()
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            ver, object_id, handle = cur.execute(
                "SELECT ver, object_id, l1_handle FROM ontology.object_versions WHERE "
                "l1_handle = 'sharepoint-list://u/both'").fetchone()
            cur.execute("CREATE TEMP TABLE diff_lv (lver bigint, object_id text, "
                        "l1_handle text, superseded_at text)")
            cur.execute("CREATE TEMP TABLE diff_pv (ver bigint, object_id text, "
                        "l1_handle text)")
            cur.execute("CREATE TEMP TABLE diff_vmatch (lver bigint, ver bigint, "
                        "superseded_at text)")
            cur.execute("INSERT INTO diff_lv VALUES (1, %s, %s, NULL), "
                        "(2, %s, 'sharepoint-list://u/both-v2', NULL)", (object_id, handle, object_id))
            cur.execute("INSERT INTO diff_pv VALUES (%s, %s, %s)", (ver, object_id, handle))
            cur.execute("INSERT INTO diff_vmatch VALUES (1, %s, NULL)", (ver,))
            self.assertEqual(ia.invalidate_staged(cur), 1)
            pg.commit()
        read, asked = self.read(self.bo)
        self.assertEqual(read, self.gate(self.bo))
        self.assertIn(handle, asked)

    def test_a_stale_or_live_decision_is_never_recorded(self):
        decisions = self.record()
        self.assertFalse(decisions["sharepoint-list://u/stale"].kept)
        self.assertFalse(decisions["original://u/live"].kept)
        # recorded decisions of a dated list expire with its stamp's capture-age bound
        dated = decisions["sharepoint-list://u/ana-only"]
        self.assertIsNotNone(dated.valid_until)
        self.assertLess(abs((dated.valid_until - (_now() + timedelta(hours=47)))
                            .total_seconds()), 120)
        self.assertIsNone(decisions["graph-mail://u/ana-only"].valid_until)
        read, asked = self.read(self.ana)
        self.assertEqual(read, self.gate(self.ana))
        self.assertTrue({"sharepoint-list://u/stale", "original://u/live"} <= asked)

    def test_an_expired_decision_falls_back_to_the_gate(self):
        import psycopg
        self.record()
        handle = "sharepoint-list://u/ana-only"
        self.runtime.items[handle]["acl"] = [BO]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("UPDATE ontology.item_acl_state SET valid_until = now() - interval "
                       "'1 second' WHERE l1_handle = %s", (handle,))
        read, asked = self.read(self.ana)
        self.assertNotIn(handle, read)
        self.assertIn(handle, asked)

    def test_an_unchanged_decision_is_not_rewritten(self):
        import psycopg
        from operating.pg import item_audiences as ia
        self.record()
        pairs = [(ia.reader_key(self.base, a), (lambda h, a=a: self.base._item_allowed(a, h)))
                 for a in (self.ana, self.bo)]
        keys = [k for k, _ in pairs]
        handle = "sharepoint-list://u/both"
        with psycopg.connect(self.dsn, autocommit=True) as out:
            report = ia.write(out, keys, [ia.decide(handle, pairs, ia.roster_id(keys))])
        self.assertEqual((report["unchanged"], report["kept"]), (1, 0))

    def test_an_in_place_permission_rewrite_never_leaves_an_allow_behind(self):
        """The review's reproduction: a mailbox's owner list (or a drive item's list, a
        unit membership) rewritten in place, with no new version and no invalidation. Those
        schemes are never recorded, so the SQL gate follows the gate at once."""
        self.record()
        for scheme in SCHEMES:
            if scheme != RECORDED:
                self.runtime.items[f"{scheme}://u/ana-only"]["acl"] = [BO]
        for actor in (self.ana, self.bo):
            read, _asked = self.read(actor)
            self.assertEqual(read, self.gate(actor))
        ana, _ = self.read(self.ana)
        self.assertNotIn("graph-mail://u/ana-only", ana)

    def test_only_recordable_schemes_are_recorded_and_the_backfill_refuses_the_rest(self):
        import psycopg
        from operating.pg import item_audiences as ia
        self.assertEqual(ia.RECORDABLE_SCHEMES, frozenset({RECORDED}))
        self.assertEqual(set(ia.REFUSED_SCHEMES) | ia.RECORDABLE_SCHEMES, set(SCHEMES))
        for scheme in sorted(ia.REFUSED_SCHEMES) + ["api", "unknown"]:
            with self.subTest(scheme=scheme):
                with self.assertRaisesRegex(ValueError, "scheme_not_recordable"):
                    ia.backfill("postgresql:///nowhere", "/nonexistent", "/nonexistent",
                                "/nonexistent", scheme, apply=True)
                with self.assertRaises(SystemExit):
                    ia.main(["backfill", "--state-dir", "x", "--config", "x", "--readers",
                             "x", "--scheme", scheme, "--apply"])
        # a forged kept decision of a refused scheme, or one with no end, only drops
        key = ia.reader_key(self.base, self.ana)
        forged = [ia.Decision("graph-mail://u/both", (key,), True, None,
                              _now() + timedelta(hours=1), "x"),
                  ia.Decision(f"{RECORDED}://u/both", (key,), True, None, None, "y")]
        with psycopg.connect(self.dsn, autocommit=True) as out:
            report = ia.write(out, [key], forged)
            self.assertEqual((report["kept"], report["dropped"]), (0, 2))
            self.assertEqual(out.execute("SELECT count(*) FROM ontology.item_acl_state")
                             .fetchone()[0], 0)
            # a state row without an end, written by hand, is not honoured either
            handle = f"{RECORDED}://u/unrecorded"   # Ana is not on its list
            out.execute("INSERT INTO ontology.item_acl_state (l1_handle, acl_digest, roster) "
                        "VALUES (%s, 'z', %s)", (handle, ia.roster_id([key])))
            out.execute("INSERT INTO ontology.item_audiences VALUES (%s, %s)", (handle, key))
        read, asked = self.read(self.ana)
        self.assertNotIn(handle, read)
        self.assertIn(handle, asked)


if __name__ == "__main__":
    unittest.main()
