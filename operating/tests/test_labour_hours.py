"""Labour hours from time sheets: aggregates only, hr-marked, small groups suppressed, the
people exclusion fail closed, money never read (``operating.labour_hours``)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from integrations.people_exclusion import PeopleExclusion
from operating import labour_hours as lh

EX_A = "11111111-1111-1111-1111-111111111111"
EX_B = "22222222-2222-2222-2222-222222222222"
OK = "33333333-3333-3333-3333-333333333333"
DIRECTORY = [(EX_A, "morgan.quill@example.test", []),
             (EX_B, "rtandy@example.test", ["r.tandy@example.test"]),
             (OK, "sam.lee@example.test", [])]
PEOPLE = [f"Worker{chr(65 + i)} Person{chr(75 + i)}" for i in range(12)]
MONDAY = date(2026, 6, 1)


def guard(ids=(EX_A, EX_B), directory=DIRECTORY):
    g = PeopleExclusion(list(ids))
    if directory is not None:
        g.attach_directory(directory)
    return g


def check(**kw):
    return lh.ExclusionCheck(guard(**kw), DIRECTORY)


def workbook(week: date, people, *, ot="2", dept="Production", run=None, rate=True,
             day="8") -> str:
    """A time sheet rendered as ``integrations.xlsx_extract`` renders a workbook."""
    days = [week + timedelta(days=i) for i in range(5)]
    head = ["Name"] + [d.strftime("%a %Y-%m-%d") for d in days] + (
        ["Reg Hours", "OT Hours"] if ot is not None else ["Hour"])
    head += (["Order #"] if run else []) + (["Rate", "Pay $"] if rate else [])
    cols = [chr(65 + i) for i in range(len(head))]
    lines = ["[Sheet: Sheet1]", f"A1=Department: {dept}" if dept else "A1=Weekly hours",
             f"A2=Week ending | B2={(week + timedelta(days=6)).isoformat()}",
             " | ".join(f"{c}3={h}" for c, h in zip(cols, head))]
    for n, person in enumerate(people):
        r = 4 + n
        vals = [person] + [day] * 5 + (["40", ot] if ot is not None else
                                       [f"FORMULA(SUM(B{r}:F{r})); cached={int(day) * 5}"]) + ([run[n]] if run else []) + \
               (["37.25", f"FORMULA(H{r}*J{r}); cached=1490.00"] if rate else [])
        lines.append(" | ".join(f"{c}{r}={v}" for c, v in zip(cols, vals)))
    lines.append(f"A{4 + len(people)}=Total | H{4 + len(people)}=FORMULA(SUM(H4:H9)); "
                 "cached=999")
    return "\n".join(lines)


def grid_text(header, rows) -> str:
    """Any table as ``xlsx_extract`` renders it (header on row 2, under a department)."""
    cols = [chr(65 + i) for i in range(len(header))]
    lines = ["[Sheet: Sheet1]", "A1=Department: Blending",
             " | ".join(f"{c}2={h}" for c, h in zip(cols, header))]
    lines += [" | ".join(f"{c}{3 + n}={v}" for c, v in zip(cols, row))
              for n, row in enumerate(rows)]
    return "\n".join(lines)


def raw(handle, text, sent=datetime(2026, 6, 8, tzinfo=timezone.utc)):
    return lh.Sheet(handle, sent, text, "time sheet.xlsx", "Time sheet", OK,
                    ("sam.lee@example.test",))


def sheet(handle, week, people, *, sender="sam.lee@example.test", sent=None, **kw):
    sent = sent or datetime.combine(week + timedelta(days=7), datetime.min.time(),
                                    tzinfo=timezone.utc)
    return lh.Sheet(handle, sent, workbook(week, people, **kw), "time sheet.xlsx",
                    "Weekly time sheet", OK, (sender, "outside@vendor.test"))


class LabourAggregates(unittest.TestCase):
    def test_cardinality_totals_exact_and_money_never_read(self):
        for n in (1, 10, 60):
            sheets = [sheet(f"h{i}", MONDAY + timedelta(weeks=i), PEOPLE[:4]) for i in range(n)]
            started = time.monotonic()
            built = lh.aggregate(sheets, check())
            self.assertLess(time.monotonic() - started, 5.0)
            weeks = built["weeks"]
            self.assertEqual(len(weeks), n, n)
            self.assertEqual({w["week"] for w in weeks},
                             {(MONDAY + timedelta(weeks=i)).isoformat() for i in range(n)})
            for w in weeks:
                self.assertEqual((w["hours"], w["overtime_hours"], w["headcount_days"]),
                                 ("168.00", "8.00", 20))
                self.assertEqual(w["markings"], ["hr"])
                self.assertEqual(len(w["sources"]), 1)
            self.assertEqual(built["coverage"]["parsed"], n)
            with tempfile.TemporaryDirectory() as tmp:
                store = lh.LabourStore(Path(tmp) / "l.db")
                store.replace(built)
                dump = "\n".join(store.db.iterdump())
            for leaked in ("37.25", "1490", "Worker", "Person"):
                self.assertNotIn(leaked, dump)
                self.assertNotIn(leaked, json.dumps(built))

    def test_no_overtime_column_reads_hours_past_44_labelled(self):
        built = lh.aggregate([sheet("h", MONDAY, PEOPLE[:4], ot=None, day="10")], check())
        [w] = built["weeks"]
        self.assertEqual((w["hours"], w["overtime_hours"], w["overtime_basis"]),
                         ("200.00", "24.00", "over_44_hours"))
        mixed = lh.aggregate([sheet("a", MONDAY, PEOPLE[:2], ot=None, day="10"),
                              sheet("b", MONDAY, PEOPLE[2:4])], check())
        self.assertEqual([(w["overtime_hours"], w["overtime_basis"]) for w in mixed["weeks"]],
                         [("16.00", "mixed")])

    def test_revised_sheet_replaces_and_copies_count_once(self):
        first = sheet("h1", MONDAY, PEOPLE[:3])
        copy = sheet("h1b", MONDAY, PEOPLE[:3])
        later = sheet("h2", MONDAY, PEOPLE[:3], ot="5",
                      sent=datetime(2026, 6, 10, tzinfo=timezone.utc))
        built = lh.aggregate([first, copy, later], check())
        self.assertEqual([w["overtime_hours"] for w in built["weeks"]], ["15.00"])
        self.assertEqual(built["coverage"]["duplicate_text"], 1)
        self.assertEqual(built["coverage"]["found"], 2)

    def test_store_refuses_a_per_person_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lh.LabourStore(Path(tmp) / "l.db")
            row = dict(lh.aggregate([sheet("h", MONDAY, PEOPLE[:3])], check())["weeks"][0])
            row["person"] = "x"
            with self.assertRaises(lh.LabourRefused):
                store.replace({"weeks": [row], "coverage": {}})

    def test_unknown_input_fails_closed(self):
        c = check()
        odd = [lh.Sheet("a", None, "", "", "", OK, ()),
               lh.Sheet("b", None, "lorem ipsum\nno table here", "", "", OK, ()),
               lh.Sheet("c", datetime(2026, 6, 8, tzinfo=timezone.utc),
                        workbook(MONDAY, PEOPLE[:4]).replace("B4=8", "B4=31"), "", "", OK, ())]
        built = lh.aggregate(odd, c)
        self.assertEqual(built["weeks"], [])
        cov = built["coverage"]
        self.assertEqual((cov["no_text"], cov["no_table"], cov["implausible"]), (1, 1, 1))
        # a department is only ever a known word: a name in the place of one is not
        self.assertEqual(lh.department_in("Morgan Quill hours"), None)
        named = lh.aggregate([sheet("d", MONDAY, PEOPLE[:3], dept="Worker Smith")], c)
        self.assertEqual(named["weeks"][0]["department"], lh.UNASSIGNED)


class ReviewRepros(unittest.TestCase):
    """The deep review's repros of 8fd41a5eb1, each kept."""

    def test_daily_log_rows_of_one_person_are_summed(self):
        rows = [[who, (MONDAY + timedelta(days=d)).isoformat(), "8"]
                for who in PEOPLE[:3] for d in range(5)]
        [w] = lh.aggregate([raw("daily", grid_text(["Name", "Date", "Hours"], rows))],
                           check())["weeks"]
        self.assertEqual((w["week"], w["hours"], w["headcount_days"]),
                         (MONDAY.isoformat(), "120.00", 15))

    def test_split_rows_of_one_person_are_summed(self):
        text = grid_text(["Week ending", "2026-06-07"], []) + "\n" + "\n".join(
            [" | ".join(f"{c}{r}={v}" for c, v in zip("AB", row)) for r, row in
             enumerate([["Name", "Total Hours"], [PEOPLE[0], "20"], [PEOPLE[0], "20"],
                        [PEOPLE[1], "40"], [PEOPLE[2], "40"]], start=4)])
        [w] = lh.aggregate([raw("split", text)], check())["weeks"]
        self.assertEqual(w["hours"], "120.00")

    def test_run_totals_never_isolate_a_person_with_run_less_hours(self):
        rows = [[p, "WO1001", "30"] for p in PEOPLE[:3]] + [[PEOPLE[0], "", "7"]]
        built = lh.aggregate([raw("runs", grid_text(["Name", "Order #", "Total Hours"],
                                                     rows))], check())
        self.assertEqual([(w["run"], w["hours"]) for w in built["weeks"]], [(None, "97.00")])

    def test_run_rows_never_carry_overtime(self):
        rows = [[p, "WO1001", "40", "2"] for p in PEOPLE[:3]] + \
               [[p, "", "40", ot] for p, ot in zip(PEOPLE[3:6], ("5", "0", "0"))]
        built = lh.aggregate([raw("runs-ot", grid_text(
            ["Name", "Order #", "Reg Hours", "OT Hours"], rows))], check())
        by_run = {w["run"]: w for w in built["weeks"]}
        self.assertEqual(set(by_run), {None, "WO1001"})
        self.assertEqual(by_run[None]["overtime_hours"], "11.00")
        self.assertEqual((by_run["WO1001"]["overtime_hours"],
                          by_run["WO1001"]["overtime_basis"]), (None, None))

    def test_overtime_of_one_or_two_people_is_never_shown(self):
        derived = [[p, h] for p, h in zip(PEOPLE[:3], ("52", "40", "40"))]
        stated = [[p, "40", ot] for p, ot in zip(PEOPLE[3:6], ("9", "0", "0"))]
        for text in (grid_text(["Name", "Total Hours"], derived),
                     grid_text(["Name", "Reg Hours", "OT Hours"], stated)):
            [w] = lh.aggregate([raw("ot", text)], check())["weeks"]
            self.assertEqual((w["overtime_hours"], w["overtime_basis"]),
                             (None, "fewer_than_3"))
        # nor judged by the alert: a latest week without shown overtime raises none
        rows = [{"department": "quality", "week": (MONDAY + timedelta(weeks=i)).isoformat(),
                 "run": None, "overtime_hours": "10", "sources": []} for i in range(8)]
        rows.append({**rows[0], "week": (MONDAY + timedelta(weeks=8)).isoformat(),
                     "overtime_hours": None})
        self.assertEqual(lh.overtime_spikes(rows), [])

    def test_a_first_name_only_address_withholds_the_full_name(self):
        directory = [(EX_A, "morgan@example.test", []), DIRECTORY[1], DIRECTORY[2]]
        c = lh.ExclusionCheck(guard(directory=directory), directory)
        built = lh.aggregate([sheet("x", MONDAY, PEOPLE[:3] + ["Morgan Quill"])], c)
        self.assertEqual((built["weeks"], built["coverage"]["withheld_excluded"]), ([], 1))
        # an address with no word of two letters names nobody: refused
        bare = [(EX_A, "m@example.test", []), DIRECTORY[1], DIRECTORY[2]]
        with self.assertRaises(lh.LabourRefused):
            lh.ExclusionCheck(guard(directory=bare), bare)
        # one excluded person listed twice never stands in for the other
        twice = [DIRECTORY[0], (EX_A, "mq@example.test", []), DIRECTORY[2]]
        with self.assertRaises(lh.LabourRefused):
            lh.ExclusionCheck(guard(directory=twice), twice)


