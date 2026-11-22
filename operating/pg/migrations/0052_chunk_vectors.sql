-- 0052_chunk_vectors: the chunk vectors in their own narrow, append-only table.
-- An embedding written into search.chunks is an UPDATE of a wide row with 26 indexes: never
-- HOT (audience_key, markings, linked_ids are indexed), so every vector re-inserted the row
-- into every index, 2-3 HNSW graphs included: 11-21 live ~375 KB of WAL per embedded row,
-- 38 s per 424-row statement, the backfill writer-bound at ~40 texts/s and 18 GB behind on
-- the Spark standby. Here a vector is one appended row (kind, content_sha, model, vector)
-- in its kind's partition, with one HNSW per partition, built once over the copied rows
-- (``python3 -m operating.pg.chunk_vectors build``) and attached.
--
-- Nothing changes until ``search.vector_mode.mode`` is 'narrow' (set by ``chunk_vectors
-- switch`` once every kind's partition is built and indexed): in 'chunks' (this
-- migration's default) writers update search.chunks and readers read it, as before.
-- In 'narrow' the writers append here and the readers join here from search.chunks on
-- (kind, content_sha), under the reader's permission predicate on search.chunks: a row of
-- this table alone names no chunk and no audience; a vector whose text changed is never
-- joined (its content_sha no longer matches) and the next prune removes it.
-- Same readers as search.chunks (plx_api, plx_eval: SELECT), no wider.
-- A database without search.chunks (a test of another schema) is left alone.
-- Undo: UPDATE search.vector_mode SET mode = 'chunks' (search.chunks.embedding is kept and
-- still filled up to the switch); DROP TABLE search.chunk_vectors, search.vector_mode.
DO $migration$
DECLARE
    dim integer;
BEGIN
    IF to_regclass('search.chunks') IS NULL THEN
        RETURN;
    END IF;
    -- the vector type of search.chunks.embedding (the configured model's dimension)
    SELECT a.atttypmod INTO dim FROM pg_attribute a
     WHERE a.attrelid = 'search.chunks'::regclass AND a.attname = 'embedding';
    IF dim IS NULL OR dim < 1 THEN
        RAISE EXCEPTION '0052_chunk_vectors: search.chunks.embedding has no dimension';
    END IF;
    EXECUTE format('CREATE TABLE IF NOT EXISTS search.chunk_vectors (
        kind        text NOT NULL,
        content_sha text NOT NULL,
        embed_model text NOT NULL,
        embedding   halfvec(%s) NOT NULL,
        PRIMARY KEY (kind, content_sha, embed_model)
    ) PARTITION BY LIST (kind)', dim);
    -- a kind with no partition of its own yet (a new source) lands here
    CREATE TABLE IF NOT EXISTS search.chunk_vectors_other
        PARTITION OF search.chunk_vectors DEFAULT;
    -- the partitioned HNSW (same build options as chunks_embedding_hnsw): made here over
    -- the empty table; a kind's partition is built standalone with its own copy of it,
    -- and ATTACH PARTITION adopts that index (no second build)
    CREATE INDEX IF NOT EXISTS chunk_vectors_hnsw ON search.chunk_vectors
        USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);

    CREATE TABLE IF NOT EXISTS search.vector_mode (
        singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
        mode       text NOT NULL DEFAULT 'chunks' CHECK (mode IN ('chunks', 'narrow')),
        changed_at timestamptz NOT NULL DEFAULT now()
    );
    INSERT INTO search.vector_mode (singleton) VALUES (true) ON CONFLICT DO NOTHING;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON search.chunk_vectors, search.chunk_vectors_other, search.vector_mode
            TO plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT SELECT ON search.chunk_vectors, search.chunk_vectors_other, search.vector_mode
            TO plx_eval;
    END IF;
END
$migration$;
