"""The ordered event queue: events (``message.received``, ``approval.requested``,
``task.assigned`` ...) handled in order per entity, by priority across entities, at least
once ("Event queue").

- **Per-entity order.** Every event has a group key: the entity it is about (a mail thread,
  a PO, a task). A message is deliverable only when it is the oldest unfinished message of
  its group: while one is in flight (or waiting for a retry), nothing after it in the group
  is handed out. Publishing serializes per group (``pg_advisory_xact_lock`` on the group)
  so ids, and the group's ``seq``, follow commit order: no later event becomes visible
  before an earlier one of the same entity.
- **Priority.** Across groups the best (lowest) priority first, then the oldest. A group's
  priority is the best of its waiting messages: an urgent event never waits behind another
  entity's routine ones, and within its own entity it still waits its turn (order beats
  priority inside a group).
- **At least once.** ``receive`` leases a message (a visibility timeout); ``ack`` finishes
  it, ``nack`` returns it after a backoff; a lease that lapses (the consumer died) makes it
  deliverable again with ``receives`` counted. After ``max_receives`` it is ``dead`` (the
  dead letter) and its group goes on. Handlers must be idempotent: the event's own
  ``event_id`` (also the deduplication key: publishing it twice is one message) and the
  group's ``seq`` let a handler see a redelivery (``Handled``: a handler's done marks).
- **Scale-out.** Each message carries ``partition = hash(group) mod partitions``; a
  consumer may take a subset of partitions (``partitions=``) and many consumers take
  heads with ``FOR UPDATE SKIP LOCKED``. Order never depends on who owns a partition: the
  head rule holds across every consumer, so overlapping or restarted consumers are safe.
- ``ack``/``nack`` are fenced by the receipt (consumer and receive count): a consumer whose
  lease lapsed and whose message was handed out again changes nothing.

SQS FIFO mapping (if this ever leaves one host): topic -> a ``.fifo`` queue; group key ->
``MessageGroupId``; ``event_id`` -> ``MessageDeduplicationId``; lease -> visibility timeout;
``max_receives`` + ``dead`` -> ``maxReceiveCount`` + redrive to a DLQ; ``ack`` ->
``DeleteMessage``; ``nack`` -> ``ChangeMessageVisibility``; ``seq`` -> a message attribute.
SQS has no priority: one FIFO queue per priority class, polled best first, keeps priority;
an entity whose events span classes then relies on ``seq`` (a consumer defers a message
whose group's previous ``seq`` is not handled yet) to keep its order.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import random
from typing import Any, Iterable, Mapping

PARTITIONS = 16
LEASE_S = 300.0
MAX_RECEIVES = 5
BACKOFF_S = 30.0
BACKOFF_CAP_S = 3600.0
CHANNEL = "jobq_events"
ANALYZE_AFTER = 500

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS jobq;
CREATE TABLE IF NOT EXISTS jobq.event_queue (
  id bigserial PRIMARY KEY,
  topic text NOT NULL,
  event_id text NOT NULL,
  event_type text NOT NULL,
  group_key text NOT NULL,
  seq bigint NOT NULL,
  part integer NOT NULL,
  priority integer NOT NULL,
  body jsonb NOT NULL,
  state text NOT NULL DEFAULT 'ready' CHECK (state IN ('ready', 'inflight', 'done', 'dead')),
  receives integer NOT NULL DEFAULT 0,
  max_receives integer NOT NULL,
  visible_after timestamptz NOT NULL DEFAULT now(),
  consumer text,
  lease_until timestamptz,
  published_at timestamptz NOT NULL DEFAULT now(),
  done_at timestamptz,
  last_error text,
  UNIQUE (topic, event_id),
  UNIQUE (topic, group_key, seq)
);
CREATE INDEX IF NOT EXISTS event_queue_open ON jobq.event_queue (topic, group_key, id)
  WHERE state IN ('ready', 'inflight');
CREATE TABLE IF NOT EXISTS jobq.event_handled (
  topic text NOT NULL,
  handler text NOT NULL,
  event_id text NOT NULL,
  at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (topic, handler, event_id)
);
"""

# Group heads: per group the oldest unfinished message and the group's best waiting priority,
# in one materialized pass over the open rows (DISTINCT ON over the open index), then each
# head by its primary key: a receive stays fast at thousands of waiting events whatever the
# planner's statistics (a correlated NOT EXISTS took 8 s at 5,000 before ANALYZE).
_HEADS = """
WITH heads AS MATERIALIZED (
  SELECT DISTINCT ON (group_key) id,
         min(priority) FILTER (WHERE state = 'ready') OVER (PARTITION BY group_key) AS p
  FROM jobq.event_queue
  WHERE topic = %(topic)s AND state IN ('ready', 'inflight')
    AND (%(parts)s::int[] IS NULL OR part = ANY(%(parts)s::int[]))
  ORDER BY group_key, id
)
SELECT q.id, q.topic, q.event_id, q.event_type, q.group_key, q.seq, q.part, q.priority, q.body,
       q.receives, q.max_receives
FROM heads h
JOIN jobq.event_queue q ON q.id = h.id
WHERE q.state = 'ready' AND q.visible_after <= now()
ORDER BY h.p, q.id
LIMIT %(limit)s
FOR UPDATE OF q SKIP LOCKED
"""


