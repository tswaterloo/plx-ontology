"""Routines (ASSIST specs 2.8, with
the 11-23 ruling: NO pass/fail gate, no support or precision threshold). A routine is a person's
(or a pool's) regular act, found in ``assist.actions`` and shown with its counts; the person
decides whether it runs (``live``). Nothing here is a threshold: every candidate found is stored
(``status = 'candidate'``) with its numbers.

Two relations, both read from the action log alone (model-free, every row of the window):

* ``clock``: the weekday on which the person does this kind of act most (by days with an act);
  ``precision`` = the share of the window's weeks with an act on that weekday, ``support`` = the
  share of the person's acts of this kind that fall on it.
* ``chain``: another kind of act (anyone's) on the SAME record shortly before (``HORIZON``):
  ``support`` = the share of the person's acts of this kind preceded by one; ``precision`` = the
  share of those earlier acts (anyone's, of that kind) that the person's act then followed;
  ``lag_s`` p50 / p90 of the gaps. Records come from ``fields.records[0]`` (the act's own record).

DATE-ONLY TIMES (said, never hidden): a kind whose acts all carry one and the same time of day in
the window (the Sage kinds and QC test entry keep a date, not a time) is ``date_only``: its
chain gaps are compared by calendar day, and an earlier act on the SAME day is counted in
``same_day`` and never as preceding (the order within a day is not in the data).

``mine`` fits on the first two thirds of the window and ``validate`` counts, on the last third
(split by date, stated in the row): ``fired`` (the trigger happened), ``acted`` (the person's act of
that kind followed within the horizon), ``anticipated`` (their acts of that kind in the third that
a fire preceded) and ``acts``. The numbers are the claim; nothing passes or fails.
Evidence: isolated-fixture (``operating/tests/test_assist_routines.py``); live-unverified."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

SCHEMA = "assist"
HORIZON = timedelta(hours=72)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_ACTS = f"""
SELECT person, kind, at, fields->'records'->>0 AS rec FROM {SCHEMA}.actions
WHERE at >= %(since)s AND at < %(until)s AND not_human = '' AND person <> '' ORDER BY at"""


def _u(at: datetime) -> datetime:
    """The instant in UTC: weekdays and days are UTC's, in Python and in SQL alike."""
    return at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)


def routine_id(scope: str, kind: str, trigger: Mapping[str, Any]) -> str:
    # the lag is a measurement that moves with every re-mine: it is not part of the identity
    ident = {k: v for k, v in trigger.items() if k != "lag_s"}
    return hashlib.sha256(f"{scope}|{kind}|{json.dumps(ident, sort_keys=True)}".encode()
                          ).hexdigest()[:16]


def split_at(since: datetime, until: datetime) -> datetime:
    """The date the later third of the window starts (mine before it, validate from it)."""
    return since + (until - since) * 2 / 3


def _date_only(acts: Sequence[tuple]) -> dict[str, bool]:
    """Per kind: every act of the kind in the window has the same time of day."""
    times: dict[str, set] = defaultdict(set)
    for _p, kind, at, _r in acts:
        times[kind].add(_u(at).timetz())
    return {k: len(v) == 1 for k, v in times.items()}


def _lag(gap: timedelta, date_only: bool, a: datetime, b: datetime) -> Optional[float]:
    """Seconds from the earlier act ``a`` to ``b``, or None when it does not precede (a date-only
    kind: not on an earlier calendar day)."""
    if date_only:
        return (_u(b).date() - _u(a).date()).total_seconds() if _u(b).date() > _u(a).date() else None
    return gap.total_seconds() if gap > timedelta(0) else None


def _chains(acts: Sequence[tuple], date_only: Mapping[str, bool]
            ) -> dict[tuple[str, str, str], dict[str, Any]]:
    """(person, kind, prior kind) -> {"pairs": [(act index, prior index, lag s)], "same_day": n}."""
    by_rec: dict[str, list[int]] = defaultdict(list)
    for i, (_p, _k, _at, rec) in enumerate(acts):
        if rec:
            by_rec[rec].append(i)
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for idx in by_rec.values():
        for j in idx:
            person, kind, at, _r = acts[j]
            for i in idx:
                if i == j:
                    continue
                _p2, kind0, at0, _r0 = acts[i]
                if kind0 == kind and _p2 == person:
                    continue            # the person's own repeat is not a trigger
                if not at0 <= at or at - at0 > HORIZON + timedelta(days=1):
                    continue
                dated = date_only.get(kind, False) or date_only.get(kind0, False)
                lag = _lag(at - at0, dated, at0, at)
                slot = out.setdefault((person, kind, kind0), {"pairs": [], "same_day": 0})
                if lag is None:
                    if _u(at).date() == _u(at0).date():
                        slot["same_day"] += 1
                elif lag <= HORIZON.total_seconds() + (86400 if dated else 0):
                    slot["pairs"].append((j, i, lag))
    return out


