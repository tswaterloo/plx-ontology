"""Insights (operator_api/insights.py): the built-in definitions, who sees which rows of a
people view, the saved store (owner only, refused before anything is read or kept), and an
Ask chart that keeps the statement it was drawn from. isolated-fixture."""
from __future__ import annotations

import json
import re
import unittest

from operator_api import insights as ins
from operator_api import models as m
from operator_api.errors import UserFacing
from operating.tests import pg_testdb
from operator_api import memory as mem
from operator_api.tests.test_memory import Guard, reader

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

ME, OTHER, HIDDEN = "aaaaaaaa-0000-4000-8000-000000000001", \
    "aaaaaaaa-0000-4000-8000-000000000002", "aaaaaaaa-0000-4000-8000-000000000003"


def person_spec(scope: str) -> ins.Spec:
    return ins.Spec(id="x", title="t", about="a", sql="SELECT 1", person="person_id",
                    scope=scope)


def people_rows(n: int) -> list[list[str]]:
    return [[f"P{i}", str(i), f"aaaaaaaa-0000-4000-8000-{i:012d}"] for i in range(1, n + 1)]


class Builtins(unittest.TestCase):
    def test_every_declared_insight_loads_and_draws_from_its_own_columns(self):
        raw = json.loads(ins.BUILTIN_FILE.read_text())["insights"]
        got = {s.id: s for s in ins.builtin()}
        self.assertEqual(sorted(got), sorted(e["id"] for e in raw))   # none dropped
        for spec in got.values():
            self.assertIn(spec.scope, ins.SCOPES)
            self.assertNotIn("{business_tz}", spec.sql)
            # the chart's and the person column are among the output names: the
            # statement's, or a counting view's groups and count
            from operator_api.charts import columns_of
            counted = [*spec.rollup["by"], spec.rollup["count"]] if spec.rollup else []
            for col in (columns_of(spec.chart) if spec.chart else []) + (
                    [spec.person] if spec.person else []) + (
                    [spec.rollup["counted"], *spec.rollup["by"]] if spec.rollup else []):
                if col not in counted or col in spec.rollup.get("by", ()):
                    self.assertRegex(spec.sql, rf"\b{re.escape(col)}\b")
            if spec.scope == "self":
                self.assertTrue(spec.person, spec.id)   # a self view names its person

    def test_an_invalid_or_unknown_scope_definition_is_left_out(self):
        bad = {"insights": [
            {"id": "ok", "title": "t", "about": "a", "sql": "SELECT 1", "scope": "org"},
            {"id": "w", "title": "t", "about": "a", "sql": "DELETE FROM staff", "scope": "org"},
            {"id": "s", "title": "t", "about": "a", "sql": "SELECT 1", "scope": "public"},
            {"id": "Bad Id", "title": "t", "about": "a", "sql": "SELECT 1", "scope": "org"}]}
        old = ins.BUILTIN_FILE
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.json"
            p.write_text(json.dumps(bad))
            ins.BUILTIN_FILE = p
            ins.builtin.cache_clear()
            try:
                self.assertEqual([s.id for s in ins.builtin()], ["ok"])
            finally:
                ins.BUILTIN_FILE = old
                ins.builtin.cache_clear()


class ViewsRead(unittest.TestCase):
    """Which views a tile's statement reads (the findings gate, the fast copies): a view's
    name standing as a whole word in any case, never inside a longer name."""

    def test_whole_words_any_case(self) -> None:
        from operator_api import insights_fast as fast
        from operating.pg import business_views as bv
        some = sorted(bv.VIEWS)[0]
        self.assertEqual(fast.views_read(f"SELECT * FROM {some.upper()} x"), [some])
        self.assertEqual(fast.views_read(f'SELECT * FROM "{some}"'), [some])
        self.assertEqual(fast.views_read(f"SELECT * FROM {some}_more, x{some}"), [])
        self.assertIn("comms_findings", fast.views_read("select * from comms_findings f"))
        # the same statement answers the same each time (its words are read once)
        self.assertEqual(fast.views_read(f"SELECT 1 FROM {some}"), [some])

    def test_findings_tiles_are_the_ones_reading_the_findings_view(self) -> None:
        for spec in ins.declared():
            self.assertEqual(ins.reads_findings(spec),
                             bool(re.search(r"\bcomms_findings\b", spec.sql, re.I)), spec.id)


class SenderNames(unittest.TestCase):
    """The decision and complaint tiles name a sender by its name; a Teams sender with no
    display name by its kind and id (never its raw JSON), and one malformed sender fails
    neither tile (a JSON cast over every row once failed the whole tile)."""

    TILES = ("decisions_said", "complaints_latest")

    def test_every_teams_sender_is_named_and_a_malformed_one_fails_nothing(self):
        import psycopg
        db, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, db)
        senders = [
            ("teams", '{"display_name":"Greg Mitchell","id":"a16b","kind":"user"}', "g"),
            ("teams", '{"id":"c0ffee","kind":"application"}', "a"),
            ("teams", '{"display_name":"", "id":"7e57","kind":"user"}', "e"),
            ("teams", '{not json', "m"),
            ("email", "ana@plx.example", "x")]
        with psycopg.connect(dsn, autocommit=True) as pg:
            pg.execute("CREATE TABLE staff (name text, email text, person_id text)")
            pg.execute("CREATE TABLE customers (name text, handle text)")
            pg.execute("CREATE TABLE comms_findings (kind text, topic text, summary text, "
                       "who text, from_side text, said_at timestamptz, source text, "
                       "sender text, thread text, customers text[], handle text)")
            pg.execute("INSERT INTO staff VALUES ('Ana Staff', 'ana@plx.example', 'p-ana')")
            for kind in ("decision", "complaint"):
                for n, (source, sender, topic) in enumerate(senders):
                    pg.execute("INSERT INTO comms_findings VALUES (%s, %s, 'said it', '', "
                               "'customer', now() - interval '1 day', %s, %s, %s, '{}', %s)",
                               (kind, topic, source, sender, f"t{n}", f"h{n}"))
            by_id = {s.id: s for s in ins.builtin()}
            for tile in self.TILES:
                with self.subTest(tile=tile):
                    rows = [r[0] for r in pg.execute(by_id[tile].sql).fetchall()]
                    self.assertEqual(len(rows), len(senders), rows)
                    names = sorted(r.split(": ", 1)[0] for r in rows)
                    self.assertEqual(names, sorted([
                        "Greg Mitchell", "Teams application c0ffee", "Teams user 7e57",
                        "Unknown sender", "Ana Staff"]))
                    self.assertFalse(any("{" in r for r in rows), rows)


class OwnerReports(unittest.TestCase):
    """The revenue outlook and supplier lead-time tiles, computed again here from the raw
    rows they read (live 11-21: CAD 3-month average 496,313.98, trend 541,157.88 =
    Python over raw month sums; supplier medians = a separate query)."""

    def test_the_outlook_and_lead_times_are_their_raw_rows_arithmetic(self):
        import datetime as dt
        import random
        from decimal import Decimal

        import psycopg
        db, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, db)
        by_id = {s.id: s for s in ins.builtin()}
        rnd = random.Random(7)
        with psycopg.connect(dsn, autocommit=True) as pg:
            today = pg.execute("SELECT current_date").fetchone()[0]
            first = today.replace(day=1)
            pg.execute("CREATE TABLE sales_invoice_lines (invoice_date date, revenue numeric, "
                       "revenue_currency text, pilot boolean)")
            months = {}
            for back in range(1, 15):          # 14 full months: only the last 12 count
                n = first.year * 12 + first.month - 1 - back
                start = dt.date(n // 12, n % 12 + 1, 1)
                total = Decimal(0)
                for _ in range(rnd.randint(1, 6)):
                    v = Decimal(rnd.randint(100, 900000)) / 100
                    total += v
                    pg.execute("INSERT INTO sales_invoice_lines VALUES (%s, %s, 'CAD', false)",
                               (start + dt.timedelta(days=rnd.randint(0, 27)), v))
                pg.execute("INSERT INTO sales_invoice_lines VALUES (%s, 999999, 'CAD', true)",
                           (start,))               # a pilot line counts nowhere
                months[start] = total
            pg.execute("INSERT INTO sales_invoice_lines VALUES (%s, 1234.56, 'CAD', false)",
                       (today,))
            row = pg.execute(by_id["revenue_this_month_outlook"].sql).fetchone()
            kept = sorted(months)[-12:]
            xs = [(m - dt.date(2000, 1, 1)).days for m in kept]
            ys = [months[m] for m in kept]
            mx, my = Decimal(sum(xs)) / 12, sum(ys) / 12
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2
                                                                          for x in xs)
            trend = my + slope * ((first - dt.date(2000, 1, 1)).days - mx)
            last_day = ((first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)).day
            self.assertEqual(row[0], "CAD")
            self.assertEqual(row[1], Decimal("1234.56"))
            self.assertEqual(row[2], (Decimal("1234.56") * last_day / today.day).quantize(Decimal("0.01")))
            self.assertEqual(row[3], (sum(ys[-3:]) / 3).quantize(Decimal("0.01")))
            self.assertAlmostEqual(float(row[4]), float(trend), delta=0.02)
            self.assertEqual(row[5], my.quantize(Decimal("0.01")))
            self.assertEqual(row[6], 12)

            pg.execute("CREATE TABLE records (handle text, type text, fields jsonb)")
            pg.execute("CREATE TABLE record_links (from_handle text, to_handle text, "
                       "link_type text)")
            pg.execute("CREATE TABLE purchase_orders (handle text, supplier text, "
                       "po_number text, order_date date, status text, source_systems text)")
            want = []
            for i in range(10):
                ordered = today - dt.timedelta(days=200 + i)
                days = 3 * i
                pg.execute("INSERT INTO purchase_orders VALUES (%s, 'Acme', %s, %s, 'closed', "
                           "'sage')", (f"po{i}", f"PO{i}", ordered))
                for k, extra in enumerate((0, 5)):   # the first receipt is the one read
                    pg.execute("INSERT INTO records VALUES (%s, 'Receipt', %s)",
                               (f"r{i}{k}", '{"received_date": "%s"}'
                                % (ordered + dt.timedelta(days=days + extra)).isoformat()))
                    pg.execute("INSERT INTO record_links VALUES (%s, %s, 'Receipt.receives')",
                               (f"r{i}{k}", f"po{i}"))
                want.append(days)
            # one receipt dated before its PO: counted apart, not a lead time
            pg.execute("INSERT INTO purchase_orders VALUES ('pox', 'Acme', 'POX', %s, 'open', "
                       "'sage')", (today,))
            # a BC pilot PO and a draft, each with a slow receipt: neither is a lead time
            for h, status, systems in (("pob", "closed", "bc"), ("pod", "draft", "sage")):
                pg.execute("INSERT INTO purchase_orders VALUES (%s, 'Acme', %s, %s, %s, %s)",
                           (h, h.upper(), today - dt.timedelta(days=300), status, systems))
                pg.execute("INSERT INTO records VALUES (%s, 'Receipt', %s)",
                           (f"r{h}", '{"received_date": "%s"}'
                            % (today - dt.timedelta(days=10)).isoformat()))
                pg.execute("INSERT INTO record_links VALUES (%s, %s, 'Receipt.receives')",
                           (f"r{h}", h))
            pg.execute("INSERT INTO records VALUES ('rx', 'Receipt', %s)",
                       ('{"received_date": "%s"}' % (today - dt.timedelta(days=3)).isoformat(),))
            pg.execute("INSERT INTO record_links VALUES ('rx', 'pox', 'Receipt.receives')")
            cur = pg.execute(by_id["supplier_lead_time"].sql)
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual((r["supplier"], r["pos_received"], r["received_before_ordered"]),
                             ("Acme", 10, 1))
            want.sort()
            self.assertEqual(float(r["median_days"]), (want[4] + want[5]) / 2)
            self.assertIsNone(r["median_days_before"])


