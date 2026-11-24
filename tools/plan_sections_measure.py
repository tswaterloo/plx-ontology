#!/usr/bin/env python3
"""The three numbers of the precomputed plan sections, read from what is already there (no write):

  python3 tools/plan_sections_measure.py [--hours 24] [--dsn postgresql:///plx]

1. change to fresh: for rows rebuilt in the last hours, the seconds from the newest version of
   any object the row depends on (``ontology.object_versions.recorded_at``) to the row's
   ``built_at``, for rows where that change came shortly before the rebuild (a day-turn rebuild
   is a different thing and is counted apart). The puller's own line ``found_to_put_s`` (journal
   of plx-plan-pull) gives the runner-found-to-put part.
2. full pass: how many of the open orders' rows were built today (New York) and the time between
   the first and last of them (``--hours`` does not apply: the day's rebuild is read whole).
3. served versus fallback: the API's hourly ``plan sections read: {...}`` totals (journal of
   plx-api) and every ``plan section read live: <reason>`` line, by reason.

Prints each as n-of-m and says what it did not measure.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Iterable, Sequence

TOTALS = re.compile(r"plan sections read: (\{.*\})")
LIVE = re.compile(r"plan section read live: (\w+) \(")
FOUND = re.compile(r'found_to_put_s": "min ([\d.]+) median ([\d.]+) max ([\d.]+)"')


def read_totals(lines: Iterable[str]) -> dict[str, int]:
    """The newest hourly totals line (cumulative since the API started)."""
    last: dict[str, int] = {}
    for line in lines:
        got = TOTALS.search(line)
        if got:
            last = json.loads(got.group(1))
    return last


def fallback_reasons(lines: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in lines:
        got = LIVE.search(line)
        if got:
            out[got.group(1)] = out.get(got.group(1), 0) + 1
    return out


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def journal(unit: str, hours: int) -> list[str]:
    out = subprocess.run(["journalctl", "-u", unit, "--since", f"{hours} hours ago", "-o", "cat",
                          "--no-pager"], capture_output=True, text=True, timeout=120)
    return out.stdout.splitlines()


def psql(dsn: str, sql: str) -> list[list[str]]:
    out = subprocess.run(["psql", dsn, "-AtF", "|", "-c", sql], capture_output=True, text=True,
                         timeout=300)
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[:300])
    return [line.split("|") for line in out.stdout.splitlines() if line]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--dsn", default="postgresql:///plx")
    args = ap.parse_args(argv)
    from operating.pg import plan_sections as ps
    # 1. change to fresh
    rows = psql(args.dsn, (
        "SELECT t.record_id, t.section, extract(epoch FROM (t.built_at::timestamptz - "
        "(SELECT max(v.recorded_at::timestamptz) FROM ontology.object_versions v WHERE v.object_id = "
        "ANY(t.dep_ids) AND v.ver <= t.dep_ver))) FROM biz.plan_section_items t "
        f"WHERE t.built_at::timestamptz > now() - interval '{int(args.hours)} hours'"))
    lat = [float(r[2]) for r in rows if len(r) > 2 and r[2] not in ("", None) and 0 <= float(r[2]) < 1800]
    later = len(rows) - len(lat)
    print(f"1. change to fresh: {len(lat)} of {len(rows)} rows rebuilt in the last {args.hours} h "
          f"had their newest dependency change under 30 min before the rebuild; "
          + (f"median {percentile(lat, .5):.0f} s, p90 {percentile(lat, .9):.0f} s, max {max(lat):.0f} s"
             if lat else "none") + f". The other {later} are day-turn or older changes (not a latency).")
    for line in journal("plx-plan-pull", args.hours):
        got = FOUND.search(line)
        if got:
            print("   puller, found-to-put:", got.group(0)[len('found_to_put_s": '):])
    # 2. full pass
    for section in sorted(ps.FAMILY):
        n, total, first, last = psql(args.dsn, (
            "SELECT count(*) FILTER (WHERE (built_at::timestamptz AT TIME ZONE 'America/New_York')::date = "
            "(now() AT TIME ZONE 'America/New_York')::date), count(*), "
            "min(built_at::timestamptz) FILTER (WHERE (built_at::timestamptz AT TIME ZONE 'America/New_York')::date = "
            "(now() AT TIME ZONE 'America/New_York')::date), max(built_at::timestamptz) "
            f"FROM biz.plan_section_items WHERE section = '{section}'"))[0]
        print(f"2. full pass, {section}: {n} of {total} rows built today (first {first}, last {last}); "
              "the open orders without a row are not counted here (see the resident's 'missing' list)")
    # 3. served versus fallback
    lines = journal("plx-api", args.hours)
    totals = read_totals(lines)
    served = totals.get("served", 0) + totals.get("served_stale", 0)
    fb = {k: v for k, v in totals.items() if k.startswith("fallback:")}
    whole = served + sum(fb.values())
    print(f"3. served versus fallback (API totals since its start): {served} of {whole} reads from a "
          f"stored row ({totals.get('served_stale', 0)} of them old rows with their age); "
          f"fallbacks {fb or 'none'}; log lines in the last {args.hours} h by reason {fallback_reasons(lines)}")
    print("Not measured: reads before the API's last start; the page's time to paint; the "
          "day-count text of an old row (it says its age, it is not re-rendered).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
