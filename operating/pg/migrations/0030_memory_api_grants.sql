-- 0030_memory_api_grants: the API's login (plx_api, deploy/pg/plx_api_role.sql) may use each
-- person's memory (migration 0020, operator_api/memory.py). 0020 granted it nothing, so on
-- the served database every Ask answered but wrote no memory row (AUDIT3-05, 11-15:
-- "permission denied for schema memory").
--
-- Exactly what the served routes run (statement inventory of operator_api/memory.py and
-- learning_memory.py, 11-15):
--   memory.log    SELECT (recall, the log screen, learning reads); INSERT ... ON CONFLICT DO
--                 NOTHING RETURNING id (log_event)
--   memory.item   SELECT, including SELECT ... FOR UPDATE (which needs UPDATE); INSERT ...
--                 RETURNING id (remember); UPDATE of at/confidence/importance/log_ids,
--                 superseded_by and deleted_at (refresh, supersede, correct, forget)
--   sequences     USAGE (nextval of each table's id) and SELECT
-- Never DELETE or TRUNCATE: a person forgets by deleted_at, and the people-exclusion purge
-- (``python3 -m operator_api.memory purge``) runs as the schema owner, not as the API. The
-- log stays append-only for the API both ways (no UPDATE grant, and its trigger).
-- Granted only where the role exists (a test database or a box without the switch).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT USAGE ON SCHEMA memory TO plx_api;
        GRANT SELECT, INSERT ON memory.log TO plx_api;
        GRANT SELECT, INSERT, UPDATE ON memory.item TO plx_api;
        GRANT USAGE, SELECT ON SEQUENCE memory.log_id_seq, memory.item_id_seq TO plx_api;
        -- a table a later migration adds to the schema is readable, never writable, until
        -- that migration grants what its writes need
        ALTER DEFAULT PRIVILEGES IN SCHEMA memory GRANT SELECT ON TABLES TO plx_api;
        ALTER DEFAULT PRIVILEGES IN SCHEMA memory GRANT SELECT ON SEQUENCES TO plx_api;
    END IF;
END
$$;
