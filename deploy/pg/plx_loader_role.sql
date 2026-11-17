-- ARCH T7: the loaders' least-privilege Postgres login (the ontology delta and follower, the
-- search follower and generation pass), instead of the host owner's superuser login.
-- Run as a superuser on the served database, with psql (it uses \gexec):
--     psql -X -d plx -f deploy/pg/plx_loader_role.sql
-- Idempotent; re-run until it reports 0 tables left. Undo: deploy/pg/plx_loader_role_undo.sql.
-- Switching a unit over is a separate step (deploy/pg/README-roles.md).
--
-- Design. The loaders create, index, alter, truncate, analyze and vacuum the tables of the
-- data schemas, so they need a table owner's rights there; doing that as a superuser gave
-- them everything else too (COPY ... PROGRAM, server files, ALTER SYSTEM, roles, every
-- database). Now:
--   plx_owner   NOLOGIN, no superuser: owns the tables and sequences of the data schemas
--               (ontology, search, plx_meta, memory, delta_stage, steps_stage), may create
--               tables there (USAGE, CREATE on each schema; the schemas stay a superuser's).
--   plx_loader  LOGIN, no superuser: a member of plx_owner, and every session of it in plx
--               runs as plx_owner (ALTER ROLE ... SET role), so what it creates is
--               plx_owner's too. Nothing else: no other database, no schema, no role, no
--               superuser setting but temp_file_limit (which the delta sets per transaction).
-- Code stays a superuser's. Superuser sessions (migrations, the ingest host, agents, the API
-- until it runs as plx_api) read and write these tables, so anything the loader could
-- define that runs inside them would be a way up. So: functions, procedures, views, types
-- and the schemas themselves are not moved (and are moved back if an earlier version of
-- this file moved them), and the event trigger plx_no_code refuses every code-defining
-- statement (CREATE/ALTER FUNCTION, PROCEDURE, TRIGGER, RULE, VIEW, POLICY, OPERATOR, CAST,
-- AGGREGATE, TYPE, DOMAIN, SCHEMA, ...) to a session whose user is no superuser. The event
-- trigger plx_data_owner gives a table a superuser creates in a data schema later (a
-- migration) to plx_owner at once; a failure there only warns.
-- Each ALTER ... OWNER is its own transaction (psql autocommit, \gexec) with a 3 s lock
-- wait, so a long reader makes one statement fail, never a queue behind a held lock; re-run
-- for what is left.
\set ON_ERROR_STOP 1
DO $$
BEGIN
    CREATE ROLE plx_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
        NOBYPASSRLS NOINHERIT;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;
ALTER ROLE plx_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
DO $$
BEGIN
    CREATE ROLE plx_loader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
        NOBYPASSRLS INHERIT CONNECTION LIMIT 64;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;
