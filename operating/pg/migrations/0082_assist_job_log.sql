-- 0082_assist_job_log: a person's "do this whole job now" (ASSIST10): the request, the plan, each prepared
-- piece, the person's decisions, Stop, and how it ended, as append-only rows (a job's state is a fold of its
-- rows; nothing is updated or deleted). The API (plx_api) reads, and appends only what the person does:
-- requested, stop, decided, prepared (the prepared-write card a change became); the worker writes the rest.
-- Nothing here sends or writes to a business system.
-- Undo: DROP TRIGGER job_log_guard ON assist.job_log; DROP FUNCTION assist.job_log_guard(); DROP TABLE assist.job_log;
CREATE TABLE IF NOT EXISTS assist.job_log (
  seq    bigserial PRIMARY KEY,
  job_id text NOT NULL,
  person text NOT NULL,
  at     timestamptz NOT NULL DEFAULT now(),
  what   text NOT NULL CHECK (what IN ('requested','started','step','planned','piece','prepared','decided',
                                       'stop','stopped','finished','failed')),
  detail jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS job_log_job ON assist.job_log (job_id, seq);
CREATE INDEX IF NOT EXISTS job_log_person ON assist.job_log (person, seq DESC) WHERE what = 'requested';
CREATE OR REPLACE FUNCTION assist.job_log_guard() RETURNS trigger LANGUAGE plpgsql AS $t$
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'assist.job_log is append-only'; END IF;
  IF current_user = 'plx_api' AND NEW.what NOT IN ('requested','stop','decided','prepared') THEN
    RAISE EXCEPTION 'the API may only append what the person does (%)', NEW.what;
  END IF;
  RETURN NEW;
END $t$;
DROP TRIGGER IF EXISTS job_log_guard ON assist.job_log;
CREATE TRIGGER job_log_guard BEFORE INSERT OR UPDATE OR DELETE ON assist.job_log
  FOR EACH ROW EXECUTE FUNCTION assist.job_log_guard();
REVOKE ALL ON assist.job_log FROM PUBLIC;
DO $api$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN GRANT USAGE ON SCHEMA assist TO plx_api; GRANT SELECT, INSERT ON assist.job_log TO plx_api; GRANT USAGE ON SEQUENCE assist.job_log_seq_seq TO plx_api; END IF; END $api$;
DO $ev$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN GRANT USAGE ON SCHEMA assist TO plx_eval; GRANT SELECT ON assist.job_log TO plx_eval; END IF; END $ev$;
