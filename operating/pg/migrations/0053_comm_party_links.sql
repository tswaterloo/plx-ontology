-- 0053_comm_party_links: the comms <-> party links in their own narrow table
-- (operating/pg/comm_links.py write_links). One row per derived link (view, message object id,
-- party kind and handle, how and how surely it was derived), carrying the door of BOTH ends:
-- the message row's (audience_keys, rec_marks, col_marks, ai_ok) and the party row's
-- (party_access_class, party_col_class, party_ai_ok); a reader must hold both. The message
-- tables are never updated for a link. Declared here, not by the writer (the declared-schema
-- rule: a served table's DDL lives in a migration); the writer's per-run stage stays its own
-- (an UNLOGGED table it makes and drops). A database without the biz schema is left alone.
-- Undo: DROP TABLE biz.comm_party_links.
DO $migration$
BEGIN
    IF to_regnamespace('biz') IS NULL THEN
        RETURN;
    END IF;
    CREATE TABLE IF NOT EXISTS biz.comm_party_links (
        view               text NOT NULL,
        object_id          text NOT NULL,
        party_kind         text NOT NULL,
        party_handle       text NOT NULL,
        via                text NOT NULL,
        confidence         text NOT NULL,
        handle             text,
        audience_keys      text[],
        rec_marks          text[],
        col_marks          jsonb,
        ai_ok              boolean,
        party_access_class integer,
        party_col_class    integer,
        party_ai_ok        boolean,
        PRIMARY KEY (view, object_id, party_kind, party_handle)
    );
END
$migration$;
