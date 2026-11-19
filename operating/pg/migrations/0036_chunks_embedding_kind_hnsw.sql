-- 0036_chunks_embedding_kind_hnsw: one partial halfvec HNSW per chunk kind.
-- A question far from every chunk (eval questions sit at cosine distance 0.35-0.5 from all of
-- them) makes one walk over the whole-table graph stop in the big kinds' region: measured on a
-- 10% copy of the served chunks, recall@10 0.80 at ef_search 200; a walk per kind, unioned,
-- 0.94 at ef 400 and 0.99 at ef 800. operating/pg/dense.py finds these indexes at run time
-- (valid ones only) and walks them with PLX_SEARCH_KINDS=1; a kind without one is scanned
-- exactly. The predicate must stay exactly kind = '<kind>' for the planner to pick the index.
--
-- This migration builds them only while search.chunks is EMPTY (a fresh or test database:
-- an empty build is instant). On a populated table it creates nothing: a deploy applies
-- migrations in one transaction before the release switch, and 14 plain CREATE INDEX over
-- the served chunks (about 10 GB, a SHARE lock blocking every chunk write) would overrun the
-- deploy's 30-minute migration timeout and roll the switch back. The served database builds
-- them after a catch-up with `python3 -m operating.pg.kind_indexes build` (CREATE INDEX
-- CONCURRENTLY, one kind at a time, resumable; docs/runbooks/search-kinds-1115.md). The list
-- is operating.pg.migrate.CHUNKS_KIND_HNSW_KINDS; the embedding switch and operating.pg.bulk
-- drop them and rebuild them only with PLX_SEARCH_KINDS on.
DO $$
DECLARE
    k text;
BEGIN
    IF to_regclass('search.chunks') IS NULL THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM search.chunks LIMIT 1) THEN
        RETURN;
    END IF;
    FOREACH k IN ARRAY ARRAY['attachment', 'bc_text', 'list_item', 'mail', 'map_stage',
                             'mc_task', 'meeting', 'original', 'portal_text', 'record_text',
                             'record_words', 'sharepoint', 'sheet_row', 'teams'] LOOP
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON search.chunks USING hnsw '
                       '(embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64) '
                       'WHERE kind = %L', 'chunks_embedding_k_' || k, k);
    END LOOP;
END
$$;
