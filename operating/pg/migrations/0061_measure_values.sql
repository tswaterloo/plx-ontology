-- 0061_measure_values: contract C3 (master plan 4.3; Ask spec R2), the measures' values made by
-- the builder (operating/pg/measure_values.py) when the views change and read by tiles, pages,
-- alerts and Ask instead of a measure's statement per request: per measure, version, dims
-- (a declared dimension's value; '_per' the currency or unit; '_part' num / den of a rate),
-- period ('now', 'day:YYYY-MM-DD', 'month:YYYY-MM') and door classes, the count of the grain (n)
-- and the value's sum. Counts and sums of business rows: the reader roles get no grant (the
-- served read path reads it through its own door, speed specs SV6 / Ask V1). Written by the
-- builder only, one transaction per build; no view table, ontology row or link changes: no
-- print or digest moves. Undo: DROP TABLE biz.measure_values.
CREATE TABLE IF NOT EXISTS biz.measure_values (
    measure      text NOT NULL,
    version      text NOT NULL,
    dims         jsonb NOT NULL DEFAULT '{}'::jsonb,
    period       text NOT NULL,
    access_class integer NOT NULL,
    col_class    integer NOT NULL,
    n            bigint NOT NULL,
    sum          numeric,
    as_of        timestamptz NOT NULL);
CREATE INDEX IF NOT EXISTS measure_values_read
    ON biz.measure_values (measure, version, period) INCLUDE (dims);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        REVOKE ALL ON biz.measure_values FROM plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        REVOKE ALL ON biz.measure_values FROM plx_eval;
    END IF;
END $$;