class StockAndChaseTiles(unittest.TestCase):
    """The stock-against-open-orders, receivables-to-chase and customers-gone-quiet tiles
    on raw rows: every open line lands on one finished product in pieces or in one
    '(not mapped) <reason>' row, never both and never twice (live 11-21: 78 of 78 mapped
    Sage item and unit pairs named the same product as their FileMaker finished product,
    5 of them by eye)."""

    def test_open_lines_map_to_pieces_or_are_counted_unmapped(self):
        from decimal import Decimal

        import psycopg
        db, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, db)
        sql = {s.id: s for s in ins.builtin()}["stock_vs_open_orders"].sql
        with psycopg.connect(dsn, autocommit=True) as pg:
            pg.execute("CREATE TABLE records (handle text, type text, fields jsonb)")
            pg.execute("CREATE TABLE stock (item_code text, name text, on_hand numeric, "
                       "on_hand_unit text, as_of date, source_systems text DEFAULT 'filemaker')")
            pg.execute("CREATE TABLE sales_order_lines (handle text, order_number text, "
                       "item_code text, outstanding_quantity numeric, order_status text, "
                       "of_record boolean, order_handle text)")
            pg.execute("CREATE TABLE sales_orders (handle text, order_date date, "
                       "required_date date)")

            def rec(kind, **fields):
                pg.execute("INSERT INTO records VALUES (NULL, %s, %s)", (kind, json.dumps(fields)))

            def fp(erp, code, per_box, lead="24"):
                rec("RelSilverFinishedProducts", erp_code=erp, code=code,
                    units_per_box=per_box, lead_time=lead)

            def uom(item, unit, conv):
                rec("ItemUnitOfMeasure", item_number=item, unit=unit, conversion=conv)

            n = [0]

            def line(item, qty, unit, status="open", of_record=True):
                n[0] += 1
                h = f"l{n[0]}"
                pg.execute("INSERT INTO records VALUES (%s, 'SalesOrderLine', %s)",
                           (h, json.dumps({"ordered_quantity_unit": unit})))
                pg.execute("INSERT INTO sales_order_lines VALUES (%s, %s, %s, %s, %s, %s, %s)",
                           (h, f"O{n[0]}", item, qty, status, of_record, f"o{n[0]}"))
                pg.execute("INSERT INTO sales_orders VALUES (%s, '2026-10-01', NULL)",
                           (f"o{n[0]}",))

            # stocking unit the case, an each a sixth rounded by Sage: 6 a case
            fp("CASE6", "FG-CASE6", "6"); uom("CASE6", "CSE", "1"); uom("CASE6", "EA", "0.166600")
            pg.execute("INSERT INTO stock VALUES ('FG-CASE6', 'Six pack', 100, 'PCS', '2026-11-21')")
            line("CASE6", 27, "CSE")     # 162 pieces
            line("case6 ", 10, "EA")     # 10 pieces (codes read trimmed, any case)
            line("CASE6", 5, "CSE", status="closed")       # not open: not counted
            line("CASE6", 5, "CSE", of_record=False)      # not of record: not counted
            # each only: no case to check
            fp("EACH1", "FG-EACH1", "1"); uom("EACH1", "EA", "1")
            pg.execute("INSERT INTO stock VALUES ('FG-EACH1', 'Single', 0, 'PCS', '2026-11-21')")
            line("EACH1", 2500, "EA")
            # Sage says a case is 1, FileMaker 4 a box: not mapped
            fp("BAD", "FG-BAD", "4"); uom("BAD", "CSE", "1"); uom("BAD", "EA", "1")
            pg.execute("INSERT INTO stock VALUES ('FG-BAD', 'Bad', 0, 'PCS', '2026-11-21')")
            line("BAD", 3, "CSE")
            # two finished products carry one code: not mapped
            fp("TWO", "FG-TWO-A", "1"); fp("TWO", "FG-TWO-B", "1"); uom("TWO", "EA", "1")
            line("TWO", 1, "EA")
            # no finished product, and a unit Sage holds no conversion for
            line("MISC", 1, "EA")
            fp("NOUNIT", "FG-NOUNIT", "1"); uom("NOUNIT", "EA", "1")
            pg.execute("INSERT INTO stock VALUES ('FG-NOUNIT', 'No unit', 5, 'PCS', '2026-11-21')")
            line("NOUNIT", 1, "PL")
            # two stock rows of one product: one figure when they agree (never added), a
            # reason when they differ; a BC pilot row is never stock
            fp("TWIN", "FG-TWIN", "1"); uom("TWIN", "EA", "1")
            pg.execute("INSERT INTO stock (item_code, name, on_hand, on_hand_unit, as_of) VALUES "
                       "('FG-TWIN', 'Twin', 40, 'PCS', '2026-11-21'), "
                       "('FG-TWIN', 'Twin', 40, 'PCS', '2026-11-21')")
            pg.execute("INSERT INTO stock VALUES ('FG-TWIN', 'Twin', 999, 'PCS', '2026-11-21', 'bc')")
            line("TWIN", 100, "EA")
            fp("SPLIT", "FG-SPLIT", "1"); uom("SPLIT", "EA", "1")
            pg.execute("INSERT INTO stock (item_code, name, on_hand, on_hand_unit, as_of) VALUES "
                       "('FG-SPLIT', 'Split', 10, 'PCS', '2026-11-21'), "
                       "('FG-SPLIT', 'Split', 30, 'PCS', '2026-11-21')")
            line("SPLIT", 5, "EA")
            cur = pg.execute(sql)
            cols = [c.name for c in cur.description]
            rows = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
        self.assertEqual(rows["FG-CASE6"]["open_order_pieces"], Decimal("172.00"))
        self.assertEqual(rows["FG-CASE6"]["short_by"], Decimal("72.00"))
        self.assertEqual(rows["FG-CASE6"]["open_lines"], 2)
        self.assertTrue(rows["FG-CASE6"]["unit_basis"].startswith("case size checked"))
        self.assertEqual(rows["FG-EACH1"]["short_by"], Decimal("2500.00"))
        self.assertTrue(rows["FG-EACH1"]["unit_basis"].startswith("each only"))
        unmapped = {k: v["open_lines"] for k, v in rows.items() if k.startswith("(not mapped)")}
        self.assertEqual(unmapped, {
            "(not mapped) case size differs between Sage and FileMaker": 1,
            "(not mapped) several FileMaker finished products carry the item code": 1,
            "(not mapped) no FileMaker finished product carries the item code": 1,
            "(not mapped) Sage states no conversion of the order unit to each": 1,
            "(not mapped) stock records for the finished product disagree": 1})
        self.assertEqual((rows["FG-TWIN"]["on_hand"], rows["FG-TWIN"]["short_by"]),
                         (Decimal("40"), Decimal("60.00")))
        for gone in ("FG-BAD", "FG-TWO-A", "FG-TWO-B", "FG-NOUNIT", "FG-SPLIT"):
            self.assertNotIn(gone, rows)
        # every open line of record counted once: 4 mapped + 5 unmapped
        self.assertEqual(sum(v["open_lines"] for v in rows.values()), 9)

    def test_chase_nets_credits_and_quiet_customers_are_the_ones_gone_90_days(self):
        import datetime as dt
        from decimal import Decimal

        import psycopg
        db, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, db)
        by_id = {s.id: s for s in ins.builtin()}
        with psycopg.connect(dsn, autocommit=True) as pg:
            today = pg.execute("SELECT (now() AT TIME ZONE 'America/New_York')::date"
                               ).fetchone()[0]
            pg.execute("CREATE TABLE customer_balances (customer text, customer_code text, "
                       "amount_due_currency text, amount_due numeric, days_overdue int, "
                       "document_type text, due_date date)")
            pg.execute("CREATE TABLE customers (customer_code text, contact_person text, "
                       "email text, phone text, salesperson text)")
            for row in (("A", "a", "CAD", 1000, 75), ("A", "a", "CAD", -200, 10),
                        ("A", "a", "CAD", 500, 0), ("B", "b", "CAD", 300, 5),
                        ("B", "b", "USD", 50, 0), ("C", "c", "CAD", 900, 0)):
                pg.execute("INSERT INTO customer_balances VALUES (%s, %s, %s, %s, %s, 'inv', "
                           "%s)", (*row, today - dt.timedelta(days=row[4])))
            pg.execute("ALTER TABLE customers ADD COLUMN name text, ADD COLUMN handle text")
            # one code on three customer rows (live 11-21: PETSUP10): one row per balance,
            # the contact of the row with the balance's own name, never another's
            pg.execute("INSERT INTO customers VALUES "
                       "('a', 'Other', 'other@x.example', '2', 'S2', 'Other Co', 'h1'), "
                       "('a', 'Ann', 'ann@a.example', '1', 'S1', 'A', 'h2'), "
                       "('a', 'Third', 'third@x.example', '3', 'S3', 'Third Co', 'h3'), "
                       "('b', 'Bo', 'bo@x.example', '4', 'S4', 'Not B', 'h4'), "
                       "('b', 'Bea', 'bea@x.example', '5', 'S5', 'Also not B', 'h5')")
            cur = pg.execute(by_id["receivables_to_chase"].sql)
            cols = [c.name for c in cur.description]
            chase = [dict(zip(cols, r)) for r in cur.fetchall()]
            self.assertEqual([(r["customer"], r["currency"], r["overdue"]) for r in chase],
                             [("A", "CAD", Decimal("800.00")), ("B", "CAD", Decimal("300.00"))])
            self.assertEqual((chase[0]["overdue_over_60"], chase[0]["oldest_days_overdue"],
                              chase[0]["overdue_documents"], chase[0]["owed_in_total"],
                              chase[0]["email"]),
                             (Decimal("1000.00"), 75, 1, Decimal("1300.00"), "ann@a.example"))
            # a shared code with no row of the balance's name: no contact at all
            self.assertIsNone(chase[1]["email"])

            pg.execute("CREATE TABLE sales_invoice_lines (customer text, customer_chain text, "
                       "revenue_currency text, invoice_date date, revenue numeric, "
                       "pilot boolean)")
            for row in (("Quiet", "Ch", "CAD", 120, 100), ("Quiet", "Ch", "CAD", 200, 50),
                        ("Quiet", "Ch", "USD", 300, 7), ("Busy", "Ch", "CAD", 10, 5),
                        ("Busy", "Ch", "CAD", 200, 5), ("Gone", None, "CAD", 500, 99),
                        ("Pilot", None, "CAD", 120, 1)):
                pg.execute("INSERT INTO sales_invoice_lines VALUES (%s, %s, %s, %s, %s, %s)",
                           (row[0], row[1], row[2], today - dt.timedelta(days=row[3]), row[4],
                            row[0] == "Pilot"))
            cur = pg.execute(by_id["customers_lost"].sql)
            cols = [c.name for c in cur.description]
            lost = [dict(zip(cols, r)) for r in cur.fetchall()]
        self.assertEqual([(r["customer"], r["currency"], r["revenue_in_the_year_before"],
                           r["days_since_last_invoice"], r["chain_still_invoiced"])
                          for r in lost],
                         [("Quiet", "CAD", Decimal("150.00"), 120, True),
                          ("Quiet", "USD", Decimal("7.00"), 120, True)])


class OutlookClock(unittest.TestCase):
    def test_the_outlook_reads_the_month_in_the_company_time_zone_in_the_evening(self):
        """At 9 PM in Toronto on 30 Nov the UTC date is already 1 Dec: the tile still reads
        November so far over 30 days (current_date in UTC once flipped it to December)."""
        import psycopg
        from decimal import Decimal
        db, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, db)
        sql = {s.id: s for s in ins.builtin()}["revenue_this_month_outlook"].sql
        self.assertNotIn("current_date", sql)
        fixed = sql.replace("now()", "TIMESTAMPTZ '2026-11-30 21:00 America/New_York'")
        with psycopg.connect(dsn, autocommit=True) as pg:
            pg.execute("CREATE TABLE sales_invoice_lines (invoice_date date, revenue numeric, "
                       "revenue_currency text, pilot boolean)")
            pg.execute("INSERT INTO sales_invoice_lines VALUES ('2026-10-15', 300, 'CAD', false),"
                       " ('2026-11-02', 600, 'CAD', false), ('2026-11-30', 300, 'CAD', false)")
            row = pg.execute(fixed).fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("CAD", Decimal("900.00"), Decimal("900.00")))


class Rows(unittest.TestCase):
    cols = ["person", "n", "person_id"]

    def test_self_reader_sees_only_their_own_row_at_any_size(self):
        for n in (1, 10, 60):
            rows = people_rows(n)
            me = frozenset({rows[-1][2]})
            cols, kept, people = ins.visible_rows(person_spec("self"), self.cols, rows,
                                                  False, me, lambda p: False)
            self.assertEqual(cols, ["person", "n"])   # the id never leaves
            self.assertEqual(kept, [[f"P{n}", str(n)]])
            self.assertEqual(people, [rows[-1][2]])

    def test_everyone_reader_sees_every_row_but_the_hidden_and_excluded(self):
        for n in (1, 10, 60):
            rows = people_rows(n)
            hidden = rows[0][2]
            cols, kept, people = ins.visible_rows(person_spec("self"), self.cols, rows,
                                                  True, frozenset(), lambda p: p == hidden)
            self.assertEqual(len(kept), n - 1)
            self.assertEqual(len(set(people)), n - 1)   # none lost, none twice
            self.assertNotIn(hidden, people)
            self.assertNotIn(hidden, json.dumps(kept))

    def test_org_rows_for_everyone_minus_the_hidden_and_a_row_without_a_person_dropped(self):
        rows = people_rows(10) + [["nobody", "0", None], ["blank", "0", ""]]
        _, kept, _ = ins.visible_rows(person_spec("org"), self.cols, rows, False,
                                      frozenset(), lambda p: p.endswith("05"))
        self.assertEqual(len(kept), 9)

    def test_a_person_view_whose_result_lacks_the_person_column_shows_nothing(self):
        _, kept, _ = ins.visible_rows(person_spec("self"), ["person", "n"], [["P", "1"]],
                                      True, frozenset(), lambda p: False)
        self.assertEqual(kept, [])


class FakeWorker:
    def __init__(self, out):
        self.out, self.sql, self.how = out, [], []

    def run(self, reader, job, timeout=0, priority=0, long=False):
        self.how.append((priority, long))
        view = type("V", (), {})()
        view._call = lambda tool, args: (self.sql.append(args["sql"]), self.out)[1]
        return job(view)


class Backend(ins.InsightsMixin):
    def __init__(self, out, everyone, me=frozenset(), memory=None):
        self.reads = FakeWorker(out)
        self.scope = (everyone, me, lambda p: p == HIDDEN, True)
        self.memory = memory
        self.access = ("policy-1", False, "excl-1", "marks-1")   # the reader's access state

    def _access_now(self, reader, screen=None):
        if isinstance(self.access, Exception):
            raise self.access
        return self.access

    def _team_scope(self, reader):
        return self.scope


