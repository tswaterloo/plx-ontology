-- 0046_insights: each person's saved insights (operator_api/insights.py): dashboard tiles
-- that stay live, monitors and recurring reports.
--
-- insights.insight: one insight a person saved (``owner``: their principals key,
-- operator_api.auth.Reader.key): a title, the question it answers in plain words, ONE
-- SELECT over the guarded business views (run again through plx.sql as the person every
-- time it is shown or checked, so it reads only what they may read NOW), how to draw it
-- (``chart``: kind, label column, value columns), and when it runs by itself
-- (``schedule``: '' never, 'hourly', 'daily', 'weekly') with what makes it come to them
-- (``notify``: 'never', 'rows' when it returns any row, 'new_rows' when a row appears
-- that the last run did not have, 'every_run'). Deleting sets ``deleted_at``.
-- insights.run: when an insight ran by itself, how many rows it returned, a digest of each
-- row's key (to tell a new row from one seen before) and whether it came to its owner. No
-- business row, value or text is stored here: the person opens the insight and it reads
-- again through the door.

CREATE SCHEMA IF NOT EXISTS insights;

CREATE TABLE IF NOT EXISTS insights.insight (
    id            bigserial PRIMARY KEY,
    owner         text NOT NULL CHECK (owner <> ''),
    title         text NOT NULL CHECK (title <> '' AND length(title) <= 160),
    question      text NOT NULL DEFAULT '' CHECK (length(question) <= 2000),
    sql           text NOT NULL CHECK (sql <> '' AND length(sql) <= 8000),
    chart         jsonb NOT NULL DEFAULT '{}',
    schedule      text NOT NULL DEFAULT '' CHECK (schedule IN ('', 'hourly', 'daily', 'weekly')),
    notify        text NOT NULL DEFAULT 'never'
                  CHECK (notify IN ('never', 'rows', 'new_rows', 'every_run')),
    position      integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz NULL
);
CREATE INDEX IF NOT EXISTS insight_owner ON insights.insight (owner, position, id)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS insight_due ON insights.insight (schedule)
    WHERE deleted_at IS NULL AND schedule <> '';

CREATE TABLE IF NOT EXISTS insights.run (
    id            bigserial PRIMARY KEY,
    insight_id    bigint NOT NULL REFERENCES insights.insight (id),
    ran_at        timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL CHECK (status IN ('ok', 'failed')),
    reason        text NOT NULL DEFAULT '' CHECK (length(reason) <= 200),
    row_count     integer NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    keys          text[] NOT NULL DEFAULT '{}' CHECK (cardinality(keys) <= 500),
    new_rows      integer NOT NULL DEFAULT 0 CHECK (new_rows >= 0),
    fired         boolean NOT NULL DEFAULT false,
    seen_at       timestamptz NULL
);
CREATE INDEX IF NOT EXISTS run_insight ON insights.run (insight_id, ran_at DESC);

-- the API's login: list, save, edit, delete (by deleted_at) its readers' insights and
-- record and read their runs; never DELETE (the people-exclusion purge runs as the owner)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT USAGE ON SCHEMA insights TO plx_api;
        GRANT SELECT, INSERT, UPDATE ON insights.insight, insights.run TO plx_api;
        GRANT USAGE, SELECT ON SEQUENCE insights.insight_id_seq, insights.run_id_seq TO plx_api;
    END IF;
END
$$;
