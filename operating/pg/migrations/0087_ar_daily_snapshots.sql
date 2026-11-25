-- 0087_ar_daily_snapshots (PLAN-P2-A5): daily snapshots of open accounts receivable
-- (from biz.customer_balances) so AR trend over time (total open, gross, overdue, by
-- currency, aging bucket, and document type) can be answered.
--
-- Permissions: each snapshot row carries an append-only access_class (biz.access_classes)
-- that strictly folds the source customer_balances rows' access_class (audience_keys,
-- rec_marks, doc_marks, blocked, including OWNER_KEY for excluded people) AND every
-- col_class marking/need on the snapshotted columns (amount_due, amount_due_currency,
-- aging_bucket, document_type, days_overdue, as_of), plus ai_ok. Guarded views
-- biz_sql.ar_daily_snapshots (plx_sql_reader, ai_only=true) and
-- biz_page.ar_daily_snapshots (plx_page_reader, ai_only=false) are built via
-- door.view_ddl. Reader roles have all direct grants revoked on the base table.
CREATE TABLE IF NOT EXISTS biz.ar_daily_snapshots (
    snapshot_date      date    NOT NULL,
    currency           text    NOT NULL,
    aging_bucket       text    NOT NULL,
    document_type      text    NOT NULL,
    document_count     integer NOT NULL DEFAULT 0,
    net_amount_due     numeric NOT NULL DEFAULT 0,
    gross_amount_due   numeric NOT NULL DEFAULT 0,
    overdue_amount_due numeric NOT NULL DEFAULT 0,
    access_class       integer NOT NULL,
    col_class          integer NOT NULL DEFAULT 0,
    ai_ok              boolean NOT NULL DEFAULT true,
    handle             text    NOT NULL,
    PRIMARY KEY (snapshot_date, currency, aging_bucket, document_type, access_class, ai_ok)
);

CREATE INDEX IF NOT EXISTS ar_daily_snapshots_date
    ON biz.ar_daily_snapshots (snapshot_date);

REVOKE ALL ON biz.ar_daily_snapshots FROM PUBLIC;

DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['plx_api', 'plx_eval', 'plx_sql_reader', 'plx_page_reader'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON biz.ar_daily_snapshots FROM %I', r);
        END IF;
    END LOOP;
END $$;
