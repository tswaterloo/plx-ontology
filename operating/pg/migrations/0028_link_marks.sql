-- 0028_link_marks (CC-P-07): the link slice kept in Postgres, so an API start stops scanning
-- link_rows. Every host's first read of a data version (a migration step, a generation, a
-- change of the fact code) read the whole link slice: per link type the markings and the
-- gated evidence handles of its rows with the latest end of each
-- (operating/pg/ontology_reader.link_slice_sql). pg_stat_statements 11-13..11-14: 1,175 GB +
-- 692 GB read over 213 calls, 316-421 s mean, link_rows seq_tup_read 18.9 G.
--
-- ontology.link_marks holds those facts, one row per (link type, markings) with handle ''
-- (the latest end over every row) and one per (link type, markings, gated handle): a few
-- thousand rows a reader reads in milliseconds (ontology_reader.LINK_MARKS_SQL).
--
-- No trigger on link_rows (a row trigger cost ~7x per row written and a statement trigger
-- is not cloned onto partitions: review of b560a49f98). The facts are rebuilt by
-- `python3 -m operating.pg.link_marks refresh` (after a train or step apply; an API start
-- runs it in the background when they are not current) and carried over a delta layer by
-- the apply itself (operating/pg/ontology_delta.apply_layer, in the layer's transaction:
-- only ever added or raised, so a deleted or superseded link keeps its marking and end, the
-- safe direction; a refresh makes them exact). A reader takes them only when
-- ontology.link_marks_vouched says so for its gated schemes:
--   * the state is kept, for exactly those schemes;
--   * the data version (ontology.sq_data_version: every load and delta layer moves it) and
--     the generation's content digest are the ones the facts were made at;
--   * the change counter of link_rows is the one recorded then: per leaf relation (every
--     partition, or the table itself) its oid, its file node (a swap, a TRUNCATE, a
--     conversion change it) and the rows inserted, updated and deleted in it as the
--     statistics collector counts them (any writer, any partition, no trigger to bypass),
--     with track_counts on.
-- Anything else: the slice is read from link_rows as before. The counters are those a
-- writer's backend has flushed (at most PGSTAT_IDLE_INTERVAL, 10 s, after its commit): a
-- writer that moves no data version is seen once its counts arrive, as the facts kept per
-- data version already see it (ontology_tools_traverse._pg_store_identity).
--
-- Undo: python3 -m operating.pg.link_marks undo (operating/pg/link_marks.UNDO_SQL drops the
-- two tables and the functions below; nothing else depends on them).

-- the trigger design of the first draft, wherever a scratch database applied it
DROP TRIGGER IF EXISTS link_marks_note ON ontology.link_rows;
DROP TRIGGER IF EXISTS link_marks_off ON ontology.link_rows;
DROP FUNCTION IF EXISTS ontology.link_marks_note();
DROP FUNCTION IF EXISTS ontology.link_marks_off();
DROP FUNCTION IF EXISTS ontology.link_marks_install();
DROP FUNCTION IF EXISTS ontology.link_marks_noted();
DROP FUNCTION IF EXISTS ontology.link_marks_leaves();
DROP TABLE IF EXISTS ontology.link_marks_state;

CREATE TABLE IF NOT EXISTS ontology.link_marks (
    link_type text NOT NULL,
    markings text NOT NULL,
    handle text NOT NULL DEFAULT '',
    until text COLLATE "C" NOT NULL,
    PRIMARY KEY (link_type, markings, handle));

CREATE TABLE ontology.link_marks_state (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    phase text NOT NULL CHECK (phase IN ('stale', 'building', 'kept')),
    schemes text[] NOT NULL DEFAULT '{}',
    data_version bigint,
    generation text,
    counter text,
    built_at timestamptz,
    why text);
INSERT INTO ontology.link_marks_state (phase, why) VALUES ('stale', 'never built');

-- The leaf relations holding link_rows now: its partitions, or the table itself while it is
-- not partitioned (pg_partition_tree lists nothing for a plain table).
CREATE OR REPLACE FUNCTION ontology.link_marks_leaf_set() RETURNS SETOF oid
LANGUAGE sql STABLE AS $$
    SELECT p.relid::oid FROM pg_partition_tree('ontology.link_rows'::regclass) p WHERE p.isleaf
    UNION
    SELECT c.oid FROM pg_class c WHERE c.oid = 'ontology.link_rows'::regclass AND c.relkind <> 'p'
$$;

-- link_rows' change counter now (see the header), this transaction's own writes counted;
-- NULL when the counts are not kept (track_counts off) or the table has no leaf.
CREATE OR REPLACE FUNCTION ontology.link_marks_counter() RETURNS text
LANGUAGE plpgsql VOLATILE AS $f$
DECLARE
    out text;
BEGIN
    IF current_setting('track_counts') <> 'on' THEN
        RETURN NULL;
    END IF;
    PERFORM pg_stat_clear_snapshot();   -- the counts as flushed now, not as first read
    SELECT string_agg(concat_ws(':', l, c.relfilenode,
                                pg_stat_get_tuples_inserted(l) + pg_stat_get_xact_tuples_inserted(l),
                                pg_stat_get_tuples_updated(l) + pg_stat_get_xact_tuples_updated(l),
                                pg_stat_get_tuples_deleted(l) + pg_stat_get_xact_tuples_deleted(l)),
                      ',' ORDER BY l)
      INTO out
      FROM ontology.link_marks_leaf_set() l JOIN pg_class c ON c.oid = l;
    RETURN out;
END
$f$;

CREATE OR REPLACE FUNCTION ontology.link_marks_generation() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT value ->> 'content_digest' FROM ontology.load_state WHERE name = 'generation'
$$;

-- Whether a reader may take the kept facts for the gated schemes it reads (the header).
CREATE OR REPLACE FUNCTION ontology.link_marks_vouched(schemes text[]) RETURNS boolean
LANGUAGE plpgsql VOLATILE AS $f$
DECLARE
    st ontology.link_marks_state;
    now_counter text;
BEGIN
    SELECT * INTO st FROM ontology.link_marks_state;
    IF NOT FOUND OR st.phase <> 'kept' OR st.schemes IS DISTINCT FROM schemes
       OR st.data_version IS DISTINCT FROM ontology.sq_data_version()
       OR st.generation IS DISTINCT FROM ontology.link_marks_generation() THEN
        RETURN false;
    END IF;
    now_counter := ontology.link_marks_counter();
    RETURN now_counter IS NOT NULL AND st.counter IS NOT NULL AND now_counter = st.counter;
END
$f$;

CREATE OR REPLACE FUNCTION ontology.link_marks_stale(why text) RETURNS void
LANGUAGE sql AS $$
    UPDATE ontology.link_marks_state SET phase = 'stale', why = $1 WHERE phase <> 'stale'
$$;
