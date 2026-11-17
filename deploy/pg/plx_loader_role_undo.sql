-- Undo deploy/pg/plx_loader_role.sql (ARCH T7): the data schemas go back to the superuser
-- that runs this file, the event trigger and the two roles are dropped. Remove the units'
-- PGUSER=plx_loader drop-ins first (deploy/pg/README-roles.md), so no loader is logged in.
--     psql -X -d plx -f deploy/pg/plx_loader_role_undo.sql
-- One ALTER ... OWNER per transaction with a 3 s lock wait, as in the forward file; re-run
-- while it reports objects left.
\set ON_ERROR_STOP 1
DROP EVENT TRIGGER IF EXISTS plx_data_owner;
DROP EVENT TRIGGER IF EXISTS plx_no_code;
DROP EVENT TRIGGER IF EXISTS plx_no_code_expr;
SET lock_timeout = '3s';
\set ON_ERROR_STOP 0
SELECT format('ALTER SCHEMA %I OWNER TO %I', nspname, current_user)
FROM pg_namespace WHERE nspowner = 'plx_owner'::regrole
\gexec
SELECT format('ALTER %s %I.%I OWNER TO %I',
              CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                             WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE'
                             ELSE 'TABLE' END, n.nspname, c.relname, current_user)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = 'plx_owner'::regrole AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
  -- as in the forward file: a partition is owned on its own, and a partitioned table's
  -- internal dependency on its own key columns is no owner
  AND (c.relispartition
       OR NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                      AND d.objid = c.oid AND d.deptype IN ('a', 'i', 'e')
                      AND d.refclassid = 'pg_class'::regclass AND d.refobjid <> c.oid))
\gexec
SELECT format('ALTER ROUTINE %s OWNER TO %I', p.oid::regprocedure, current_user)
FROM pg_proc p WHERE p.proowner = 'plx_owner'::regrole
\gexec
SELECT format('ALTER TYPE %s OWNER TO %I', t.oid::regtype, current_user)
FROM pg_type t WHERE t.typowner = 'plx_owner'::regrole AND t.typtype IN ('e', 'd', 'r', 'm')
\gexec
\set ON_ERROR_STOP 1
RESET lock_timeout;
SELECT count(*) AS left_with_plx_owner FROM pg_class WHERE relowner = 'plx_owner'::regrole;
-- once that is 0 (and the same holds in every database the role was used in):
--     DROP OWNED BY plx_loader, plx_owner; DROP ROLE plx_loader; DROP ROLE plx_owner;
--     DROP SCHEMA plx_admin CASCADE;
