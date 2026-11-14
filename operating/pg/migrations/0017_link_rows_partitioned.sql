-- 0017_link_rows_partitioned: ontology.link_rows partitioned by link type (LIST), so a
-- link or step change rebuilds only its types' partitions beside the served ones and
-- swaps them in (operating/pg/link_partitions.py; devloop/DECISIONS/2026-11-11-link-rows-by-type.md).
-- * Already partitioned (the served database after `python3 -m operating.pg.link_partitions
--   convert`): nothing to do.
-- * Empty (a new or test database): made again partitioned, every row in the DEFAULT
--   partition until a rebuild places its type; the primary key holds the partition key
--   (link_id, link_type), as Postgres requires (no byte-order twin: 0018).
-- * Holding rows and not partitioned: left as it is (a notice), so the migrations after it
--   still apply. Copying 180 GB in one migration transaction is exactly the long migration
--   this layout ends; the online conversion (parallel, beside the served table, a rename of
--   milliseconds) is the change, run on its own.
DO $$
DECLARE
    kind "char";
    opts text[];
    view text;
BEGIN
    SELECT c.relkind, c.reloptions INTO kind, opts FROM pg_class c
     WHERE c.oid = 'ontology.link_rows'::regclass;
    IF kind = 'p' THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM ontology.link_rows) THEN
        -- left unpartitioned: the online conversion is the change for a table holding rows
        -- (every reader and writer works on both layouts), so later migrations still apply
        RAISE NOTICE 'ontology.link_rows holds rows: left as it is; convert it online '
            '(python3 -m operating.pg.link_partitions convert)';
        RETURN;
    END IF;
    view := pg_get_viewdef('ontology.links'::regclass, true);
    DROP VIEW ontology.links;
    ALTER TABLE ontology.link_rows RENAME TO link_rows_0017;
    CREATE TABLE ontology.link_rows (LIKE ontology.link_rows_0017 INCLUDING DEFAULTS
        INCLUDING CONSTRAINTS) PARTITION BY LIST (link_type);
    DROP TABLE ontology.link_rows_0017;
    EXECUTE 'CREATE TABLE ontology.link_rows_default_0 PARTITION OF ontology.link_rows DEFAULT'
        || CASE WHEN opts IS NULL THEN '' ELSE ' WITH (' || array_to_string(opts, ', ') || ')' END;
    ALTER TABLE ontology.link_rows ADD CONSTRAINT link_rows_pkey PRIMARY KEY (link_id, link_type);
    CREATE INDEX link_rows_by_row_no ON ontology.link_rows (row_no);
    CREATE INDEX link_rows_by_type ON ontology.link_rows (link_type, tier);
    CREATE INDEX link_rows_from ON ontology.link_rows (from_object, link_type);
    CREATE INDEX link_rows_to ON ontology.link_rows (to_object, link_type);
    CREATE INDEX link_rows_lead_from ON ontology.link_rows (from_object, link_id COLLATE "C")
        WHERE tier = 'lead' AND superseded_at IS NULL;
    CREATE INDEX link_rows_contra_from ON ontology.link_rows (from_object)
        INCLUDE (to_object, link_type) WHERE tier = 'contradicted';
    -- CC-P-07's odd evidence slots (0019 builds it on a plain table only); the default
    -- partition's index is made first under a name holding the parent's, which the parent
    -- index then attaches
    CREATE INDEX link_rows_odd_slots_d0 ON ontology.link_rows_default_0 USING gin
        ((slots[1:ek])) WHERE evidence_id IS NOT NULL;
    CREATE INDEX link_rows_odd_slots ON ontology.link_rows USING gin ((slots[1:ek]))
        WHERE evidence_id IS NOT NULL;
    EXECUTE 'CREATE VIEW ontology.links AS ' || view;
END $$;
