-- 0062_assist_readings: one model reading per message (contract C4; operating/assist_readings.py).
-- assist.readings: per message (a mail's message_id, so its mailbox copies are one message, or its
-- handle when it has none; a Teams message's handle) what it asks of whom (needs_act, actor, act,
-- act_class, due), the sender's promise and wait, counterpart, record numbers, summary, quote, the
-- findings (decision / request / complaint / praise, each quoted from the text), read_ver, the
-- text basis (whole or cut(n)) and a typed status (ok / screened with the reason / failed with
-- the reason and attempts). assist.reading_runs: per run its counts (due, withheld, read,
-- screened, failed, left) and seconds. A side table keyed by message: no comms view changes, no
-- fingerprint moves. Every reader role's grant is revoked (a reading quotes its message):
-- people read the findings only through biz_sql.comms_findings (made by the reading run and
-- the comms views' install, operating/pg/comms_findings.py) and Assist's own doors; plx_eval
-- (agents and the nightly scoring, owner 11-23: agents read everything) may select.
-- The findings pass's tables (0047) are folded in: each message it read becomes a reading of
-- read_ver 0 (its findings kept, no act fields: needs_act NULL, read again newest first), then
-- biz_sql.comms_findings (rebuilt over the readings by the next reading run, within minutes)
-- and the three old tables are dropped.
-- Undo: DROP SCHEMA assist CASCADE (the findings were copied, not moved: re-apply 0047 and
-- rerun the old pass to rebuild them).
CREATE SCHEMA IF NOT EXISTS assist;
CREATE TABLE IF NOT EXISTS assist.readings (source text NOT NULL CHECK (source IN ('emails', 'teams_messages')), message_key text NOT NULL, handles text[] NOT NULL CHECK (cardinality(handles) > 0), thread_key text, said_at timestamptz NOT NULL, read_ver smallint NOT NULL, model text NOT NULL DEFAULT '', text_basis text NOT NULL DEFAULT '', needs_act boolean, actor text NOT NULL DEFAULT '', act text NOT NULL DEFAULT '', act_class text CHECK (act_class IN ('reply', 'forward', 'send_document', 'system_entry', 'decide', 'do', 'follow_up')), due date, counterpart text NOT NULL DEFAULT '', counterpart_party text, records text[] NOT NULL DEFAULT '{}', promise text NOT NULL DEFAULT '', waiting_on text NOT NULL DEFAULT '', finding_kind text, topic text, quote text NOT NULL DEFAULT '', summary text NOT NULL DEFAULT '', findings jsonb NOT NULL DEFAULT '[]', from_side text NOT NULL DEFAULT '' CHECK (from_side IN ('us', 'customer', 'outside', '')), status text NOT NULL CHECK (status IN ('ok', 'screened', 'failed')), reason text NOT NULL DEFAULT '', attempts smallint NOT NULL DEFAULT 1, read_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (source, message_key));
CREATE INDEX IF NOT EXISTS readings_said ON assist.readings (said_at);
CREATE INDEX IF NOT EXISTS readings_handles ON assist.readings USING gin (handles);
CREATE INDEX IF NOT EXISTS readings_thread ON assist.readings (thread_key, said_at);
CREATE TABLE IF NOT EXISTS assist.reading_runs (run_at timestamptz PRIMARY KEY, status text NOT NULL, reason text NOT NULL DEFAULT '', counts jsonb NOT NULL DEFAULT '{}', seconds double precision);
REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM PUBLIC;
DO $migration$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_sql_reader') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM plx_sql_reader;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT USAGE ON SCHEMA assist TO plx_eval;
        GRANT SELECT ON ALL TABLES IN SCHEMA assist TO plx_eval;
    END IF;
    IF to_regclass('biz.comms_read') IS NULL THEN
        RETURN;
    END IF;
    CREATE TEMP TABLE m0062 (message_id text PRIMARY KEY, handles text[]) ON COMMIT DROP;
    IF to_regclass('biz.emails') IS NOT NULL THEN
        EXECUTE 'INSERT INTO m0062 SELECT e.message_id, array_agg(e.handle ORDER BY e.handle) '
            'FROM biz.emails e WHERE e.message_id IN (SELECT message_key FROM biz.comms_read '
            'WHERE source_view = ''emails'' AND status = ''none'') GROUP BY 1';
    END IF;
    INSERT INTO assist.readings (source, message_key, handles, said_at, read_ver, model,
        text_basis, findings, finding_kind, topic, from_side, status, reason, attempts, read_at)
    SELECT r.source_view, r.message_key, coalesce(h.handles, m.handles, ARRAY[r.message_key]),
        r.said_at, 0, r.model, '', coalesce(f.items, '[]'::jsonb), f.kind, f.topic,
        coalesce(f.from_side, ''), 'ok', 'read by the findings pass before 0062: no act fields',
        1, r.read_at
    FROM biz.comms_read r
    LEFT JOIN (SELECT source_view, message_key,
            jsonb_agg(jsonb_build_object('kind', kind, 'topic', topic, 'summary', summary,
                'who', who, 'quote', '') ORDER BY n) AS items,
            (array_agg(kind ORDER BY n))[1] AS kind, (array_agg(topic ORDER BY n))[1] AS topic,
            min(from_side) AS from_side
        FROM biz.comms_findings GROUP BY 1, 2) f USING (source_view, message_key)
    LEFT JOIN (SELECT DISTINCT ON (source_view, message_key) source_view, message_key, handles
        FROM biz.comms_findings ORDER BY source_view, message_key, n) h
        USING (source_view, message_key)
    LEFT JOIN m0062 m ON r.source_view = 'emails' AND m.message_id = r.message_key
    WHERE r.status IN ('found', 'none')
    ON CONFLICT (source, message_key) DO NOTHING;
    DROP VIEW IF EXISTS biz_sql.comms_findings;
    DROP TABLE IF EXISTS biz.comms_findings, biz.comms_read, biz.comms_findings_runs;
END
$migration$;