@dataclass(frozen=True)
class Message:
    id: int
    event_id: str
    event_type: str
    group_key: str
    seq: int
    priority: int
    body: dict[str, Any]
    receives: int          # this delivery's count (1: first)
    consumer: str

    @property
    def redelivered(self) -> bool:
        return self.receives > 1


class Stale(Exception):
    """The receipt no longer holds the message (its lease lapsed and it went elsewhere)."""


class EventQueue:
    def __init__(self, conninfo: str, *, topic: str, partitions: int = PARTITIONS,
                 lease_s: float = LEASE_S, max_receives: int = MAX_RECEIVES,
                 backoff_s: float = BACKOFF_S):
        import psycopg
        from psycopg.rows import dict_row
        if not topic or partitions < 1:
            raise ValueError("a topic and at least one partition")
        self.topic, self.partitions = topic, partitions
        self.lease_s, self.max_receives, self.backoff_s = lease_s, max_receives, backoff_s
        self.conninfo = conninfo
        self.conn = psycopg.connect(conninfo, autocommit=True, row_factory=dict_row,
                                    connect_timeout=10, application_name="plx-jobq-events")

    def close(self) -> None:
        self.conn.close()

    def init(self) -> None:
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext('jobq.events.init'))")
            self.conn.execute(SCHEMA)

    def partition_of(self, group_key: str) -> int:
        row = self.conn.execute("SELECT abs(hashtext(%s)::bigint) %% %s AS p",
                                (group_key, self.partitions)).fetchone()
        return int(row["p"])

    # ---------------------------------------------------------------- publish
    def publish(self, events: Iterable[Mapping[str, Any]], *, revive: bool = False) -> int:
        """Queue each event (``event_id``, ``event_type``, ``group_key``, ``priority``,
        ``body``) once; how many were new (or revived). An event without an id or group is
        refused (ValueError): order and deduplication need both. ``revive``: the events are
        still pending at their source, so one this queue already finished or dead-lettered
        is made ready again with fresh receives (its place in its group is kept): an event
        the source still waits on is never lost here."""
        new = 0
        for event in events:
            event_id, group = event.get("event_id"), event.get("group_key")
            if not event_id or not group or not event.get("event_type"):
                raise ValueError("an event needs event_id, event_type and group_key")
            priority = event.get("priority")
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise ValueError(f"event {event_id}: priority must be an integer")
            with self.conn.transaction():
                self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                  (f"jobq.events:{self.topic}:{group}",))
                row = self.conn.execute(
                    "INSERT INTO jobq.event_queue (topic, event_id, event_type, group_key, seq,"
                    " part, priority, body, max_receives)"
                    " SELECT %(t)s, %(e)s, %(y)s, %(g)s, coalesce((SELECT max(seq) FROM"
                    "   jobq.event_queue WHERE topic = %(t)s AND group_key = %(g)s), 0) + 1,"
                    " abs(hashtext(%(g)s)::bigint) %% %(n)s, %(p)s, %(b)s, %(m)s"
                    " ON CONFLICT (topic, event_id) DO " + (
                        "UPDATE SET state = 'ready', receives = 0, visible_after = now(),"
                        " done_at = NULL, consumer = NULL, lease_until = NULL,"
                        " last_error = 'revived: still pending at its source'"
                        " WHERE jobq.event_queue.state IN ('done', 'dead')" if revive
                        else "NOTHING") + " RETURNING id",
                    {"t": self.topic, "e": str(event_id), "y": str(event["event_type"]),
                     "g": str(group), "n": self.partitions, "p": priority,
                     "b": json.dumps(event.get("body") or {}, sort_keys=True, default=str),
                     "m": self.max_receives}).fetchone()
                new += row is not None
        if new:
            if new >= ANALYZE_AFTER:
                self.conn.execute("ANALYZE jobq.event_queue")
            self.conn.execute(f"NOTIFY {CHANNEL}")
        return new

    # ---------------------------------------------------------------- consume
    def _release_lapsed(self) -> None:
        """Lapsed leases: deliverable again, or dead when out of receives."""
        self.conn.execute(
            "UPDATE jobq.event_queue SET state = CASE WHEN receives >= max_receives THEN 'dead'"
            " ELSE 'ready' END, done_at = CASE WHEN receives >= max_receives THEN now() END,"
            " last_error = 'lease lapsed', consumer = NULL, lease_until = NULL"
            " WHERE topic = %s AND state = 'inflight' AND lease_until < now()", (self.topic,))

    def receive(self, consumer: str, *, limit: int = 1,
                partitions: Iterable[int] | None = None) -> list[Message]:
        """Up to `limit` group heads for `consumer`, leased, best priority first; at most
        one per group (the next of a group waits for this one's ack)."""
        parts = sorted(set(partitions)) if partitions is not None else None
        with self.conn.transaction():
            self._release_lapsed()
            rows = self.conn.execute(_HEADS, {"topic": self.topic, "parts": parts,
                                              "limit": limit}).fetchall()
            out = []
            for row in rows:
                self.conn.execute(
                    "UPDATE jobq.event_queue SET state = 'inflight', consumer = %s,"
                    " receives = receives + 1, lease_until = now() + make_interval(secs => %s)"
                    " WHERE id = %s", (consumer, self.lease_s, row["id"]))
                body = row["body"] if isinstance(row["body"], dict) else json.loads(row["body"])
                out.append(Message(row["id"], row["event_id"], row["event_type"],
                                   row["group_key"], row["seq"], row["priority"], body,
                                   row["receives"] + 1, consumer))
        return out

    def _settle(self, msg: Message, sql: str, args: tuple) -> None:
        cur = self.conn.execute(
            sql + " WHERE id = %s AND state = 'inflight' AND consumer = %s AND receives = %s",
            (*args, msg.id, msg.consumer, msg.receives))
        if cur.rowcount != 1:
            raise Stale(f"event {msg.event_id} is no longer held by {msg.consumer}")
        self.conn.execute(f"NOTIFY {CHANNEL}")

    def ack(self, msg: Message) -> None:
        self._settle(msg, "UPDATE jobq.event_queue SET state = 'done', done_at = now(),"
                          " consumer = NULL, lease_until = NULL", ())

    def nack(self, msg: Message, error: str, *, delay_s: float | None = None,
             dead: bool = False, spend: bool = True) -> str:
        """Return a message after a backoff (doubling per receive), or to the dead letter
        when out of receives or `dead`; its new state. ``spend=False``: the handler only
        deferred it (a rate limit, not a failure): this delivery is not counted, so a
        deferral however often repeated never sends it to the dead letter."""
        if not spend and not dead:
            if delay_s is None:
                delay_s = self.backoff_s
            self._settle(msg, "UPDATE jobq.event_queue SET state = 'ready', consumer = NULL,"
                              " lease_until = NULL, last_error = %s, receives = receives - 1,"
                              " visible_after = now() + make_interval(secs => %s)",
                         (error[:500], delay_s))
            return "ready"
        if dead or msg.receives >= self.max_receives:
            self._settle(msg, "UPDATE jobq.event_queue SET state = 'dead', done_at = now(),"
                              " consumer = NULL, lease_until = NULL, last_error = %s",
                         (error[:500],))
            return "dead"
        if delay_s is None:
            delay_s = min(BACKOFF_CAP_S, self.backoff_s * 2 ** (msg.receives - 1)) \
                * (1 + random.random() / 4)
        self._settle(msg, "UPDATE jobq.event_queue SET state = 'ready', consumer = NULL,"
                          " lease_until = NULL, last_error = %s,"
                          " visible_after = now() + make_interval(secs => %s)",
                     (error[:500], delay_s))
        return "ready"

    def extend(self, msg: Message) -> None:
        self._settle(msg, "UPDATE jobq.event_queue SET"
                          " lease_until = now() + make_interval(secs => %s)", (self.lease_s,))

    # ---------------------------------------------------------------- idempotency
    def handled(self, handler: str, event_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobq.event_handled WHERE topic = %s AND"
                                 " handler = %s AND event_id = %s",
                                 (self.topic, handler, event_id)).fetchone() is not None

    def mark_handled(self, handler: str, event_id: str) -> bool:
        """Record that `handler` finished `event_id`; False when it already had (a
        redelivery: the handler's effect must not be repeated)."""
        return self.conn.execute(
            "INSERT INTO jobq.event_handled (topic, handler, event_id) VALUES (%s, %s, %s)"
            " ON CONFLICT DO NOTHING RETURNING 1", (self.topic, handler, event_id)
        ).fetchone() is not None

    # ---------------------------------------------------------------- status
    def depth(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT state, priority, count(*) AS n, count(DISTINCT group_key) AS groups,"
            " extract(epoch FROM now() - min(published_at))::int AS oldest_s"
            " FROM jobq.event_queue WHERE topic = %s AND state IN ('ready', 'inflight', 'dead')"
            " GROUP BY state, priority ORDER BY state, priority", (self.topic,)).fetchall()
        return {"topic": self.topic, "by_state": rows}

    def prune(self, days: int = 14) -> None:
        self.conn.execute("DELETE FROM jobq.event_queue WHERE topic = %s AND state = 'done'"
                          " AND done_at < now() - make_interval(days => %s)", (self.topic, days))
