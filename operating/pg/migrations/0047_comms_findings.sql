-- 0047_comms_findings: decisions, requests and customer feedback read out of mail and Teams
-- text (operating/pg/comms_findings.py). comms_findings: per message (a mail's message_id: its
-- mailbox copies are one message; a Teams message's handle) the items found in it: kind,
-- topic, a one-sentence summary, who decided or asked as the message names them, when, and
-- every copy's handle. No body, quote or address. comms_read: per message when it was read
-- and what came of it (found / none / screened with a reason / failed with attempts), so a
-- message is read once. comms_findings_runs: per run its counts (no text).
-- The runner (python3 -m operating.pg.comms_findings run) creates the same tables IF NOT
-- EXISTS (comms_findings.ddl); this file is that DDL for schema biz. The business build swaps
-- only its own views' tables, never these. No objects, value or link row changes: no digest
-- moves. The guarded view biz_sql.comms_findings (comms_findings.view_ddl: a finding shows
-- only with a copy of its message the reader's own guarded emails / teams_messages view shows)
-- is made by the runner and again with the communications views (sql_views.create), granted
-- to plx_sql_reader only.
-- Undo: DROP VIEW biz_sql.comms_findings; DROP TABLE biz.comms_findings, biz.comms_read,
-- biz.comms_findings_runs.
CREATE TABLE IF NOT EXISTS biz.comms_findings (source_view text NOT NULL CHECK (source_view IN ('emails', 'teams_messages')), message_key text NOT NULL, n smallint NOT NULL CHECK (n >= 0), handles text[] NOT NULL CHECK (cardinality(handles) > 0), kind text NOT NULL CHECK (kind IN ('decision', 'request', 'complaint', 'praise')), topic text NOT NULL CHECK (length(topic) <= 40), summary text NOT NULL CHECK (summary <> '' AND length(summary) <= 240), who text NOT NULL DEFAULT '' CHECK (length(who) <= 80), from_side text NOT NULL DEFAULT '' CHECK (from_side IN ('us', 'customer', 'outside', '')), said_at timestamptz NOT NULL, model text NOT NULL, found_at timestamptz NOT NULL, PRIMARY KEY (source_view, message_key, n));
CREATE INDEX IF NOT EXISTS comms_findings_said ON biz.comms_findings (said_at);
CREATE TABLE IF NOT EXISTS biz.comms_read (source_view text NOT NULL, message_key text NOT NULL, said_at timestamptz NOT NULL, read_at timestamptz NOT NULL, status text NOT NULL CHECK (status IN ('found', 'none', 'screened', 'failed')), reason text NOT NULL DEFAULT '' CHECK (length(reason) <= 40), found smallint NOT NULL DEFAULT 0, attempts smallint NOT NULL DEFAULT 1, model text NOT NULL DEFAULT '', PRIMARY KEY (source_view, message_key));
CREATE INDEX IF NOT EXISTS comms_read_said ON biz.comms_read (said_at);
CREATE TABLE IF NOT EXISTS biz.comms_findings_runs (run_at timestamptz PRIMARY KEY, status text NOT NULL, reason text NOT NULL DEFAULT '' CHECK (length(reason) <= 200), candidates integer NOT NULL DEFAULT 0, read integer NOT NULL DEFAULT 0, found integer NOT NULL DEFAULT 0, screened integer NOT NULL DEFAULT 0, failed integer NOT NULL DEFAULT 0, calls integer NOT NULL DEFAULT 0, pruned integer NOT NULL DEFAULT 0, seconds double precision);