class EveryRowTest(unittest.TestCase):
    """plx.sql shows one page of a result and holds the rest under its id: a tile's read
    pages on to the last row (no cut), and a failing later page fails the read."""

    def worker(self, total, page, fail_at=None):
        rows = [{"cite": f"sql://x#row={n}", "cells": [str(n)]} for n in range(total)]
        calls = []

        def call(tool, args):
            calls.append(dict(args))
            start = args.get("offset", 0)
            if fail_at is not None and start == fail_at:
                return {"error": {"code": "not_found", "message": "gone"}}
            got = rows[start:start + page]
            more = start + len(got) < total
            out = {"result": "sql://x", "columns": ["n"], "rows": got, "row_count": len(got),
                   "truncated": more, "profile": {"rows": total}}
            if more:
                out["profile"]["page"] = "read on"
            return out
        w = FakeWorker(None)
        w.run = lambda reader, job, timeout=0, priority=0, long=False: job(
            type("V", (), {"_call": staticmethod(call)})())
        return w, calls

    def test_every_row_of_a_paged_result_is_read(self):
        for total in (1, 10, 60, 450):
            b = Backend(None, everyone=True)
            b.reads, calls = self.worker(total, 200)
            got = ins.read_sql(b, reader("owner"), "SELECT n FROM t")
            self.assertEqual([r["cells"][0] for r in got["rows"]],
                             [str(n) for n in range(total)])
            self.assertFalse(got["truncated"])
            self.assertEqual(len(calls), 1 + (total - 1) // 200)

    def test_a_failing_later_page_is_an_error_not_a_whole_result(self):
        b = Backend(None, everyone=True)
        b.reads, _ = self.worker(450, 200, fail_at=200)
        with self.assertRaises(UserFacing):
            ins.read_sql(b, reader("owner"), "SELECT n FROM t")


class ReadQueueTest(unittest.TestCase):
    """Insight reads run on the page read workers, never the access workers (those decide
    every request's door: a dashboard's tiles held both and every page waited); a read
    nobody waits on goes after the page reads and as a long read."""

    def test_tile_reads_skip_the_access_workers_and_reads_ahead_yield(self):
        b = Backend(sql_out([["P", "1", "p1"]]), everyone=True)
        b.access_worker = FakeWorker(None)   # would fail the read if it were used
        ins.read_sql(b, reader("owner"), "SELECT 1")
        ins.read_sql(b, reader("owner"), "SELECT 2", ahead=True)
        self.assertEqual(b.access_worker.sql, [])
        self.assertEqual(b.reads.sql, ["SELECT 1", "SELECT 2"])
        self.assertEqual(b.reads.how, [(0, False), (ins.AHEAD_PRIORITY, True)])
        self.assertGreater(ins.AHEAD_PRIORITY, 2)   # after warm-ups (serving.WARM_PRIORITY)


def sql_out(rows, cols=("person", "mail_sent", "person_id")):
    return {"result": "sql://r", "columns": list(cols), "row_count": len(rows),
            "rows": [{"cite": f"sql://r#row={i}", "cells": r} for i, r in enumerate(rows)],
            "truncated": False, "as_of": "2026-11-18T00:00:00+00:00"}


OUTPUT = ("person", "mail_sent", "teams_sent", "person_id")


class NotMeasurableTest(unittest.TestCase):
    """A tile whose base column is unknown everywhere says so instead of an empty chart."""

    def setUp(self):
        ins._KNOWN.clear()

    def _backend(self, filled: bool):
        empty = sql_out([], ("person", "mail_sent", "person_id"))
        b = Backend(empty, everyone=True)

        def run(reader_, job, timeout=0, priority=0, long=False):
            view = type("V", (), {})()
            def call(tool, args):
                b.reads.sql.append(args["sql"])
                if "IS NOT NULL LIMIT 1" in args["sql"]:
                    return sql_out([["1"]] if filled else [], ("?column?",))
                return empty
            view._call = call
            return job(view)
        b.reads.run = run
        return b

    def test_every_declared_need_names_a_real_view_column(self):
        raw = json.loads(ins.BUILTIN_FILE.read_text(encoding="utf-8"))
        declared = {e["id"]: e["needs"] for e in raw["insights"] if e.get("needs")}
        self.assertTrue(declared)
        kept = {s.id: s.needs for s in ins.builtin() if s.needs}
        self.assertEqual(kept, {k: tuple(v) for k, v in declared.items()})

    def test_an_empty_tile_over_a_column_nobody_filled_is_not_measurable(self):
        b = self._backend(filled=False)
        spec = ins.Spec(id="x", title="t", about="a", sql="SELECT 1", needs=("emails.owner_role",))
        got = ins.result(b, reader("owner"), spec, b.scope)
        self.assertIn("Not measurable", got.not_measurable)
        self.assertIn("emails.owner_role", got.not_measurable)

    def test_an_empty_tile_over_a_filled_column_is_really_empty(self):
        b = self._backend(filled=True)
        spec = ins.Spec(id="x", title="t", about="a", sql="SELECT 1", needs=("emails.owner_role",))
        self.assertIsNone(ins.result(b, reader("owner"), spec, b.scope).not_measurable)

    def test_a_tile_with_rows_makes_no_check(self):
        b = Backend(sql_out([["P", "1", "p1"]]), everyone=True)
        spec = ins.Spec(id="x", title="t", about="a", sql="SELECT 1", needs=("emails.owner_role",))
        got = ins.result(b, reader("owner"), spec, b.scope)
        self.assertIsNone(got.not_measurable)
        self.assertEqual(b.reads.sql, ["SELECT 1"])


class CutListTest(unittest.TestCase):
    """A list the 200-row / 20 KB cap cut says how many rows the statement has in all."""

    def _backend(self, n_total, cut=True):
        rows = [[f"P{i}", "1", f"p{i}"] for i in range(5)]
        shown = dict(sql_out(rows), truncated=cut)
        count = {"columns": ["n"], "rows": [{"cells": [str(n_total)]}], "truncated": False}
        b = Backend(shown, everyone=True)

        def run(reader_, job, timeout=0, priority=0, long=False):
            view = type("V", (), {})()
            def call(tool, args):
                b.reads.sql.append(args["sql"])
                return count if "plx_all" in args["sql"] else shown
            view._call = call
            return job(view)
        b.reads.run = run
        return b

    def test_a_cut_result_carries_the_statements_real_row_count(self):
        b = self._backend(233)
        spec = ins.Spec(id="x", title="t", about="a", sql="SELECT 1",
                        chart={"kind": "bar", "x": "person", "y": ["mail_sent"]})
        got = ins.result(b, reader("owner"), spec, b.scope)
        self.assertEqual((got.row_count, got.truncated, got.total_rows), (5, True, 233))
        if got.drawn:
            self.assertEqual(got.drawn.rows_total, 233)
        self.assertTrue(any("plx_all" in q for q in b.reads.sql))

    def test_a_statement_the_team_rule_can_drop_rows_of_has_no_total_not_a_wrong_one(self):
        b = self._backend(233)
        spec = ins.Spec(id="x", title="t", about="a", sql="SELECT 1", person="person_id")
        got = ins.result(b, reader("owner"), spec, b.scope)
        self.assertTrue(got.truncated)
        self.assertIsNone(got.total_rows)

    def test_the_unmeasurable_check_is_kept_per_reader(self):
        ins._KNOWN.clear()
        spec = ins.Spec(id="x", title="t", about="a", sql="SELECT 1", needs=("emails.owner_role",))
        nb = NotMeasurableTest._backend(self, filled=False)
        yes = NotMeasurableTest._backend(self, filled=True)
        self.assertIsNotNone(ins.result(nb, reader("ana"), spec, nb.scope).not_measurable)
        # another reader (who may read a filled column) is not told it is unmeasurable
        self.assertIsNone(ins.result(yes, reader("owner"), spec, yes.scope).not_measurable)

    def test_a_whole_result_makes_no_second_read(self):
        b = self._backend(5, cut=False)
        got = b.insight_result(reader("owner"), "team_output")
        self.assertEqual((got.truncated, got.total_rows), (False, None))
        self.assertFalse(any("plx_all" in q for q in b.reads.sql))

    def test_a_count_that_fails_says_unknown_not_the_cut_count(self):
        b = self._backend(0)
        orig = b.reads.run
        def run(reader_, job, timeout=0, priority=0, long=False):
            view = type("V", (), {})()
            def call(tool, args):
                if "plx_all" in args["sql"]:
                    return {"error": {"code": "too_costly", "message": "x"}}
                return sql_out([["P", "1", "p"]], ("person", "mail_sent", "person_id")) | {"truncated": True}
            view._call = call
            return job(view)
        b.reads.run = run
        got = b.insight_result(reader("owner"), "team_output")
        self.assertTrue(got.truncated)
        self.assertIsNone(got.total_rows)


class Mixin(unittest.TestCase):
    rows = [["Me", 3, 1, ME], ["Other", 9, 2, OTHER], ["Hidden", 7, 3, HIDDEN]]

    def test_a_staff_reader_gets_their_own_row_and_no_company_wide_view(self):
        b = Backend(sql_out(self.rows, OUTPUT), everyone=False, me=frozenset({ME}))
        listed = b.insights_list(reader("ana"))
        self.assertEqual(listed.scope, "self")
        # the agreed measures' definitions are the same for every reader: no figure in them
        from operating import measures
        self.assertEqual({x.id for x in listed.measures}, set(measures.measures()))
        self.assertTrue(all(x.definition and x.owner and x.ask for x in listed.measures))
        full = [s.id for s in ins.builtin() if s.scope == "full"]
        self.assertTrue(full)
        self.assertFalse({i.id for i in listed.items} & set(full))
        for iid in full:   # withheld by name, like a findings tile; nothing read
            with self.subTest(iid=iid):
                with self.assertRaises(UserFacing) as e:
                    b.insight_result(reader("ana"), iid)
                self.assertEqual((e.exception.code, e.exception.status),
                                 ("insight_withheld", 403))
        self.assertEqual(b.reads.sql, [])
        got = b.insight_result(reader("ana"), "team_output")
        self.assertEqual(got.rows, [["Me", "3", "1"]])
        self.assertEqual(got.drawn.labels, ["Me"])
        self.assertNotIn(OTHER, got.model_dump_json())

    def test_a_reader_without_full_read_is_shown_no_findings_tile(self):
        """A tile over the communications findings needs full read over every source: for
        anyone else their door has no such table, so it was a listed tile that only ever
        answered 503 "not built yet". Not listed for them, 403 when asked for by name; the
        owner keeps every one."""
        findings = [s.id for s in ins.builtin() if ins.reads_findings(s)]
        self.assertTrue(findings)
        staff = Backend(sql_out([], OUTPUT), everyone=False, me=frozenset({ME}))
        listed = {i.id for i in staff.insights_list(reader("ana")).items}
        self.assertFalse(listed & set(findings))
        self.assertIn("team_output", listed)
        for iid in findings:
            with self.subTest(iid=iid):
                with self.assertRaises(UserFacing) as e:
                    staff.insight_result(reader("ana"), iid)
                self.assertEqual((e.exception.code, e.exception.status),
                                 ("insight_withheld", 403))
        self.assertEqual(staff.reads.sql, [])   # nothing was read for them
        from operating.employee_permissions import FULL_READ_SCOPES
        owner = reader("owner")
        owner.session.access = "full_read:" + "+".join(sorted(FULL_READ_SCOPES)) + ":mail_all"
        boss = Backend(sql_out([], OUTPUT), everyone=True)
        self.assertTrue(set(findings) <= {i.id for i in boss.insights_list(owner).items})

    def test_the_owner_sees_everyone_but_the_hidden_with_the_definition(self):
        b = Backend(sql_out(self.rows, OUTPUT), everyone=True)
        got = b.insight_result(reader("owner"), "team_output")
        self.assertEqual([r[0] for r in got.rows], ["Me", "Other"])
        self.assertEqual(got.sql, next(s.sql for s in ins.builtin() if s.id == "team_output"))
        self.assertEqual(b.reads.sql, [got.sql])   # read through plx.sql, as the reader
        self.assertNotIn("Hidden", got.model_dump_json())

    def test_unknown_ids_and_a_door_refusal_fail_closed(self):
        b = Backend({"error": {"code": "not_ready", "message": "rebuilt"}}, everyone=True)
        for iid in ("no_such", "s1", "../x"):
            with self.assertRaises(UserFacing) as e:
                b.insight_result(reader("owner"), iid)
            self.assertEqual(e.exception.status, 404)
        with self.assertRaises(UserFacing) as e:
            b.insight_result(reader("owner"), "org_span")
        self.assertEqual((e.exception.code, e.exception.status), ("insight_not_ready", 503))

    def test_a_tile_that_cannot_be_read_says_what_is_missing(self):
        """plx.sql's own error texts (operating/ontology_tools_sql.py) -> what the reader is
        told: a column the business views declare but the served tables lack names it and
        says it is not built yet; never a nudge to rephrase a tile nobody typed."""
        cases = [
            ({"code": "sql_error", "message": "column s.person_id does not exist"},
             "insight_not_built", 503, "staff.person_id"),
            ({"code": "sql_error", "message": 'column "person_id" does not exist'},
             "insight_not_built", 503, "staff.person_id"),
            ({"code": "sql_error", "message": 'column "no_such_col" does not exist'},
             "insight_gone", 422, "no_such_col"),
            ({"code": "unknown_view", "message": 'relation "biz.no_such_view" does not exist'},
             "insight_gone", 422, "no_such_view"),
            ({"code": "timeout", "message": "the query ran past 10 s"},
             "insight_too_slow", 504, "10-second"),
            ({"code": "not_readable", "message": "outside"}, "insight_withheld", 403, "outside"),
            ({"code": "busy", "message": "8 plx.sql calls"}, "busy", 503, "busy"),
            ({"code": "something_new", "message": "x"}, "insight_unreadable", 503, "Refresh"),
            ({}, "insight_unreadable", 503, "Refresh"),
        ]
        for err, code, status, said in cases:
            with self.subTest(err=err):
                b = Backend({"error": err} if err else {"rows": []}, everyone=True)
                with self.assertRaises(UserFacing) as e:
                    b.insight_result(reader("owner"), "team_output")
                self.assertEqual((e.exception.code, e.exception.status), (code, status))
                self.assertIn(said, e.exception.message)
                self.assertNotIn("rephras", e.exception.message.lower())
                self.assertNotIn("does not exist", e.exception.message)


@unittest.skipIf(psycopg is None, "psycopg not installed")
class ServedView(unittest.TestCase):
    """The console view the reads worker hands a tile (``HostAccess.tools``) serves plx.sql
    for the reader: every tile 422'd 'plx.sql is not served by this console' while the
    fakes above stood in for it."""

    def test_a_tile_reads_its_statement_through_the_host_as_the_gated_reader(self):
        from contextlib import nullcontext
        from ontology.types import Actor
        from operator_api.workspace import object_workspace as objects
        actor = Actor("t", frozenset({"p:owner"}))
        gated = Actor("t", frozenset({"p:owner", "g:staff"}))
        calls = []

        class Host:
            pinned = None

            def policy_now(self, who):
                return None

            def request(self):
                return nullcontext()

            def call(self, name, arguments, who):
                calls.append((name, dict(arguments), who))
                return sql_out([["Me", 3, 1, ME]], OUTPUT)

        access = objects.HostAccess(Host(), type("R", (), {
            "employee_gate": staticmethod(lambda a: gated if a == actor else None)})(), actor)
        view = objects.ObjectWorkspace(access.tools(), actor, access=access)

        class Worker:
            def run(self, reader, job, timeout=0, priority=0, long=False):
                return job(view)

        b = Backend(None, everyone=True)
        b.reads = Worker()
        got = b.insight_result(reader("owner"), "team_output")
        spec = next(s for s in ins.builtin() if s.id == "team_output")
        self.assertEqual(calls, [("plx.sql", {"sql": spec.sql}, gated)])
        self.assertEqual([r[0] for r in got.rows], ["Me"])
        with self.assertRaises(PermissionError):   # never as another reader
            access.tools()["plx.sql"]["handler"]({"sql": "SELECT 1"},
                                                 Actor("t", frozenset({"p:other"})))


def person(principal, *, access=None, expires=None):
    import time as _t
    session = type("S", (), {"principals": frozenset({principal}), "clearance": "internal",
                             "access": access, "view_as": False, "canary": False,
                             "expired": lambda self: expires is not None and _t.time() >= expires})()
    return type("R", (), {"key": principal, "session": session, "display_name": principal,
                          "email": None})()


class Later:
    """A keeper's pool whose submitted reads run only at ``run()`` (in order, on this thread)."""

    def __init__(self) -> None:
        self.queued: list = []

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future
        done: Future = Future()
        self.queued.append((done, fn, args, kwargs))
        return done

    def run(self) -> None:
        while self.queued:
            done, fn, args, kwargs = self.queued.pop(0)
            try:
                done.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - kept on the future, as a pool does
                done.set_exception(exc)


class KeptAnswers(unittest.TestCase):
    """insights_cache: a tile's answer is kept per reader under the door stamp it was read
    under, never served to another reader or another grant, read once when opened at once."""

    def setUp(self):
        from operator_api import insights_cache as ic
        self.ic = ic
        self.now, self.stamp, self.reads = [0.0], [7], []

    def kept(self, fresh=300.0):
        return self.ic.Kept(lambda: self.stamp[0], fresh=fresh, clock=lambda: self.now[0])

    def read(self, value):
        def go():
            self.reads.append(value)
            return value
        return go

    def test_another_reader_or_grant_never_gets_a_kept_answer(self):
        k = self.kept()
        a, b = self.ic.reader_key(person("employee:a")), self.ic.reader_key(person("employee:b"))
        a_full = self.ic.reader_key(person("employee:a", access="full_read:x"))
        self.assertEqual(k.get((a, "S"), self.read("A")), "A")
        self.assertEqual(k.get((a, "S"), self.read("A2")), "A")      # kept for a
        self.assertEqual(k.get((b, "S"), self.read("B")), "B")       # b reads their own
        self.assertEqual(k.get((a_full, "S"), self.read("AF")), "AF")  # a under a new grant
        self.assertEqual(self.reads, ["A", "B", "AF"])
        self.assertIsNone(self.ic.reader_key(type("R", (), {"session": None})()))

    def test_a_moved_or_unreadable_door_stamp_or_age_reads_again(self):
        k, key = self.kept(), ("p",)
        k.get((key, "S"), self.read(1))
        self.stamp[0] = 8                                   # the door changed: read now
        self.assertEqual(k.get((key, "S"), self.read(2)), 2)
        self.stamp[0] = None                                # unreadable: never kept
        self.assertEqual(k.get((key, "S"), self.read(3)), 3)
        self.assertEqual(k.get((key, "S"), self.read(4)), 4)
        self.stamp[0] = 8
        self.now[0] = 299.0
        self.assertEqual(k.get((key, "S"), self.read(5)), 2)   # still the one read under 8
        self.now[0] = 301.0
        self.assertEqual(k.get((key, "S"), self.read(6)), 6)   # stale
        self.assertEqual(k.get((key, "S"), self.read(7), again=True), 7)   # Refresh
        broken = self.ic.Kept(lambda: 1 / 0, fresh=300.0)
        self.assertEqual(broken.get((key, "S"), self.read(8)), 8)
        self.assertEqual(broken.get((key, "S"), self.read(9)), 9)

    def test_opened_at_once_one_read_and_a_failed_read_is_everyone_s_error(self):
        import threading
        k, gate, started = self.kept(), threading.Event(), threading.Event()

        def slow():
            started.set()
            gate.wait(5)
            self.reads.append("x")
            return "x"
        out = []
        first = threading.Thread(target=lambda: out.append(k.get(("p", "S"), slow)))
        first.start()
        started.wait(5)
        second = threading.Thread(target=lambda: out.append(k.get(("p", "S"), self.read("y"))))
        second.start()
        gate.set()
        first.join(5), second.join(5)
        self.assertEqual((out, self.reads), (["x", "x"], ["x"]))

        def boom():
            raise TimeoutError("slow")
        with self.assertRaises(TimeoutError):
            k.get(("p", "T"), boom)
        self.assertEqual(k.get(("p", "T"), self.read("ok")), "ok")   # not kept as failed

    def test_kept_on_disk_is_served_after_a_restart_only_as_it_was_read(self):
        import tempfile
        import time as _t
        from operator_api.page_store import PageStore
        with tempfile.TemporaryDirectory() as tmp:
            store = PageStore(__import__("pathlib").Path(tmp), code="c1")
            offset = [0.0]

            def kept():   # a process: its own memory, the same file
                return self.ic.Kept(lambda: self.stamp[0], fresh=300.0, disk=store,
                                    wall=lambda: _t.time() + offset[0])
            a = self.ic.reader_key(person("employee:a"))
            b = self.ic.reader_key(person("employee:b"))
            a_full = self.ic.reader_key(person("employee:a", access="full_read:x"))
            kept().get((a, "S"), self.read("A"))
            again = kept()                                          # restarted
            self.assertEqual(again.get((a, "S"), self.read("A2")), "A")   # from the file
            self.assertLess(again.age((a, "S")), 5)
            self.assertEqual(kept().get((b, "S"), self.read("B")), "B")     # never a's
            self.assertEqual(kept().get((a_full, "S"), self.read("AF")), "AF")
            self.assertEqual(kept().get((a, "T"), self.read("AT")), "AT")   # another statement
            self.stamp[0] = 8                                       # the door moved
            self.assertEqual(kept().get((a, "S"), self.read("A8")), "A8")
            self.stamp[0] = None                                    # unreadable: nothing kept
            self.assertEqual(kept().get((a, "S"), self.read("AN")), "AN")
            self.assertIsNone(kept().age((a, "S")))
            self.stamp[0] = 7                                       # back: the file's A again
            self.assertEqual(kept().get((a, "S"), self.read("A7")), "A")
            # stale on disk: served only with a read behind it (``later``), never past
            # ``stale_most``; a Refresh reads now
            offset[0] = 400.0
            self.assertEqual(kept().get((a, "S"), self.read("A9")), "A9")   # no later: now
            asked = []
            late = self.ic.Kept(lambda: self.stamp[0], fresh=300.0, disk=store,
                                later=asked.append, wall=lambda: _t.time() + offset[0])
            offset[0] = 700.0
            self.assertEqual(late.get((a, "S"), self.read("A10")), "A9")
            self.assertEqual(asked, [(a, "S")])
            self.assertEqual(late.get((a, "S"), self.read("A11"), again=True), "A11")
            offset[0] = 1000.0 + self.ic.STALE_MOST_SECONDS
            fresh_proc = self.ic.Kept(lambda: self.stamp[0], fresh=300.0, disk=store,
                                      later=asked.append, wall=lambda: _t.time() + offset[0])
            self.assertEqual(fresh_proc.get((a, "S"), self.read("A12")), "A12")
            self.assertEqual(len(asked), 1)
            self.assertEqual(self.reads, ["A", "B", "AF", "AT", "A8", "AN", "A9", "A11",
                                          "A12"])

    def test_a_tile_kept_on_disk_survives_a_deploy_only_for_the_same_statement_and_door(self):
        import tempfile
        from operator_api.page_store import PageStore
        with tempfile.TemporaryDirectory() as tmp:
            path = __import__("pathlib").Path(tmp)

            def deployed(code):   # a process of one code version: its own memory, the same file
                return self.ic.Kept(lambda: self.stamp[0], fresh=300.0,
                                    disk=PageStore(path, code=code),
                                    disk_code=self.ic.INSIGHT_FORMAT)
            a = self.ic.reader_key(person("employee:a"))
            deployed("code-1").get((a, "S"), self.read("A"))
            new = deployed("code-2")                                 # a deploy
            self.assertEqual(new.get((a, "S"), self.read("A2")), "A")  # not thrown away
            self.assertEqual(new.get((a, "T"), self.read("T")), "T")   # another statement: read
            self.stamp[0] = 8                                          # the door moved
            self.assertEqual(deployed("code-3").get((a, "S"), self.read("A8")), "A8")
            b = self.ic.reader_key(person("employee:b"))
            self.assertEqual(deployed("code-3").get((b, "S"), self.read("B")), "B")   # never a's
            self.stamp[0] = 7
            new.drop((a, "S"))   # a failed re-read drops it for every version
            self.assertEqual(deployed("code-4").get((a, "S"), self.read("A9")), "A9")
            self.assertEqual(self.reads, ["A", "T", "A8", "B", "A9"])

    def test_a_stale_answer_is_served_while_the_keeper_reads_it_again_as_that_reader(self):
        k = self.kept()
        keeper = self.ic.Keeper(k, lambda r, sql: self.reads.append((r.key, sql)) or "new",
                                clock=lambda: self.now[0])
        a = person("employee:a")
        key = (self.ic.reader_key(a), "S")
        keeper.opened(a, "S")
        self.assertEqual(k.get(key, self.read("old")), "old")
        self.now[0] = 400.0                                   # stale, same door
        self.assertEqual(k.get(key, self.read("now")), "old")   # served, read behind it
        keeper.pool().shutdown(wait=True)
        self.assertEqual(self.reads, ["old", ("employee:a", "S")])
        self.assertEqual(k.get(key, self.read("x")), "new")
        # a reader the keeper never saw (or whose session ended): read now, nothing stale
        b = person("employee:b")
        kb = (self.ic.reader_key(b), "S")
        k.get(kb, self.read("b-old"))
        self.now[0] = 800.0
        self.assertEqual(k.get(kb, self.read("b-now")), "b-now")

    def test_a_read_behind_a_stale_tile_that_fails_leaves_no_old_tile(self):
        """A reader whose access narrowed: the read behind the stale tile is refused, and
        the old tile is not served again from memory or the kept file."""
        import tempfile
        from operator_api.page_store import PageStore
        with tempfile.TemporaryDirectory() as tmp:
            store = PageStore(__import__("pathlib").Path(tmp), code="c1")
            k = self.ic.Kept(lambda: self.stamp[0], fresh=300.0, clock=lambda: self.now[0],
                             disk=store)

            def refused(r, sql):
                raise ins.Refused("insight_refused", "x")
            keeper = self.ic.Keeper(k, refused, clock=lambda: self.now[0])
            a = person("employee:a")
            key = (self.ic.reader_key(a), "S")
            keeper.opened(a, "S")
            self.assertEqual(k.get(key, self.read("old")), "old")
            self.now[0] = 400.0
            self.assertEqual(k.get(key, self.read("x")), "old")   # served, read behind it
            keeper.pool().shutdown(wait=True)                      # ... which was refused
            self.assertIsNone(k.age(key))                          # none in memory
            self.assertIsNone(k._from_disk(key, 7))                # nor on disk
            self.assertEqual(k.get(key, self.read("now")), "now")

    def test_listing_reads_ahead_and_the_tile_opens_from_it_as_that_reader(self):
        b = Backend(sql_out(Mixin.rows, OUTPUT), everyone=True)
        b._insight_keep = self.ic.Keeper(self.kept(), lambda r, sql: ins.read_now(b, r, sql))
        owner = person("employee:owner")
        listed = b.insights_list(owner)
        b._insight_keep.pool().shutdown(wait=True)        # the reads ahead are done
        n = len(b.reads.sql)
        self.assertEqual(n, len({s.sql for s in ins.declared() if not ins.reads_findings(s)}))   # each statement once, the measure tiles too
        got = b.insight_result(owner, "team_output")
        self.assertEqual(len(b.reads.sql), n)                       # from the read ahead
        self.assertEqual([r[0] for r in got.rows], ["Me", "Other"])  # Team rule applied now
        self.assertIn("team_output", [i.id for i in listed.items])
        other = person("employee:other")
        b.insight_result(other, "team_output")
        self.assertEqual(len(b.reads.sql), n + 1)                   # never the owner's

    def test_a_tile_whose_fresh_read_cannot_finish_serves_its_last_good_answer(self):
        """11-21 live: six tiles 504 together after 30 s while the business tables were being
        refreshed (the door stamp moved, so nothing kept was current). The reader's own last
        good answer is served with the time it was read; another reader, a refusal about
        access, and an answer older than ``LAST_GOOD_SECONDS`` still get the error."""
        b = Backend(sql_out(Mixin.rows, OUTPUT), everyone=True)
        # the keeper's background re-reads run when the test says (``Later``), not whenever
        # a thread gets to them: which answer a call sees must not depend on that timing
        later = Later()
        b._insight_keep = self.ic.Keeper(self.kept(), lambda r, sql: ins.read_now(b, r, sql),
                                         pool=later)
        owner, other = person("employee:owner"), person("employee:other")
        first = b.insight_result(owner, "team_output")
        self.assertEqual([r[0] for r in first.rows], ["Me", "Other"])
        self.stamp[0] = 8                       # the views refresh moved every reader's stamp

        def refuse(code):
            def run(reader, job, timeout=0, priority=0, long=False):
                if code == "timeout":
                    raise TimeoutError("30 s")
                view = type("V", (), {})()
                view._call = lambda tool, args: {"error": {"code": code, "message": "m"}}
                return job(view)
            return run
        real = b.reads.run
        b.reads.run = refuse("timeout")
        got = b.insight_result(owner, "team_output")
        self.assertEqual(got.rows, first.rows)
        self.assertEqual(got.read_at, first.read_at)           # says when it was read
        for again in (False, True):                             # a person's Refresh too
            self.assertEqual(b.insight_result(owner, "team_output", again=again).rows,
                             first.rows)
        b.reads.run = refuse("not_ready")
        self.assertEqual(b.insight_result(owner, "team_output").rows, first.rows)
        with self.assertRaises(UserFacing) as e:                # never another reader's
            b.insight_result(other, "team_output")
        self.assertEqual(e.exception.code, "insight_not_ready")
        # a grant revoked, a source re-marked or an exclusion added since the read (the
        # access state moved), or a state that cannot be read now: the error stands
        for moved in (("policy-2", False, "excl-1", "marks-1"),       # a grant revoked
                      ("policy-1", False, "excl-1", "marks-2"),       # a source re-marked
                      ("policy-1", False, "excl-2", "marks-1"),       # an exclusion added
                      None, RuntimeError("undecided")):
            with self.subTest(access=moved):
                b.access = moved
                b.reads.run = refuse("timeout")
                with self.assertRaises(UserFacing) as e:
                    b.insight_result(owner, "team_output")
                self.assertEqual(e.exception.code, "insight_too_slow")
        b.access = ("policy-1", False, "excl-1", "marks-1")
        b.reads.run = refuse("timeout")
        self.assertEqual(b.insight_result(owner, "team_output").rows, first.rows)   # same again
        b.reads.run = refuse("not_readable")                    # access refused: no old tile
        later.run()      # the re-read the last call started meets the refusal: the old one goes
        with self.assertRaises(UserFacing) as e:
            b.insight_result(owner, "team_output")
        self.assertEqual(e.exception.code, "insight_withheld")
        b.reads.run = refuse("timeout")
        self.now[0] += self.ic.LAST_GOOD_SECONDS + 1            # too old to stand for a read
        with self.assertRaises(UserFacing) as e:
            b.insight_result(owner, "team_output")
        self.assertEqual(e.exception.code, "insight_too_slow")
        b.reads.run = real

    def test_the_warm_ups_read_ahead_is_kept_where_the_tile_asks_for_it(self):
        # the warm-up keeps each tile under its ref (a built-in by its id), the key the
        # API's own tile request looks under: a statement-keyed read was never found
        b = Backend(sql_out(Mixin.rows, OUTPUT), everyone=True)
        b._insight_keep = self.ic.Keeper(self.kept(), lambda r, what: ins.read_now(
            b, r, next((s.sql for s in ins.builtin() if ins.ref(s) == what), what)))
        owner = person("employee:owner")
        done = b._insights_ahead(owner)
        self.assertEqual(done, len([s for s in ins.declared() if not ins.reads_findings(s)]))
        n = len(b.reads.sql)
        got = b.insight_result(owner, "team_output")
        self.assertEqual(len(b.reads.sql), n)   # served from what the warm-up kept
        self.assertEqual([r[0] for r in got.rows], ["Me", "Other"])

    def test_kept_warm_only_while_the_session_is_valid(self):
        import time as _t
        from concurrent.futures import wait
        k = self.kept()
        keeper = self.ic.Keeper(k, lambda r, sql: self.reads.append((r.key, sql)) or "v",
                                clock=lambda: self.now[0])
        live, gone = person("employee:a"), person("employee:b", expires=_t.time() - 1)
        keeper.opened(live, "S")
        keeper.opened(gone, "S")
        wait(keeper.tick())
        self.assertEqual(self.reads, [("employee:a", "S")])        # the expired one: not read
        wait(keeper.tick())
        self.assertEqual(len(self.reads), 1)                        # fresh: not read again
        self.now[0] = 200.0
        wait(keeper.tick())
        self.assertEqual(len(self.reads), 2)                        # going stale: read ahead
        self.now[0] = 200.0 + 3601
        wait(keeper.tick())
        self.assertEqual(len(self.reads), 2)                        # quiet an hour: forgotten


class StreamedDashboard(unittest.TestCase):
    """``insights_stream``: the whole dashboard in one response (the list, then a line per
    tile), each tile through the same door as its own request."""

    def lines(self, b, who):
        """The stream as a person sees it once the background reads have stored the tiles: the
        first stream computes nothing (every tile is waiting), the reads it started finish,
        the next stream draws the stored values."""
        from operator_api import insights_cache as ic
        if b._insight_keep is None:      # a door stamp that holds (without one nothing is stored)
            b._insight_keep = ic.Keeper(
                ic.Kept(lambda: 7, fresh=300.0),
                lambda r, sql: b._insight_read(r, sql, ic.AHEAD_SECONDS, ahead=True))
        first = list(b.insights_stream(who))
        self.assertTrue(all(t.keys() & {"result", "waiting", "error"} for t in first[1:]))
        keeper = b._insight_keeper()
        keeper.pool().shutdown(wait=True)
        keeper._pool = None
        return list(b.insights_stream(who))

    def test_the_list_comes_first_then_one_line_per_tile_equal_to_its_own_read(self):
        b = Backend(sql_out(Mixin.rows, OUTPUT), everyone=True)
        owner = person("employee:owner")
        got = self.lines(b, owner)
        self.assertIn("list", got[0])
        ids = [i["id"] for i in got[0]["list"]["items"]]
        self.assertGreater(len(ids), 20)
        tiles = got[1:]
        self.assertEqual(sorted(t["id"] for t in tiles), sorted(ids))   # none lost or doubled
        self.assertEqual(len({t["id"] for t in tiles}), len(ids))
        for t in tiles:
            if "result" in t:
                alone = b.insight_result(owner, t["id"]).model_dump(mode="json")
                alone["read_at"] = t["result"]["read_at"]
                self.assertEqual(t["result"], alone)
        team = next(t for t in tiles if t["id"] == "team_output")
        self.assertNotIn("Hidden", json.dumps(team))

    def test_a_view_as_and_a_canary_session_are_streamed_from_their_own_stored_tiles(self):
        """P7: a session that views as someone, and the canary, has a stored key of its own
        (``insights_cache.reader_key``), so its dashboard stream computes nothing in the
        request either: every tile is waiting on the first stream, then drawn from what the
        background read stored under that key; the person's own stored tiles never serve it
        (nor its tiles the person)."""
        from operator_api import insights_cache as ic
        b = Backend(sql_out(Mixin.rows, OUTPUT), everyone=True)
        owner = person("employee:owner")
        viewing, canary = person("employee:owner"), person("employee:owner")
        viewing.session.view_as = True
        canary.session.canary = True
        keys = {ic.reader_key(r) for r in (owner, viewing, canary)}
        self.assertEqual(len(keys), 3)
        self.assertNotIn(None, keys)
        self.lines(b, owner)                      # the person's own tiles are stored
        import threading
        ran = []
        real = b.reads.run

        def run(reader, job, *a, **k):
            ran.append(threading.current_thread().name)
            return real(reader, job, *a, **k)
        b.reads.run = run
        for who in (viewing, canary):
            ran.clear()
            first = list(b.insights_stream(who))
            keeper = b._insight_keeper()
            keeper.pool().shutdown(wait=True)
            keeper._pool = None
            self.assertTrue(ran, "its own reads never ran")
            # every read ran behind the request, in the background pool (never on the thread
            # that answers the stream), and under the session's own key
            self.assertTrue(all(n.startswith("insights-ahead") for n in ran), ran)
            tiles = first[1:]
            self.assertTrue(all(t.keys() & {"result", "waiting"} for t in tiles))
            again = list(b.insights_stream(who))[1:]
            self.assertTrue(any("result" in t for t in again))
            self.assertFalse(any("waiting" in t for t in again))
        b.reads.run = real
        # a session with no principals has no key to keep tiles under: each tile says so,
        # nothing is read for it in the request
        bare = person("employee:owner")
        bare.session.principals = frozenset()
        before = len(b.reads.sql)
        lines = list(b.insights_stream(bare))
        tiles = lines[1:] if "list" in lines[0] else lines
        self.assertTrue(all(t.get("error", {}).get("code") == "insight_unkept" for t in tiles),
                        [sorted(t) for t in tiles][:3])
        self.assertEqual(len(b.reads.sql), before)

    def test_a_staff_reader_is_streamed_only_their_own_tiles_and_their_own_rows(self):
        b = Backend(sql_out(Mixin.rows, OUTPUT), everyone=False, me=frozenset({ME}))
        got = self.lines(b, person("employee:ana"))
        shown = {i["id"] for i in got[0]["list"]["items"]}
        withheld = {s.id for s in ins.builtin() if s.scope == "full" or ins.reads_findings(s)}
        self.assertTrue(withheld)
        self.assertFalse(shown & withheld)
        self.assertEqual({t["id"] for t in got[1:]}, shown)
        mine = next(t for t in got[1:] if t["id"] == "team_output")   # a people view
        self.assertEqual(mine["result"]["rows"], [["Me", "3", "1"]])
        self.assertNotIn(OTHER, json.dumps(mine))

    def test_one_tile_that_cannot_be_read_is_its_own_error_line_never_the_streams(self):
        b = Backend(sql_out(Mixin.rows, OUTPUT), everyone=True)
        real = b.reads.run

        def run(reader, job, timeout=0, priority=0, long=False):
            view = type("V", (), {})()

            def call(tool, args):
                if "mail_sent" in args["sql"] and "teams_sent" in args["sql"]:
                    return {"error": {"code": "not_ready", "message": "rebuilt"}}
                return b.reads.out
            view._call = call
            return job(view)
        b.reads.run = run
        got = self.lines(b, person("employee:owner"))
        failed = [t for t in got[1:] if "error" in t]
        self.assertTrue(failed)
        self.assertTrue(all(t["error"]["code"] == "insight_not_ready" and t["error"]["status"] == 503
                            for t in failed))
        self.assertTrue(any("result" in t for t in got[1:]))
        b.reads.run = real

    def test_an_unexpected_failure_says_nothing_of_its_cause(self):
        b = Backend(sql_out(Mixin.rows, OUTPUT), everyone=True)

        def boom(*a, **k):
            raise RuntimeError("secret /path detail")
        b.reads.run = boom
        with self.assertLogs("operator_api.insights", level="ERROR"):
            got = self.lines(b, person("employee:owner"))
        errs = [t["error"] for t in got[1:] if "error" in t]
        # a background read that failed is said (typed), never "waiting" for ever
        self.assertEqual(len(errs), len(got[1:]))
        self.assertTrue(errs)
        self.assertTrue(all(e["status"] in (500, 503) for e in errs))
        self.assertNotIn("secret", json.dumps(got))


class Saved(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.dsn = pg_testdb.create_db()
        from operating.pg import migrate
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 3})
        cls.memory = mem.Memory.for_dsn(cls.dsn, sync=True)

    @classmethod
    def tearDownClass(cls):
        cls.memory.pool.close()
        pg_testdb.drop_db(cls.db)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DELETE FROM insights.run")
            conn.execute("DELETE FROM insights.insight")
        self.memory.guard = lambda: Guard()
        self.memory.names = lambda: frozenset({"quinn harrow", "quinn", "harrow"})
        self.b = Backend(sql_out([["A", 1, ME]], ("customer", "total", "person_id")),
                         everyone=True, memory=self.memory)

    def body(self, **kw):
        return m.InsightSave(**{"title": "Top customers", "question": "top customers",
                                "sql": "SELECT customer, total FROM sales_orders",
                                "chart": {"kind": "bar", "x": "customer", "y": ["total"]},
                                **kw})

    def test_pinned_reads_again_for_its_owner_only(self):
        ana, ben = reader("ana"), reader("ben")
        saved = self.b.insight_save(ana, self.body())
        self.assertTrue(saved.saved and saved.id.startswith("s"))
        self.assertIn(saved.id, [i.id for i in self.b.insights_list(ana).items])
        self.assertNotIn(saved.id, [i.id for i in self.b.insights_list(ben).items])
        got = self.b.insight_result(ana, saved.id)
        self.assertEqual((got.columns, got.rows), (["customer", "total", "person_id"],
                                                   [["A", "1", ME]]))
        for who in (ben,):
            with self.assertRaises(UserFacing) as e:
                self.b.insight_result(who, saved.id)
            self.assertEqual(e.exception.status, 404)
            self.assertFalse(self.b.insight_delete(who, saved.id))
        self.assertTrue(self.b.insight_delete(ana, saved.id))
        self.assertNotIn(saved.id, [i.id for i in self.b.insights_list(ana).items])

    def test_refused_before_anything_is_read_or_kept(self):
        ana = reader("ana")
        cases = [(self.body(sql="DELETE FROM customers"), "insight_invalid_sql"),
                 (self.body(sql="SELECT 1; SELECT 2"), "insight_invalid_sql"),
                 (self.body(chart={"kind": "bar", "x": "customer", "y": ["margin"]}),
                  "insight_chart")]
        for body, code in cases:
            with self.assertRaises(UserFacing) as e:
                self.b.insight_save(ana, body)
            self.assertEqual(e.exception.code, code)
        viewing = reader("ana")
        viewing.session.view_as = True
        with self.assertRaises(UserFacing) as e:
            self.b.insight_save(viewing, self.body())
        self.assertEqual(e.exception.code, "view_as_refused")
        self.memory.guard = lambda: None   # the exclusion undecidable: nothing is kept
        with self.assertRaises(UserFacing) as e:
            self.b.insight_save(ana, self.body())
        self.assertEqual(e.exception.code, "insight_not_saved")
        self.assertEqual(self.b.reads.sql, ["SELECT customer, total FROM sales_orders"])
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM insights.insight").fetchone(),
                             (0,))


    def test_0046_applied_to_postgres_keeps_tiles_and_runs_without_business_rows(self):
        with psycopg.connect(self.dsn) as conn:
            cols = {t: [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = "
                "'insights' AND table_name = %s ORDER BY ordinal_position", (t,))]
                for t in ("insight", "run")}
        self.assertTrue({"owner", "sql", "chart", "schedule", "notify", "deleted_at"}
                        <= set(cols["insight"]), cols)
        self.assertTrue(cols["run"])

    def test_viewing_as_someone_lists_and_opens_none_of_their_saved(self):
        ana = reader("ana")
        saved = self.b.insight_save(ana, self.body())
        viewing = reader("ana")
        viewing.session.view_as = True
        listed = self.b.insights_list(viewing)
        self.assertFalse(listed.saving)
        self.assertFalse([i for i in listed.items if i.saved])
        with self.assertRaises(UserFacing) as e:
            self.b.insight_result(viewing, saved.id)
        self.assertEqual(e.exception.status, 404)

    def test_a_chart_its_rows_cannot_carry_is_refused_with_the_reason(self):
        self.b.reads.out = sql_out([["A", "CAD", "1"], ["B", "USD", "2"]],
                                   ("customer", "currency", "total"))
        with self.assertRaises(UserFacing) as e:
            self.b.insight_save(reader("ana"), self.body(
                sql="SELECT customer, currency, total FROM customers",
                chart={"kind": "pie", "x": "customer", "y": ["total"]}))
        self.assertEqual(e.exception.code, "insight_chart")
        self.assertIn("more than one currency", e.exception.message)


