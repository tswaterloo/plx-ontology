-- ARCH T7: the tests' own Postgres login on the host's separate test cluster (port 5433,
-- deploy/ops/test_pg_cluster.sh), so no test session is a superuser. Run as a superuser of
-- the test cluster (never the served one):
--     psql -X -v ON_ERROR_STOP=1 -p 5433 -d postgres -f deploy/pg/plx_test_role.sql
-- Idempotent. Then map the OS user to it (deploy/pg/README-roles.md) and tools/testslot.py
-- logs a wrapped run in as plx_test once the role exists.
--
-- What the tests need, measured 11-13 (the Postgres test modules run as this role): create
-- and drop their own throwaway databases (plx_test_a_<pid>_<n>, operating/pg/scratch_db.py)
-- and own everything in them. The migrations' CREATE EXTENSION vector needs a superuser
-- (vector is not a trusted extension), so vector, pg_trgm and unaccent are installed in
-- template1 once here and every new database starts with them (CREATE EXTENSION IF NOT
-- EXISTS is then a no-op). Never: superuser, roles, replication, other databases' data.
DO $$
BEGIN
    CREATE ROLE plx_test LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
        CONNECTION LIMIT -1;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;
ALTER ROLE plx_test NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT -1;   -- six test runs at once open more than 64 (the server's cap holds)
-- the facts reads bound their temporary files (operating/pg/sqlite_compat.py), as the API's
-- role may; the tests end their own stuck sessions (pg_terminate_backend, DROP ... FORCE)
GRANT SET ON PARAMETER temp_file_limit TO plx_test;
GRANT pg_signal_backend TO plx_test;
\connect template1
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
