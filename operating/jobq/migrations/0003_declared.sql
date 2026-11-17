-- What each job declares for itself: the disk its peak may take on one file system, and
-- who started it (tools/job.py, tools/testslot.py), so admission refuses a job the disk
-- cannot hold and the status shows each one's load.
ALTER TABLE jobq.jobs ADD COLUMN IF NOT EXISTS disk_mb integer NOT NULL DEFAULT 0
  CHECK (disk_mb >= 0);
ALTER TABLE jobq.jobs ADD COLUMN IF NOT EXISTS disk_path text;
ALTER TABLE jobq.jobs ADD COLUMN IF NOT EXISTS lane text;
CREATE INDEX IF NOT EXISTS jobs_held_lane ON jobq.jobs (lane)
  WHERE state IN ('running', 'preempting');