class SmallGroups(unittest.TestCase):
    def test_fewer_than_three_people_are_never_stored(self):
        built = lh.aggregate([sheet("h", MONDAY, PEOPLE[:2])], check())
        self.assertEqual(built["weeks"], [])
        self.assertEqual(built["coverage"]["groups_suppressed"], 1)

    def test_a_small_run_suppresses_every_run_of_that_week(self):
        runs = ["MO-1001"] * 4 + ["MO-1002"] * 2
        built = lh.aggregate([sheet("h", MONDAY, PEOPLE[:6], run=runs)], check())
        self.assertEqual([(w["run"], w["hours"]) for w in built["weeks"]], [(None, "252.00")])
        runs = ["MO-1001"] * 3 + ["MO-1002"] * 3
        built = lh.aggregate([sheet("h", MONDAY, PEOPLE[:6], run=runs)], check())
        self.assertEqual([w["run"] for w in built["weeks"]], [None, "MO-1001", "MO-1002"])


class PeopleExclusionFailClosed(unittest.TestCase):
    def test_no_guard_or_unresolved_excluded_person_builds_nothing(self):
        with self.assertRaises(lh.LabourRefused):
            lh.ExclusionCheck(PeopleExclusion(None), DIRECTORY)
        with self.assertRaises(lh.LabourRefused):
            lh.ExclusionCheck(guard(), None)
        with self.assertRaises(lh.LabourRefused):   # an excluded id the directory lacks
            lh.ExclusionCheck(guard(directory=DIRECTORY[1:]), DIRECTORY[1:])

    def test_owner_given_addresses_resolve_only_from_a_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "addresses.json"
            path.write_text(json.dumps({EX_A: ["morgan.quill@example.test"]}))
            path.chmod(0o644)
            env = {lh.ADDRESSES_ENV: str(path)}
            self.assertEqual(lh.excluded_addresses(env), [])     # readable by others
            path.chmod(0o600)
            given = lh.excluded_addresses(env)
            c = lh.ExclusionCheck(guard(ids=(EX_A,), directory=DIRECTORY[2:]),
                                  DIRECTORY[2:] + given)
            self.assertTrue(c.names_excluded("Quill, Morgan"))
            self.assertTrue(c.names_excluded("Morgan Lee"))   # a first name alone: over-excluded
            self.assertFalse(c.names_excluded("Sam Lee"))

    def test_excluded_people_never_enter_an_aggregate(self):
        c = check()
        base = PEOPLE[:4]
        sheets = [sheet("named-a", MONDAY, base + ["Morgan Quill"]),
                  sheet("named-b", MONDAY + timedelta(weeks=1), base + ["R. Tandy"]),
                  sheet("from-ex", MONDAY + timedelta(weeks=2), base,
                        sender="morgan.quill@example.test"),
                  sheet("undecided", MONDAY + timedelta(weeks=3), base,
                        sender="nobody.known@example.test"),
                  sheet("clean", MONDAY + timedelta(weeks=4), base)]
        built = lh.aggregate(sheets, c)
        self.assertEqual([w["week"] for w in built["weeks"]],
                         [(MONDAY + timedelta(weeks=4)).isoformat()])
        self.assertEqual(built["weeks"][0]["sources"][0]["handle"], "clean")
        cov = built["coverage"]
        self.assertEqual((cov["withheld_excluded"], cov["withheld_participants"]), (2, 2))