class Counting(unittest.TestCase):
    """A counting view counts people after the Team rule: a hidden or excluded person is
    in no count and no row."""

    def test_span_of_control_never_counts_or_shows_a_hidden_person(self):
        mgr, boss = "aaaaaaaa-0000-4000-8000-0000000000a1", "aaaaaaaa-0000-4000-8000-0000000000b2"
        for n in (1, 10, 60):
            rows = [["Mgr", "Ops", mgr, f"cccccccc-0000-4000-8000-{i:012d}"] for i in range(n)]
            rows += [["Mgr", "Ops", mgr, HIDDEN], ["Boss", "Exec", boss, mgr],
                     ["Hidden", "X", HIDDEN, f"aaaaaaaa-0000-4000-8000-{999:012d}"]]
            b = Backend(sql_out(rows, ("manager", "department", "person_id", "report_id")),
                        everyone=False, me=frozenset())
            got = b.insight_result(reader("ana"), "org_span")
            self.assertEqual(got.columns, ["manager", "department", "direct_reports"])
            self.assertEqual(got.rows, [["Mgr", "Ops", str(n)], ["Boss", "Exec", "1"]]
                             if n > 1 else [["Mgr", "Ops", "1"], ["Boss", "Exec", "1"]])
            self.assertNotIn("Hidden", got.model_dump_json())
            self.assertEqual(sum(got.drawn.series[0].values), n + 1)

    def test_levels_count_each_visible_person_once(self):
        rows = [["1", ME], ["2", OTHER], ["2", HIDDEN], ["3", ""]]
        b = Backend(sql_out(rows, ("level", "person_id")), everyone=True)
        got = b.insight_result(reader("owner"), "org_depth")
        self.assertEqual(got.rows, [["1", "1"], ["2", "1"]])


