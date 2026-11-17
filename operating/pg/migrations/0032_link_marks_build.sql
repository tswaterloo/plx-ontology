-- 0032_link_marks_build (LM-MIG): the kept link slice's one-row build record
-- (operating/pg/link_marks.py, the shadow build of perf-1115) declared here instead of being
-- made by the first refresh at run time. Review of 1eae9261e5: a persistent served table the
-- schema rehearsals, contracts and the migration ledger did not see, its grants left to the
-- creating role's default ACL.
--
-- Same columns as the run-time text (link_marks.BUILD_SQL, kept for a database without this
-- migration); IF NOT EXISTS: on the served database the first build already made it (owned
-- by plx_owner, SELECT to plx_api), and this leaves that table and its rows as they are.
-- Owned by the migrating role, as ontology.link_marks is; the API's login reads it
-- (link_marks.status: the build record on the status read), nothing more. The work tables
-- (link_marks_parts, _next, _catchup) stay per build: made and dropped by one build.
-- Undo: python3 -m operating.pg.link_marks undo (drops it with the rest of 0028).
CREATE TABLE IF NOT EXISTS ontology.link_marks_build (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    active boolean NOT NULL DEFAULT false,
    started_at timestamptz,
    counter text,
    data_version bigint,
    generation text,
    layers integer NOT NULL DEFAULT 0,
    broken text,
    failed_at timestamptz,
    why text,
    kept_at timestamptz,
    seconds double precision);
INSERT INTO ontology.link_marks_build (one) VALUES (true) ON CONFLICT DO NOTHING;

-- The work tables of one build (made and dropped by it, link_marks.WORK_ID) get a primary
-- key: a train's undo capture refuses any ontology table without one. A generated id, not a
-- natural key (the staging table takes the same key from two workers' ranges as two rows).
-- A build's leftovers already there (one killed mid-way) get it too.
ALTER TABLE IF EXISTS ontology.link_marks_parts
    ADD COLUMN IF NOT EXISTS id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY;
ALTER TABLE IF EXISTS ontology.link_marks_catchup
    ADD COLUMN IF NOT EXISTS id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY;

-- granted only where the role exists (a test database or a box without the switch)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON ontology.link_marks_build TO plx_api;
    END IF;
END
$$;
