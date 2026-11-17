-- 0034_chunks_embedding_bq_hnsw: a binary-quantized HNSW index over the chunk embeddings.
-- One bit per dimension (the sign), Hamming distance: 1/16 of the halfvec index's size, so
-- the graph of every mail chunk fits in memory. operating/pg/dense.py and
-- operating/search/retrievers.py read it with PLX_SEARCH_BQ=1: the index yields 200-400
-- candidates under the reader's permission filter and the exact halfvec cosine re-ranks them.
-- The ORDER BY expression must stay this exact expression for the planner to use the index.
--
-- On the served database it was built CONCURRENTLY by hand first (11-15), so this is a no-op
-- there; a fresh or test database builds it here (migrations run in a transaction, so not
-- concurrently). The dimension is the column's own (its type modifier), and a database
-- without search.chunks (or its vector column) is left alone. set_embedding and
-- operating.pg.bulk drop and rebuild it with the halfvec one.
DO $$
DECLARE
    dim integer;
BEGIN
    SELECT a.atttypmod INTO dim FROM pg_attribute a
     WHERE a.attrelid = to_regclass('search.chunks') AND a.attname = 'embedding'
       AND NOT a.attisdropped;
    IF dim IS NOT NULL AND dim > 0 THEN
        EXECUTE format('CREATE INDEX IF NOT EXISTS chunks_embedding_bq_hnsw ON search.chunks '
                       'USING hnsw ((binary_quantize(embedding)::bit(%s)) bit_hamming_ops) '
                       'WITH (m = 16, ef_construction = 64)', dim);
    END IF;
END
$$;
