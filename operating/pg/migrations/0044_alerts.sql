-- 0044_alerts: the alerts table the SQL alert rules write (operating/pg/alerts.py; ARCH-FINAL-1117
-- section 6) and its per-rule run record. One row per (rule, record handle, topic, person it
-- concerns): open or resolved, since when, why (one sentence), and what a reader must hold to see
-- it (access_class, needs: every class of every row the verdict was computed from; col_needs:
-- column classes of the cells the sentence shows; units: a mail alert's comms units). Pages read
-- it only through alerts.read_sql (every one of those held, else not shown). The AI's door
-- (biz_sql) has no view over it. alert_runs: per rule its last run (ok / blocked / failed, counts,
-- seconds, skipped: records judged and left out, per reason).
-- The runner (python3 -m operating.pg.alerts run) creates the same tables IF NOT EXISTS
-- (alerts._ddl); this file is that DDL for schema biz. The business build swaps only its own
-- views' tables, never these. No objects, value or link row changes: no digest moves.
-- The person's guarded view biz_page.alerts (alerts.page_view_ddl) is made by the runner once
-- 0043's schema exists (alerts.ensure), granted to plx_page_reader only.
-- Undo: DROP VIEW biz_page.alerts; DROP TABLE biz.alerts, biz.alert_runs.
CREATE TABLE IF NOT EXISTS biz.alerts (rule text NOT NULL, kind text NOT NULL, record_table text NOT NULL, handle text NOT NULL, topic text NOT NULL DEFAULT '', concerns text NOT NULL DEFAULT '', private boolean NOT NULL DEFAULT false, mailbox text, why text NOT NULL, since timestamptz NOT NULL, first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL, state text NOT NULL CHECK (state IN ('open', 'resolved')), resolved_at timestamptz, access_class smallint, needs smallint[] NOT NULL, col_needs integer[] NOT NULL DEFAULT '{}', units integer[] NOT NULL DEFAULT '{}', ai_ok boolean NOT NULL, PRIMARY KEY (rule, handle, topic, concerns), CHECK (access_class IS NOT NULL OR cardinality(units) > 0));
CREATE INDEX IF NOT EXISTS alerts_open ON biz.alerts (state, kind, since);
CREATE INDEX IF NOT EXISTS alerts_record ON biz.alerts (record_table, handle);
CREATE INDEX IF NOT EXISTS alerts_concerns ON biz.alerts (concerns) WHERE concerns <> '';
CREATE TABLE IF NOT EXISTS biz.alert_runs (rule text PRIMARY KEY, run_at timestamptz NOT NULL, status text NOT NULL, reason text, open integer, opened integer, resolved integer, seconds double precision);
ALTER TABLE biz.alert_runs ADD COLUMN IF NOT EXISTS skipped jsonb;
ALTER TABLE biz.alerts ADD COLUMN IF NOT EXISTS shows text[] NOT NULL DEFAULT '{}';
