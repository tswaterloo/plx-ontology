-- 0083_assist_workflows: a person's own Assist workflows (PLAN-1124 "Assist = always-on agent" (b)):
-- created from plain language, mined routines or templates; triggered on a schedule, on an event
-- (new mail from X, PO goes late, MC question arrives) or on demand. Append-only log rows folded
-- into each workflow's current state (nothing is updated or deleted). Each run appends a job to
-- assist.job_log and prepares its pieces through the same approval and confirm-first desk.
-- The API (plx_api) reads and appends only what the person does: created, updated, paused, resumed,
-- stopped, run_now; the worker writes fired, run_succeeded, run_failed, auto_paused.
-- Also grants plx_api SELECT on assist.routines so a person can create a workflow from their own mined routines.
-- Undo: DROP TRIGGER workflow_log_guard ON assist.workflow_log; DROP FUNCTION assist.workflow_log_guard(); DROP TABLE assist.workflow_log;
CREATE TABLE IF NOT EXISTS assist.workflow_log (
  seq         bigserial PRIMARY KEY,
  workflow_id text NOT NULL,
  person      text NOT NULL,
  at          timestamptz NOT NULL DEFAULT now(),
  what        text NOT NULL CHECK (what IN ('created','updated','paused','resumed','stopped','run_now',
                                            'fired','run_succeeded','run_failed','auto_paused')),
  detail      jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS workflow_log_wf ON assist.workflow_log (workflow_id, seq);
CREATE INDEX IF NOT EXISTS workflow_log_person ON assist.workflow_log (person, seq DESC) WHERE what = 'created';
CREATE UNIQUE INDEX IF NOT EXISTS workflow_log_dedupe
  ON assist.workflow_log (workflow_id, (detail->>'dedupe_key'))
  WHERE what = 'fired' AND detail ? 'dedupe_key' AND coalesce(detail->>'dedupe_key', '') <> '';
CREATE OR REPLACE FUNCTION assist.workflow_log_guard() RETURNS trigger LANGUAGE plpgsql AS $t$
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'assist.workflow_log is append-only'; END IF;
  IF current_user = 'plx_api' AND NEW.what NOT IN ('created','updated','paused','resumed','stopped','run_now') THEN
    RAISE EXCEPTION 'the API may only append what the person does (%)', NEW.what;
  END IF;
  RETURN NEW;
END $t$;
DROP TRIGGER IF EXISTS workflow_log_guard ON assist.workflow_log;
CREATE TRIGGER workflow_log_guard BEFORE INSERT OR UPDATE OR DELETE ON assist.workflow_log
  FOR EACH ROW EXECUTE FUNCTION assist.workflow_log_guard();
REVOKE ALL ON assist.workflow_log FROM PUBLIC;
DO $api$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
  GRANT USAGE ON SCHEMA assist TO plx_api;
  GRANT SELECT, INSERT ON assist.workflow_log TO plx_api;
  GRANT USAGE ON SEQUENCE assist.workflow_log_seq_seq TO plx_api;
  IF to_regclass('assist.routines') IS NOT NULL THEN GRANT SELECT ON assist.routines TO plx_api; END IF;
END IF; END $api$;
DO $ev$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
  GRANT USAGE ON SCHEMA assist TO plx_eval;
  GRANT SELECT ON assist.workflow_log TO plx_eval;
END IF; END $ev$;