class AskChartKeepsItsStatement(unittest.TestCase):
    def test_a_chart_names_the_statement_of_the_result_it_draws(self):
        from operator_api.simple_ask import charts
        sql = "SELECT customer, total FROM sales_orders"
        events = [{"type": "tool_call", "name": "plx.sql", "arguments": {"sql": sql},
                   "is_error": False, "result": json.dumps(sql_out(
                       [["A", 3, ME]], ("customer", "total", "person_id")))}]
        text = ('Top.\n```chart\n{"type": "bar", "title": "T", "result": "sql://r", '
                '"x": "customer", "y": ["total"]}\n```\n')
        shown, drawn = charts(text, events)
        self.assertEqual(shown, "Top.")
        self.assertEqual(drawn[0]["sql"], sql)
        self.assertEqual(drawn[0]["series"], [{"name": "total", "values": [3.0]}])
        m.AskChart(**drawn[0])

    def test_a_measure_chart_keeps_its_statement_and_definition_and_a_forecast_keeps_none(self):
        """A chart of a plx.sql measure result pins (its statement reads again as whoever
        opens it) and says what its numbers mean; a forecast's rows are more than its
        statement reads, so it is not pinned as if they were."""
        from operator_api.simple_ask import charts
        from operating import measures
        sql, entry = measures.statement("revenue", "customer")
        out = sql_out([["A", "CAD", 3, 1, 0, 100]], ("customer", "revenue_currency", "value",
                                                       "row_count", "unread_cells",
                                                       "share_pct"))
        text = ('Top.\n```chart\n{"type": "bar", "title": "T", "result": "sql://r", '
                '"x": "customer", "y": ["value"]}\n```\n')

        def drawn(args, entry):
            # the session's result carries the entry's text as source text (envelopes), as
            # the live host sends it: the chart reads none of it
            wrapped = {k: ({"untrusted_text": v} if isinstance(v, str) else v)
                       for k, v in entry.items()}
            events = [{"type": "tool_call", "name": "plx.sql", "is_error": False,
                       "arguments": args, "result": json.dumps({**out, "measure": wrapped})}]
            return charts(text, events)[1][0]

        got = drawn({"measure": "revenue", "by": "customer"}, entry)
        self.assertEqual(got["sql"], sql)
        self.assertEqual((got["measure"]["id"], got["measure"]["definition"]),
                         ("revenue", entry["definition"]))
        m.AskChart(**got)
        got = drawn({"measure": "revenue", "forecast": 3}, {**entry, "forecast": {}})
        self.assertIsNone(got["sql"])
        self.assertEqual(got["measure"]["id"], "revenue")
        m.AskChart(**got)
        # a {"sql"} call's chart keeps its own statement, whatever its result says
        events = [{"type": "tool_call", "name": "plx.sql", "is_error": False,
                   "arguments": {"sql": "SELECT 1"},
                   "result": json.dumps({**out, "measure": {"id": "x", "definition": "y",
                                                            "sql": "SELECT 2"}})}]
        got = charts(text, events)[1][0]
        self.assertEqual(got["sql"], "SELECT 1")
        self.assertIsNone(got.get("measure"))

