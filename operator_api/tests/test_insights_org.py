"""The built-in org tiles (``insights_builtin.json``: leader scorecard, workload balance, who
colleagues are waiting on, approval speed): each statement run on Postgres over a small
directory and mailbox whose every figure is worked out by hand below, and each chart drawn
from its own result. isolated-fixture."""
from __future__ import annotations

import json
import unittest

from operator_api import charts
from operator_api import insights as ins
from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

S = "org_fixture"
# name, department, email, person id, manager's email, reports
STAFF = [("Lee", "Ops", "lee@plx.example", "pl", None, 1),
         ("Mia", "Ops", "mia@plx.example", "pm", "lee@plx.example", 1),
         ("Ned", "Ops", "ned@plx.example", "pn", "mia@plx.example", 0),
         ("Oz", "Sales", "oz@plx.example", "po", None, 0),
         ("Pat", "", "pat@plx.example", "pp", None, 0)]
OZ, LEE, MIA, NED, OUT = ("oz@plx.example", "lee@plx.example", "mia@plx.example",
                          "ned@plx.example", "x@out.example")
# id, mailbox, sender, days ago, role, bulk, hours to the owner's reply, thread
MAIL = [("i1", "pm", OZ, 2, "to", False, 2, "t1"),
        ("i2", "pm", OUT, 5, "to", False, 4, "t2"),
        ("i3", "pm", OZ, 3.5, "to", False, None, "t3"),         # a colleague waiting
        ("i4", "pm", "news@out.example", 2, "to", True, None, "t6"),   # bulk
        ("i5", "pm", OZ, 0.5, "to", False, None, "t7"),         # less than a day old
        ("i6", "pn", LEE, 2, "to", False, 10, "t4"),
        ("i7", "pl", MIA, 2, "to", False, 1, "t5"),
        ("s1", "pm", MIA, 1, "from", False, None, "t9"),
        ("s1", "pl", MIA, 1, "cc", False, None, "t9"),          # a second copy: one email
        ("s2", "pm", MIA, 4, "from", False, None, "t3"),        # Mia wrote in t3
        ("i8", "pm", NED, 6, "to", False, None, "t8"),          # a colleague waiting
        ("s3", "pm", MIA, 7, "from", False, None, "t8"),
        ("i9", "pm", OZ, 2, "to", False, None, "t10"),          # Mia never wrote in t10
        ("i10", "pm", OUT, 2, "to", False, None, "t11"),        # from outside: not a colleague
        ("s4", "pm", MIA, 3, "from", False, None, "t11"),
        ("old", "pm", OZ, 40, "to", False, None, "t12")]        # outside every window


def spec(sid: str) -> ins.Spec:
    return next(s for s in ins.builtin() if s.id == sid)


