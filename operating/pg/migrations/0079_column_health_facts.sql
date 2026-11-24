-- 0079_column_health_facts (ASK-GENIE 20 #5, G8): value facts on biz.column_health: the column's
-- kind (date, number, text, flag), its smallest and largest value (dates and numbers of a column
-- no marking may hide, never for a comms view: it is read on a sample) and top_share, the share
-- of its filled rows that hold its commonest value (a stamped date or a default shows as a share
-- near 1). The closed table keeps min and max; the counts view (readable by plx_api and plx_eval)
-- shows top_share, kind and the range of DATE columns only (a date range is no business figure;
-- an amount's range stays closed, as top_values does). Additive: three columns on a 549-row table
-- and the counts view's new columns at its end; no view table, ontology row or link changes, so
-- no fingerprint or digest moves. Undo: drop the three columns and recreate the 0069 view.
ALTER TABLE biz.column_health ADD COLUMN IF NOT EXISTS kind text;
ALTER TABLE biz.column_health ADD COLUMN IF NOT EXISTS min_value text;
ALTER TABLE biz.column_health ADD COLUMN IF NOT EXISTS max_value text;
ALTER TABLE biz.column_health ADD COLUMN IF NOT EXISTS top_share double precision;
CREATE OR REPLACE VIEW biz.column_health_counts AS
    SELECT view, "column", fill, distinct_n, cut_share, measured_at,
           kind, top_share,
           CASE WHEN kind = 'date' THEN min_value END AS range_min,
           CASE WHEN kind = 'date' THEN max_value END AS range_max
    FROM biz.column_health;
