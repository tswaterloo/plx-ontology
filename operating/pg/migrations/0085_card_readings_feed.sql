-- 0085_card_readings_feed: read-only face of assist.readings for record cards (ASK-GENIE 19 S9).
-- Exposes only the structured thread-state columns of ok readings (no screened/failed rows, no
-- raw quote or findings jsonb) to plx_api and plx_eval. Record cards query this view ONLY for
-- message handles already returned by the reader's guarded biz_sql.emails / biz_sql.teams_messages
-- views. plx_sql_reader holds NO grant on assist.* (arbitrary model SQL stays isolated).
-- Additive view in schema assist only; no biz.* or comms view changes, no fingerprint moves.
-- Undo: DROP VIEW IF EXISTS assist.card_readings;
CREATE OR REPLACE VIEW assist.card_readings AS
SELECT source,
       message_key,
       handles,
       thread_key,
       said_at,
       from_side,
       needs_act,
       actor,
       actor_person,
       actor_basis,
       sender_person,
       act,
       act_class,
       due,
       promise,
       waiting_on,
       counterpart,
       summary,
       read_ver,
       read_at
FROM assist.readings
WHERE status = 'ok';
REVOKE ALL ON assist.card_readings FROM PUBLIC;
DO $card_readings$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_sql_reader') THEN
        REVOKE ALL ON assist.card_readings FROM plx_sql_reader;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT USAGE ON SCHEMA assist TO plx_api;
        GRANT SELECT ON assist.card_readings TO plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT USAGE ON SCHEMA assist TO plx_eval;
        GRANT SELECT ON assist.card_readings TO plx_eval;
    END IF;
END
$card_readings$;
