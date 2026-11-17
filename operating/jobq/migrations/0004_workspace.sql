-- The workspace that started a job (a checkout or a service, as tools/job.py and
-- tools/testslot.py name it) is the column workspace. Checkouts older than this file
-- still write the earlier column, lane, so both stay until every one writes workspace:
-- an insert that fills only one of the two gets the other copied from it.
ALTER TABLE jobq.jobs ADD COLUMN IF NOT EXISTS workspace text;
UPDATE jobq.jobs SET workspace = lane WHERE workspace IS NULL AND lane IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_held_workspace ON jobq.jobs (workspace)
  WHERE state IN ('running', 'preempting');
CREATE OR REPLACE FUNCTION jobq.jobs_workspace_copy() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.workspace := COALESCE(NEW.workspace, NEW.lane);
  NEW.lane := COALESCE(NEW.lane, NEW.workspace);
  RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS jobs_workspace_copy ON jobq.jobs;
CREATE TRIGGER jobs_workspace_copy BEFORE INSERT ON jobq.jobs
  FOR EACH ROW EXECUTE FUNCTION jobq.jobs_workspace_copy();
