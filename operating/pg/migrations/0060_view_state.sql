-- 0060_view_state: contract C6 (master plan 4.3, specs 1.2 M1), freshness and health of the
-- business views. Written by the refresh only (operating/pg/view_state.py,
-- operating/pg/column_health.py); no view table, ontology row or link changes: no fingerprint
-- or digest moves. Undo: DROP TABLE biz.view_state, biz.column_health.
-- * biz.view_state (speed specs P2 + P6): one row per view the refresh keeps. watermark = the
--   change-feed seq the view's rows include; newest_source_at = the newest change of a type the
--   view reads; last_ok_at = when the view was last known to hold every change of its inputs
--   (built, or checked and nothing it reads had moved): the "as of" of anything shown from it;
--   failing_since / reason / fail_class / fail_sig / fail_count = the current failure (cleared by
--   a good build); quarantined_until = not retried before then (the same failure twice);
--   version = the view's code print; last_seconds, heartbeat_at, phase = the run's bookkeeping.
CREATE TABLE IF NOT EXISTS biz.view_state (
    view              text PRIMARY KEY,
    watermark         bigint,
    newest_source_at  timestamptz,
    last_ok_at        timestamptz,
    failing_since     timestamptz,
    reason            text,
    version           text,
    fail_class        text,
    fail_sig          text,
    fail_count        integer NOT NULL DEFAULT 0,
    quarantined_until timestamptz,
    last_seconds      double precision,
    heartbeat_at      timestamptz,
    phase             text);
-- * biz.column_health (contract C6 exactly): per view and column, measured after the refresh
--   built the view (the comms views on a 2% sample): fill = share of rows not NULL (0-1),
--   distinct_n (scalar columns), top_values = up to 12 short values with their counts (NULL for
--   a column a marking may hide, and for the comms views), cut_share = share of filled text
--   values at the column's largest length (a preview column, cut at one length, shows itself).
CREATE TABLE IF NOT EXISTS biz.column_health (
    view        text NOT NULL,
    "column"    text NOT NULL,
    fill        double precision,
    distinct_n  bigint,
    top_values  jsonb,
    cut_share   double precision,
    measured_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (view, "column"));
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON biz.view_state TO plx_api;
        REVOKE ALL ON biz.column_health FROM plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT SELECT ON biz.view_state TO plx_eval;
        REVOKE ALL ON biz.column_health FROM plx_eval;
    END IF;
END $$;
