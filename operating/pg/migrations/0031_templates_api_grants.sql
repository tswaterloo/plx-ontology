-- 0031_templates_api_grants: the API's login (plx_api, deploy/pg/plx_api_role.sql) may use
-- each person's saved templates (migration 0022, operator_api/templates.py). 0022 granted it
-- nothing, and the role file grants templates only when it runs after 0022; on the served
-- database it ran before, so every /api/templates read answered 500 ("permission denied for
-- schema templates", SP-01, 11-15).
--
-- The same grants as the role file's templates block, which match what the served routes
-- run (operator_api/templates.py): list and read (SELECT), save (INSERT ... RETURNING id),
-- change and count uses (UPDATE), delete (deleted_at, an UPDATE) and the people-exclusion
-- purge (DELETE); nextval of the id sequence (USAGE). Nothing on any other schema.
-- Granted only where the role exists (a test database or a box without the switch).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT USAGE ON SCHEMA templates TO plx_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON templates.template TO plx_api;
        GRANT USAGE ON SEQUENCE templates.template_id_seq TO plx_api;
    END IF;
END
$$;
