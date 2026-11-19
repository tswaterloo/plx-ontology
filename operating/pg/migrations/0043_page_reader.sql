-- 0043_page_reader: the role and schema a person's pages read through (operating/pg/sql_views.py,
-- operating/page_reads.py). The owner's rule: Freightcom data never goes through an AI model,
-- but people may still see it. So the AI's door (biz_sql, role plx_sql_reader, migration 0042)
-- keeps only ai_ok rows, and a person's pages read a second set of views without that filter.
-- * role plx_page_reader: NOLOGIN, no superuser, no inherit; granted USAGE on schema biz_page
--   and SELECT on its views only. The API's role (plx_api) may SET ROLE to it inside a READ
--   ONLY transaction (plx.page only, never a model tool). The evaluation role and
--   plx_sql_reader get nothing: the AI's role cannot read a person-only view at all.
-- * schema biz_page: one security_barrier view per business table biz.<view>, the same door
--   as biz_sql's (audience keys, markings, hidden documents, people exclusion) without the
--   ai_ok predicate; made by ``python3 -m operating.pg.sql_views install`` and re-made by
--   each business build's swap.
-- No objects, value or link row changes: no digest moves.
-- Undo: DROP SCHEMA biz_page CASCADE; DROP ROLE plx_page_reader (after revoking memberships).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_page_reader') THEN
        CREATE ROLE plx_page_reader NOLOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT plx_page_reader TO plx_api;
    END IF;
    -- never a member of the AI's role, nor the AI's role a member of this one
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_sql_reader') THEN
        REVOKE plx_page_reader FROM plx_sql_reader;
        REVOKE plx_sql_reader FROM plx_page_reader;
    END IF;
END
$$;
CREATE SCHEMA IF NOT EXISTS biz_page;
REVOKE ALL ON SCHEMA biz_page FROM PUBLIC;
GRANT USAGE ON SCHEMA biz_page TO plx_page_reader;
