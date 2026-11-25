"""Model-free truth for the catalogue's measures over records (``source``) and the period
shapes on their declared dates: each statement the measure makes, run on a database that
holds the business tables (the standby), against a hand-written statement over the raw
records. Prints n of m and exits non-zero on any difference.

    python3 evals/genie_source_truth.py --dsn "host=/run/plx-standby-pg dbname=plx user=plx_api"

Not covered: the door (a reader's access classes, ``records`` guard); it reads ``biz.records``
with the declared day joined as the door does (``operating.source_health.RECORDS``).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from operating import measures as M
from operating import source_health as H


def run(conn, name: str, **kw):
    m = M.measures()[name]
    sql, _cite = M.statement(name, unchecked=True, **kw)
    sql = sql.replace(M.from_of(m), M.from_of(m).replace("FROM records r", f"FROM {H.RECORDS} r"))
    cur = conn.execute(sql)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def hand(conn, sql: str):
    return conn.execute(sql).fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    args = ap.parse_args(argv)
    today = M.business_today()
    y = today.year
    ok, bad = 0, []

    def check(label, got, want):
        nonlocal ok
        same = (got == want)
        if same:
            ok += 1
        else:
            bad.append((label, got, want))
        print(("ok   " if same else "DIFF ") + label, "" if same else f"got {got} want {want}")

    with psycopg.connect(args.dsn, autocommit=True) as c:
        c.execute("SET default_transaction_read_only = on")
        c.execute("SET search_path = biz, public")
        day = f"COALESCE(d.day, x.record_date)"
        gl = (f"FROM biz.records x LEFT JOIN biz.record_dates d ON d.type = x.type AND d.rid = x.rid "
              f"WHERE x.type = 'RelSageGlPjd' AND x.fields->>'srcetype' <> 'CL'")
        amt = "(x.fields->>'transamt')::numeric"
        # the headline: account 41900310001 this year
        got = run(c, "ledger_posted", by="account", period="year_to_date", filter={"account": "41900310001"})
        want = hand(c, f"SELECT count(*), sum({amt}) {gl} AND btrim(x.fields->>'acctid') = '41900310001' "
                       f"AND {day} >= '{y}-01-01' AND {day} <= '{today}'")[0]
        check("ledger 41900310001 year to date: rows", got[0]["row_count"], want[0])
        check("ledger 41900310001 year to date: sum", got[0]["value"], want[1])
        check("ledger 41900310001 reads 4,926,129", int(got[0]["value"]), 4926129)
        # closing entries counted in when asked
        with_cl = run(c, "ledger_posted", period="year_to_date", parameters={"closing_entries": True})
        wcl = hand(c, f"SELECT sum({amt}) FROM biz.records x LEFT JOIN biz.record_dates d ON d.type = x.type AND d.rid = x.rid "
                      f"WHERE x.type='RelSageGlPjd' AND {day} >= '{y}-01-01' AND {day} <= '{today}'")[0][0]
        check("ledger year to date with closing entries", with_cl[0]["value"], wcl)
        # a. same period last year (to-date aligned): current and previous
        cmp_ = run(c, "ledger_posted", period="year_to_date", compare=True, against="same_period_last_year")
        gone = (today - dt.date(y, 1, 1)).days
        last = dt.date(y - 1, 1, 1)
        prev = hand(c, f"SELECT sum({amt}) FROM biz.records x LEFT JOIN biz.record_dates d ON d.type = x.type AND d.rid = x.rid "
                       f"WHERE x.type='RelSageGlPjd' AND x.fields->>'srcetype' <> 'CL' AND {day} >= '{last}' "
                       f"AND {day} <= '{last + dt.timedelta(days=gone)}'")[0][0]
        cur_ = hand(c, f"SELECT sum({amt}) {gl} AND {day} >= '{y}-01-01' AND {day} <= '{today}'")[0][0]
        check("same period last year: current", cmp_[0]["current"], cur_)
        check("same period last year: previous (same days)", cmp_[0]["previous"], prev)
        # b. year over year by month: the months add up to the period
        yoy = run(c, "ledger_posted", period="year_to_date", by="month", compare=True, against="same_period_last_year")
        check("year over year by month: months add to current", sum(r["current"] or 0 for r in yoy), cur_)
        check("year over year by month: months add to previous", sum(r["previous"] or 0 for r in yoy), prev)
        # c. two dimensions: a pivot adds up to the whole
        pivot = run(c, "ledger_posted", period="year_to_date", by=["ledger", "month"])
        check("pivot ledger x month adds to the period", sum(r["value"] or 0 for r in pivot), cur_)
        # d. a receipt lead time against hand statements
        rl = run(c, "receipt_lead_time", period=f"{y - 1}")
        rcp = ("FROM biz.records x LEFT JOIN biz.record_dates d ON d.type = x.type AND d.rid = x.rid "
               "WHERE x.type = 'Receipt'")
        hl = hand(c, f"WITH r AS (SELECT {day}::date AS received, (SELECT min(p.order_date) FROM biz.record_links l "
                     f"JOIN biz.purchase_orders p ON p.handle = l.to_handle WHERE l.from_handle = x.handle "
                     f"AND l.link_type = 'Receipt.receives') AS od {rcp}) "
                     f"SELECT count(*), avg(received - od) FROM r WHERE received >= '{y - 1}-01-01' AND received < '{y}-01-01' "
                     f"AND od IS NOT NULL AND received - od >= 0")[0]
        check("receipt lead time last year: receipts counted", rl[0]["row_count"], hl[0])
        check("receipt lead time last year: average days", round(Decimal(str(rl[0]["value"])), 2), round(Decimal(str(hl[1])), 2))
        # the stated exclusions count what they leave out (a NULL in a condition must not drop a row from its own count)
        for p_, sql in M.excluded_statements("receipt_lead_time", period=f"{y - 1}"):
            m = M.measures()["receipt_lead_time"]
            sql = sql.replace(M.from_of(m), M.from_of(m).replace("FROM records r", f"FROM {H.RECORDS} r"))
            got_n = c.execute(sql).fetchone()
            cols = [d.name for d in c.execute(sql).description]
            n = got_n[cols.index("row_count")]
            cond = {"unlinked": "od IS NULL", "before_order": "received - od < 0"}[p_["id"]]
            want_n = hand(c, f"WITH r AS (SELECT {day}::date AS received, (SELECT min(p.order_date) FROM biz.record_links l "
                             f"JOIN biz.purchase_orders p ON p.handle = l.to_handle WHERE l.from_handle = x.handle "
                             f"AND l.link_type = 'Receipt.receives') AS od {rcp}) "
                             f"SELECT count(*) FROM r WHERE received >= '{y - 1}-01-01' AND received < '{y}-01-01' AND {cond}")[0][0]
            check(f"receipt lead time {y - 1}: receipts left out as '{p_['id']}'", n, want_n)
    print(f"{ok} of {ok + len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
