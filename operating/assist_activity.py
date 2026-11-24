"""What has happened on a matter, from the data (ASSIST specs 8.5 F1 and F2; no model).

``touched_at`` is the newest thing that happened on an open matter: a message of its thread
(``assist.readings``, every message of the thread whoever sent it), an act of a person on the
thread (``assist.actions``), or, for a matter whose source is a captured record (a Planner or
Mission Control task), the newest captured change of that record (a version after its first).
It never goes below the matter's ``basis_at``. ``plan_seen_at`` is the ``touched_at`` a plan was
made from. A plan is made again only when the matter's ``touched_at`` moves past it
(``requeue``): a later message or act. The plan queue (``assist_plan``) runs newest touch first,
so a matter nothing has happened on for months comes last without any age cut-off, and none is
dropped.

``still_open`` is the one definition of "nothing has happened on it since it was due": the
matter has a due time that has passed and its last activity is not after it. The person is asked
"still open?" (``assist_matters.item_of``); the matter stays listed and planned.
Evidence: isolated-fixture (``operating/tests/test_assist_activity.py``); live-unverified."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

SCHEMA = "assist"

def _touch_sql(versions: bool) -> str:
    return f"""
UPDATE {SCHEMA}.matters mm SET touched_at = x.t
FROM (
  SELECT m.matter_id, greatest(m.basis_at, rm.last_msg, ra.last_act{", ob.last_change" if versions else ""}) AS t
  FROM {SCHEMA}.matters m
  LEFT JOIN {SCHEMA}.readings b ON jsonb_typeof(m.sources->'readings') = 'array'
       AND b.source = m.sources->'readings'->-1->>0 AND b.message_key = m.sources->'readings'->-1->>1
  LEFT JOIN LATERAL (SELECT max(r.said_at) AS last_msg FROM {SCHEMA}.readings r
       WHERE b.thread_key IS NOT NULL AND r.source = b.source AND r.thread_key = b.thread_key) rm ON true
  LEFT JOIN LATERAL (SELECT max(a.at) AS last_act FROM {SCHEMA}.actions a
       WHERE b.thread_key IS NOT NULL AND a.thread_key = b.thread_key AND a.not_human = '') ra ON true
  {_VERSIONS if versions else ""}
  WHERE m.state IN ('open', 'unverified')) x
WHERE mm.matter_id = x.matter_id AND mm.touched_at IS DISTINCT FROM x.t"""


# a captured change of the task's own record: a version after the record's first
_VERSIONS = """LEFT JOIN LATERAL (SELECT max(v.recorded_at::timestamptz) AS last_change
       FROM ontology.object_versions v
       WHERE jsonb_typeof(m.sources->'records') = 'array'
         AND v.object_id IN (SELECT jsonb_array_elements_text(m.sources->'records'))
         AND v.ver > (SELECT min(f.ver) FROM ontology.object_versions f
                      WHERE f.object_id = v.object_id)) ob ON true"""


def touch(pg: Any) -> dict[str, int]:
    """Set every open or unverified matter's ``touched_at`` (one statement; only changed rows
    are written). ``changed``; ``record_versions`` says whether the record versions were read
    (a database without them: the matters of records are touched by their basis only, said)."""
    has = bool(pg.execute("SELECT to_regclass('ontology.object_versions') IS NOT NULL").fetchone()[0])
    cur = pg.execute(_touch_sql(has))
    return {"changed": cur.rowcount, "record_versions": int(has)}


_REQUEUE = f"""
WITH q AS (
  UPDATE {SCHEMA}.matters m SET plan = NULL, plan_status = 'none', plan_reason = '', plan_ver = NULL,
         plan_at = NULL, plan_seen_at = NULL, ready_at = NULL
  WHERE m.state = 'open' AND m.plan_status IN ('planned', 'failed')
    AND m.touched_at > coalesce(m.plan_seen_at, m.basis_at)
  RETURNING m.matter_id, m.touched_at),
lg AS (
  INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
  SELECT matter_id, 'basis_moved', jsonb_build_object('touched_at', touched_at, 'why', 'later_activity')
  FROM q RETURNING 1)
SELECT count(*) FROM q"""


def requeue(pg: Any) -> dict[str, int]:
    """A planned (or failed) plan whose matter has had activity the plan did not see is cleared,
    so the plan queue plans it again (``requeued``). Nothing else clears a plan except a moved
    basis (``assist_matters``)."""
    return {"requeued": int(pg.execute(_REQUEUE).fetchone()[0])}


def still_open(due_at: Optional[dt.datetime], touched_at: Optional[dt.datetime],
               basis_at: Optional[dt.datetime], now: dt.datetime) -> bool:
    """True when the matter's due time has passed and nothing has happened on it since: its last
    activity is not after its due time, nor after its own start (``basis_at``: a task made after its
    due date is not activity on it)."""
    if due_at is None or due_at > now:
        return False
    last = touched_at or basis_at
    start = max(due_at, basis_at) if basis_at is not None else due_at
    return last is None or last <= start


def cycle(pg: Any) -> dict[str, int]:
    return {**touch(pg), **requeue(pg)}