class TilesUseTheMeasure(unittest.TestCase):
    def test_the_mail_sent_tiles_count_by_the_measures_own_definition(self):
        from operating import measures
        where = measures.where_sql("mail_sent")
        tiles = {t.id: t.sql for t in ins.builtin()}
        for tile in ("team_output", "team_mail_weekly"):
            self.assertIn(where, tiles[tile], tile)
            self.assertNotIn("{where:", tiles[tile])

    def test_a_tile_over_mail_with_its_own_count_says_so_and_names_the_measure_it_is_not(self):
        """customer_mail counts mail in both directions that the customer links name: not
        the mail_received measure. Its words say that, so two mail totals are never read as one."""
        from operating import measures
        tile = {t.id: t for t in ins.builtin()}["customer_mail"]
        self.assertNotIn(measures.where_sql("mail_received"), tile.sql)
        self.assertIn("not the mail_received measure", tile.about)
        self.assertIn("mail_received", measures.measures())

    def test_every_measure_tile_is_the_catalogues_own_statement(self):
        """A tile named for a measure shows that measure: its statement is the catalogue's
        (value expression and where clause both), never a rewrite, so its total is the one
        Ask, Genie and ``tools/page_truth.py`` give."""
        from operating import measures
        specs = {s.id: s for s in ins.measure_specs()}
        self.assertTrue(specs)
        for name, entry in measures.measures().items():
            tile = specs.get(f"{ins.MEASURE_TILE}{name}")
            if tile is None:
                continue                       # one the checker refuses is left out, never altered
            with self.subTest(name):
                want, _ = measures.statement(name, by="month" if entry.get("date") else None)
                self.assertEqual(tile.sql, want)
                if entry.get("where"):
                    self.assertIn(measures.where_sql(name), tile.sql)
        # a declared tile never stands in for a measure's tile id with another statement
        for t in ins.builtin():
            if t.id.startswith(ins.MEASURE_TILE):
                self.assertNotIn(t.id.removeprefix(ins.MEASURE_TILE), measures.measures(), t.id)



if __name__ == "__main__":
    unittest.main()


ANA, BEN = "aaaaaaaa-0000-4000-8000-00000000000a", "aaaaaaaa-0000-4000-8000-00000000000b"
FAST_SQL = ("SELECT s.name AS person, count(DISTINCT e.message_id) AS mail_sent, s.person_id "
            "FROM emails e JOIN staff s ON s.email = e.sender "
            "WHERE e.received_at >= now() - interval '28 days' "
            "GROUP BY s.name, s.person_id ORDER BY 1")


def fast_spec(sql=FAST_SQL, sid="fx", person="person_id", scope="self", **kw):
    return ins.Spec(id=sid, title="t", about="a", sql=sql, person=person, scope=scope,
                    fast=True, **kw)


class FastServes(unittest.TestCase):
    """insights_fast.serves: who is answered from the shared copies (everyone else reads
    through their own door)."""

    def owner(self):
        from operating.employee_permissions import FULL_READ_SCOPES
        r = reader("owner")
        r.session.access = "full_read:" + "+".join(sorted(FULL_READ_SCOPES)) + ":mail_all"
        return r

    def test_who_is_served_from_the_copies(self):
        from operator_api import insights_fast as f
        staff, owner = reader("ana"), self.owner()
        own = fast_spec()
        org = fast_spec(sql="SELECT name, person_id FROM staff", scope="org")
        company = fast_spec(person="", scope="full", sql=(
            "SELECT count(*) AS n FROM emails WHERE received_at >= now() - interval '7 days'"))
        self.assertTrue(f.serves(own, staff, False))       # their own row, after the Team rule
        self.assertFalse(f.serves(org, staff, False))      # org rows: their own door
        self.assertTrue(f.serves(org, staff, True))        # a reader shown everyone
        self.assertFalse(f.serves(company, staff, True))   # company-wide: full read only
        self.assertTrue(f.serves(company, owner, True))

    def test_a_statement_the_copies_cannot_answer_is_never_fast(self):
        from operator_api import insights_fast as f
        cases = [
            fast_spec(sql="SELECT s.person_id, sum(i.amount) AS n FROM sales_invoices i JOIN "
                          "staff s ON s.name = i.customer GROUP BY 1"),       # a record view
            fast_spec(sql="SELECT sender AS person_id, count(*) AS n FROM emails GROUP BY 1"),
            fast_spec(sql=FAST_SQL.replace("28 days", "200 days")),         # past the copy
            ins.Spec(**{**fast_spec().__dict__, "saved": True}),           # a saved one
            ins.Spec(**{**fast_spec().__dict__, "fast": False}),
        ]
        for spec in cases:
            with self.subTest(sql=spec.sql[:60]):
                self.assertFalse(f.eligible(spec))
                self.assertFalse(f.serves(spec, self.owner(), True))
        self.assertTrue(f.eligible(fast_spec()))
        for spec in ins.builtin():   # every built-in declared fast fits the copies
            if spec.fast:
                self.assertTrue(f.eligible(spec), spec.id)

    def test_a_saved_statement_with_a_built_ins_text_is_read_as_its_reader(self):
        spec = next(s for s in ins.builtin() if s.fast and s.person)
        made = []

        class Maker:
            def get(self, s):
                made.append(s.id)
                return {"got": sql_out([], OUTPUT), "read_at": "2026-11-19T00:00:00+00:00"}

        class B(Backend):
            def _insight_fast(self):
                return Maker()

        b = B(sql_out([], OUTPUT), everyone=False, me=frozenset({ME}))
        b._insight_read(reader("ana"), ins.ref(spec))
        self.assertEqual((made, b.reads.sql), ([spec.id], []))
        saved = ins.Spec(**{**spec.__dict__, "id": "s9", "saved": True})
        b._insight_read(reader("ana"), ins.ref(saved))   # same text, saved: their door
        self.assertEqual((made, b.reads.sql), ([spec.id], [spec.sql]))