def _pct(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def candidates(acts: Sequence[tuple], since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Every clock and chain candidate of ``acts`` ((person, kind, at, record) rows of the window)
    with its numbers. No threshold: a person-and-kind with one act has its candidate with n = 1."""
    date_only = _date_only(acts)
    weeks = max(1, -(-(_u(until).date() - _u(since).date()).days // 7))
    mine: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    kind_n: dict[str, int] = defaultdict(int)
    for row in acts:
        mine[(row[0], row[1])].append(row)
        kind_n[row[1]] += 1
    out: list[dict[str, Any]] = []
    for (person, kind), rows in sorted(mine.items()):
        days = {_u(r[2]).date() for r in rows}
        per_day = defaultdict(set)
        for r in rows:
            per_day[_u(r[2]).weekday()].add(_u(r[2]).date())
        wd = max(per_day, key=lambda w: (len(per_day[w]), -w))
        on_wd = sum(1 for r in rows if _u(r[2]).weekday() == wd)
        weeks_with = len({d.isocalendar()[:2] for d in per_day[wd]})
        trigger = {"event": "time", "relation": "clock", "weekday": WEEKDAYS[wd]}
        out.append({"scope": person, "kind": kind, "trigger": trigger,
                    "mined": {"acts": len(rows), "days": len(days), "support": round(on_wd / len(rows), 3),
                              "precision": round(weeks_with / weeks, 3), "weeks": weeks,
                              "date_only": date_only[kind]}})
    kind_total: dict[str, int] = defaultdict(int)
    for _p, k, _at, rec in acts:
        if rec:
            kind_total[k] += 1
    for (person, kind, prior), slot in sorted(_chains(acts, date_only).items()):
        pairs = slot["pairs"]
        if not pairs and not slot["same_day"]:
            continue
        acted = {j for j, _i, _l in pairs}
        priors = {i for _j, i, _l in pairs}
        n_person = len(mine[(person, kind)])
        lags = [l for _j, _i, l in pairs]
        trigger = {"event": f"act:{prior}", "relation": "chain", "horizon_s": int(HORIZON.total_seconds()),
                   "lag_s": {"p50": _pct(lags, 0.5), "p90": _pct(lags, 0.9)}}
        out.append({"scope": person, "kind": kind, "trigger": trigger,
                    "mined": {"acts": n_person, "preceded": len(acted), "support": round(len(acted) / n_person, 3),
                              "precision": round(len(priors) / kind_total[prior], 3) if kind_total[prior] else None,
                              "same_day": slot["same_day"], "date_only": date_only[kind]
                              or date_only.get(prior, False)}})
    return out


def validation(acts: Sequence[tuple], cand: Mapping[str, Any], since: datetime, until: datetime
               ) -> dict[str, Any]:
    """The candidate counted on ``acts`` of the later third [since, until): fired, acted,
    anticipated, acts (the person's acts of that kind)."""
    person, kind, trig = cand["scope"], cand["kind"], cand["trigger"]
    mine = [r for r in acts if r[0] == person and r[1] == kind]
    base = {"since": since.isoformat(), "until": until.isoformat(), "acts": len(mine)}
    if trig["relation"] == "clock":
        wd = WEEKDAYS.index(trig["weekday"])
        days, d = [], _u(since).date()
        while d < _u(until).date():
            if d.weekday() == wd:
                days.append(d)
            d += timedelta(days=1)
        have = {_u(r[2]).date() for r in mine}
        hit = [d for d in days if d in have]
        return {**base, "fired": len(days), "acted": len(hit),
                "anticipated": sum(1 for r in mine if _u(r[2]).weekday() == wd)}
    prior = trig["event"].split(":", 1)[1]
    date_only = _date_only(acts)
    slot = _chains(acts, date_only).get((person, kind, prior), {"pairs": []})
    fired = sum(1 for r in acts if r[1] == prior and r[3])
    return {**base, "fired": fired, "acted": len({i for _j, i, _l in slot["pairs"]}),
            "anticipated": len({j for j, _i, _l in slot["pairs"]})}


_UPSERT = f"""
INSERT INTO {SCHEMA}.routines (routine_id, scope, kind, trigger, mined, validated, status, status_reason)
SELECT x.routine_id, x.scope, x.kind, x.trigger, x.mined, x.validated, 'candidate', ''
FROM jsonb_to_recordset(%(v)s::jsonb) AS x(routine_id text, scope text, kind text, trigger jsonb,
                                          mined jsonb, validated jsonb)
ON CONFLICT (routine_id) DO UPDATE SET mined = EXCLUDED.mined, validated = EXCLUDED.validated,
  updated_at = now() WHERE {SCHEMA}.routines.status = 'candidate'"""


def mine(pg: Any, since: datetime, until: datetime) -> dict[str, Any]:
    """Mine [since, until): candidates from the first two thirds, validated on the last; every
    candidate is stored once (a ``live`` or ``retired`` routine is never overwritten)."""
    cut = split_at(since, until)
    rows = pg.execute(_ACTS, {"since": since, "until": until}).fetchall()
    early = [r for r in rows if r[2] < cut]
    late = [r for r in rows if r[2] >= cut]
    cands = candidates(early, since, cut)
    stored = []
    for c in cands:
        v = validation(late, c, cut, until)
        v["split"] = "by date: first two thirds mined, last third counted"
        c["mined"].update(since=since.isoformat(), until=cut.isoformat())
        stored.append({"routine_id": routine_id(c["scope"], c["kind"], c["trigger"]), **c, "validated": v})
    if stored:
        pg.execute(_UPSERT, {"v": json.dumps(stored, default=str)})
    return {"status": "ok", "acts": len(rows), "candidates": len(stored),
            "clock": sum(1 for c in cands if c["trigger"]["relation"] == "clock"),
            "chain": sum(1 for c in cands if c["trigger"]["relation"] == "chain"),
            "split": cut.isoformat()}


def set_status(pg: Any, routine_id: str, status: str, by: str) -> bool:
    """The person's switch: ``live`` / ``retired`` / back to ``candidate``. A routine only fires from
    the moment it is live (``updated_at``): triggers older than that are never opened. Returns
    whether a routine had that id."""
    if status not in ("candidate", "live", "retired"):
        raise ValueError(f"not a status a person can set: {status}")
    return pg.execute(f"UPDATE {SCHEMA}.routines SET status = %s, status_reason = %s, updated_at = now() "
                      "WHERE routine_id = %s", (status, f"{status}: set by {by}", routine_id)).rowcount == 1


_FIRE_CHAIN = f"""
SELECT r.routine_id, r.scope, r.kind, t.rec, t.at AS basis_at, r.description, r.mined->>'date_only' AS date_only
FROM {SCHEMA}.routines r
CROSS JOIN LATERAL (
  SELECT a.fields->'records'->>0 AS rec, a.at FROM {SCHEMA}.actions a
  WHERE a.kind = split_part(r.trigger->>'event', ':', 2) AND a.at >= r.updated_at AND a.not_human = ''
    AND a.fields->'records'->>0 IS NOT NULL) t
WHERE r.status = 'live' AND r.trigger->>'relation' = 'chain'
  AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.actions m WHERE m.person = r.scope AND m.kind = r.kind
                  AND m.fields->'records'->>0 = t.rec AND m.at >= t.at)"""

_FIRE_CLOCK = f"""
SELECT r.routine_id, r.scope, r.kind, g.d::date::text AS rec, g.d AT TIME ZONE 'UTC' AS basis_at, r.description, 'false' AS date_only
FROM {SCHEMA}.routines r
CROSS JOIN LATERAL generate_series((r.updated_at AT TIME ZONE 'UTC')::date::timestamp, (%(now)s::timestamptz AT TIME ZONE 'UTC')::date::timestamp, interval '1 day') g(d)
WHERE r.status = 'live' AND r.trigger->>'relation' = 'clock'
  AND extract(isodow FROM g.d)::int = array_position(ARRAY['Mon','Tue','Wed','Thu','Fri','Sat','Sun'], r.trigger->>'weekday')
  AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.actions m WHERE m.person = r.scope AND m.kind = r.kind
                  AND (m.at AT TIME ZONE 'UTC')::date = g.d::date)"""


def open_from_routines(pg: Any, now: datetime) -> dict[str, int]:
    """One ``routine`` matter per (live routine, trigger): chain = an act of the trigger's kind on
    a record since the routine went live that the person has not followed with theirs; clock = a
    day of the routine's weekday without their act. The matter's next act is a system write, its
    ``sources.routine`` names the routine; a repeat opens nothing (same owner, kind, subject)."""
    from operating import assist_matters as am
    rows: list[dict[str, Any]] = []
    for sql, params in ((_FIRE_CHAIN, {}), (_FIRE_CLOCK, {"now": now})):
        cur = pg.execute(sql, params)
        cols = [d.name for d in cur.description]
        for r in (dict(zip(cols, x)) for x in cur.fetchall()):
            rows.append({"owner": r["scope"], "kind": "routine", "subject": f"{r['routine_id']}|{r['rec']}",
                         "sources": {"routine": r["routine_id"], "records": [r["rec"]], "act_kind": r["kind"]},
                         "basis_at": r["basis_at"],
                         "title": f"{r['kind']} for {r['rec']}",
                         "detail": r["description"] or "your usual step, from what you did before",
                         "next_act": "system_write", "audiences": [], "markings": []})
    return am.upsert(pg, rows)


_ENTERED = f"""
SELECT m.matter_id, 'entered' FROM {SCHEMA}.matters m
WHERE m.kind = 'routine' AND m.state IN ('open', 'unverified') AND EXISTS (
  SELECT 1 FROM {SCHEMA}.actions a WHERE a.person = m.owner_person AND a.kind = m.sources->>'act_kind'
    AND a.not_human = '' AND ((a.fields->'records'->>0 = m.sources->'records'->>0 AND a.at >= m.basis_at)
                              OR ((a.at AT TIME ZONE 'UTC')::date = (m.basis_at AT TIME ZONE 'UTC')::date AND m.sources->'records'->>0 ~ '^[0-9]{{4}}-')))"""


def reverify(pg: Any) -> dict[str, int]:
    """A routine matter closes ``entered`` when the person's act of that kind is in the log on
    its record (clock: on its day); nothing else closes it here."""
    from operating import assist_matters as am
    done = pg.execute(_ENTERED).fetchall()
    return am.set_states(pg, [{"matter": m, "state": "done", "reason": why} for m, why in done]) if done else {}


WRITE_UP = ("Describe in two plain sentences what this person does and when, for the person themself. "
            "Use only the facts below. No advice.\nFACTS: {facts}")


def write_up(pg: Any, call: Any, routine_ids: Sequence[str]) -> dict[str, int]:
    """One model call per named routine (``call(prompt) -> text or None``): its plain description,
    stored only when the model answered; a routine nobody asked about costs nothing."""
    done = failed = 0
    for rid in routine_ids:
        row = pg.execute(f"SELECT kind, trigger, mined, validated FROM {SCHEMA}.routines WHERE routine_id = %s",
                         (rid,)).fetchone()
        if row is None:
            failed += 1
            continue
        facts = json.dumps({"kind": row[0], "trigger": row[1], "counts": row[2], "later_third": row[3]},
                           sort_keys=True, default=str)
        text = call(WRITE_UP.format(facts=facts))
        if not text or not text.strip():
            failed += 1
            continue
        pg.execute(f"UPDATE {SCHEMA}.routines SET description = %s, updated_at = updated_at WHERE routine_id = %s",
                   (" ".join(text.split()), rid))
        done += 1
    return {"written": done, "failed": failed}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 -m operating.assist_routines mine --since D --until D`` (reads and writes
    ``assist.*`` only; run it against the box's table after the actions are loaded)."""
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_routines")
    p.add_argument("command", choices=("mine",))
    p.add_argument("--since", required=True)
    p.add_argument("--until", required=True)
    a = p.parse_args(argv)
    import psycopg
    from operating.pg import config

    def when(s: str) -> datetime:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=__import__("datetime").timezone.utc)
    with psycopg.connect(config.dsn(), autocommit=True) as pg:
        print(json.dumps(mine(pg, when(a.since), when(a.until)), sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