ALTER ROLE plx_loader NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT;
GRANT plx_owner TO plx_loader WITH INHERIT TRUE, SET TRUE;
DO $$
BEGIN
    EXECUTE format('ALTER ROLE plx_loader IN DATABASE %I SET role = plx_owner',
                   current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO plx_loader', current_database());
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO plx_owner', current_database());
    -- an earlier version of this file let it create schemas
    EXECUTE format('REVOKE CREATE ON DATABASE %I FROM plx_owner', current_database());
END $$;
GRANT SET ON PARAMETER temp_file_limit TO plx_owner;
-- the extensions' functions (vector, pg_trgm, unaccent) are executable by everyone already

-- Which schemas are data schemas: one definition, used here and by the trigger.
CREATE SCHEMA IF NOT EXISTS plx_admin;
REVOKE ALL ON SCHEMA plx_admin FROM PUBLIC;
CREATE OR REPLACE FUNCTION plx_admin.is_data_schema(name text) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS $f$
    SELECT name IN ('ontology', 'search', 'plx_meta', 'memory', 'delta_stage', 'steps_stage')
$f$;
-- the event trigger runs as whoever ran the DDL (a loader too): it may look the rule up,
-- nothing in plx_admin may be created or changed but by a superuser
GRANT USAGE ON SCHEMA plx_admin TO PUBLIC;
GRANT EXECUTE ON FUNCTION plx_admin.is_data_schema(text) TO PUBLIC;

-- The moves: tables (their indexes, TOAST and owned sequences follow) and standalone
-- sequences to plx_owner; schemas, views, functions and types back to the superuser if an
-- earlier version of this file moved them.
SET lock_timeout = '3s';
\set ON_ERROR_STOP 0
SELECT format('ALTER SCHEMA %I OWNER TO %I', nspname, current_user)
FROM pg_namespace WHERE nspowner = 'plx_owner'::regrole
\gexec
SELECT format('ALTER %s %I.%I OWNER TO %I',
              CASE c.relkind WHEN 'v' THEN 'VIEW' ELSE 'MATERIALIZED VIEW' END,
              n.nspname, c.relname, current_user)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v', 'm') AND c.relowner = 'plx_owner'::regrole
\gexec
SELECT format('ALTER ROUTINE %s OWNER TO %I', p.oid::regprocedure, current_user)
FROM pg_proc p WHERE p.proowner = 'plx_owner'::regrole
\gexec
SELECT format('ALTER TYPE %s OWNER TO %I', t.oid::regtype, current_user)
FROM pg_type t WHERE t.typowner = 'plx_owner'::regrole AND t.typtype IN ('e', 'd', 'r', 'm')
\gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA %I TO plx_owner', nspname)
FROM pg_namespace WHERE plx_admin.is_data_schema(nspname)
\gexec
-- the superuser's views (and any table not moved yet) stay readable to the loader; a view
-- runs as its owner, so a view must never be the loader's while a table under it is not
-- (11-13 8 PM: two views moved before their tables failed every reader until granted)
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO plx_owner', nspname)
FROM pg_namespace WHERE plx_admin.is_data_schema(nspname)
\gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO plx_owner',
              nspname)
FROM pg_namespace WHERE plx_admin.is_data_schema(nspname)
\gexec
SELECT format('ALTER %s %I.%I OWNER TO plx_owner',
              CASE c.relkind WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE plx_admin.is_data_schema(n.nspname) AND c.relkind IN ('r', 'p', 'S')
  AND c.relowner <> 'plx_owner'::regrole
  -- owned by a table (a sequence); a partition depends on its parent yet has its own owner
  -- (ALTER TABLE ... OWNER on a partitioned table does not recurse), and a partitioned
  -- table's internal dependency on its own key columns is no owner
  AND (c.relispartition
       OR NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                      AND d.objid = c.oid AND d.deptype IN ('a', 'i', 'e')
                      AND d.refclassid = 'pg_class'::regclass AND d.refobjid <> c.oid))
ORDER BY c.relkind, n.nspname, c.relname
\gexec
\set ON_ERROR_STOP 1
RESET lock_timeout;

