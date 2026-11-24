"""Assist done-checks (ASSIST specs section 2): one line ``<check_id> <n>/<m> pass|fail
<what was not measured>``, exit 0 on pass, 1 on fail, 2 when it could not run (said).

    python3 -m operating.assist_checks as3_true20 [--labels FILE] [--state-dir DIR] [--dsn DSN]
    python3 -m operating.assist_checks as5_built [--dsn DSN]

``as3_true20`` (spec 2.3): 20 open matters of shown people drawn ``ORDER BY md5(matter_id ||
current_date)``; each is printed with its sources for the hand check, re-checked from SQL (a
fresh re-verify inside a transaction that is rolled back: still open?), and the hand verdicts
are read from ``--labels`` (JSON lines ``{"matter_id": ..., "true": true|false}``). Pass at
19 of 20 with both the SQL re-check and the hand verdict true. Without labels it prints the
20 and exits 2 (not measured: the hand check).

``as5_built`` (spec 2.5): of the people with at least ``MIN_SENDS`` sends in the last
``SEND_DAYS`` days (assist.actions, human mail and chat sends), how many have at least
``MIN_SENDS`` precedents; pass at all of them; the act mix of the precedents is printed as a
claim, and the date fence is checked live on ``FENCE_SAMPLE`` open matters (every precedent
``nearest_many`` returns is dated before that matter's basis)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

MIN_SENDS = 30       # spec 2.5: a person with this many sends should have this many precedents
SEND_DAYS = 90
FENCE_SAMPLE = 60
SAMPLE = 20          # the spec's sample size (a stated measure, not a cut of a list)
PASS_AT = 19


def shown_people(pg: Any, state: Path) -> Optional[list[str]]:
    """Staff ids whose matters are shown (``assist_matters.shown_to``); None when the opt-in
    journal cannot be read."""
    from operating import assist_matters as am
    from operating import autopilot_engine as ae
    from operating import autopilot_write as aw
    from operating.autopilot_write import Journal
    from operating.card_actions import JournalBroken
    path = state / "autopilot-write" / aw.AUDIT_FILE
    try:
        rows = Journal(path).rows() if path.exists() else []
    except (OSError, ValueError, JournalBroken):
        return None
    people = am.people_of(pg)
    draft = ae.load_draft()
    return sorted(p for p in people.ids
                  if am.shown_to({p, *people.addresses.get(p, ())}, rows=rows, draft=draft))


def as3_true20(pg: Any, *, state: Path, labels: Optional[Path]) -> tuple[int, str]:
    from operating import assist_matters as am
    people = shown_people(pg, state)
    if people is None:
        print("as3_true20 0/0 fail not measured: the opt-in journal could not be read")
        return 2, ""
    rows = pg.execute(
        f"SELECT matter_id, owner_person, kind, title, sources, basis_at FROM {am.SCHEMA}.matters "
        "WHERE state = 'open' AND owner_person = ANY(%s) ORDER BY md5(matter_id || "
        "current_date::text) LIMIT %s", (people, SAMPLE)).fetchall()
    unverified = pg.execute(f"SELECT count(*) FROM {am.SCHEMA}.matters WHERE state = "
                            "'unverified' AND owner_person = ANY(%s)", (people,)).fetchone()[0]
    with pg.transaction(force_rollback=True):         # a fresh re-verify, never kept
        am.reverify_conversations(pg)
        still = {r[0] for r in pg.execute(
            f"SELECT matter_id FROM {am.SCHEMA}.matters WHERE state = 'open' AND matter_id = "
            "ANY(%s)", ([r[0] for r in rows],)).fetchall()}
    for mid, owner, kind, title, sources, basis in rows:
        print(json.dumps({"matter_id": mid, "owner": owner, "kind": kind, "title": title,
                          "basis_at": basis, "sources": sources, "sql_still_open": mid in still},
                         default=str))
    if labels is None:
        print(f"as3_true20 0/{len(rows)} fail not measured: the hand verdicts (--labels); "
              f"unverified beside the list: {unverified}")
        return 2, ""
    hand = {}
    for line in labels.read_text(encoding="utf-8").splitlines():
        if line.strip():
            got = json.loads(line)
            hand[str(got.get("matter_id"))] = got.get("true") is True
    n = sum(1 for r in rows if r[0] in still and hand.get(r[0]) is True)
    m = len(rows)
    ok = m == SAMPLE and n >= PASS_AT
    missing = sum(1 for r in rows if r[0] not in hand)
    print(f"as3_true20 {n}/{m} {'pass' if ok else 'fail'} not measured: "
          f"{missing} without a hand verdict; unverified beside the list: {unverified}"
          + ("" if m == SAMPLE else f"; only {m} open matters of shown people"))
    return (0 if ok else 1), ""


def as5_built(pg: Any) -> tuple[int, str]:
    """The AS5 done-check (see the module doc). 2 when the tables are not there or hold no
    actions (said), never a pass on nothing."""
    from operating import assist_precedents as ap
    people = pg.execute(
        "SELECT a.person, count(*), (SELECT count(*) FROM assist.precedents p WHERE p.person = a.person) "
        "FROM assist.actions a WHERE a.kind IN ('mail_sent', 'teams_sent') AND a.not_human = '' "
        "AND a.at >= now() - make_interval(days => %s) GROUP BY a.person HAVING count(*) >= %s "
        "ORDER BY 1", (SEND_DAYS, MIN_SENDS)).fetchall()
    mix = dict(pg.execute("SELECT act, count(*) FROM assist.precedents GROUP BY 1 ORDER BY 2 DESC").fetchall())
    if not people:
        print(f"as5_built 0/0 fail not measured: no person has {MIN_SENDS} sends in {SEND_DAYS} days in "
              f"assist.actions (precedents by act: {mix})")
        return 2, ""
    n = sum(1 for _p, _s, c in people if c >= MIN_SENDS)
    short = [p for p, _s, c in people if c < MIN_SENDS]
    fence = "fence not measured: no embedded precedent or no open matter"
    vec = pg.execute("SELECT embed_model, embedding::text FROM assist.precedent_vectors LIMIT 1").fetchone()
    matters = pg.execute(
        "SELECT matter_id, owner_person, basis_at FROM assist.matters WHERE state = 'open' "
        "ORDER BY md5(matter_id || current_date::text) LIMIT %s", (FENCE_SAMPLE,)).fetchall()
    fence_ok = True
    if vec and matters:
        got = ap.nearest_many(pg, [{"id": mid, "person": who, "role": "", "basis_at": at,
                                    "vector": json.loads(vec[1])} for mid, who, at in matters],
                              model=vec[0])
        late = sum(1 for (mid, _w, at) in matters for r in got[mid][0] if r["at"] >= at)
        fence_ok = late == 0
        fence = f"fence {len(matters)} matters, {late} precedents dated at or after their basis"
    ok = n == len(people) and fence_ok
    print(f"as5_built {n}/{len(people)} {'pass' if ok else 'fail'} not measured: "
          f"people short of {MIN_SENDS}: {short or 'none'}; {fence}; precedents by act: {mix}")
    return (0 if ok else 1), ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_checks")
    p.add_argument("check", choices=("as3_true20", "as5_built"))
    p.add_argument("--labels", default=None)
    p.add_argument("--state-dir", default="/data/plx-state")
    p.add_argument("--dsn", default="postgresql:///plx")
    a = p.parse_args(argv)
    import psycopg
    try:
        with psycopg.connect(a.dsn, autocommit=True) as pg:
            if a.check == "as5_built":
                rc, _ = as5_built(pg)
            else:
                rc, _ = as3_true20(pg, state=Path(a.state_dir),
                                   labels=Path(a.labels) if a.labels else None)
    except psycopg.Error as exc:
        print(f"{a.check} 0/0 fail not measured: {str(exc).splitlines()[0]}")
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
