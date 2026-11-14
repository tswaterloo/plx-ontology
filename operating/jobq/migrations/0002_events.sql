-- The ordered event queue (operating/jobq/events.py): messages and per-handler receipts.
-- IF NOT EXISTS so a database made by the earlier inline script opens unchanged.
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
