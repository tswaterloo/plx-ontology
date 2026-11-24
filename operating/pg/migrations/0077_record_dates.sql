-- 0077_record_dates (ASK12 dates, review finding): biz.record_dates, the declared day of a
-- record the build wrote before the declarations existed (date_declare apply). Until now only
-- make_views / apply made it, so between a deploy and the apply the label search statements in
-- operating/pg/records_search.py (which join it) failed with "relation does not exist".
-- The same DDL as generic_views.DATES_DDL (idempotent); the read grant is explicit: the search
-- reads it as plx_api / plx_eval, beside biz.records. One day per record; no text, no marking.
-- Undo: DROP TABLE biz.record_dates (every reader then reads biz.records.record_date alone).
CREATE TABLE IF NOT EXISTS biz.record_dates (
    type text NOT NULL, rid bigint NOT NULL, ver bigint NOT NULL, day text NOT NULL,
    field text NOT NULL, PRIMARY KEY (type, rid));
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON biz.record_dates TO plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT SELECT ON biz.record_dates TO plx_eval;
    END IF;
END $$;
