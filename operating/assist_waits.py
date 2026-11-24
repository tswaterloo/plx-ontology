"""Promises and waits (ASSIST specs 2.9 steps 2-3).

A reading of a message the person SENT (``sender_person``) that holds a ``promise`` (what they said
they would do) opens a ``promise`` matter for them; one that holds ``waiting_on`` (what they wait
for from whom) opens a ``wait`` matter. There is no fixed default: when the message states no
date, the matter is due after the person's OWN measured lag (the 75th percentile, over the
answered waits or kept promises in their mail), and a person with no such history gets the matter
shown at once asking when to look for it (``next_act = 'ask_person'``, due NULL). A wait stays out
of the list until it is due (``assist_matters.as_items``), then reads "No answer yet from <who>".
A wait closes when someone else writes in the thread after it; a promise when the person writes
in the thread after it (``reverify``). A matter whose message is gone is unverified, never
closed. Teams has no measured history here (the chat's members are not in the data), so a chat's
wait or promise asks the person. One statement each; no per-matter loop.
Evidence: isolated-fixture (``operating/tests/test_assist_waits.py``); live-unverified."""
from __future__ import annotations

from typing import Any

SCHEMA = "assist"

_OPEN = f"""
WITH k AS (
  SELECT r.*, v.kind, v.said, coalesce(r.thread_key, r.message_key) AS tk
  FROM {SCHEMA}.readings r
  CROSS JOIN LATERAL (VALUES ('wait', r.waiting_on), ('promise', r.promise)) v(kind, said)
  WHERE r.status = 'ok' AND r.sender_person IS NOT NULL AND v.said <> ''),
newest AS (SELECT DISTINCT ON (sender_person, kind, tk) * FROM k
           ORDER BY sender_person, kind, tk, said_at DESC, message_key DESC),
mine AS (SELECT o.mailbox, o.addrs FROM biz.mailbox_owners o),
-- the person's measured lag: for each earlier wait the seconds to the next message of someone
-- else in the thread, for each earlier promise the seconds to the person's next message
pairs AS (
  SELECT k.sender_person AS person, k.kind,
         extract(epoch FROM (SELECT min(e.received_at) FROM biz.emails e
              WHERE k.source = 'emails' AND e.thread = k.thread_key AND e.received_at > k.said_at
                AND ((k.kind = 'wait' AND NOT lower(e.sender) = ANY (SELECT lower(a) FROM mine m,
                          unnest(m.addrs) a WHERE m.mailbox = k.sender_person))
                  OR (k.kind = 'promise' AND lower(e.sender) = ANY (SELECT lower(a) FROM mine m,
                          unnest(m.addrs) a WHERE m.mailbox = k.sender_person)))) - k.said_at)
         AS secs
  FROM k),
lag AS (SELECT person, kind, percentile_cont(0.75) WITHIN GROUP (ORDER BY secs) AS p75
        FROM pairs WHERE secs IS NOT NULL GROUP BY 1, 2),
up AS (
  INSERT INTO {SCHEMA}.matters AS m (matter_id, owner_person, kind, subject_key, sources, basis_at,
      title, detail, next_act, due_at, counterpart, counterpart_party, audiences, markings)
  SELECT left(encode(sha256(convert_to(n.sender_person || '|' || n.kind || '|' || n.tk, 'UTF8')),
              'hex'), 24),
         n.sender_person, n.kind, n.tk,
         jsonb_build_object('readings', jsonb_build_array(jsonb_build_array(n.source, n.message_key))),
         n.said_at,
         CASE n.kind WHEN 'wait' THEN 'No answer yet from ' || coalesce(nullif(n.counterpart, ''),
                                                                         'them')
                     ELSE 'You said: ' || n.said END,
         n.said || CASE WHEN due.at IS NULL THEN ' (nothing in your mail says how long this usually '
                   'takes: when should it be looked for?)' ELSE '' END,
         CASE WHEN due.at IS NULL THEN 'ask_person' WHEN n.kind = 'wait' THEN 'new_message'
              ELSE 'nothing' END,
         due.at, coalesce(n.counterpart, ''), n.counterpart_party,
         coalesce((SELECT jsonb_agg(DISTINCT a) FROM (
             SELECT unnest(e.audience_keys) AS a FROM biz.emails e WHERE e.handle = ANY(n.handles)
             UNION ALL SELECT unnest(t.audience_keys) FROM biz.teams_messages t
             WHERE t.handle = ANY(n.handles)) z), '[]'::jsonb),
         coalesce((SELECT jsonb_agg(DISTINCT mk) FROM (
             SELECT jsonb_array_elements_text(rm::jsonb) AS mk FROM (
               SELECT unnest(e.rec_marks) AS rm FROM biz.emails e WHERE e.handle = ANY(n.handles)
               UNION ALL SELECT unnest(t.rec_marks) FROM biz.teams_messages t
               WHERE t.handle = ANY(n.handles)) y) z), '[]'::jsonb)
  FROM newest n
  LEFT JOIN lag l ON l.person = n.sender_person AND l.kind = n.kind
  CROSS JOIN LATERAL (SELECT coalesce(((n.due + 1)::timestamp AT TIME ZONE %(tz)s),
                                      n.said_at + l.p75 * interval '1 second') AS at) due
  ON CONFLICT (owner_person, kind, subject_key) DO UPDATE SET
      sources = EXCLUDED.sources, basis_at = EXCLUDED.basis_at, title = EXCLUDED.title,
      detail = EXCLUDED.detail, next_act = EXCLUDED.next_act, due_at = EXCLUDED.due_at,
      counterpart = EXCLUDED.counterpart, counterpart_party = EXCLUDED.counterpart_party,
      audiences = EXCLUDED.audiences, markings = EXCLUDED.markings,
      state = 'open', done_reason = NULL, done_at = NULL, plan = NULL, plan_status = 'none'
  WHERE m.basis_at < EXCLUDED.basis_at
  RETURNING m.matter_id, (xmax = 0) AS fresh),
logged AS (INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
           SELECT matter_id, CASE WHEN fresh THEN 'opened' ELSE 'basis_moved' END, '{{}}'::jsonb
           FROM up RETURNING what)
SELECT (SELECT count(*) FROM up), (SELECT count(*) FROM logged WHERE what = 'opened')"""

