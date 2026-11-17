-- The read check of deploy/pg/README-roles.md: every relation of the data schemas, read once (LIMIT 1) as the session role.
\set ON_ERROR_STOP 0
SET statement_timeout = '20s';
SELECT format('SELECT %L AS who, %L AS rel, (SELECT count(*) FROM (SELECT 1 FROM %I.%I LIMIT 1) s) AS ok', current_user, n.nspname||'.'||c.relname, n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('ontology','search','plx_meta','memory','delta_stage') AND c.relkind IN ('r','p','v','m') AND NOT c.relispartition
ORDER BY 1
\gexec