class Access(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        weeks = [sheet(f"h{i}", MONDAY + timedelta(weeks=i), PEOPLE[:4],
                       ot="2" if i < 8 else "4") for i in range(9)]
        path = Path(self.tmp.name) / lh.STORE_NAME
        lh.LabourStore(path).replace(lh.aggregate(weeks, check()))
        self.store = lh.open_store(self.tmp.name)

    def test_reader_without_hr_sees_nothing(self):
        grants = {"hr": frozenset({"user:hr-lead"}),
                  "ops-management": frozenset({"user:plant-manager"})}
        self.assertFalse(lh.holds_labour({"user:clerk", "group:all"}, grants))
        page = lh.labour_page(self.store, lh.holds_labour({"user:clerk"}, grants))
        self.assertEqual((page["state"], page["weeks"], page["trend"], page["alerts"]),
                         ("not_granted", [], [], []))
        said = lh.answer("overtime in production last week?", self.store, False)
        self.assertEqual((said["outcome"], said["sources"]), ("refused", []))
        self.assertNotIn("hours,", said["text"])
        for who in ("user:hr-lead", "user:plant-manager"):
            self.assertTrue(lh.holds_labour({who}, grants))

    def test_api_route_serves_only_a_granted_reader(self):
        from types import SimpleNamespace
        from unittest import mock
        from operator_api.briefs import BriefsMixin
        backend = SimpleNamespace(state_dir=self.tmp.name)
        grants = Path(self.tmp.name) / "grants.json"
        grants.write_text(json.dumps({"hr": ["user:hr-lead"]}))
        with mock.patch.dict("os.environ", {lh.GRANTS_FILE_ENV: str(grants)}):
            clerk = SimpleNamespace(session=SimpleNamespace(principals=["user:clerk"]))
            page = BriefsMixin.labour(backend, clerk)
            self.assertEqual((page.state, page.weeks, page.alerts), ("not_granted", [], []))
            lead = SimpleNamespace(session=SimpleNamespace(principals=["user:hr-lead"]))
            page = BriefsMixin.labour(backend, lead)
            self.assertEqual((page.state, len(page.weeks), len(page.alerts)), ("ready", 9, 1))
            self.assertEqual(page.weeks[0].markings, ["hr"])

    def test_grants_file_fails_closed(self):
        path = Path(self.tmp.name) / "grants.json"
        for bad in ({"hr": ["*"]}, {"payroll": ["user:x"]}, {"hr": "user:x"}, [1]):
            path.write_text(json.dumps(bad))
            self.assertEqual(lh.load_grants(path), {})
        self.assertEqual(lh.load_grants(None, {}), {})
        path.write_text(json.dumps({"hr": ["user:a"], "ops-management": []}))
        self.assertTrue(lh.holds_labour({"user:a"}, lh.load_grants(path)))
        shipped = json.loads((Path(lh.__file__).with_name("labour_grants.json")).read_text())
        self.assertEqual({g["grant"] for g in shipped["grants"]}, set(lh.GRANTS))
        self.assertTrue(all(g["principals"] == [] for g in shipped["grants"]))

    def test_granted_reader_page_alert_and_ask(self):
        page = lh.labour_page(self.store, True)
        self.assertEqual(page["state"], "ready")
        self.assertEqual(len(page["trend"][0]["points"]), 9)
        [alert] = page["alerts"]
        self.assertEqual((alert["department"], alert["overtime_hours"],
                          alert["baseline_median"], alert["rise_percent"]),
                         ("production", "16.00", "8.00", "100"))
        said = lh.answer("labour hours for production?", self.store, True)
        self.assertEqual(said["outcome"], "answered")
        self.assertIn("176.00 hours, 16.00 overtime hours, 20 headcount-days", said["text"])
        self.assertIn("Overtime spike", said["text"])
        self.assertTrue(lh.serves("How much overtime did shipping work?"))
        self.assertFalse(lh.serves("Where is PO 1234?"))

    def test_spike_needs_thirty_percent_over_the_median(self):
        rows = [{"department": "quality", "week": (MONDAY + timedelta(weeks=i)).isoformat(),
                 "run": None, "overtime_hours": v, "sources": []}
                for i, v in enumerate(["10"] * 8 + ["13"])]
        self.assertEqual(lh.overtime_spikes(rows), [])
        rows[-1]["overtime_hours"] = "13.01"
        self.assertEqual(len(lh.overtime_spikes(rows)), 1)
        self.assertEqual(lh.overtime_spikes(rows[-4:]), [])   # too few prior weeks


if __name__ == "__main__":
    unittest.main()