-- No code from a user who is no superuser: in this database a loader (or any other login)
-- defines tables, indexes and sequences, never something that runs (statement tags in
-- PostgreSQL 17's event trigger matrix).
CREATE OR REPLACE FUNCTION plx_admin.no_code() RETURNS event_trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $f$
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'plx_no_code: % is a superuser''s statement in this database (ARCH T7)',
            tg_tag USING ERRCODE = 'insufficient_privilege';
    END IF;
END
$f$;
REVOKE ALL ON FUNCTION plx_admin.no_code() FROM PUBLIC;
DROP EVENT TRIGGER IF EXISTS plx_no_code;
CREATE EVENT TRIGGER plx_no_code ON ddl_command_start
    WHEN TAG IN ('CREATE FUNCTION', 'ALTER FUNCTION', 'CREATE PROCEDURE', 'ALTER PROCEDURE',
                 'ALTER ROUTINE', 'CREATE TRIGGER', 'ALTER TRIGGER', 'CREATE RULE',
                 'CREATE VIEW', 'ALTER VIEW', 'CREATE MATERIALIZED VIEW',
                 'ALTER MATERIALIZED VIEW', 'CREATE POLICY', 'ALTER POLICY',
                 'CREATE OPERATOR', 'ALTER OPERATOR', 'CREATE OPERATOR CLASS',
                 'CREATE OPERATOR FAMILY', 'ALTER OPERATOR FAMILY', 'CREATE CAST',
                 'CREATE AGGREGATE', 'ALTER AGGREGATE', 'CREATE TYPE', 'ALTER TYPE',
                 'CREATE DOMAIN', 'ALTER DOMAIN', 'CREATE SCHEMA', 'ALTER SCHEMA',
                 'CREATE LANGUAGE', 'CREATE CONVERSION', 'CREATE COLLATION',
                 'CREATE TEXT SEARCH CONFIGURATION', 'CREATE TEXT SEARCH DICTIONARY',
                 'CREATE TEXT SEARCH PARSER', 'CREATE TEXT SEARCH TEMPLATE',
                 'CREATE ACCESS METHOD', 'CREATE STATISTICS', 'CREATE PUBLICATION',
                 'CREATE SUBSCRIPTION', 'CREATE FOREIGN DATA WRAPPER', 'CREATE SERVER',
                 'CREATE USER MAPPING', 'CREATE FOREIGN TABLE', 'CREATE EXTENSION',
                 'ALTER EXTENSION', 'CREATE TRANSFORM',
                 'ALTER DEFAULT PRIVILEGES', 'SECURITY LABEL', 'IMPORT FOREIGN SCHEMA')
    EXECUTE FUNCTION plx_admin.no_code();

-- Nor an expression that runs in someone else's session: a column default, generated
-- column, CHECK constraint or index expression of a table runs with the rights of whoever
-- writes the row (a superuser session), so a non-superuser's table may call only the plain
-- functions below in them (lo_export, pg_reload_conf and the like never). Checked after
-- every table or index statement of a non-superuser; anything else is refused (the
-- statement rolls back). Temporary tables are the session's own and are not checked.
CREATE OR REPLACE FUNCTION plx_admin.safe_expr(expr text) RETURNS boolean
    LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $f$
    SELECT NOT EXISTS (
        SELECT 1 FROM regexp_matches(coalesce(expr, ''),
                                     '([A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?)\s*\(',
                                     'g') AS m(f)
        WHERE lower(m.f[1]) NOT IN (
            'nextval', 'now', 'clock_timestamp', 'statement_timestamp',
            'transaction_timestamp', 'gen_random_uuid', 'lower', 'upper', 'length',
            'char_length', 'octet_length', 'md5', 'btrim', 'ltrim', 'rtrim', 'substr',
            'substring', 'left', 'right', 'replace', 'coalesce', 'nullif', 'greatest',
            'least', 'abs', 'round', 'floor', 'ceil', 'array_length', 'cardinality',
            'jsonb_typeof', 'json_typeof', 'to_tsvector', 'array', 'row', 'date_trunc',
            'search.alias_text', 'ontology.sq_words', 'any', 'all', 'in', 'numeric',
            'character varying', 'varchar', 'timestamp', 'timestamptz', 'interval',
            'bpchar', 'text', 'setweight', 'array_position', 'position',
            -- SQL words pg_get_expr writes before a parenthesis
            'and', 'or', 'not', 'in', 'is', 'between', 'case', 'when', 'then', 'else',
            'end', 'like', 'ilike', 'similar', 'exists', 'distinct', 'from', 'as'))
$f$;
GRANT EXECUTE ON FUNCTION plx_admin.safe_expr(text) TO PUBLIC;
CREATE OR REPLACE FUNCTION plx_admin.no_code_expr() RETURNS event_trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $f$
DECLARE
    obj record;
    rel oid;
    bad text;
BEGIN
    IF (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RETURN;
    END IF;
    FOR obj IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
        CONTINUE WHEN obj.classid <> 'pg_class'::regclass OR obj.schema_name LIKE 'pg\_temp%';
        rel := coalesce((SELECT indrelid FROM pg_index WHERE indexrelid = obj.objid), obj.objid);
        SELECT e INTO bad FROM (
            SELECT pg_get_expr(adbin, adrelid) AS e FROM pg_attrdef WHERE adrelid = rel
            UNION ALL
            SELECT pg_get_expr(conbin, conrelid) FROM pg_constraint
            WHERE conrelid = rel AND conbin IS NOT NULL
            UNION ALL
            SELECT pg_get_expr(indexprs, indrelid) FROM pg_index WHERE indrelid = rel
            UNION ALL
            SELECT pg_get_expr(indpred, indrelid) FROM pg_index WHERE indrelid = rel
        ) x WHERE NOT plx_admin.safe_expr(e) LIMIT 1;
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION 'plx_no_code: an expression of % calls a function only a superuser '
                'may put there (ARCH T7): %', obj.object_identity, left(bad, 200)
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END LOOP;
END
$f$;
REVOKE ALL ON FUNCTION plx_admin.no_code_expr() FROM PUBLIC;
DROP EVENT TRIGGER IF EXISTS plx_no_code_expr;
CREATE EVENT TRIGGER plx_no_code_expr ON ddl_command_end
    WHEN TAG IN ('CREATE TABLE', 'ALTER TABLE', 'CREATE INDEX', 'CREATE TABLE AS',
                 'SELECT INTO')
    EXECUTE FUNCTION plx_admin.no_code_expr();

-- What a superuser creates in a data schema later (a migration) becomes plx_owner's at once.
-- A failure here never fails the statement that fired it: it warns, and the next run of
-- this file moves the object.
CREATE OR REPLACE FUNCTION plx_admin.data_owner() RETURNS event_trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $f$
DECLARE
    obj record;
BEGIN
    FOR obj IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
        BEGIN
            CONTINUE WHEN obj.in_extension OR obj.schema_name IS NULL
                OR obj.object_type NOT IN ('table', 'sequence')
                OR NOT plx_admin.is_data_schema(obj.schema_name);
            IF (SELECT relowner FROM pg_class WHERE oid = obj.objid) <> 'plx_owner'::regrole
               -- the same rule as the bulk move above (a partition, e.g. one
               -- operating.pg.link_partitions adds, is owned on its own)
               AND ((SELECT relispartition FROM pg_class WHERE oid = obj.objid)
                    OR NOT EXISTS (SELECT 1 FROM pg_depend d
                                   WHERE d.classid = 'pg_class'::regclass
                                     AND d.objid = obj.objid AND d.deptype IN ('a', 'i')
                                     AND d.refclassid = 'pg_class'::regclass
                                     AND d.refobjid <> obj.objid)) THEN
                EXECUTE format('ALTER %s %s OWNER TO plx_owner', upper(obj.object_type),
                               obj.object_identity);
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'plx_admin.data_owner: % left with its creator (%)',
                obj.object_identity, SQLERRM;
        END;
    END LOOP;
END
$f$;
REVOKE ALL ON FUNCTION plx_admin.data_owner() FROM PUBLIC;
DROP EVENT TRIGGER IF EXISTS plx_data_owner;
CREATE EVENT TRIGGER plx_data_owner ON ddl_command_end
    WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO', 'CREATE SEQUENCE')
    EXECUTE FUNCTION plx_admin.data_owner();

-- The API's reads (deploy/pg/plx_api_role.sql) follow the new owner's later tables too.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        ALTER DEFAULT PRIVILEGES FOR ROLE plx_owner IN SCHEMA ontology, search, plx_meta
            GRANT SELECT ON TABLES TO plx_api;
        ALTER DEFAULT PRIVILEGES FOR ROLE plx_owner IN SCHEMA ontology, search, plx_meta
            GRANT SELECT ON SEQUENCES TO plx_api;
    END IF;
END $$;

-- What is left (re-run while this is not 0: a 3 s lock wait was missed)
SELECT count(*) AS left_with_another_owner
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE plx_admin.is_data_schema(n.nspname) AND c.relkind IN ('r', 'p')
  AND c.relowner <> 'plx_owner'::regrole;
