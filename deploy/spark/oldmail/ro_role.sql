-- The box's older-mail search leg (operating.search.oldmail_leg) reads the old-mail store as
-- this role: SELECT on the search tables only, read-only transactions, 900 ms per statement.
-- Run once in the store as its owner:  psql -h ~/oldmail-store/run -p 55710 -d plx -f ro_role.sql
-- Then pg_hba.conf, ABOVE "local all all peer":  local plx plx_oldmail_ro peer map=oldmail_ro
-- and pg_ident.conf:  oldmail_ro <the store's OS login> plx_oldmail_ro   (reload). Undo: DROP OWNED BY
-- plx_oldmail_ro; DROP ROLE plx_oldmail_ro; remove the two lines; reload.
DO $$ BEGIN
    CREATE ROLE plx_oldmail_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER ROLE plx_oldmail_ro SET default_transaction_read_only = on;
ALTER ROLE plx_oldmail_ro SET statement_timeout = 900;
GRANT CONNECT ON DATABASE plx TO plx_oldmail_ro;
GRANT USAGE ON SCHEMA search TO plx_oldmail_ro;
DO $$ DECLARE t text; BEGIN
    FOR t IN SELECT format('%I.%I', schemaname, tablename) FROM pg_tables
             WHERE schemaname = 'search' AND (tablename IN ('chunks', 'audience_units',
                   'vector_mode', 'chunk_groups', 'reader_bits', 'reader_bits_state')
                   OR tablename LIKE 'chunk\_vectors%') LOOP
        EXECUTE 'GRANT SELECT ON ' || t || ' TO plx_oldmail_ro';
    END LOOP;
END $$;
