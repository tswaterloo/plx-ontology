-- The job queue (operating/jobq/queue.py): jobs and their event log. Recorded in
-- jobq.migrations by operating/jobq/schema.py; IF NOT EXISTS so a database made by the
-- earlier inline script opens unchanged.
CREATE SCHEMA IF NOT EXISTS jobq;
CREATE TABLE IF NOT EXISTS jobq.jobs (
  id bigserial PRIMARY KEY,
  class text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('unit', 'inline')),
  unit text,
  idem_key text NOT NULL,
  label text,
  priority integer NOT NULL,
  mem_mb integer NOT NULL CHECK (mem_mb >= 0),
  cpus real NOT NULL CHECK (cpus >= 0),
  state text NOT NULL CHECK (state IN ('queued', 'running', 'preempting', 'done', 'failed',
                                       'dead', 'cancelled')),
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL,
  preemptions integer NOT NULL DEFAULT 0,
  run_after timestamptz NOT NULL DEFAULT now(),
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  owner text,
  lease_until timestamptz,
  heartbeat_at timestamptz,
  host text,
  pid integer,
  pid_start bigint,
  pgid integer,
  invocation text,
  exit_code integer,
  result text,
  last_error text,
  refusals integer NOT NULL DEFAULT 0,
  last_refusal text,
  last_refusal_at timestamptz,
  waited_s real
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_queued ON jobq.jobs (idem_key) WHERE state = 'queued';
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_held ON jobq.jobs (idem_key)
  WHERE state IN ('running', 'preempting');
CREATE INDEX IF NOT EXISTS jobs_due ON jobq.jobs (priority, enqueued_at, id) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS jobs_finished ON jobq.jobs (finished_at) WHERE finished_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS jobq.events (
  id bigserial PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  job_id bigint,
  kind text NOT NULL,
  detail text
);
CREATE INDEX IF NOT EXISTS events_at ON jobq.events (at);