_CLOSE = f"""
WITH m AS (
  SELECT m.matter_id, m.kind, m.owner_person, m.subject_key, m.basis_at,
         m.sources->'readings'->-1->>0 AS src,
         coalesce((SELECT o.addrs FROM biz.mailbox_owners o WHERE o.mailbox = m.owner_person),
                  '{{}}') AS mine
  FROM {SCHEMA}.matters m WHERE m.kind IN ('wait', 'promise') AND m.state = 'open'),
v AS (
  SELECT m.matter_id, CASE
    WHEN m.src = 'emails' AND m.kind = 'wait' AND EXISTS (SELECT 1 FROM biz.emails e
         WHERE e.thread = m.subject_key AND e.received_at > m.basis_at
           AND NOT lower(e.sender) = ANY (SELECT lower(a) FROM unnest(m.mine) a)) THEN 'answered'
    WHEN m.src = 'emails' AND m.kind = 'promise' AND EXISTS (SELECT 1 FROM biz.emails e
         WHERE e.thread = m.subject_key AND e.received_at > m.basis_at
           AND lower(e.sender) = ANY (SELECT lower(a) FROM unnest(m.mine) a)) THEN 'kept'
    WHEN m.src = 'teams_messages' AND m.kind = 'wait' AND EXISTS (SELECT 1
         FROM biz.teams_messages t WHERE t.chat = m.subject_key AND t.created_at > m.basis_at
           AND t.sender LIKE '{{%%' AND t.sender::jsonb->>'id' <> m.owner_person) THEN 'answered'
    WHEN m.src = 'teams_messages' AND m.kind = 'promise' AND EXISTS (SELECT 1
         FROM biz.teams_messages t WHERE t.chat = m.subject_key AND t.created_at > m.basis_at
           AND t.sender LIKE '{{%%' AND t.sender::jsonb->>'id' = m.owner_person) THEN 'kept'
    END AS why FROM m),
upd AS (UPDATE {SCHEMA}.matters mm SET state = 'done', done_reason = v.why, done_at = now()
        FROM v WHERE mm.matter_id = v.matter_id AND v.why IS NOT NULL RETURNING mm.matter_id, v.why),
lg AS (INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
       SELECT matter_id, 'closed', jsonb_build_object('reason', why) FROM upd RETURNING 1)
SELECT count(*) FROM upd"""


def open_from_readings(pg: Any, tz: str) -> dict[str, int]:
    """Promise and wait matters from the readings of messages the person sent."""
    row = pg.execute(_OPEN, {"tz": tz}).fetchone()
    return {"changed": int(row[0]), "opened": int(row[1])}


def reverify(pg: Any) -> dict[str, int]:
    """Close a wait someone answered and a promise the person kept (module text)."""
    return {"closed": int(pg.execute(_CLOSE).fetchone()[0])}
