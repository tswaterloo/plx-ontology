-- 0009_schema_migrations: schema changes apply to the ontology in place
-- (operating/pg/ontology_migrate.py, docs/no-builds.md).
-- * applied_owned: the objects each applied delta layer replaced, in apply order; a
--   migration reads the ones applied since its capture snapshot began.
-- * reproject: objects whose rows a migration or an older-schema delta layer could not
--   bring under the served schema; the next delta cycle projects their records again and
--   removes them here.
-- * schema_migrations: every migration applied, with its report.
-- The served schema is ontology.store_meta 'schema_digest' (the read tools' own key);
-- ontology.load_state 'schema' records its version and the migration that set it.

CREATE TABLE IF NOT EXISTS ontology.applied_owned (
    seq bigserial PRIMARY KEY,
    layer text NOT NULL,
    object_id text NOT NULL);

CREATE TABLE IF NOT EXISTS ontology.reproject (
    object_id text PRIMARY KEY,
    migration text NOT NULL,
    since timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS ontology.schema_migrations (
    id text PRIMARY KEY,
    from_digest text NOT NULL,
    to_digest text NOT NULL,
    scope jsonb NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    report jsonb NOT NULL);

-- A build step's links are its link id prefix (ontology/step_migrate.py): a step migration
-- deletes them by a range of the id's binary order, on this index (never a scan).
CREATE INDEX IF NOT EXISTS link_rows_by_id ON ontology.link_rows (link_id COLLATE "C");