@unittest.skipIf(psycopg is None, "psycopg not installed")
class FastCopies(unittest.TestCase):
    """insights_fast.Fast on Postgres: the copies keep only the rows the comms door gives
    any reader, follow the people exclusion's stamp, and fail one statement alone."""

    @classmethod
    def setUpClass(cls):
        from operating.pg import business_views as bv
        cls.schema = bv.SCHEMA
        cls.db, cls.dsn = pg_testdb.create_db()

    @classmethod
    def tearDownClass(cls):
        pg_testdb.drop_db(cls.db)

    def setUp(self):
        s = self.schema
        with psycopg.connect(self.dsn) as pg:
            pg.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE; DROP SCHEMA IF EXISTS ontology "
                       f"CASCADE; CREATE SCHEMA {s}; CREATE SCHEMA ontology")
            pg.execute(f"CREATE TABLE {s}.emails (message_id text, sender text, mailbox text, "
                       "received_at timestamptz, handle text, object_ids text[], ver bigint, "
                       "excluded boolean, ai_ok boolean, unit integer)")
            pg.execute(f"CREATE TABLE {s}.staff (name text, email text, person_id text, "
                       "handle text)")
            pg.execute(f"CREATE TABLE {s}.state (view text, built_at text, rows bigint)")
            pg.execute(f"INSERT INTO {s}.state VALUES ('emails', '2026-11-19T05:00:00+00:00', "
                       "10), ('staff', '2026-11-19T04:00:00+00:00', 2)")
            pg.execute("CREATE TABLE ontology.people_excluded_state (exclusion text, built_at "
                       "text, updated_at text, covered_ver bigint)")
            pg.execute("CREATE TABLE ontology.people_excluded_objects (object_id text)")
            pg.execute("INSERT INTO ontology.people_excluded_state VALUES ('ids:x', 'b', 'u1', "
                       "100)")
            pg.execute(f"INSERT INTO {s}.staff VALUES ('Ana', 'ana@plx.example', '{ANA}', "
                       f"'h:ana'), ('Ben', 'ben@plx.example', '{BEN}', 'h:ben')")

    def mail(self, mid, sender, days=1, ver=1, excluded=False, ai_ok=True, handle="h",
             obj=None, mailbox="mb"):
        with psycopg.connect(self.dsn) as pg:
            pg.execute(f"INSERT INTO {self.schema}.emails VALUES (%s, %s, %s, now() - "
                       "make_interval(days => %s), %s, %s, %s, %s, %s, 1)",
                       (mid, sender, mailbox, days, handle and f"{handle}:{mid}:{mailbox}",
                        [obj or f"o:{mid}"], ver, excluded, ai_ok))

    def maker(self, specs, clock=None, role=None):
        from operator_api import insights_fast as f

        def connect():
            conn = psycopg.connect(self.dsn)
            if role:
                conn.execute(f"SET ROLE {role}")
                conn.commit()
            return conn
        return f.Fast(connect, lambda: specs, **({"clock": clock} if clock else {}),
                      fresh=300.0)

    def counts(self, maker, spec):
        got = maker.get(spec)["got"]
        return {r["cells"][0]: r["cells"][1] for r in got["rows"]}

    def test_only_rows_the_door_gives_any_reader_are_counted(self):
        a = "ana@plx.example"
        self.mail("m1", a)
        self.mail("m1", a, mailbox="other")       # a second mailbox's copy: counted once
        self.mail("m2", a, excluded=True)         # in the people exclusion's set at build
        self.mail("m3", a, ai_ok=False)           # names Freightcom
        self.mail("m4", a, ai_ok=None)            # Freightcom not decided yet
        self.mail("m5", a, ver=101)               # a version the exclusion has not decided
        self.mail("m6", a, obj="o:gone")          # its object excluded since the build
        self.mail("m7", a, handle=None)
        self.mail("m8", a, days=40)               # outside the statement's 28 days
        self.mail("m9", a, days=150)              # outside the copied window
        self.mail("b1", "ben@plx.example")
        with psycopg.connect(self.dsn) as pg:
            pg.execute("INSERT INTO ontology.people_excluded_objects VALUES ('o:gone')")
        spec = fast_spec()
        got = self.maker([spec]).get(spec)
        self.assertEqual({r["cells"][0]: r["cells"][1:] for r in got["got"]["rows"]},
                         {"Ana": ["1", ANA], "Ben": ["1", BEN]})
        self.assertEqual(got["got"]["as_of"], "2026-11-19T04:00:00+00:00")   # oldest read
        # the Team rule then keeps a staff reader's own row only
        b = Backend(None, everyone=False, me=frozenset({ANA}))
        shown = ins.result(b, reader("ana"), spec, b.scope, got)
        self.assertEqual(shown.rows, [["Ana", "1"]])

    def test_one_row_per_person_at_any_size(self):
        import time
        for n in (1, 10, 60):
            with self.subTest(n=n), psycopg.connect(self.dsn) as pg:
                pg.execute(f"TRUNCATE {self.schema}.staff, {self.schema}.emails")
                for i in range(n):
                    pg.execute(f"INSERT INTO {self.schema}.staff VALUES (%s, %s, %s, 'h')",
                               (f"P{i:02d}", f"p{i}@plx.example", f"id{i}"))
                    for k in range(i % 3 + 1):
                        pg.execute(f"INSERT INTO {self.schema}.emails VALUES (%s, %s, 'mb', "
                                   "now() - interval '2 days', 'h', ARRAY['o'], 1, false, "
                                   "true, 1)", (f"m{i}.{k}", f"p{i}@plx.example"))
                pg.commit()
                spec = fast_spec()
                started = time.monotonic()
                got = self.counts(self.maker([spec]), spec)
                self.assertLess(time.monotonic() - started, 5.0)
                self.assertEqual(got, {f"P{i:02d}": str(i % 3 + 1) for i in range(n)})

    def test_a_moved_exclusion_is_made_again_and_none_serves_nothing(self):
        from operator_api import insights_fast as f
        self.mail("m1", "ana@plx.example")
        spec, now = fast_spec(), [0.0]
        maker = self.maker([spec], clock=lambda: now[0])
        self.assertEqual(self.counts(maker, spec), {"Ana": "1"})
        self.mail("m2", "ana@plx.example")
        self.mail("m3", "ana@plx.example")
        self.assertEqual(self.counts(maker, spec), {"Ana": "1"})   # kept: same stamp, fresh
        with psycopg.connect(self.dsn) as pg:    # the set grew by m1's object
            pg.execute("INSERT INTO ontology.people_excluded_objects VALUES ('o:m1')")
            pg.execute("UPDATE ontology.people_excluded_state SET updated_at = 'u2'")
        self.assertEqual(self.counts(maker, spec), {"Ana": "2"})   # m2, m3; never m1 again
        self.mail("m4", "ana@plx.example")
        self.assertEqual(self.counts(maker, spec), {"Ana": "2"})
        now[0] = 400.0                           # older than fresh: made again
        self.assertEqual(self.counts(maker, spec), {"Ana": "3"})
        with psycopg.connect(self.dsn) as pg:
            pg.execute("DELETE FROM ontology.people_excluded_state")
        with self.assertRaises(f.Unserved):
            maker.get(spec)

    def test_a_failing_statement_fails_alone_and_says_what_is_missing(self):
        self.mail("m1", "ana@plx.example")
        bad = fast_spec(sid="bad", sql=FAST_SQL.replace("s.person_id", "s.no_such_col"))
        good = fast_spec()
        maker = self.maker([bad, good])
        with self.assertRaises(UserFacing) as e:
            maker.get(bad)
        self.assertEqual(e.exception.code, "insight_gone")
        self.assertIn("no_such_col", e.exception.message)
        self.assertEqual(self.counts(maker, good), {"Ana": "1"})

    def test_kept_ready_ahead_of_the_next_open_only_while_someone_looks(self):
        from operator_api import insights_fast as f
        self.mail("m1", "ana@plx.example")
        spec, now = fast_spec(), [0.0]
        maker = self.maker([spec], clock=lambda: now[0])
        maker.tick()                      # nobody opened one: nothing is made
        self.assertIsNone(maker._made)
        self.assertEqual(self.counts(maker, spec), {"Ana": "1"})
        self.mail("m2", "ana@plx.example")
        now[0] = 250.0                    # near fresh: the tick makes it again
        maker.tick()
        self.assertEqual(maker._made[1], 250.0)
        self.assertEqual(self.counts(maker, spec), {"Ana": "2"})
        now[0] = 250.0 + f.WARM_SECONDS + 1   # nobody looked for an hour: left alone
        maker.tick()
        self.assertEqual(maker._made[1], 250.0)

    def test_made_twice_on_one_connection_inside_an_open_transaction(self):
        from operator_api import insights_fast as f
        self.mail("m1", "ana@plx.example")
        with psycopg.connect(self.dsn) as pg:
            stamp = f.exclusion_stamp(pg)            # opens a transaction on pg
            for _ in range(2):
                got = f.compute(pg, [fast_spec()], int(stamp[3]))
                self.assertEqual(len(got["fx"]["got"]["rows"]), 1)

    def findings_fixture(self):
        """Readings with findings over the comms tables (the door cases); the spec counting
        them by kind."""
        s = self.schema
        with psycopg.connect(self.dsn) as pg:
            pg.execute(f"ALTER TABLE {s}.emails ADD COLUMN subject text, ADD COLUMN thread "
                       "text, ADD COLUMN customers text[], ADD COLUMN suppliers text[]")
            from operating import assist_readings as ar
            from operating.pg import comms_findings as cf
            for stmt in ar.ddl() + cf.feed_ddl():
                pg.execute(stmt)
            pg.execute("TRUNCATE assist.readings")
            pg.execute(f"CREATE TABLE {s}.teams_messages (created_at timestamptz, sender "
                       "text, chat text, subject text, customers text[], suppliers text[], "
                       "handle text, object_ids text[], ver bigint, excluded boolean, ai_ok "
                       "boolean)")
        a = "ana@plx.example"
        self.mail("m1", a)
        self.mail("m2", a, excluded=True)       # its message is in the exclusion's set
        self.mail("m3", a, ai_ok=False)         # names Freightcom
        self.mail("m4", a, obj="o:gone")        # excluded since the build
        with psycopg.connect(self.dsn) as pg:
            pg.execute("INSERT INTO ontology.people_excluded_objects VALUES ('o:gone')")
            # one reading per message (assist.readings), its findings as items
            for n, (kind, days, handles) in enumerate((
                    ("complaint", 3, ["h:m1:mb"]), ("complaint", 3, ["h:m2:mb"]),
                    ("complaint", 3, ["h:m3:mb"]), ("complaint", 3, ["h:m4:mb"]),
                    ("praise", 3, ["h:none:mb"]),         # no copy at all
                    ("complaint", 150, ["h:m1:mb"]))):    # outside the copied window
                pg.execute("INSERT INTO assist.readings (source, message_key, handles, said_at, "
                           "read_ver, status, from_side, findings) VALUES ('emails', %s, %s, "
                           "now() - make_interval(days => %s), 1, 'ok', 'customer', "
                           "jsonb_build_array(jsonb_build_object('kind', %s::text, 'topic', "
                           "'delivery', 'summary', 'x', 'who', '')))",
                           (f"k{n}", handles, days, kind))
        spec = fast_spec(person="", scope="full", sid="fb", sql=(
            "SELECT kind, count(*) AS n FROM comms_findings WHERE said_at >= now() - "
            "interval '90 days' GROUP BY kind"))
        return spec

    def test_findings_are_counted_only_with_a_copy_of_their_message_the_door_gives_any_reader(self):
        from operator_api import insights_fast as f
        s = self.schema
        spec = self.findings_fixture()
        self.assertTrue(f.eligible(ins.Spec(**{**spec.__dict__, "fast": True})))
        # a table it reads is missing (no Teams messages table): never answered here
        with psycopg.connect(self.dsn) as pg:
            pg.execute(f"ALTER TABLE {s}.teams_messages RENAME TO teams_gone")
        with self.assertRaises(f.Unserved):
            self.maker([spec]).get(spec)
        with psycopg.connect(self.dsn) as pg:
            pg.execute(f"ALTER TABLE {s}.teams_gone RENAME TO teams_messages")
        self.assertEqual(self.counts(self.maker([spec]), spec), {"complaint": "1"})
        # a message hidden from every reader (PLX's own documents) is not counted, and the
        # copies are made again when that set changes (the stamp holds it)
        from unittest import mock
        maker = self.maker([spec])
        before = maker.stamp()
        with mock.patch("operating.search.document_marks._own_issued",
                        return_value=frozenset({"o:m1"})):
            self.assertNotEqual(maker.stamp(), before)
            self.assertEqual(self.counts(self.maker([spec]), spec), {})
        # a finding is never a person's own row: a people view of it is never fast
        mine = fast_spec(sql="SELECT who AS person_id, count(*) AS n FROM comms_findings "
                             "WHERE said_at >= now() - interval '30 days' GROUP BY 1")
        self.assertFalse(f.eligible(mine))



