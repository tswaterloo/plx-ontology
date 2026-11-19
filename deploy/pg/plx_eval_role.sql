-- ops-hardening-1117: the read-only Postgres role every eval and harness runs as
-- (scripts/eval_guard.py sets PGUSER=plx_eval and refuses to start on any other login).
-- Run as the role that owns the schemas (the migrations' role), on the served database:
--     psql -X -v ON_ERROR_STOP=1 -d plx -f deploy/pg/plx_eval_role.sql
-- Idempotent. Logging in as it is a peer map (deploy/pg/README-roles.md "Logging in as a role").
--
-- What it may do: SELECT what the API reads (ontology*, search, plx_meta, memory, templates)
-- and keep session-private temporary tables (the store reader's item gate,
-- operating/pg/sqlite_compat.py). No INSERT, UPDATE, DELETE, TRUNCATE, CREATE or COPY FROM
-- on any shared table, no superuser, and every transaction starts read-only
-- (default_transaction_read_only). The 11-17 Ask eval that wrote served state is why.
DO $$
BEGIN
    CREATE ROLE plx_eval LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
        NOINHERIT CONNECTION LIMIT 80;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;
ALTER ROLE plx_eval NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
-- Two concurrent Ask hosts held ~15 connections each at their peak (each plx.sql/pages/search
-- pool is max_size 4 and busy during a tool-call burst; pools close one idle past 60 s and keep
-- at most 2, operating/pg/pool.py): a 32 cap failed 57 of 187 Ask tool calls with "too many
-- connections for role plx_eval". 80 leaves the served API (64 cap, ~16 used) and the loader
-- (64 cap) room under max_connections = 200.
ALTER ROLE plx_eval CONNECTION LIMIT 80;
ALTER ROLE plx_eval SET default_transaction_read_only = on;
ALTER ROLE plx_eval SET statement_timeout = '300s';
ALTER ROLE plx_eval SET lock_timeout = '30s';
-- a transaction left open between statements (a read stream not drained) gives its
-- login back after as long as one statement may run
ALTER ROLE plx_eval SET idle_in_transaction_session_timeout = '5min';

DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM plx_eval', current_database());
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO plx_eval', current_database());
END $$;
REVOKE CREATE ON SCHEMA public FROM plx_eval;

-- SELECT only, on each read schema that exists in this database; tables a later migration
-- adds (created by this file's role or by the loader's plx_owner) are readable too.
DO $$
DECLARE
    s text;
BEGIN
    FOREACH s IN ARRAY ARRAY['ontology', 'search', 'plx_meta', 'ontology_seen', 'ontology_bare',
                             'ontology_bare_seen', 'memory', 'templates'] LOOP
        IF to_regnamespace(s) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO plx_eval', s);
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM plx_eval', s);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO plx_eval', s);
        EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO plx_eval', s);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO plx_eval', s);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON SEQUENCES TO plx_eval', s);
        IF to_regrole('plx_owner') IS NOT NULL THEN
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE plx_owner IN SCHEMA %I '
                           'GRANT SELECT ON TABLES TO plx_eval', s);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE plx_owner IN SCHEMA %I '
                           'GRANT SELECT ON SEQUENCES TO plx_eval', s);
        END IF;
    END LOOP;
END $$;
-- the one superuser setting the store reader sets (Postgres 15+): bounding its temp files
GRANT SET ON PARAMETER temp_file_limit TO plx_eval;
