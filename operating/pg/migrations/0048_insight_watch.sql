-- 0048_insight_watch: a saved insight can watch a number (operator_api/insights_runs.py).
-- insights.insight.watch: {"column": <a column of its result>, "op": "above" | "below",
-- "value": <number>} ('{}' none); notify 'crosses': what it found comes to its owner on Home
-- when, in a run by itself (as them, through their own door), some row's value in that
-- column is past the line and in the run before none was (the first run: any row past it).
-- insights.run.over_rows: how many rows were past the line in that run (NULL: it watches
-- nothing). No business row, value or text is kept: the person's own line and a count.
-- Undo: ALTER TABLE insights.run DROP COLUMN over_rows; ALTER TABLE insights.insight DROP
-- COLUMN watch; the notify check back to 0046's list (after setting 'crosses' rows to 'rows').
ALTER TABLE insights.insight ADD COLUMN IF NOT EXISTS watch jsonb NOT NULL DEFAULT '{}';
ALTER TABLE insights.insight DROP CONSTRAINT IF EXISTS insight_notify_check;
ALTER TABLE insights.insight ADD CONSTRAINT insight_notify_check
    CHECK (notify IN ('never', 'rows', 'new_rows', 'every_run', 'crosses'));
ALTER TABLE insights.insight DROP CONSTRAINT IF EXISTS insight_watch_check;
ALTER TABLE insights.insight ADD CONSTRAINT insight_watch_check
    CHECK (jsonb_typeof(watch) = 'object' AND (notify <> 'crosses' OR watch ? 'column'));
ALTER TABLE insights.run ADD COLUMN IF NOT EXISTS over_rows integer NULL
    CHECK (over_rows IS NULL OR over_rows >= 0);