@unittest.skipIf(psycopg is None, "psycopg not installed")
class OrgTiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.dsn = pg_testdb.create_db()
        with psycopg.connect(cls.dsn) as pg:
            pg.execute(f"CREATE SCHEMA {S}")
            pg.execute(f"CREATE TABLE {S}.staff (name text, department text, email text, "
                       "person_id text, manager_email text, reports_count integer)")
            pg.execute(f"CREATE TABLE {S}.emails (message_id text, mailbox text, sender text, "
                       "received_at timestamptz, owner_role text, is_bulk boolean, "
                       "owner_replied_at timestamptz, thread text)")
            pg.execute(f"CREATE TABLE {S}.teams_messages (sender text, created_at timestamptz)")
            pg.execute(f"CREATE TABLE {S}.records (type text, fields jsonb)")
            for row in STAFF:
                pg.execute(f"INSERT INTO {S}.staff VALUES (%s, %s, %s, %s, %s, %s)", row)
            for mid, box, sender, days, role, bulk, hours, thread in MAIL:
                pg.execute(f"INSERT INTO {S}.emails VALUES (%s, %s, %s, now() - make_interval("
                           "secs => %s), %s, %s, now() - make_interval(secs => %s) + "
                           "make_interval(secs => %s), %s)",
                           (mid, box, sender, days * 86400, role, bulk, days * 86400,
                            (hours or 0) * 3600, thread))
                if hours is None:
                    pg.execute(f"UPDATE {S}.emails SET owner_replied_at = NULL WHERE "
                               "message_id = %s AND mailbox = %s", (mid, box))
            for _ in range(3):   # Ned's Teams messages; one from outside the directory
                pg.execute(f"INSERT INTO {S}.teams_messages VALUES (%s, now() - interval "
                           "'2 days')", (json.dumps({"id": "pn"}),))
            pg.execute(f"INSERT INTO {S}.teams_messages VALUES ('Guest', now())")
            # approvals: Lee 10 and 90 minutes (one rejected), Mia 5 minutes; one pending
            # and one decided 40 days ago are not counted
            for who, minutes, status, days in (("LEE@plx.example", 10, "approved", 1),
                                               ("lee@plx.example", 90, "rejected", 2),
                                               ("mia@plx.example", 5, "approved", 3),
                                               ("mia@plx.example", 5, "approved", 40)):
                pg.execute(f"INSERT INTO {S}.records SELECT 'MCApproval', jsonb_build_object("
                           "'decided_by', %s::text, 'status', %s::text, 'requested_at', "
                           "to_char((now() - make_interval(days => %s)) AT TIME ZONE 'UTC', "
                           "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), 'decided_at', to_char((now() - "
                           "make_interval(days => %s) + make_interval(mins => %s)) AT TIME ZONE "
                           "'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'))",
                           (who, status, days, days, minutes))
            pg.execute(f"INSERT INTO {S}.records VALUES ('MCApproval', '{{\"status\": "
                       "\"pending\", \"requested_at\": \"2026-01-01T00:00:00Z\"}')")

    @classmethod
    def tearDownClass(cls):
        pg_testdb.drop_db(cls.db)

    def run_tile(self, sid: str) -> tuple[list[str], list[list]]:
        s = spec(sid)
        with psycopg.connect(self.dsn) as pg:
            pg.execute(f"SET search_path = {S}")
            cur = pg.execute(s.sql)
            cols = [d.name for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        # every tile draws its own chart from what it returned
        drawn = charts.build(s.chart, cols, [[None if v is None else str(v) for v in r]
                                             for r in rows], title=s.title)
        self.assertEqual(drawn["kind"], s.chart["kind"])
        return cols, rows

    def table(self, sid: str, key: str) -> dict:
        cols, rows = self.run_tile(sid)
        return {r[cols.index(key)]: {c: (None if v is None else float(v)) for c, v in zip(cols, r)
                                     if c not in (key, "department", "person_id")}
                for r in rows}

    def test_leader_scorecard_counts_the_whole_team_under_each_manager(self):
        got = self.table("leader_scorecard", "leader")
        self.assertEqual(got, {
            # team Mia + Ned (two levels); sent: Mia 5 emails, Ned 1 email + 3 Teams;
            # mail To them 1..28 days, not bulk: Mia 6 (2 answered, 2 h and 4 h), Ned 1 (10 h)
            "Lee": {"direct_reports": 1, "team_size": 2, "team_active": 2, "team_sent": 9,
                    "busiest_member_share_pct": 56, "team_messages_to_them": 7,
                    "team_replied_pct": 43, "team_median_reply_hours": 4.0,
                    "leader_replied_pct": 100, "leader_median_reply_hours": 1.0},
            "Mia": {"direct_reports": 1, "team_size": 1, "team_active": 1, "team_sent": 4,
                    "busiest_member_share_pct": 100, "team_messages_to_them": 1,
                    "team_replied_pct": 100, "team_median_reply_hours": 10.0,
                    "leader_replied_pct": 33, "leader_median_reply_hours": 3.0}})

    def test_workload_balance_shows_how_few_people_send_half(self):
        got = self.table("workload_balance", "department")
        self.assertEqual(got, {
            "Ops": {"people": 3, "active": 3, "sent": 10, "median_per_active_person": 4.0,
                    "busiest_person": 5, "busiest_share_pct": 50, "people_sending_half": 1},
            "Sales": {"people": 1, "active": 1, "sent": 4, "median_per_active_person": 4.0,
                      "busiest_person": 4, "busiest_share_pct": 100, "people_sending_half": 1},
            "(none)": {"people": 1, "active": 0, "sent": 0, "median_per_active_person": None,
                       "busiest_person": 0, "busiest_share_pct": None,
                       "people_sending_half": None}})

    def test_colleagues_waiting_counts_only_colleagues_in_threads_the_person_is_in(self):
        got = self.table("colleagues_waiting", "person")
        # t3 (Oz) and t8 (Ned); not t10 (Mia never wrote), t11 (outside), t7 (too new),
        # t6 (bulk); her answers to colleagues: i1 only (2 h)
        self.assertEqual(got, {"Mia": {"colleagues_waiting": 2, "threads_waiting": 2,
                                       "waiting_over_3_days": 2, "oldest_days": 6.0,
                                       "median_reply_hours_to_colleagues": 2.0}})

    def test_approval_speed_per_person_who_decided(self):
        got = self.table("approval_speed", "decided_by")
        self.assertEqual(got, {
            "Lee": {"decided": 2, "median_minutes": 50.0, "slowest_tenth_hours": 1.4,
                    "within_an_hour_pct": 50, "rejected": 1},
            "Mia": {"decided": 1, "median_minutes": 5.0, "slowest_tenth_hours": 0.1,
                    "within_an_hour_pct": 100, "rejected": 0}})

    def test_people_rows_carry_their_person_for_the_team_rule(self):
        for sid in ("leader_scorecard", "colleagues_waiting"):
            self.assertEqual(spec(sid).person, "person_id")
        self.assertEqual(spec("leader_scorecard").scope, "full")
        self.assertEqual(spec("workload_balance").scope, "full")


if __name__ == "__main__":
    unittest.main()
