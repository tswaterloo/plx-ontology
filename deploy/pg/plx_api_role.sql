-- SEC-S-13: the least-privilege Postgres role the API (python3 -m operator_api serve) runs as.
-- Run as the role that owns the schemas (the migrations' role), on the API's database:
--     psql -X -v ON_ERROR_STOP=1 -d plx -f deploy/pg/plx_api_role.sql
-- Idempotent. It only creates and grants; switching the API over is a separate deploy step
-- (deploy/README.md, "The API's Postgres role").
--
-- What the API writes, measured 11-13 (code inventory of every statement reachable from the
-- served routes, and log_statement=mod over the Postgres HTTP tests: no write but the two
-- logs below):
--   search.query_log   INSERT ... RETURNING id (each search), DELETE of rows past retention
--                      (at most hourly per process; operating/search/core.py _log, prune)
--   search.feedback    INSERT ... RETURNING id (an answer's rating; operator_api/backend.py)
--   temporary tables   sq_calls / sq_pending / sq_lists and COPY FROM STDIN into them (the
--                      item gate of operating/pg/sqlite_compat.py; session-local)
--   temp_file_limit    SET per session (operating/pg/sqlite_compat.py: a facts read bounds
--                      its temporary files at 1GB; without the grant the SET is refused
--                      and skipped, and the server's 25GB limit holds on a near-full disk:
--                      found by deploy/pg/role_walk.py, 11-13)
-- Everything else is read: ontology.*, search.*, plx_meta.*, their sequences' positions
-- (search.write_generation: what the suggest cache is keyed by; the test run found it), and
-- the public functions of the
-- vector, pg_trgm and unaccent extensions). It never needs CREATE, ALTER, TRUNCATE, COPY to
-- or from a file or program, other roles' rights, or the superuser.
DO $$
BEGIN
    CREATE ROLE plx_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
        NOINHERIT CONNECTION LIMIT 64;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;
ALTER ROLE plx_api NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
-- harden-1114: every session of the role is bounded even where the code sets no limit of
-- its own (a bare psycopg connection: the feedback log, health reads). The API's own
-- readers SET their limits per session (sqlite_compat: 120 s; a request's deadline lower),
-- which these defaults never override. A statement past 5 minutes, or a read queued over
-- 30 s behind another session's lock (a migration's DDL), ends with an error the route
-- reports, instead of holding a request worker and its connection with no end.
ALTER ROLE plx_api SET statement_timeout = '300s';
ALTER ROLE plx_api SET lock_timeout = '30s';
ALTER ROLE plx_api SET idle_in_transaction_session_timeout = '15min';

DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM plx_api', current_database());
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO plx_api', current_database());
END $$;

REVOKE CREATE ON SCHEMA public FROM plx_api;
GRANT USAGE ON SCHEMA ontology, search, plx_meta TO plx_api;
GRANT SELECT ON ALL TABLES IN SCHEMA ontology, search, plx_meta TO plx_api;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA ontology, search, plx_meta TO plx_api;
GRANT INSERT, DELETE ON search.query_log TO plx_api;
GRANT INSERT ON search.feedback TO plx_api;
GRANT USAGE ON SEQUENCE search.query_log_id_seq, search.feedback_id_seq TO plx_api;
-- tables a later migration adds (created by the role running this file) are readable too
ALTER DEFAULT PRIVILEGES IN SCHEMA ontology, search, plx_meta GRANT SELECT ON TABLES TO plx_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA ontology, search, plx_meta GRANT SELECT ON SEQUENCES TO plx_api;
-- the one superuser setting the API sets (Postgres 15+): bounding its own temporary files
GRANT SET ON PARAMETER temp_file_limit TO plx_api;

-- CC-C-04: the reader's view without the marked mention repeats (migration 0029), read
-- only when PLX_PG_MENTION_REPEATS=1 puts ontology_seen on the search_path.
DO $$
BEGIN
    IF to_regclass('ontology_seen.links') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA ontology_seen TO plx_api;
        GRANT SELECT ON ontology_seen.links TO plx_api;
    END IF;
END
$$;
-- templates-1113: each person's saved templates (operator_api/templates.py, migration 0022):
-- the API lists, saves, changes, counts uses of and deletes them (and the exclusion purge
-- removes rows). Granted only once the migration made the table.
DO $$
BEGIN
    IF to_regclass('templates.template') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA templates TO plx_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON templates.template TO plx_api;
        GRANT USAGE ON SEQUENCE templates.template_id_seq TO plx_api;
    END IF;
END $$;
-- grants-1115 (AUDIT3-05): each person's memory (operator_api/memory.py, migration 0020):
-- the API appends to the log, keeps, refreshes, supersedes and forgets (deleted_at) items,
-- and reads both; never DELETE (the exclusion purge runs as the schema owner). The same
-- grants as migration 0030, for a role made after the migrations ran.
DO $$
BEGIN
    IF to_regclass('memory.item') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA memory TO plx_api;
        GRANT SELECT, INSERT ON memory.log TO plx_api;
        GRANT SELECT, INSERT, UPDATE ON memory.item TO plx_api;
        GRANT USAGE, SELECT ON SEQUENCE memory.log_id_seq, memory.item_id_seq TO plx_api;
        ALTER DEFAULT PRIVILEGES IN SCHEMA memory GRANT SELECT ON TABLES TO plx_api;
        ALTER DEFAULT PRIVILEGES IN SCHEMA memory GRANT SELECT ON SEQUENCES TO plx_api;
    END IF;
END $$;
