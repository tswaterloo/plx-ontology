-- 0045_alerts_read_time: what the person's alerts view (biz_page.alerts, alerts.page_view_ddl)
-- re-checks at READ time, in the reader's own snapshot, so a people exclusion or a marking
-- tightened in place hides an alert at once (never after the next run): record_shows (the
-- record row's columns the sentence shows), source_table/sources (the rows its numbers rest
-- on), objects (every object their lineage names: own, read, per column). An alert written
-- before these were recorded has them NULL and is hidden until its rule runs again.
-- alert_runs.ok_at: the rule's last finished run (a failed or blocked run keeps it); pages say
-- "unavailable since" once it is older than two refresh cycles. The runner (alerts.ensure)
-- adds a missing column the same way, only when it is missing (no lock otherwise).
-- Columns only (nullable or defaulted): no rewrite, no objects, value or link row changes.
-- Undo: ALTER TABLE biz.alerts DROP COLUMN record_shows, DROP COLUMN source_table,
--   DROP COLUMN sources, DROP COLUMN objects; ALTER TABLE biz.alert_runs DROP COLUMN ok_at.
ALTER TABLE biz.alerts ADD COLUMN IF NOT EXISTS record_shows text[] NOT NULL DEFAULT '{}';
ALTER TABLE biz.alerts ADD COLUMN IF NOT EXISTS source_table text;
ALTER TABLE biz.alerts ADD COLUMN IF NOT EXISTS sources text[];
ALTER TABLE biz.alerts ADD COLUMN IF NOT EXISTS objects text[];
ALTER TABLE biz.alert_runs ADD COLUMN IF NOT EXISTS ok_at timestamptz;