class MeasureWatch(unittest.TestCase):
    """A monitor over one catalogue measure, built without reading any business record."""

    def test_a_dated_measure_is_watched_for_unusual_by_default_with_its_why(self):
        from operating.ontology_tools_sql import check
        for name in ("mail_received", "Mail received from outside", "units_sold"):
            spec = ins.measure_watch(name, days=30 if name == "units_sold" else None)
            self.assertEqual((spec.notify, spec.watch),
                             ("crosses", {"column": "unusual", "op": "above", "value": 2.0}))
            self.assertTrue(spec.question.startswith("Why is"), spec.question)
            self.assertIn("periods_before >= 4", spec.sql)
            check(spec.sql)
        self.assertIn("last 30 days", ins.measure_watch("units_sold", days=30).question)

    def test_a_move_in_percent_watches_the_size_of_the_move_either_way(self):
        from operating.ontology_tools_sql import check
        spec = ins.measure_watch("units_sold", days=30, move_pct=25)
        self.assertEqual(spec.watch, {"column": "move_pct", "op": "above", "value": 25.0})
        self.assertIn("abs(100.0 * m.change / nullif(m.previous, 0))", spec.sql)
        check(spec.sql)
        self.assertEqual(ins.measure_watch("units_sold", move_pct=None, days=None).watch["column"],
                         "unusual")

    def test_a_snapshot_measure_is_watched_only_against_a_number(self):
        from operating.ontology_tools_sql import check
        spec = ins.measure_watch("late_orders", above=50)
        self.assertEqual(spec.watch, {"column": "value", "op": "above", "value": 50.0})
        self.assertIn("above 50", spec.question)
        check(spec.sql)
        self.assertEqual(ins.measure_watch("late_orders", below=3).watch["op"], "below")
        for kw in ({}, {"move_pct": 20}, {"days": 7}):
            with self.assertRaises(ins.Refused, msg=kw) as e:
                ins.measure_watch("late_orders", **kw)
            self.assertEqual(e.exception.code, "insight_watch")

    def test_a_name_period_or_line_the_measure_does_not_hold_is_refused(self):
        for args in [("no_such_measure",), (None,), ("units_sold", 0), ("units_sold", 4000),
                     ("units_sold", True)]:
            with self.assertRaises(ins.Refused, msg=args):
                ins.measure_watch(*args)
        for kw in ({"above": 1, "below": 2}, {"above": 1, "move_pct": 5}, {"above": 1, "days": 7},
                   {"above": float("nan")}, {"above": "50"}, {"move_pct": 0},
                   {"move_pct": True}, {"move_pct": 2000}):
            with self.assertRaises(ins.Refused, msg=kw):
                ins.measure_watch("units_sold", **kw)


@unittest.skipUnless(psycopg, "psycopg not installed")
class UnusualStatement(unittest.TestCase):
    """``unusual_sql`` over a small table: rolling 7-day periods, judged per part."""

    @classmethod
    def setUpClass(cls):
        cls.db, cls.dsn = pg_testdb.create_db()

    @classmethod
    def tearDownClass(cls):
        pg_testdb.drop_db(cls.db)

    def rows(self, counts):
        """``counts``: {part: [rows in the latest period, the one before, ...]} (None: no
        record in that period)."""
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DROP TABLE IF EXISTS t")
            conn.execute("CREATE TABLE t (at timestamptz, part text)")
            for part, per in counts.items():
                for ago, n in enumerate(per):
                    for _ in range(n or 0):
                        conn.execute("INSERT INTO t VALUES (now() - make_interval(days => "
                                     "%s), %s)", (1 + 7 * ago, part))
            entry = {"per": "part", "date": "at", "where": "", "view": "t",
                     "value": "count(*)"}
            cur = conn.execute(ins.unusual_sql(entry, 7) + " ORDER BY part")
            cols = [d.name for d in cur.description]
            return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

    def test_a_rate_measure_is_watched_on_its_rate_not_on_what_it_is_a_rate_of(self):
        """A rate (``rate_where``: the share of its rows meeting it): each period's value is
        the percentage, never the row count it divides by."""
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DROP TABLE IF EXISTS r")
            conn.execute("CREATE TABLE r (at timestamptz, part text, bad boolean)")
            for ago, (n, bad) in enumerate([(10, 5), (10, 1), (20, 2), (10, 1), (30, 3)]):
                for i in range(n):
                    conn.execute("INSERT INTO r VALUES (now() - make_interval(days => %s), "
                                 "'x', %s)", (1 + 7 * ago, i < bad))
            entry = {"per": "", "date": "at", "where": "", "view": "r",
                     "value": "count(*)", "rate_where": "bad"}
            cur = conn.execute(ins.unusual_sql(entry, 7))
            row = dict(zip([d.name for d in cur.description], cur.fetchone()))
        self.assertEqual(float(row["current"]), 50.0)
        self.assertEqual(float(row["usual"]), 10.0)
        self.assertEqual(int(row["periods_before"]), 4)

    def test_the_latest_period_is_judged_against_the_spread_of_the_earlier_ones(self):
        got = self.rows({"a": [10, 5, 6, 5, 4, 6], "b": [5, 5, 6, 5, 4, 6]})
        a, b = got["a"], got["b"]
        self.assertEqual((a["current"], a["periods_before"], float(a["usual"])), (10, 5, 5.2))
        self.assertEqual(float(a["lowest"]), 4)
        self.assertEqual(float(a["highest"]), 6)
        self.assertGreater(float(a["unusual"]), 2.0)                # 4.8 / 0.84
        self.assertLess(float(b["unusual"]), 2.0)

    def test_a_part_with_short_coverage_or_no_spread_is_not_judged(self):
        got = self.rows({"new": [9, 1, 2, None, 3, None, None, None],   # 3 earlier periods
                         "flat": [9, 2, 2, 2, 2, 2],                     # no spread
                         "gap": [None, 2, 3, 2, 4, 3]})                  # nothing this period
        self.assertIsNone(got["new"]["unusual"])
        self.assertEqual(got["new"]["periods_before"], 3)
        self.assertIsNone(got["flat"]["unusual"])
        self.assertIsNotNone(got["gap"]["unusual"])                 # 0 this period: judged


@unittest.skipUnless(psycopg, "psycopg not installed")
class WatchTool(unittest.TestCase):
    """plx.watch_measure: the Ask's tool that saves a monitor as the person, for them."""

    @classmethod
    def setUpClass(cls):
        cls.db, cls.dsn = pg_testdb.create_db()
        from operating.pg import migrate
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 3})
        cls.memory = mem.Memory.for_dsn(cls.dsn, sync=True)

    @classmethod
    def tearDownClass(cls):
        cls.memory.pool.close()
        pg_testdb.drop_db(cls.db)

    def setUp(self):
        from operator_api.assistant_tools import AssistantToolsMixin
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DELETE FROM insights.run")
            conn.execute("DELETE FROM insights.insight")
        self.memory.guard = lambda: Guard()
        self.memory.names = lambda: frozenset({"quinn harrow", "quinn", "harrow"})
        self.b = type("TB", (AssistantToolsMixin, Backend), {})(
            sql_out([["A", 1, ME]]), everyone=True, memory=self.memory)

    def tools(self, who):
        return {t.spec["name"]: t for t in self.b.assistant_tools(who)}

    def saved(self):
        with psycopg.connect(self.dsn) as conn:
            return conn.execute("SELECT owner, title, question, sql, schedule, notify, watch "
                                "FROM insights.insight ORDER BY id").fetchall()

    def test_offered_only_in_the_person_s_own_session(self):
        self.assertIn("plx.watch_measure", self.tools(reader("ana")))
        viewing, canary = reader("ana"), reader("ana")
        viewing.session.view_as = True
        canary.session.canary = True
        self.assertEqual(self.b.assistant_tools(viewing), [])
        self.assertEqual(self.b.assistant_tools(canary), [])

    def test_it_saves_a_scheduled_monitor_as_the_person_and_reads_no_figure(self):
        ana, ben = reader("ana"), reader("ben")
        out = self.tools(ana)["plx.watch_measure"].call({"measure": "mail_received", "days": 30})
        self.assertNotIn("not_kept", out)
        self.assertEqual(out["runs"], "every morning")
        spec = ins.measure_watch("mail_received", days=30)
        (owner, title, question, sql, schedule, notify, watch), = self.saved()
        self.assertEqual((owner, title, question, sql), (ana.key, spec.title, spec.question,
                                                         spec.sql))
        self.assertEqual((schedule, notify, watch), ("daily", "crosses", spec.watch))
        self.assertTrue(out["open"].endswith(f"insight-s{self.b.insight_store().list(ana.key)[0].id[1:]}"))
        self.assertEqual(self.b.reads.sql, [])          # nothing read: no business figure kept
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM insights.run").fetchone()[0], 0)
        self.assertEqual(self.b.insight_store().list(ben.key), [])

    MONTHS = [["2026-09", "USD", "350992.64"], ["2026-10", "USD", "688513.34"],
              ["2026-11", "USD", "164001.36"], ["2026-09", "CAD", "472011.04"],
              ["2026-10", "CAD", "637051.47"], ["2026-11", "CAD", "428191.22"]]

    def pinned(self, who, rows=None):
        self.b.reads = FakeWorker(sql_out(rows or self.MONTHS,
                                          ("month", "revenue_currency", "revenue")))
        return self.b.insight_store().save(who, ins.Spec(
            id="", title="Revenue by month", about="", question="Why did revenue move?",
            sql="SELECT month, revenue_currency, revenue FROM sales_invoice_lines",
            chart={"kind": "line", "x": "month", "y": ["revenue"],
                   "facet": "revenue_currency"})).id

    def test_a_pinned_chart_is_watched_from_words_read_back_first_and_kept_only_on_yes(self):
        ana = reader("ana")
        sid = self.pinned(ana)
        tool = self.tools(ana)["plx.watch_measure"]
        shape = tool.call({"insight": sid})
        self.assertEqual((shape["axis"], shape["columns"], shape["currency_columns"]),
                         ("month", ["month", "revenue_currency", "revenue"],
                          ["revenue_currency"]))
        line = {"insight": sid, "column": "revenue", "op": "below", "value": 400000}
        # per currency: a line in one currency is never set against the other
        self.assertIn("more than one currency", tool.call(line)["not_kept"])
        # a chart along time is never watched on any row: earlier months would fire
        self.assertIn("along time", tool.call({**line, "row": "any", "where": {
            "column": "revenue_currency", "value": "USD"}})["not_kept"])
        usd = {**line, "where": {"column": "revenue_currency", "value": "USD"}}
        back = tool.call(usd)
        self.assertFalse(back["kept"])
        self.assertEqual(back["read_back"], 'On "Revenue by month", tell you on Home when the '
                         'latest revenue in USD (along month) goes below 400,000; checked '
                         'every morning.')
        self.assertEqual(back["now"], [{"at": "2026-11", "currency": "USD", "value": "164001.36",
                                        "past_the_line_now": True}])
        self.assertEqual([r[4:6] for r in self.saved()], [("", "never")])   # nothing set yet
        # the save is the server's: confirmed alone, another line's, another person's, another
        # chart's or a stale token keeps nothing
        token = back["token"]
        other = self.pinned(ana)
        stale = self.b._watch_token(ana.key, sid, "daily", ins.watch_of(
            {"column": "revenue", "op": "below", "value": 400000, "row": "latest",
             "where": usd["where"], "order": "month"}), now=0)
        for bad in ({**usd, "confirmed": True}, {**usd, "confirmed": True, "token": "x"},
                    {**usd, "value": 1, "confirmed": True, "token": token},
                    {**usd, "schedule": "hourly", "confirmed": True, "token": token},
                    {**usd, "insight": other, "confirmed": True, "token": token},
                    {**usd, "confirmed": True, "token": stale}):
            self.assertIn("Nothing was set", tool.call(bad)["not_kept"], bad)
        self.assertIn("not_kept", self.tools(reader("ben"))["plx.watch_measure"].call(
            {**usd, "confirmed": True, "token": token}))
        self.assertEqual([r[4:6] for r in self.saved()], [("", "never")] * 2)
        kept = tool.call({**usd, "confirmed": True, "token": token})
        self.assertEqual(kept["watching"], back["read_back"])
        (_o, _t, _q, _s, schedule, notify, watch), _other = self.saved()
        self.assertEqual((schedule, notify, watch), ("daily", "crosses", {
            "column": "revenue", "op": "below", "value": 400000.0, "row": "latest",
            "where": {"column": "revenue_currency", "value": "USD"}, "order": "month"}))

    def test_a_chart_watch_refuses_what_it_cannot_read_and_keeps_nothing(self):
        ana, ben = reader("ana"), reader("ben")
        sid = self.pinned(ana)
        tool = self.tools(ben)["plx.watch_measure"]
        self.assertIn("not one of their pinned charts", tool.call({"insight": sid})["not_kept"])
        mine = self.tools(ana)["plx.watch_measure"]
        usd = {"column": "revenue_currency", "value": "USD"}
        for args in ({"insight": sid, "measure": "revenue"},
                     {"insight": sid, "column": "revenue", "op": "near", "value": 1},
                     {"insight": sid, "column": "margin", "op": "above", "value": 1,
                      "where": usd},
                     {"insight": sid, "column": "revenue", "op": "above", "value": 1,
                      "row": "total"},
                     {"insight": sid, "column": "revenue", "op": "above", "value": 1,
                      "where": {"column": "region", "value": "USD"}, "confirmed": True}):
            self.assertIn("not_kept", mine.call(args), args)
        self.assertEqual([r[4:6] for r in self.saved()], [("", "never")])
        # a total of one currency is a number to watch
        total = mine.call({"insight": sid, "column": "revenue", "op": "above", "value": 1e6,
                           "row": "total", "where": usd})
        self.assertEqual(total["now"][0]["value"], "1203507.34")

    def test_a_refusal_says_why_and_keeps_nothing(self):
        tool = self.tools(reader("ana"))["plx.watch_measure"]
        for args in ({"measure": "late_orders"}, {"measure": "nope"},
                     {"measure": "units_sold", "schedule": "minutely"},
                     {"measure": "units_sold", "above": 1, "below": 2}):
            self.assertIn("not_kept", tool.call(args), args)
        self.assertEqual(self.saved(), [])
        viewing = reader("ana")
        viewing.session.view_as = True
        late = self.b._watch_call(viewing, {"measure": "units_sold"})
        self.assertIn("not_kept", late)               # a view-as call that got through anyway
        self.assertEqual(self.saved(), [])
