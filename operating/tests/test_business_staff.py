"""The org chart (``biz.staff``) and the meetings tables (``meetings``,
``meeting_transcripts``) read through the one door (``plx.sql``).

Checked at the boundary:
- ``staff_rows`` at 1/10/60 reports per manager: no row lost or doubled, the count exact; a
  manager is named only when listed; one without a served Person (excluded, withheld) is
  never named.
- Who holds ``org:chart``: a staff reader only with a usable people exclusion; a guest's
  policy never (``reader_grants``).
- Postgres (the mail parity fixture, its finance owner B excluded): the excluded person is
  never a row nor anyone's manager or report; a reader with ``org:chart`` reads the rest, a
  guest nothing.
- Postgres (the meetings capture fixture plus 1/10/60 events on one calendar, an event and a
  transcript naming Freightcom): a calendar reader sees their calendar only, counts exact;
  Freightcom never reaches the AI; a transcript is read by a participant holding the unit
  or a full reader, never by the unit alone (a delegate) or an unset reader setting.

Evidence label: injected-transport (no network, made-up ids and addresses)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_policy as otp
from operating import ontology_tools_sql as ts
from operating import staff_scope
from operating.pg import business_staff as bs
from operating.pg import business_views as bv
from operating.pg import people_excluded, sql_views
from tests import test_ontology_tools_evidence as ev


def oid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def org_row(n: int, boss: int | None, state: str = "listed") -> dict:
    return {"object_id": oid(n), "upn": f"p{n}@example-plx.com", "display_name": f"P {n:03d}",
            "job_title": "Clerk", "department": "Ops", "office": "Mill",
            "manager_object_id": oid(boss) if boss is not None and state == "listed" else None,
            "manager_state": state}


def _cell(value):
    return value["untrusted_text"] if isinstance(value, dict) else value


class StaffRows(unittest.TestCase):
    def test_reports_at_1_10_60_exact_and_unlisted_managers_never_named(self):
        org, persons = {}, {}
        boss_of = {}
        n = 1000
        for boss, size in ((1, 1), (2, 10), (3, 60)):
            org[oid(boss)] = org_row(boss, None, "none")
            for _ in range(size):
                n += 1
                org[oid(n)] = org_row(n, boss)
                boss_of[n] = boss
        # 900: the excluded / withheld manager (in the org store, no served Person);
        # 901 reports to them; 902's manager was withheld by the capture
        org[oid(900)] = org_row(900, None, "none")
        org[oid(901)] = org_row(901, 900)
        org[oid(902)] = org_row(902, None, "withheld")
        for o in org:
            if o != oid(900):
                persons[o] = (f"Person:{o}", f"h://{o}", '["pii"]')   # a mail contact's
        rows = list(bs.staff_rows(org, persons))
        # the org chart's values carry no record marking of the Person's
        self.assertEqual({m for r in rows for m in r.marks}, {"[]"})
        names = [r.values["name"] for r in rows]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(rows), len(org) - 1)
        self.assertNotIn("P 900", names)
        by = {r.values["name"]: r for r in rows}
        for boss, size in ((1, 1), (2, 10), (3, 60)):
            r = by[f"P {boss:03d}"]
            self.assertEqual(r.values["reports_count"], size)
            self.assertEqual(len(r.values["reports"]), size)
            self.assertEqual(r.col_names["reports"], {f"Person:{oid(k)}" for k, b in
                                                      boss_of.items() if b == boss})
        for k, b in boss_of.items():
            self.assertEqual(by[f"P {k:03d}"].values["manager"], f"P {b:03d}")
        flat = json.dumps([r.values for r in rows])
        self.assertNotIn("P 900", flat)
        self.assertNotIn("p900@", flat)
        self.assertNotIn(oid(900).lower(), flat.lower())   # never the excluded one's id
        # the join key to their own mailbox, calendar and Teams sender id
        self.assertEqual(by["P 002"].values["person_id"], oid(2).lower())
        self.assertIsNone(by["P 901"].values["manager"])
        self.assertEqual(by["P 901"].values["manager_state"], "not_known")
        self.assertEqual(by["P 902"].values["manager_state"], "not_known")
        self.assertTrue(all(r.audience == {staff_scope.ORG_CHART_KEY} for r in rows))
        self.assertTrue(all(len(r.objects) == 1 for r in rows))

    def test_org_chart_key_only_with_a_usable_exclusion(self):
        own = oid(1)
        got = staff_scope.staff_grants([], {}, own=own, excluded=lambda ids: False)
        self.assertIs(got[staff_scope.ORG_CHART_KEY], True)
        got = staff_scope.staff_grants([], {}, own=own, excluded=None)
        self.assertIs(got[staff_scope.ORG_CHART_KEY], False)
        # the owner's alone key rides the same view-only list (2ed8b9108a)
        self.assertEqual(otp.VIEW_ONLY_KEYS, (staff_scope.ORG_CHART_KEY, otp.OWNER_KEY))
        guest = ot.ReadPolicy(tenant="plx", audiences={})
        reader = Actor("plx", frozenset({"guest:x"}), "restricted")
        keys, *_ = otp.reader_grants(object(), guest, reader, ("mail:x",))
        self.assertNotIn(staff_scope.ORG_CHART_KEY, keys)
        staff = ot.ReadPolicy(tenant="plx", audiences={"org:chart": {"staff:a"}})
        keys, *_ = otp.reader_grants(object(), staff, Actor(
            "plx", frozenset({"staff:a"}), "restricted"), ("mail:x",))
        self.assertIn(staff_scope.ORG_CHART_KEY, keys)
        self.assertNotIn(otp.OWNER_KEY, keys)   # a staff reader never holds the owner-only key
        self.assertNotIn("mail:x", keys)


class StaffMarkings(unittest.TestCase):
    def test_a_staff_reader_holds_quality_controlled_never_finance_or_pii(self):
        """Decision 2026-11-17 (only finance and pii restrict staff; no QC restriction).
        11-18 live: a staff reader held no marking, so every business view row carrying a
        receipt's QC link (4,341 of 11,662 purchase orders, PO100045 for Cos) was hidden.
        A staff reader now holds quality_controlled, and with it the marking sets of such a
        row; finance, pii, purchasing_prices and l5 stay with the host's grants; a guest
        (no employee) is decided as before."""
        from types import SimpleNamespace
        from operating import ontology_host as oh
        from operating.employee_permissions import EmployeeEntitlement, EmployeePrincipal
        own = oid(7)
        actor = Actor("plx", frozenset({"employee:" + own}), "confidential")
        entitlement = EmployeeEntitlement(
            principal=EmployeePrincipal(oid(9), own), actor=actor, denied_reason=None,
            source_identities=frozenset({own}))
        runtime = SimpleNamespace(
            employee_gate=SimpleNamespace(entitlement=lambda a: entitlement if a == actor
                                          else None),
            people_exclusion=SimpleNamespace(unavailable=False),
            staff_shared_mailboxes=lambda: frozenset())
        grants = {"finance": frozenset({"staff:finance"}),
                  "quality_controlled": frozenset({"staff:quality"})}
        policy = oh._staff_policy(runtime, actor, actor.principals, ["api:public.receipt"],
                                  None, None, grants, {})
        held = policy.held_markings(actor)
        self.assertIn("quality_controlled", held)
        self.assertFalse(held & {"finance", "pii", "purchasing_prices", "l5"}, held)
        keys, _held, sets, _g = otp.reader_grants(object(), policy, actor,
                                                  ("api:public.receipt",))
        self.assertIn('["quality_controlled"]', sets)
        self.assertNotIn('["finance"]', sets)
        self.assertIn("api:public.receipt", keys)
        # the host's own grant holders keep theirs
        self.assertEqual(policy.markings["quality_controlled"],
                         frozenset({"staff:quality", "employee:" + own}))
        guest = Actor("plx", frozenset({"guest:x"}), "restricted")
        self.assertIsNone(oh._staff_policy(runtime, guest, guest.principals, [], None, None,
                                           grants, {}))


def _tool(dsn, sqlite, compiled, policy):
    from operating.pg.ontology_reader import PgStoreReader
    from operating.pg.sqlite_compat import PgCompatConnection
    conn = PgCompatConnection(dsn, link_guard=True)
    reader = PgStoreReader(conn, ot.StoreReader(sqlite))
    base = ot.OntologyTools(reader, policy, item_gate=lambda a, h: True,
                            schema=ot.SchemaView.from_compiled(compiled), grammar=None,
                            contracts=ev.CONTRACTS, clock=lambda: ev.NOW)
    return conn, ts.SqlTool(base)


def _migrate(dsn):
    import psycopg
    from operating.pg.migrate import MIGRATIONS_DIR
    with psycopg.connect(dsn, autocommit=True) as pg:
        for name in ("0027_item_audiences.sql", "0038_own_issued_documents.sql", "0040_people_excluded_objects.sql",
                     "0041_business_views.sql"):
            pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))


class StaffThroughTheDoor(unittest.TestCase):
    """B (the finance mailbox's owner) excluded; A manages C and B; B manages A."""

    @classmethod
    def setUpClass(cls):
        import psycopg
        from integrations.capture_org import DB_FILE, ORG_KIND_NAME, STORE_DIR
        from operating.tests import test_table_mail_parity as tmp
        cls.tmp_mod = tmp
        tmp.TableMailParity.setUpClass.__func__(cls)
        cls.env = mock.patch.dict(os.environ, {"PLX_STATE_DIR": str(cls.state)})
        cls.env.start()
        a, b, c = tmp.BOX_A.lower(), tmp.BOX_B.lower(), tmp.BOX_C.lower()
        rows = [{"object_id": a, "upn": "orders@example-plx.com", "display_name": "Ann Orders",
                 "manager_object_id": b, "manager_state": "listed"},
                {"object_id": b, "upn": "finance@example-plx.com",
                 "display_name": "Bea Finance", "manager_object_id": None,
                 "manager_state": "none"},
                {"object_id": c, "upn": "sales@example-plx.com", "display_name": "Cal Sales",
                 "manager_object_id": a, "manager_state": "listed"}]
        (cls.state / STORE_DIR).mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(cls.state / STORE_DIR / DB_FILE) as db:
            # the columns ``capture_org.current_rows`` reads (the store's own layout is
            # tested in integrations/tests/test_capture_org.py)
            db.execute("CREATE TABLE versions (kind TEXT, superseded_at TEXT, "
                       "missing_from_source_since TEXT, payload TEXT)")
            db.executemany("INSERT INTO versions VALUES (?, NULL, NULL, ?)",
                           [(ORG_KIND_NAME, json.dumps({"row": r})) for r in rows])
        # the fixture serves one staff Person (B's): A's and C's are made as copies of it
        # in the served store (every text column with B's id read with theirs; a new
        # version number), before the people exclusion's set is built over them
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for table in ("objects", "object_versions"):
                cols = pg.execute(
                    "SELECT column_name, data_type, column_default IS NOT NULL OR "
                    "is_identity = 'YES' FROM information_schema.columns WHERE "
                    "table_schema = 'ontology' AND table_name = %s ORDER BY ordinal_position",
                    (table,)).fetchall()
                for new in (a, c):
                    keep = [(n, t) for n, t, _ in cols]
                    names = ", ".join(f'"{n}"' for n, _ in keep)
                    picks = ", ".join(
                        f'replace("{n}", %(b)s, %(n)s)' if t == "text" else
                        "(SELECT max(ver) + 1 FROM ontology.object_versions)" if n == "ver"
                        else f'"{n}"' for n, t in keep)
                    pg.execute(f"INSERT INTO ontology.{table} ({names}) SELECT {picks} FROM "
                               f"ontology.{table} WHERE object_id = %(id)s",
                               {"b": b, "n": new, "id": bs.person_id(b)})
            persons = {r[0] for r in pg.execute(
                "SELECT object_id FROM ontology.object_versions WHERE object_id LIKE "
                "'Person:%%' AND superseded_at IS NULL")}
        assert {bs.person_id(o) for o in (a, b, c)} <= persons, sorted(persons)[:5]
        _migrate(cls.dsn)
        with psycopg.connect(cls.dsn) as pg:
            people_excluded.build(pg, cls.state, environ=tmp.EXCLUDED_B, workers=2)
        bv.build(cls.dsn, ["staff"], schema=cls.compiled)
        sql_views.install(cls.dsn)
        # as served: a staff reader holds the org chart key and no pii marking (only
        # STAFF_MARKINGS); the guest holds pii and not the key
        cls.policy = ot.ReadPolicy(tenant="plx", audiences={"org:chart": {"staff:orders"}},
                                   markings={"pii": {"guest:visitor"},
                                             "quality_controlled": {"staff:orders"}})

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp_mod.TableMailParity.tearDownClass.__func__(cls)

    def rows(self, actor):
        conn, tool = _tool(self.dsn, self.sqlite, self.compiled, self.policy)
        self.addCleanup(conn.close)
        out = tool.call("plx.sql", {"sql": "SELECT name, manager, reports, email FROM staff "
                                           "ORDER BY name"}, actor)
        self.assertNotIn("error", out, out)
        return [[_cell(c) for c in r["cells"]] for r in out["rows"]]

    def test_excluded_person_is_never_a_row_manager_or_report(self):
        got = self.rows(self.tmp_mod.NARROW)
        self.assertEqual([r[0] for r in got], ["Ann Orders", "Cal Sales"])
        ann, cal = got
        self.assertIsNone(ann[1])   # her manager is the excluded person: never named
        self.assertEqual(cal[1], "Ann Orders")
        self.assertIn("Cal Sales", json.dumps(ann[2]))
        flat = json.dumps(got)
        self.assertNotIn("Bea", flat)
        self.assertNotIn("finance@", flat)

    def test_a_guest_reads_no_row(self):
        # holds the marking, not the org chart key: nothing
        guest = Actor("plx", frozenset({"guest:visitor"}), "restricted")
        self.assertEqual(self.rows(guest), [])

    def test_a_staff_reader_without_pii_reads_the_org_chart(self):
        # the staff Person versions are pii-marked mail contacts; the org chart's own
        # values carry no marking, so a reader holding no marking at all still reads it
        policy = self.policy
        self.policy = ot.ReadPolicy(tenant="plx", audiences={"org:chart": {"staff:orders"}})
        try:
            self.assertEqual([r[0] for r in self.rows(self.tmp_mod.NARROW)],
                             ["Ann Orders", "Cal Sales"])
        finally:
            self.policy = policy


class MeetingsThroughTheDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from contextlib import ExitStack
        from integrations import capture_meetings as cm
        from integrations.tests import test_capture_meetings as tcm
        from operating import host
        from operating.tests import pg_store_mode
        cls.tcm = tcm
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmpdir.name)
        graph = tcm.FakeGraph()
        past = tcm.NOW - timedelta(days=2)
        cls.sizes = (1, 10, 60)
        for size in cls.sizes:
            for i in range(size):
                graph.event(tcm.B_ID, f"Bulk{size}x{i}", organizer=(tcm.B, "Bob Test"),
                            attendees=[(tcm.A, "Ann Test")], start=past,
                            subject=f"Bulk {size} {i}", organizer_flag=True)
        graph.event(tcm.A_ID, "EvFC", organizer=(tcm.A, "Ann Test"),
                    attendees=[(tcm.B, "Bob Test")], start=past, organizer_flag=True,
                    subject="Freightcom pickup")
        graph.event(tcm.A_ID, "EvA5", organizer=(tcm.A, "Ann Test"),
                    attendees=[(tcm.B, "Bob Test")], start=past, online=5,
                    organizer_flag=True, subject="Carrier sync")
        graph.meeting(5, "MeetingFive", ["TranscriptFive"])
        graph.vtt["TranscriptFive"] = tcm.VTT.replace("Vendor Acme", "Freightcom")
        store = cm.open_store(cls.state, tcm.audience())
        api, _ = cm.build_api(tcm.ENV, sender=graph)
        report = cm.MeetingsCapture(store, api, tcm.audience(), tcm.CAPTURE,
                                    transcripts_granted=True, clock=lambda: tcm.NOW,
                                    ).run(tcm.scope(page_size=100))
        store.close()
        assert report["status"] == "complete", report["reasons"]
        with ExitStack() as stack:
            sources = host.state_captures(cls.state, stack)
            host.build_projection(cls.state, sources)
        cls.sqlite = sqlite3.connect(host.projection_path(cls.state))
        cls.dsn = pg_store_mode._load(str(host.projection_path(cls.state)), cls.sqlite)
        from ontology import schema as schema_loader
        cls.compiled = schema_loader.load_schema()
        cls.env = mock.patch.dict(os.environ, {"PLX_STATE_DIR": str(cls.state)})
        cls.env.start()
        _migrate(cls.dsn)
        cls.tag = people_excluded.current_exclusion(tcm.ENV)
        assert cls.tag
        # the set's build needs a mail capture this fixture has not: its state row says the
        # list decided every served version and names no object (the meetings capture never
        # stores an excluded person's event or transcript, ``test_capture_meetings``)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            pg.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, "
                       "objects, handles, covered_ver) SELECT %s, '2026-11-18T00:00:00Z', 0, "
                       "0, max(ver) FROM ontology.object_versions", (cls.tag,))
        bv.build(cls.dsn, ["meetings", "meeting_transcripts"], schema=cls.compiled)
        sql_views.install(cls.dsn)
        a, b = tcm.A_ID, tcm.B_ID
        cls.ann = Actor("plx", frozenset({"staff:ann"}), "restricted")
        cls.bob = Actor("plx", frozenset({"staff:bob"}), "restricted")
        cls.delegate = Actor("plx", frozenset({"staff:delegate"}), "restricted")
        cls.owner = Actor("plx", frozenset({"staff:owner"}), "restricted")
        cls.policy = ot.ReadPolicy(tenant="plx", audiences={
            f"meetings:calendar:{a}": {"staff:ann", "staff:owner"},
            f"meetings:calendar:{b}": {"staff:bob", "staff:owner"},
            f"meetings:organizer:{a}": {"staff:ann", "staff:delegate", "staff:owner"},
            f"meetings:organizer:{b}": {"staff:bob", "staff:owner"}})
        cls.self_of = {
            cls.ann: {"full": False, "ids": [a, tcm.A]},
            cls.bob: {"full": False, "ids": [b, tcm.B]},
            cls.delegate: {"full": False, "ids": ["delegate@example-plx.com"]},
            cls.owner: {"full": True, "ids": []}}

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.sqlite.close()
        cls.tmpdir.cleanup()

    def sql(self, actor, sql, me=None):
        conn, tool = _tool(self.dsn, self.sqlite, self.compiled, self.policy)
        self.addCleanup(conn.close)
        with mock.patch.object(ts, "_people_tag", return_value=self.tag), \
                mock.patch.object(ts, "_transcript_reader",
                                  return_value=self.self_of[actor] if me is None else me):
            out = tool.call("plx.sql", {"sql": sql}, actor)
        self.assertNotIn("error", out, out)
        self.assertFalse(out.get("truncated"))
        return [[_cell(c) for c in r["cells"]] for r in out["rows"]]

    def test_each_calendar_reader_sees_their_calendar_counts_exact(self):
        tcm = self.tcm
        bob = self.sql(self.bob, "SELECT subject, mailbox FROM meetings")
        self.assertEqual({r[1] for r in bob}, {tcm.B_ID.lower()})
        counts: dict[str, int] = {}
        for subject, _ in bob:
            if subject and subject.startswith("Bulk "):
                counts[subject.split()[1]] = counts.get(subject.split()[1], 0) + 1
        self.assertEqual(counts, {"1": 1, "10": 10, "60": 60})
        ann = self.sql(self.ann, "SELECT subject, mailbox FROM meetings")
        self.assertEqual({r[1] for r in ann}, {tcm.A_ID.lower()})
        self.assertFalse(any((r[0] or "").startswith("Bulk ") for r in ann))
        guest = Actor("plx", frozenset({"guest:x"}), "restricted")
        self.assertEqual(self.sql(guest, "SELECT subject FROM meetings",
                                  me={"full": False, "ids": []}), [])

    def test_the_excluded_never_reach_the_ai_and_freightcom_is_read_like_any(self):
        rows = self.sql(self.owner, "SELECT subject, organizer, attendees FROM meetings")
        flat = json.dumps(rows)
        self.assertIn("Freightcom", flat)
        self.assertNotIn(self.tcm.X, flat)
        self.assertIn("Carrier sync", flat)
        subjects = [r[0] for r in self.sql(self.owner, "SELECT subject FROM "
                                                       "meeting_transcripts")]
        self.assertEqual(sorted(subjects), ["Carrier sync", "Weekly PO review"])   # Five names Freightcom: read like any

    def test_transcripts_by_participant_or_full_reader_never_the_unit_alone(self):
        q = "SELECT subject FROM meeting_transcripts"
        # ann organised both (Five names Freightcom: read like any other since 11-23)
        self.assertEqual(sorted(r[0] for r in self.sql(self.ann, q)),
                         ["Carrier sync", "Weekly PO review"])
        self.assertEqual(self.sql(self.delegate, q), [])     # the unit, no participant
        self.assertEqual(self.sql(self.bob, q), [])          # a participant, not the unit
        for me in ({"full": False, "ids": []}, {"ids": "x"}, {}):
            with self.subTest(me=me):
                self.assertEqual(self.sql(self.ann, q, me=me), [])


if __name__ == "__main__":
    unittest.main()
