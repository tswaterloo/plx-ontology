-- 0069_column_health_counts (Ask R3): biz.column_health without its values. column_health keeps
-- top_values (business values of a column) and stays closed to the API and eval roles; this
-- view is its counts only (fill share, distinct values, share of filled text values at the column's longest
-- length, when measured), readable by them so `describe` of a view can say how much of each column is
-- filled. Same standing as biz.view_state (no business rows). Undo: DROP VIEW.
CREATE OR REPLACE VIEW biz.column_health_counts AS
    SELECT view, "column", fill, distinct_n, cut_share, measured_at
    FROM biz.column_health;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON biz.column_health_counts TO plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT SELECT ON biz.column_health_counts TO plx_eval;
    END IF;
END $$;
