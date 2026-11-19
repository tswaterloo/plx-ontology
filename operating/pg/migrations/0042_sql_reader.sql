-- 0042_sql_reader: the role and schema ``plx.sql`` reads through (operating/pg/sql_views.py).
-- * role plx_sql_reader: NOLOGIN, no superuser, no inherit; granted USAGE on schema biz_sql
--   and SELECT on its views only (no ontology or business table, no function of ours).
--   The API's role (plx_api) and the evaluation role (plx_eval) may SET ROLE to it inside a
--   READ ONLY transaction; nothing else changes for them.
-- * schema biz_sql: one security_barrier view per business table biz.<view>, made by
--   ``python3 -m operating.pg.sql_views install`` and re-made by each business build's swap;
--   each view reads the reader's scope from SET LOCAL plx.reader_* settings (unset: no row).
-- No objects, value or link row changes: no digest moves.
-- Undo: DROP SCHEMA biz_sql CASCADE; DROP ROLE plx_sql_reader (after revoking memberships).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_sql_reader') THEN
        CREATE ROLE plx_sql_reader NOLOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT plx_sql_reader TO plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT plx_sql_reader TO plx_eval;
    END IF;
END
$$;
CREATE SCHEMA IF NOT EXISTS biz_sql;
REVOKE ALL ON SCHEMA biz_sql FROM PUBLIC;
GRANT USAGE ON SCHEMA biz_sql TO plx_sql_reader;
-- (no REVOKE on public here: what any role may do there is PUBLIC's grant, not this
-- role's; its barriers are the READ ONLY transaction, grants on the guarded views only and
-- the plx.sql function allowlist)
