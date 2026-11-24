"""The action log (ASSIST specs 2.5 step 1; contract C5 ``assist.actions``): what each person
did, one row per act, from the ONE definition of an act (``day_coverage``: its system
activity and the replay's providers: mail and Teams sent, approvals decided, MC answers).

``build`` runs on a Spark against the standby (the store is read only there) and writes JSON
lines; ``load`` puts them in ``assist.actions`` on the box, resolving each message's handle,
source and thread from the comms views by its record id in one statement and keeping what is
already there (``ON CONFLICT (action_key) DO NOTHING``). Everyone the people exclusion clears is
built: nothing here picks people by name or role. A send's ``not_human`` is the reason
``day_coverage.not_human`` gives (a template, a scheduled send, automated), else ''.

What a row does NOT hold is said by ``fill``: the share of each kind's rows with a thread, a
target handle, recipients, fields, an attachment flag (``has_attachment`` stays NULL until the
comms views carry it: the provider reads the 255-character preview, not the message).
Evidence: isolated-fixture (``operating/tests/test_assist_actions.py``); live-unverified."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

SCHEMA = "assist"
LOAD_ROWS = 2000                    # rows per statement when a file is loaded
SYSTEM_OF = {"mail_sent": "mail", "teams_sent": "teams"}


def row_of(a: Mapping[str, Any], reason: str = "") -> dict[str, Any]:
    """One provider or system action as an ``assist.actions`` row (the keys ``load`` reads)."""
    from operating import day_coverage as dc
    kind = str(a.get("kind") or "")
    sent = kind in SYSTEM_OF
    return {
        "action_key": dc.action_key(a), "person": str(a.get("person") or ""),
        "at": a.get("at"), "system": a.get("system") or SYSTEM_OF.get(kind, "mission_control"),
        "kind": kind, "object_id": str(a.get("object_id") or ""),
        "thread_key": str(a.get("conversation") or "") or None,
        "recipients": sorted({str(t) for t in a.get("to") or () if t}),
        "records": [str(r) for r in a.get("records") or ()],
        "attributed_by": "sender" if sent else "store_who_field",
        "not_human": reason}


def build(st: Any, people: Any, who: set[str], since: datetime, until: datetime, *,
          providers: Optional[Sequence[Any]] = None, cache: Optional[Path] = None
          ) -> dict[str, Any]:
    """Every act of ``who`` in [since, until): the replay providers' (``autopilot_replay.
    load_config``'s list unless ``providers`` is given), the store's system activity, each
    with its ``not_human`` reason. Returns ``{"rows": [...], "lost": {...}}``: ``lost`` counts
    the activity no one person could be named for, by (system, kind, reason)."""
    from operating import autopilot as ap
    from operating import autopilot_replay as rp
    from operating import day_coverage as dc
    cfg_providers = providers if providers is not None else rp.load_config()["providers"]
    lo, hi = ap._iso(since), ap._iso(until)
    actions: list[dict[str, Any]] = []
    for provider in cfg_providers:
        actions += [{**a, "system": SYSTEM_OF.get(a.get("kind"), "mission_control")}
                    for a in dc._provided(provider, st, people, who, lo, hi, cache)]
    system, lost = dc.system_actions(st, people, who, lo, hi, cache=cache)
    actions += system
    dropped = dc.not_human(actions)
    rows = [row_of(a, dropped.get(id(a), "")) for a in actions]
    return {"rows": rows, "lost": {"|".join(k): v for k, v in lost.items()}}


def write(out: Path, built: Mapping[str, Any]) -> int:
    """The rows as JSON lines (``load``'s input)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in built["rows"]:
            fh.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
    return len(built["rows"])


def _load_sql() -> str:
    from operating import assist_readings as ar
    return f"""
WITH x AS (SELECT * FROM jsonb_to_recordset(%(v)s::jsonb) AS x(action_key text, person text,
    at timestamptz, system text, kind text, object_id text, thread_key text,
    recipients text[], records jsonb, attributed_by text, not_human text)),
m AS (
  SELECT x.*, e.handle AS e_handle, e.thread AS e_thread, t.handle AS t_handle, t.chat AS t_chat,
         (SELECT true FROM biz.emails u WHERE x.system = 'mail' AND u.object_ids[1] = x.object_id
          LIMIT 1) AS e_seen,
         (SELECT true FROM biz.teams_messages u WHERE x.system = 'teams'
          AND u.object_ids[1] = x.object_id LIMIT 1) AS t_seen
  FROM x
  LEFT JOIN LATERAL (SELECT handle, thread FROM biz.emails e WHERE e.object_ids[1] = x.object_id
                     AND x.system = 'mail' AND {ar._rule('e')} LIMIT 1) e ON true
  LEFT JOIN LATERAL (SELECT handle, chat FROM biz.teams_messages t
                     WHERE t.object_ids[1] = x.object_id AND x.system = 'teams'
                     AND {ar._rule('t')} LIMIT 1) t ON true),
verdict AS (
  SELECT m.*, CASE WHEN m.system IN ('mail', 'teams') AND e_handle IS NULL AND t_handle IS NULL
                   THEN CASE WHEN coalesce(e_seen, t_seen, false) THEN 'not_readable'
                             ELSE 'message_not_served' END END AS dropped
  FROM m),
ins AS (
  INSERT INTO {SCHEMA}.actions (action_key, person, at, system, kind, target_type, target_handle,
      source, message_key, thread_key, recipients, has_attachment, fields, attributed_by,
      not_human)
  SELECT action_key, person, at, system, kind,
         CASE WHEN object_id <> '' THEN split_part(object_id, ':', 1) ELSE '' END,
         coalesce(e_handle, t_handle, object_id),
         CASE WHEN e_handle IS NOT NULL THEN 'emails' WHEN t_handle IS NOT NULL
              THEN 'teams_messages' END, coalesce(e_handle, t_handle),
         coalesce(e_thread, t_chat, thread_key), coalesce(recipients, '{{}}'), NULL,
         jsonb_build_object('records', coalesce(records, '[]'::jsonb)), attributed_by,
         coalesce(not_human, '')
  FROM verdict WHERE dropped IS NULL ON CONFLICT (action_key) DO NOTHING RETURNING 1)
SELECT (SELECT count(*) FROM ins), v.dropped, count(*) FROM verdict v WHERE v.dropped IS NOT NULL
GROUP BY 2 UNION ALL SELECT (SELECT count(*) FROM ins), NULL, 0 WHERE NOT EXISTS (
  SELECT 1 FROM verdict WHERE dropped IS NOT NULL)"""


def load(pg: Any, path: Path, *, covered: Optional[int] = None) -> dict[str, Any]:
    """Put a built file's rows in place: ``{"rows", "added", "dropped": {reason: n}}``. A message
    is kept only when the readings' own door passes it (``assist_readings._rule``: the AI-ok flag,
    the exclusion, the covered version); a row failing it is dropped and counted with a typed
    reason (``not_readable``, ``message_not_served``), never kept as a half row. ``covered`` is the
    exclusion's covered version (``assist_readings._covered``): undecided -> nothing is loaded."""
    if covered is None:
        return {"status": "blocked", "reason": "the people exclusion is not decided here",
                "rows": 0, "added": 0, "dropped": {}}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    added, dropped = 0, {}
    sql = _load_sql()
    for i in range(0, len(rows), LOAD_ROWS):
        got = pg.execute(sql, {"v": json.dumps(rows[i:i + LOAD_ROWS]),
                               "covered": covered}).fetchall()
        added += int(got[0][0]) if got else 0
        for _n, why, k in got:
            if why:
                dropped[why] = dropped.get(why, 0) + int(k)
    return {"status": "ok", "rows": len(rows), "added": added, "dropped": dropped}


def fill(pg: Any, since: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Per kind: rows and the share holding each field (guardrail 7: measured before anything
    is built on a column); NULL for a column no row can hold yet."""
    cur = pg.execute(
        f"SELECT kind, count(*), round(avg((thread_key IS NOT NULL)::int), 3), "
        "round(avg((target_handle <> '')::int), 3), round(avg((cardinality(recipients) > 0)::int), 3), "
        "round(avg((fields <> '{}'::jsonb)::int), 3), round(avg((has_attachment IS NOT NULL)::int), 3), "
        f"round(avg((not_human <> '')::int), 3) FROM {SCHEMA}.actions "
        "WHERE (%s::timestamptz IS NULL OR at >= %s) GROUP BY 1 ORDER BY 2 DESC", (since, since))
    keys = ("kind", "rows", "thread_key", "target_handle", "recipients", "fields",
            "has_attachment", "not_human")
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_actions")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--store", required=True)
    b.add_argument("--source", default="postgres")
    b.add_argument("--since", required=True)
    b.add_argument("--until", default=None)
    b.add_argument("--out", required=True)
    lo = sub.add_parser("load")
    lo.add_argument("path")
    sub.add_parser("fill")
    a = p.parse_args(argv)
    if a.cmd == "build":
        from operating import autopilot as ap
        from operating import proactive_engine as pe
        people = pe._people(Path(a.store))
        if people is None or getattr(people, "unavailable", None):
            print(json.dumps({"status": "blocked", "reason": "people_exclusion_unavailable"}))
            return 1
        since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc)
        until = (datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc) if a.until
                 else datetime.now(timezone.utc))
        directory, _addresses, _domains = ap._people_maps(people)
        who = {o for o in set(directory.values()) if people.identity_clear(o)}
        st = ap._store(a.store, a.source)
        try:
            built = build(st, people, who, since, until)
        finally:
            st.close()
        n = write(Path(a.out), built)
        print(json.dumps({"status": "ok", "rows": n, "people": len(who), "lost": built["lost"]},
                         sort_keys=True))
        return 0
    import psycopg
    from operating.pg import config
    with psycopg.connect(config.dsn(), autocommit=True) as pg:
        if a.cmd == "load":
            from operating import assist_readings as ar
            print(json.dumps(load(pg, Path(a.path), covered=ar._covered(pg, None)), default=str))
        else:
            print(json.dumps(fill(pg), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
